//! Python backend sidecar의 native-only 보안 handshake와 process lifecycle을 소유한다.

use crate::{BackendConnectionDescriptor, BACKEND_SCHEMA_VERSION};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use security_framework::passwords::{generic_password, PasswordOptions};
use serde::{Deserialize, Serialize};
use std::error::Error;
use std::fmt::{Display, Formatter};
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager, State, WebviewWindowBuilder};
use zeroize::{Zeroize, Zeroizing};

pub const BACKEND_SIDECAR_EXIT_EVENT: &str = "backend-sidecar-exited";

const SIDECAR_BINARY_NAME: &str = "binance-auto-sidecar";
const KEYCHAIN_SERVICE: &str = "com.binance-auto.trader.testnet";
const KEYCHAIN_API_KEY_ACCOUNT: &str = "api-key";
const KEYCHAIN_API_SECRET_ACCOUNT: &str = "api-secret";
const DEVELOPMENT_UI_ORIGIN: &str = "http://127.0.0.1:5173";
const PRODUCTION_UI_ORIGIN: &str = "tauri://localhost";
const HISTORY_FILE_NAME: &str = "history.jsonl";
const PRODUCTION_HTTP_CSP_SENTINEL: &str = "http://127.0.0.1:0";
const PRODUCTION_WS_CSP_SENTINEL: &str = "ws://127.0.0.1:0";
const TOKEN_CHILD_FD: RawFd = 3;
const READY_CHILD_FD: RawFd = 4;
const STOP_CHILD_FD: RawFd = 5;
const CONFIG_CHILD_FD: RawFd = 6;
const MINIMUM_STAGING_FD: RawFd = 16;
const MAXIMUM_SECRET_BYTES: usize = 512;
const MAXIMUM_CONFIG_BYTES: usize = 8 * 1024;
const MAXIMUM_READY_BYTES: usize = 4 * 1024;
const SIDECAR_READY_TIMEOUT: Duration = Duration::from_secs(20);
const LATE_READY_ATTEMPT_TIMEOUT: Duration = Duration::from_secs(1);
const SIDECAR_MONITOR_INTERVAL: Duration = Duration::from_millis(50);
const PROVEN_PRE_RUNTIME_ABORT_GRACE: Duration = Duration::from_millis(500);
const DEFAULT_EXIT_TIMEOUT_MS: u64 = 30_000;
const MAXIMUM_EXIT_TIMEOUT_MS: u64 = 120_000;

/// Spawned child failure가 forced abort 가능한 pre-runtime인지 exposure ambiguous 보존 구간인지 표현한다.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum StartupChildRecoveryPolicy {
    AbortProvenPreRuntime,
    RetainAmbiguousExposure,
}

/// renderer에 공개해도 credential이나 handshake 값을 포함하지 않는 native failure이다.
#[derive(Clone, Debug, Serialize)]
pub struct SidecarFailure {
    pub code: &'static str,
    pub message: &'static str,
}

impl SidecarFailure {
    /// 함수 이름: startup()
    /// 기능: sidecar 시작 상세와 filesystem 경로를 숨긴 고정 startup failure를 만든다.
    /// 인자: 없음
    /// 반환값: BACKEND_SIDECAR_STARTUP_FAILED failure
    /// 작성 날짜: 2026/08/24
    fn startup() -> Self {
        Self {
            code: "BACKEND_SIDECAR_STARTUP_FAILED",
            message: "Backend sidecar could not be started safely.",
        }
    }

    /// 함수 이름: credentials_unavailable()
    /// 기능: 어느 Keychain item이 실패했는지 반사하지 않는 credential failure를 만든다.
    /// 인자: 없음
    /// 반환값: BACKEND_CREDENTIALS_UNAVAILABLE failure
    /// 작성 날짜: 2026/08/24
    fn credentials_unavailable() -> Self {
        Self {
            code: "BACKEND_CREDENTIALS_UNAVAILABLE",
            message: "Required backend credentials are unavailable.",
        }
    }

    /// 함수 이름: descriptor_rejected()
    /// 기능: ready payload 원문을 숨긴 descriptor validation failure를 만든다.
    /// 인자: 없음
    /// 반환값: INVALID_BACKEND_DESCRIPTOR failure
    /// 작성 날짜: 2026/08/24
    pub fn descriptor_rejected() -> Self {
        Self {
            code: "INVALID_BACKEND_DESCRIPTOR",
            message: "Backend connection descriptor is invalid.",
        }
    }

    /// 함수 이름: state_unavailable()
    /// 기능: poisoned native state를 내부 상세 없이 fail closed한다.
    /// 인자: 없음
    /// 반환값: BACKEND_SIDECAR_STATE_UNAVAILABLE failure
    /// 작성 날짜: 2026/08/24
    fn state_unavailable() -> Self {
        Self {
            code: "BACKEND_SIDECAR_STATE_UNAVAILABLE",
            message: "Backend sidecar state is unavailable.",
        }
    }

    /// 함수 이름: unavailable()
    /// 기능: 실행 중인 child가 없는 lifecycle 요청을 고정 typed failure로 만든다.
    /// 인자: 없음
    /// 반환값: BACKEND_SIDECAR_UNAVAILABLE failure
    /// 작성 날짜: 2026/08/24
    fn unavailable() -> Self {
        Self {
            code: "BACKEND_SIDECAR_UNAVAILABLE",
            message: "Backend sidecar is unavailable.",
        }
    }

    /// 함수 이름: invalid_timeout()
    /// 기능: native hard limit 밖의 wait timeout을 child mutation 전에 거부한다.
    /// 인자: 없음
    /// 반환값: INVALID_BACKEND_SIDECAR_EXIT_TIMEOUT failure
    /// 작성 날짜: 2026/08/24
    fn invalid_timeout() -> Self {
        Self {
            code: "INVALID_BACKEND_SIDECAR_EXIT_TIMEOUT",
            message: "Backend sidecar exit timeout is invalid.",
        }
    }

    /// 함수 이름: exit_timeout()
    /// 기능: kill이나 window destroy 없이 operator 결정을 요구하는 wait timeout을 만든다.
    /// 인자: 없음
    /// 반환값: BACKEND_SIDECAR_EXIT_TIMEOUT failure
    /// 작성 날짜: 2026/08/24
    fn exit_timeout() -> Self {
        Self {
            code: "BACKEND_SIDECAR_EXIT_TIMEOUT",
            message: "Backend sidecar did not exit within the allowed time.",
        }
    }

    /// 함수 이름: unexpected_exit()
    /// 기능: 정상 종료 요청 전에 끝난 child를 final window destroy와 구분한다.
    /// 인자: 없음
    /// 반환값: BACKEND_SIDECAR_EXITED_UNEXPECTEDLY failure
    /// 작성 날짜: 2026/08/24
    fn unexpected_exit() -> Self {
        Self {
            code: "BACKEND_SIDECAR_EXITED_UNEXPECTEDLY",
            message: "Backend sidecar exited unexpectedly.",
        }
    }

    /// 함수 이름: nonzero_exit()
    /// 기능: expected stop 뒤의 signal 또는 non-zero exit를 정상 종료로 위장하지 않는다.
    /// 인자: 없음
    /// 반환값: BACKEND_SIDECAR_EXIT_FAILED failure
    /// 작성 날짜: 2026/08/24
    fn nonzero_exit() -> Self {
        Self {
            code: "BACKEND_SIDECAR_EXIT_FAILED",
            message: "Backend sidecar did not complete a clean exit.",
        }
    }
}

impl Display for SidecarFailure {
    /// 함수 이름: fmt()
    /// 기능: secret 없는 고정 failure code만 native startup error에 표시한다.
    /// 인자: formatter -> Rust formatting target
    /// 반환값: formatting 결과
    /// 작성 날짜: 2026/08/24
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.code)
    }
}

impl Error for SidecarFailure {}

/// abnormal exit event와 expected exit 판정이 공유하는 exact secret-free payload이다.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct BackendSidecarExitPayload {
    pub expected: bool,
    pub code: Option<i32>,
}

/// renderer가 정상 child exit를 확인한 뒤에만 final 상태로 갈 수 있게 하는 receipt이다.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct BackendSidecarExitReceipt {
    pub exited: bool,
    pub code: Option<i32>,
}

/// Renderer가 sidecar exit listener를 설치했음을 확인하는 exact command receipt이다.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct BackendSidecarExitEventArmReceipt {
    pub armed: bool,
}

/// Python ready FD에서 허용하는 secret 없는 exact descriptor wire shape이다.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ReadyDescriptorWire {
    port: u16,
    session_id: String,
    schema_version: u32,
}

/// FD4 parser가 재시도 가능한 timeout과 framing을 잃은 terminal failure를 구분한다.
#[derive(Debug)]
enum ReadyDescriptorReadFailure {
    Timeout,
    Terminal(SidecarFailure),
}

/// Keychain에서 읽은 credential을 Drop 시점에 zeroize하는 native-only container이다.
struct KeychainCredentials {
    api_key: String,
    api_secret: String,
}

impl Drop for KeychainCredentials {
    /// 함수 이름: drop()
    /// 기능: FD6 publication 뒤 native credential String backing memory를 즉시 덮어쓴다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    fn drop(&mut self) {
        self.api_key.zeroize();
        self.api_secret.zeroize();
    }
}

/// FD6에만 직렬화하는 production read-only bootstrap configuration이다.
#[derive(Serialize)]
struct SidecarBootstrapConfiguration<'a> {
    schema_version: u32,
    allowed_origin: &'a str,
    history_path: &'a str,
    api_key: &'a str,
    api_secret: &'a str,
    allow_testnet_orders: bool,
    max_notional: Option<&'static str>,
}

/// renderer publication 전에 native state로 이전할 child와 descriptor 묶음이다.
pub struct PreparedBackendSidecar {
    pub descriptor: BackendConnectionDescriptor,
    pub child: Child,
    pub stop_writer: File,
    pub port: u16,
}

/// Token/config publication 후 ready를 확인하지 못해 exposure 여부가 ambiguous한 child이다.
pub struct AmbiguousBackendSidecar {
    pub child: Child,
    pub stop_writer: File,
    pub late_ready_recovery: Option<LateReadySidecarRecovery>,
}

/// Initial timeout 뒤 FD4 framing과 token을 zeroizing 상태로 보유하는 late READY recovery이다.
pub struct LateReadySidecarRecovery {
    ready_reader: File,
    ready_payload: Zeroizing<Vec<u8>>,
    token: Zeroizing<String>,
}

/// Startup이 renderer-ready인지 operator-owned ambiguous recovery인지를 ownership과 함께 반환한다.
pub enum BackendSidecarPreparation {
    Ready(PreparedBackendSidecar),
    Ambiguous(AmbiguousBackendSidecar),
}

/// 실행 중 child handle, stop pipe와 expected/actual exit를 동일 lock으로 보호한다.
struct SidecarProcessLifecycle {
    child_handle: Option<Arc<Mutex<Child>>>,
    stop_writer: Option<File>,
    late_ready_pending: bool,
    late_ready_terminal: bool,
    expected_exit_requested: bool,
    exit_record: Option<BackendSidecarExitPayload>,
    exit_event_armed: bool,
    exit_event_delivered: bool,
}

impl Default for SidecarProcessLifecycle {
    /// 함수 이름: default()
    /// 기능: child와 이전 launch secret이 없는 초기 lifecycle을 만든다.
    /// 인자: 없음
    /// 반환값: 빈 lifecycle state
    /// 작성 날짜: 2026/08/24
    fn default() -> Self {
        Self {
            child_handle: None,
            stop_writer: None,
            late_ready_pending: false,
            late_ready_terminal: false,
            expected_exit_requested: false,
            exit_record: None,
            exit_event_armed: false,
            exit_event_delivered: false,
        }
    }
}

/// monitor thread와 Tauri command가 공유하는 child lifecycle synchronization state이다.
struct SidecarProcessShared {
    lifecycle: Mutex<SidecarProcessLifecycle>,
    exit_condition: Condvar,
}

/// Tauri managed state에 저장하는 clone 가능한 backend sidecar owner이다.
#[derive(Clone)]
pub struct SidecarProcessState {
    shared: Arc<SidecarProcessShared>,
}

impl Default for SidecarProcessState {
    /// 함수 이름: default()
    /// 기능: 아직 sidecar가 없는 process owner와 exit condition을 만든다.
    /// 인자: 없음
    /// 반환값: 빈 SidecarProcessState
    /// 작성 날짜: 2026/08/24
    fn default() -> Self {
        Self {
            shared: Arc::new(SidecarProcessShared {
                lifecycle: Mutex::new(SidecarProcessLifecycle::default()),
                exit_condition: Condvar::new(),
            }),
        }
    }
}

impl SidecarProcessState {
    /// 함수 이름: install()
    /// 기능: ready 검증을 통과한 child/stop pipe를 소유하고 abnormal exit monitor를 시작한다.
    /// 인자: child -> spawned Python process, stop_writer -> parent FD5 writer,
    ///      app_handle -> secret-free exit event를 emit할 Tauri handle
    /// 반환값: lifecycle install 성공 또는 typed state failure
    /// 작성 날짜: 2026/08/24
    pub fn install(
        &self,
        child: Child,
        stop_writer: File,
        app_handle: AppHandle,
    ) -> Result<(), SidecarFailure> {
        // Child handle과 stop pipe를 monitor 시작 전에 같은 lifecycle publication으로 옮긴다.
        let mut lifecycle = match self.shared.lifecycle.lock() {
            Ok(lifecycle) => lifecycle,
            Err(poisoned) => poisoned.into_inner(),
        };
        if lifecycle.child_handle.is_some() || lifecycle.exit_record.is_some() {
            return Err(SidecarFailure::state_unavailable());
        }
        let child_handle = Arc::new(Mutex::new(child));
        lifecycle.child_handle = Some(Arc::clone(&child_handle));
        lifecycle.stop_writer = Some(stop_writer);
        lifecycle.late_ready_pending = false;
        lifecycle.late_ready_terminal = false;
        lifecycle.expected_exit_requested = false;
        lifecycle.exit_event_armed = false;
        lifecycle.exit_event_delivered = false;
        drop(lifecycle);

        // Runtime blocking pool은 OS thread 생성 failure를 install Result 뒤에 숨기지 않고 lifecycle을 계속 소유한다.
        let monitor_state = self.clone();
        tauri::async_runtime::spawn_blocking(move || {
            monitor_child_exit(monitor_state, child_handle, app_handle);
        });

        Ok(())
    }

    /// 함수 이름: begin_late_ready_recovery()
    /// 기능: native status dialog가 recoverable timeout과 terminal ambiguity를 구분하도록 pending 상태를 publish한다.
    /// 인자: 없음
    /// 반환값: state update 성공 또는 typed failure
    /// 작성 날짜: 2026/08/24
    fn begin_late_ready_recovery(&self) -> Result<(), SidecarFailure> {
        let mut lifecycle = self.lock_lifecycle()?;
        if lifecycle.child_handle.is_none() || lifecycle.exit_record.is_some() {
            return Err(SidecarFailure::unavailable());
        }
        lifecycle.late_ready_pending = true;
        lifecycle.late_ready_terminal = false;
        Ok(())
    }

    /// 함수 이름: finish_late_ready_recovery()
    /// 기능: valid 또는 terminal FD4 결과 뒤 pending status dialog 반복을 중단한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    fn finish_late_ready_recovery(&self) {
        let mut lifecycle = match self.shared.lifecycle.lock() {
            Ok(lifecycle) => lifecycle,
            Err(poisoned) => poisoned.into_inner(),
        };
        lifecycle.late_ready_pending = false;
        lifecycle.late_ready_terminal = false;
    }

    /// 함수 이름: mark_late_ready_terminal()
    /// 기능: main-thread dispatch를 완료할 수 없는 late READY를 status dialog가 fatal recovery로 전환하게 표시한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    fn mark_late_ready_terminal(&self) {
        let mut lifecycle = match self.shared.lifecycle.lock() {
            Ok(lifecycle) => lifecycle,
            Err(poisoned) => poisoned.into_inner(),
        };
        lifecycle.late_ready_pending = false;
        lifecycle.late_ready_terminal = true;
    }

    /// 함수 이름: is_late_ready_pending()
    /// 기능: renderer 없는 timeout recovery가 진행 중일 때만 native status surface를 유지한다.
    /// 인자: 없음
    /// 반환값: live child의 late READY wait 여부
    /// 작성 날짜: 2026/08/24
    pub fn is_late_ready_pending(&self) -> bool {
        self.shared
            .lifecycle
            .lock()
            .map(|state| {
                state.late_ready_pending
                    && state.child_handle.is_some()
                    && state.exit_record.is_none()
            })
            .unwrap_or(true)
    }

    /// 함수 이름: is_late_ready_terminal()
    /// 기능: pending status dialog가 main-thread dispatch failure를 fatal operator surface로 승격해야 하는지 확인한다.
    /// 인자: 없음
    /// 반환값: live child의 terminal late READY 상태 여부
    /// 작성 날짜: 2026/08/24
    pub fn is_late_ready_terminal(&self) -> bool {
        self.shared
            .lifecycle
            .lock()
            .map(|state| {
                state.late_ready_terminal
                    && state.child_handle.is_some()
                    && state.exit_record.is_none()
            })
            .unwrap_or(true)
    }

    /// 함수 이름: await_expected_exit()
    /// 기능: HTTP shutdown 수락 뒤 FD5를 닫고 bounded clean exit를 기다리되 timeout에는 kill하지 않는다.
    /// 인자: timeout_ms -> optional renderer wait timeout
    /// 반환값: clean exit receipt 또는 secret 없는 typed failure
    /// 작성 날짜: 2026/08/24
    fn await_expected_exit(
        &self,
        timeout_ms: Option<u64>,
    ) -> Result<BackendSidecarExitReceipt, SidecarFailure> {
        let timeout = normalize_exit_timeout(timeout_ms)?;
        let stop_writer = {
            let mut lifecycle = self.lock_lifecycle()?;
            if let Some(exit_record) = lifecycle.exit_record.as_ref() {
                return receipt_from_exit_record(exit_record);
            }
            if lifecycle.child_handle.is_none() {
                return Err(SidecarFailure::unavailable());
            }

            // 이 flag를 stop FD보다 먼저 publish해 monitor가 정상 종료 요청을 정확히 분류하게 한다.
            lifecycle.expected_exit_requested = true;
            lifecycle.stop_writer.take()
        };

        // 한 번의 byte와 EOF는 Python runner의 stop read를 해제하며 실패해도 자동 kill로 전환하지 않는다.
        if let Some(mut writer) = stop_writer {
            let _ = writer.write_all(&[0_u8]);
        }

        // Condvar wait는 process를 변경하지 않고 timeout 시 operator decision 경로만 연다.
        let lifecycle = self.lock_lifecycle()?;
        let (lifecycle, timeout_result) = self
            .shared
            .exit_condition
            .wait_timeout_while(lifecycle, timeout, |state| state.exit_record.is_none())
            .map_err(|_| SidecarFailure::state_unavailable())?;
        if timeout_result.timed_out() && lifecycle.exit_record.is_none() {
            return Err(SidecarFailure::exit_timeout());
        }

        lifecycle
            .exit_record
            .as_ref()
            .ok_or_else(SidecarFailure::exit_timeout)
            .and_then(receipt_from_exit_record)
    }

    /// 함수 이름: is_running()
    /// 기능: app-level exit가 live child를 우회해 종료하지 못하도록 fail-closed 상태를 반환한다.
    /// 인자: 없음
    /// 반환값: child가 있고 exit record가 없으면 true
    /// 작성 날짜: 2026/08/24
    pub fn is_running(&self) -> bool {
        self.shared
            .lifecycle
            .lock()
            .map(|state| state.child_handle.is_some() && state.exit_record.is_none())
            .unwrap_or(true) // poisoned state에서는 native app exit를 허용하지 않는다.
    }

    /// 함수 이름: record_exit()
    /// 기능: monitor가 관찰한 status를 expected flag와 원자적으로 결합해 waiters를 깨운다.
    /// 인자: code -> clean/non-zero code 또는 signal을 나타내는 None
    /// 반환값: renderer event exact payload와 armed listener emit 필요 여부
    /// 작성 날짜: 2026/08/24
    fn record_exit(&self, code: Option<i32>) -> (BackendSidecarExitPayload, bool) {
        let mut lifecycle = match self.shared.lifecycle.lock() {
            Ok(lifecycle) => lifecycle,
            Err(poisoned) => poisoned.into_inner(),
        };
        let payload = BackendSidecarExitPayload {
            expected: classify_expected_exit(lifecycle.expected_exit_requested, code),
            code,
        };

        // Exit publication과 handle/pipe 참조 제거를 한 critical section에서 완료한다.
        lifecycle.exit_record = Some(payload.clone());
        lifecycle.child_handle = None;
        lifecycle.stop_writer = None;
        lifecycle.late_ready_pending = false;
        lifecycle.late_ready_terminal = false;
        let should_emit = lifecycle.exit_event_armed && !lifecycle.exit_event_delivered;
        lifecycle.exit_event_delivered = should_emit;
        self.shared.exit_condition.notify_all();
        (payload, should_emit)
    }

    /// 함수 이름: arm_exit_event_delivery()
    /// 기능: renderer listener를 armed로 publish하고 이미 기록된 exit가 있으면 한 번 전달할 payload를 꺼낸다.
    /// 인자: 없음
    /// 반환값: optional pending payload 또는 typed state failure
    /// 작성 날짜: 2026/08/24
    fn arm_exit_event_delivery(&self) -> Result<Option<BackendSidecarExitPayload>, SidecarFailure> {
        let mut lifecycle = self.lock_lifecycle()?;
        lifecycle.exit_event_armed = true;
        if lifecycle.exit_event_delivered {
            return Ok(None);
        }
        let Some(payload) = lifecycle.exit_record.clone() else {
            return Ok(None);
        };

        lifecycle.exit_event_delivered = true;
        Ok(Some(payload))
    }

    /// 함수 이름: restore_exit_event_delivery()
    /// 기능: Tauri emit 실패 후 recorded exit를 재-arm command이 다시 전달할 수 있게 복원한다.
    /// 인자: 없음
    /// 반환값: 복원 성공 또는 typed state failure
    /// 작성 날짜: 2026/08/24
    fn restore_exit_event_delivery(&self) -> Result<(), SidecarFailure> {
        let mut lifecycle = self.lock_lifecycle()?;
        lifecycle.exit_event_delivered = false;
        Ok(())
    }

    /// 함수 이름: lock_lifecycle()
    /// 기능: poison 상세를 반사하지 않고 shared lifecycle guard를 얻는다.
    /// 인자: 없음
    /// 반환값: lifecycle mutex guard 또는 typed state failure
    /// 작성 날짜: 2026/08/24
    fn lock_lifecycle(&self) -> Result<MutexGuard<'_, SidecarProcessLifecycle>, SidecarFailure> {
        self.shared
            .lifecycle
            .lock()
            .map_err(|_| SidecarFailure::state_unavailable())
    }
}

/// 함수 이름: disable_process_core_dumps()
/// 기능: renderer token과 FD6 credential이 native/backend crash dump에 기록되지 않게 RLIMIT_CORE를 0으로 고정한다.
/// 인자: 없음
/// 반환값: 설정 성공 또는 secret 없는 startup failure
/// 작성 날짜: 2026/08/24
pub fn disable_process_core_dumps() -> Result<(), SidecarFailure> {
    set_zero_core_dump_limit().map_err(|_| SidecarFailure::startup())
}

/// 함수 이름: prepare_backend_sidecar()
/// 기능: Keychain/config/token pipe를 조립하고 ready를 strict 검증해 renderer publication 직전 상태를 만든다.
/// 인자: app_handle -> bundle path와 app data path를 해석할 Tauri handle
/// 반환값: ready 검증을 마친 child/descriptor 묶음 또는 secret 없는 failure
/// 작성 날짜: 2026/08/24
pub fn prepare_backend_sidecar(
    app_handle: &AppHandle,
) -> Result<BackendSidecarPreparation, SidecarFailure> {
    let credentials = read_keychain_credentials()?;
    let app_data_directory = app_handle
        .path()
        .app_data_dir()
        .map_err(|_| SidecarFailure::startup())?;
    ensure_private_app_data_directory(&app_data_directory)?;
    let history_path = build_history_path(&app_data_directory)?;
    let allowed_origin = select_allowed_origin(tauri::is_dev());

    // Production configuration은 주문 opt-in을 false/null로 고정하고 credential을 참조로만 직렬화한다.
    let configuration = SidecarBootstrapConfiguration {
        schema_version: BACKEND_SCHEMA_VERSION,
        allowed_origin,
        history_path: &history_path,
        api_key: &credentials.api_key,
        api_secret: &credentials.api_secret,
        allow_testnet_orders: false,
        max_notional: None,
    };
    let configuration_payload = serialize_bootstrap_configuration(&configuration)?;

    // 각 child end를 16 이상 CLOEXEC staging FD로 옮겨 dup2 target 3~6 충돌을 제거한다.
    let (token_writer, token_child_reader) =
        create_parent_writer_child_reader().map_err(|_| SidecarFailure::startup())?;
    let (mut ready_reader, ready_child_writer) =
        create_parent_reader_child_writer().map_err(|_| SidecarFailure::startup())?;
    let (stop_writer, stop_child_reader) =
        create_parent_writer_child_reader().map_err(|_| SidecarFailure::startup())?;
    let (config_writer, config_child_reader) =
        create_parent_writer_child_reader().map_err(|_| SidecarFailure::startup())?;
    let sidecar_path = resolve_sidecar_executable()?;

    // Child는 argv와 inherited environment 없이 anonymous FD 네 개와 null standard streams만 받는다.
    let mut command = Command::new(sidecar_path);
    command
        .env_clear()
        .current_dir(&app_data_directory)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    configure_child_file_descriptors(
        &mut command,
        token_child_reader.as_raw_fd(),
        ready_child_writer.as_raw_fd(),
        stop_child_reader.as_raw_fd(),
        config_child_reader.as_raw_fd(),
    );
    let child = command.spawn().map_err(|_| SidecarFailure::startup())?;

    // Spawn 뒤 parent가 가진 child staging ends를 즉시 닫아 pipe EOF ownership을 단일화한다.
    drop(token_child_reader);
    drop(ready_child_writer);
    drop(stop_child_reader);
    drop(config_child_reader);

    // Token이 없으면 backend가 runtime factory를 호출할 수 없으므로 pre-runtime child만 회수한다.
    let mut token = match generate_session_token() {
        Ok(token) => Zeroizing::new(token),
        Err(failure) => {
            abort_proven_pre_runtime_child(child, stop_writer);
            return Err(failure);
        }
    };

    // Token/config은 각 전용 pipe에 한 번 쓰고 buffer를 지우며 ready FD에는 secret이 존재하지 않는다.
    let token_result = write_pipe_payload(token_writer, token.as_bytes());
    let config_result = write_pipe_payload(config_writer, configuration_payload.as_slice());
    if token_result.is_err() || config_result.is_err() {
        token.zeroize();
        abort_proven_pre_runtime_child(child, stop_writer);
        return Err(SidecarFailure::startup());
    }
    let mut ready_payload = Zeroizing::new(Vec::with_capacity(256));
    let ready_result =
        read_ready_descriptor(&mut ready_reader, &mut ready_payload, SIDECAR_READY_TIMEOUT);
    let ready = match ready_result {
        Ok(ready) => ready,
        Err(ReadyDescriptorReadFailure::Timeout) => {
            debug_assert_eq!(
                select_startup_child_recovery_policy(true),
                StartupChildRecoveryPolicy::RetainAmbiguousExposure
            );
            return Ok(BackendSidecarPreparation::Ambiguous(
                AmbiguousBackendSidecar {
                    child,
                    stop_writer,
                    late_ready_recovery: Some(LateReadySidecarRecovery {
                        ready_reader,
                        ready_payload,
                        token,
                    }),
                },
            ));
        }
        Err(ReadyDescriptorReadFailure::Terminal(_failure)) => {
            debug_assert_eq!(
                select_startup_child_recovery_policy(true),
                StartupChildRecoveryPolicy::RetainAmbiguousExposure
            );
            return Ok(BackendSidecarPreparation::Ambiguous(
                AmbiguousBackendSidecar {
                    child,
                    stop_writer,
                    late_ready_recovery: None,
                },
            ));
        }
    };

    // Ready/token은 각각 strict parser/generator를 통과했으므로 추가 fallible rollback 없이 소유권을 이전한다.
    let port = ready.port;
    let descriptor = build_connection_descriptor(ready, &mut token);

    Ok(BackendSidecarPreparation::Ready(PreparedBackendSidecar {
        descriptor,
        child,
        stop_writer,
        port,
    }))
}

/// 함수 이름: build_connection_descriptor()
/// 기능: strict READY와 zeroizing token을 복사 없이 renderer one-shot descriptor로 이전한다.
/// 인자: ready -> 검증된 FD4 wire, token -> CSPRNG token owner
/// 반환값: native connection descriptor
/// 작성 날짜: 2026/08/24
fn build_connection_descriptor(
    ready: ReadyDescriptorWire,
    token: &mut Zeroizing<String>,
) -> BackendConnectionDescriptor {
    let transferred_token = std::mem::take(&mut **token);
    BackendConnectionDescriptor {
        port: ready.port,
        session_id: ready.session_id,
        schema_version: ready.schema_version,
        token: transferred_token,
    }
}

/// 함수 이름: start_late_ready_recovery()
/// 기능: initial READY timeout 뒤 FD4를 계속 drain하고 valid late READY면 descriptor/window startup을 복구한다.
/// 인자: recovery -> preserved FD4 framing/token, app_handle -> main-thread publication owner,
///      process_state -> child exit와 recovery race를 차단할 lifecycle owner
/// 반환값: recovery task scheduling 성공 또는 typed state failure
/// 작성 날짜: 2026/08/24
pub fn start_late_ready_recovery(
    mut recovery: LateReadySidecarRecovery,
    app_handle: AppHandle,
    process_state: SidecarProcessState,
) -> Result<(), SidecarFailure> {
    process_state.begin_late_ready_recovery()?;

    // Blocking runtime task가 FD4 reader를 소유하므로 timeout 뒤 Python READY write가 BrokenPipe가 되지 않는다.
    tauri::async_runtime::spawn_blocking(move || loop {
        let ready_result = read_ready_descriptor(
            &mut recovery.ready_reader,
            &mut recovery.ready_payload,
            LATE_READY_ATTEMPT_TIMEOUT,
        );
        match ready_result {
            Ok(ready) => {
                if !process_state.is_running() {
                    return; // Exit가 먼저 기록되면 token을 renderer에 stage하지 않고 Drop zeroize한다.
                }
                publish_late_ready_descriptor(ready, recovery.token, app_handle, process_state);
                return;
            }
            Err(ReadyDescriptorReadFailure::Timeout) => {
                if !process_state.is_running() {
                    return;
                }
            }
            Err(ReadyDescriptorReadFailure::Terminal(_failure)) => {
                schedule_late_ready_terminal_recovery(app_handle, process_state);
                return;
            }
        }
    });

    Ok(())
}

/// 함수 이름: publish_late_ready_descriptor()
/// 기능: late READY와 preserved token을 main thread에서 one-shot stage한 뒤에만 renderer window를 만든다.
/// 인자: ready -> strict late descriptor, token -> zeroizing session token,
///      app_handle -> Tauri publication owner, process_state -> live child guard
/// 반환값: 없음
/// 작성 날짜: 2026/08/24
fn publish_late_ready_descriptor(
    ready: ReadyDescriptorWire,
    mut token: Zeroizing<String>,
    app_handle: AppHandle,
    process_state: SidecarProcessState,
) {
    let port = ready.port;
    let descriptor = build_connection_descriptor(ready, &mut token);
    let dispatch_handle = app_handle.clone();
    let dispatch_process_state = process_state.clone();

    // Tauri managed state와 WebView 생성은 main event-loop thread에서 순서대로 수행한다.
    let dispatch_result = app_handle.run_on_main_thread(move || {
        if !dispatch_process_state.is_running() {
            return;
        }
        dispatch_process_state.finish_late_ready_recovery();
        let descriptor_state = dispatch_handle.state::<crate::BackendConnectionDescriptorState>();
        if descriptor_state.stage(descriptor).is_err() {
            crate::schedule_ready_fatal_recovery(dispatch_handle);
            return;
        }
        if create_ready_main_window(&dispatch_handle, port).is_err() {
            crate::schedule_ready_window_recovery(dispatch_handle, port);
        }
    });

    // Background thread는 dialog API를 직접 호출하지 않고 기존 status callback에 terminal 전환만 알린다.
    if dispatch_result.is_err() && process_state.is_running() {
        process_state.mark_late_ready_terminal();
    }
}

/// 함수 이름: schedule_late_ready_terminal_recovery()
/// 기능: invalid/EOF로 framing 복구가 불가능한 live child를 kill하지 않고 native operator surface로 전환한다.
/// 인자: app_handle -> native dialog dispatcher, process_state -> live child guard
/// 반환값: 없음
/// 작성 날짜: 2026/08/24
fn schedule_late_ready_terminal_recovery(
    app_handle: AppHandle,
    process_state: SidecarProcessState,
) {
    if !process_state.is_running() {
        return;
    }
    let dispatch_handle = app_handle.clone();
    let dispatch_process_state = process_state.clone();
    let dispatch_result = app_handle.run_on_main_thread(move || {
        if dispatch_process_state.is_running() {
            dispatch_process_state.finish_late_ready_recovery();
            crate::schedule_ready_fatal_recovery(dispatch_handle);
        }
    });

    // Main-thread dispatch가 불가능하면 기존 status callback이 fatal operator surface로 전환한다.
    if dispatch_result.is_err() && process_state.is_running() {
        process_state.mark_late_ready_terminal();
    }
}

/// 함수 이름: abort_proven_pre_runtime_child()
/// 기능: token/config 입력이 완성되지 않아 runtime 생성 불가가 증명된 child만 bounded grace 뒤 회수한다.
/// 인자: child -> runtime 생성 전 process, stop_writer -> parent FD5 writer
/// 반환값: 없음
/// 작성 날짜: 2026/08/24
fn abort_proven_pre_runtime_child(mut child: Child, mut stop_writer: File) {
    debug_assert_eq!(
        select_startup_child_recovery_policy(false),
        StartupChildRecoveryPolicy::AbortProvenPreRuntime
    );
    let _ = stop_writer.write_all(&[0_u8]);
    drop(stop_writer);

    // Backend가 stop FD를 처리할 짧은 기회를 제공한 뒤 startup-only child를 회수한다.
    let deadline = Instant::now() + PROVEN_PRE_RUNTIME_ABORT_GRACE;
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) | Err(_) => thread::sleep(Duration::from_millis(10)),
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

/// 함수 이름: select_startup_child_recovery_policy()
/// 기능: runtime input publication 후 READY 관찰을 시작했다면 timeout/parse 실패에 timed kill을 금지한다.
/// 인자: ready_observation_started -> token/config publication 후 ready wait 진입 여부
/// 반환값: forced abort 또는 ambiguous child retention policy
/// 작성 날짜: 2026/08/24
fn select_startup_child_recovery_policy(
    ready_observation_started: bool,
) -> StartupChildRecoveryPolicy {
    if ready_observation_started {
        StartupChildRecoveryPolicy::RetainAmbiguousExposure
    } else {
        StartupChildRecoveryPolicy::AbortProvenPreRuntime
    }
}

/// 함수 이름: create_ready_main_window()
/// 기능: ready port를 production CSP에 exact 주입하고 deferred main window를 처음 생성한다.
/// 인자: app_handle -> Tauri window manager, assigned_port -> validated random loopback port
/// 반환값: main window 생성 성공 또는 secret 없는 startup failure
/// 작성 날짜: 2026/08/24
pub fn create_ready_main_window(
    app_handle: &AppHandle,
    assigned_port: u16,
) -> Result<(), SidecarFailure> {
    if assigned_port == 0 {
        return Err(SidecarFailure::descriptor_rejected());
    }
    let window_configuration = app_handle
        .config()
        .app
        .windows
        .first()
        .cloned()
        .ok_or_else(SidecarFailure::startup)?;
    if window_configuration.label != "main" || window_configuration.create {
        return Err(SidecarFailure::startup());
    }
    if app_handle.get_webview_window("main").is_some() {
        return Ok(()); // 첫 build가 window를 만든 후 오류를 반환한 recovery race는 duplicate 창 없이 성공 처리한다.
    }

    // Production base CSP가 exact placeholder 두 개를 한 번씩 가질 때만 renderer를 생성한다.
    if !tauri::is_dev() {
        let configured_csp = app_handle
            .config()
            .app
            .security
            .csp
            .as_ref()
            .map(ToString::to_string)
            .ok_or_else(SidecarFailure::startup)?;
        replace_production_csp(&configured_csp, assigned_port)
            .ok_or_else(SidecarFailure::startup)?;
    }

    // Tauri protocol response의 nonce/hash 보강을 보존하면서 sentinel만 assigned origin으로 바꾼다.
    WebviewWindowBuilder::from_config(app_handle, &window_configuration)
        .map_err(|_| SidecarFailure::startup())?
        .on_web_resource_request(move |request, response| {
            if request.uri().scheme_str() != Some("tauri") {
                return;
            }
            let Some(header_value) = response.headers_mut().get_mut("content-security-policy")
            else {
                return;
            };
            let Ok(current_csp) = header_value.to_str() else {
                return;
            };
            let Some(runtime_csp) = replace_production_csp(current_csp, assigned_port) else {
                return;
            };
            if let Ok(runtime_header) = tauri::http::HeaderValue::from_str(&runtime_csp) {
                *header_value = runtime_header; // Header parse 실패 시 sentinel CSP가 연결을 fail closed한다.
            }
        })
        .build()
        .map_err(|_| SidecarFailure::startup())?;

    Ok(())
}

/// 함수 이름: await_backend_sidecar_exit()
/// 기능: renderer shutdown 202 뒤 native stop FD를 release하고 clean child exit를 bounded wait한다.
/// 인자: state -> managed sidecar owner, timeout_ms -> optional wait timeout
/// 반환값: exact clean-exit receipt 또는 typed failure Promise
/// 작성 날짜: 2026/08/24
#[tauri::command]
pub async fn await_backend_sidecar_exit(
    state: State<'_, SidecarProcessState>,
    timeout_ms: Option<u64>,
) -> Result<BackendSidecarExitReceipt, SidecarFailure> {
    let owned_state = state.inner().clone();

    // Condvar wait를 async command executor의 blocking pool로 분리해 Tauri main thread를 막지 않는다.
    tauri::async_runtime::spawn_blocking(move || owned_state.await_expected_exit(timeout_ms))
        .await
        .map_err(|_| SidecarFailure::state_unavailable())?
}

/// 함수 이름: arm_sidecar_exit_event_bridge()
/// 기능: renderer exit listener를 armed로 publish하고 listener 전 recorded exit를 손실 없이 flush한다.
/// 인자: app_handle -> main renderer event emitter, state -> managed sidecar owner
/// 반환값: exact armed receipt 또는 typed failure
/// 작성 날짜: 2026/08/24
#[tauri::command]
pub fn arm_sidecar_exit_event_bridge(
    app_handle: AppHandle,
    state: State<'_, SidecarProcessState>,
) -> Result<BackendSidecarExitEventArmReceipt, SidecarFailure> {
    if let Some(payload) = state.arm_exit_event_delivery()? {
        if app_handle
            .emit_to("main", BACKEND_SIDECAR_EXIT_EVENT, payload)
            .is_err()
        {
            state.restore_exit_event_delivery()?;
            return Err(SidecarFailure::state_unavailable());
        }
    }

    Ok(BackendSidecarExitEventArmReceipt { armed: true })
}

/// 함수 이름: monitor_child_exit()
/// 기능: child exit만 polling하고 exact expected/code event와 native wait condition을 publish한다.
/// 인자: state -> shared lifecycle, child_handle -> retained child handle,
///      app_handle -> renderer event emitter
/// 반환값: 없음
/// 작성 날짜: 2026/08/24
fn monitor_child_exit(
    state: SidecarProcessState,
    child_handle: Arc<Mutex<Child>>,
    app_handle: AppHandle,
) {
    loop {
        let status_result = recover_child_lock(&child_handle).try_wait();
        match status_result {
            Ok(Some(status)) => {
                let (payload, should_emit) = state.record_exit(status.code());
                if should_emit
                    && app_handle
                        .emit_to("main", BACKEND_SIDECAR_EXIT_EVENT, payload)
                        .is_err()
                {
                    let _ = state.restore_exit_event_delivery();
                }
                return;
            }
            Ok(None) | Err(_) => thread::sleep(SIDECAR_MONITOR_INTERVAL),
        }
    }
}

/// 함수 이름: read_keychain_credentials()
/// 기능: macOS Security.framework에서 두 credential을 renderer와 subprocess stdout 밖에서 읽는다.
/// 인자: 없음
/// 반환값: strict native credential pair 또는 generic unavailable failure
/// 작성 날짜: 2026/08/24
fn read_keychain_credentials() -> Result<KeychainCredentials, SidecarFailure> {
    // Stable service와 별도 account는 renderer 입력이나 일반 environment 없이 Keychain item을 식별한다.
    let api_key = read_keychain_secret(KEYCHAIN_API_KEY_ACCOUNT)?;
    let api_secret = match read_keychain_secret(KEYCHAIN_API_SECRET_ACCOUNT) {
        Ok(secret) => secret,
        Err(failure) => {
            let mut api_key = api_key;
            api_key.zeroize();
            return Err(failure);
        }
    };

    Ok(KeychainCredentials {
        api_key,
        api_secret,
    })
}

/// 함수 이름: read_keychain_secret()
/// 기능: generic-password bytes를 bounded printable ASCII String으로 검증하고 오류 bytes를 zeroize한다.
/// 인자: account -> stable non-secret Keychain account identifier
/// 반환값: 검증된 secret String 또는 generic unavailable failure
/// 작성 날짜: 2026/08/24
fn read_keychain_secret(account: &str) -> Result<String, SidecarFailure> {
    let options = PasswordOptions::new_generic_password(KEYCHAIN_SERVICE, account);
    let password_bytes =
        generic_password(options).map_err(|_| SidecarFailure::credentials_unavailable())?;
    let mut secret = match String::from_utf8(password_bytes) {
        Ok(secret) => secret,
        Err(error) => {
            let mut invalid_bytes = error.into_bytes();
            invalid_bytes.zeroize();
            return Err(SidecarFailure::credentials_unavailable());
        }
    };

    // Binance credential identity를 바꾸는 trim 없이 non-empty printable ASCII와 size만 제한한다.
    if secret.is_empty()
        || secret.len() > MAXIMUM_SECRET_BYTES
        || !secret.bytes().all(|byte| matches!(byte, 0x21..=0x7e))
    {
        secret.zeroize();
        return Err(SidecarFailure::credentials_unavailable());
    }

    Ok(secret)
}

/// 함수 이름: build_history_path()
/// 기능: app data directory 아래 고정 durable history path를 UTF-8 absolute string으로 만든다.
/// 인자: app_data_directory -> Tauri identifier-scoped application data directory
/// 반환값: backend FD6 history_path 또는 startup failure
/// 작성 날짜: 2026/08/24
fn build_history_path(app_data_directory: &Path) -> Result<String, SidecarFailure> {
    let history_path = app_data_directory.join(HISTORY_FILE_NAME);
    if !history_path.is_absolute() {
        return Err(SidecarFailure::startup());
    }

    history_path
        .to_str()
        .map(str::to_owned)
        .ok_or_else(SidecarFailure::startup)
}

/// 함수 이름: ensure_private_app_data_directory()
/// 기능: credential-adjacent history directory가 symlink가 아닌 per-user directory이며 mode 0700임을 보장한다.
/// 인자: app_data_directory -> Tauri identifier-scoped application data directory
/// 반환값: private directory 준비 성공 또는 startup failure
/// 작성 날짜: 2026/08/24
fn ensure_private_app_data_directory(app_data_directory: &Path) -> Result<(), SidecarFailure> {
    fs::create_dir_all(app_data_directory).map_err(|_| SidecarFailure::startup())?;
    let metadata =
        fs::symlink_metadata(app_data_directory).map_err(|_| SidecarFailure::startup())?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err(SidecarFailure::startup());
    }

    // umask와 기존 directory mode에 의존하지 않고 group/other access를 매 launch에서 제거한다.
    fs::set_permissions(app_data_directory, fs::Permissions::from_mode(0o700))
        .map_err(|_| SidecarFailure::startup())
}

/// 함수 이름: serialize_bootstrap_configuration()
/// 기능: exact read-only FD6 JSON을 bounded bytes로 직렬화한다.
/// 인자: configuration -> native-only credential/config references
/// 반환값: 전송 뒤 zeroize할 JSON bytes 또는 startup failure
/// 작성 날짜: 2026/08/24
fn serialize_bootstrap_configuration(
    configuration: &SidecarBootstrapConfiguration<'_>,
) -> Result<Zeroizing<Vec<u8>>, SidecarFailure> {
    let mut payload = serde_json::to_vec(configuration).map_err(|_| SidecarFailure::startup())?;
    if payload.is_empty() || payload.len() > MAXIMUM_CONFIG_BYTES {
        payload.zeroize();
        return Err(SidecarFailure::startup());
    }

    Ok(Zeroizing::new(payload))
}

/// 함수 이름: generate_session_token()
/// 기능: launch마다 CSPRNG 32 bytes를 base64url no-padding 43자 token으로 만든다.
/// 인자: 없음
/// 반환값: memory-only session token 또는 startup failure
/// 작성 날짜: 2026/08/24
fn generate_session_token() -> Result<String, SidecarFailure> {
    let mut token_bytes = [0_u8; 32];
    if getrandom::fill(&mut token_bytes).is_err() {
        token_bytes.zeroize();
        return Err(SidecarFailure::startup());
    }
    let mut token = URL_SAFE_NO_PAD.encode(token_bytes);
    token_bytes.zeroize();

    if token.len() != 43 {
        token.zeroize();
        return Err(SidecarFailure::startup());
    }
    Ok(token)
}

/// 함수 이름: select_allowed_origin()
/// 기능: packaged macOS와 fixed Vite dev server를 별도 exact Origin으로 선택한다.
/// 인자: is_development -> Tauri dev build 여부
/// 반환값: backend strict Origin allowlist 단일 값
/// 작성 날짜: 2026/08/24
fn select_allowed_origin(is_development: bool) -> &'static str {
    if is_development {
        DEVELOPMENT_UI_ORIGIN
    } else {
        PRODUCTION_UI_ORIGIN
    }
}

/// 함수 이름: resolve_sidecar_executable()
/// 기능: target suffix가 제거되어 main executable 옆에 bundle된 exact sidecar를 검증한다.
/// 인자: 없음
/// 반환값: canonical executable path 또는 startup failure
/// 작성 날짜: 2026/08/24
fn resolve_sidecar_executable() -> Result<PathBuf, SidecarFailure> {
    let current_executable = std::env::current_exe().map_err(|_| SidecarFailure::startup())?;
    let executable_directory = current_executable
        .parent()
        .ok_or_else(SidecarFailure::startup)?;
    let canonical_directory = executable_directory
        .canonicalize()
        .map_err(|_| SidecarFailure::startup())?;
    let sidecar_path = executable_directory.join(SIDECAR_BINARY_NAME);
    let canonical_sidecar = sidecar_path
        .canonicalize()
        .map_err(|_| SidecarFailure::startup())?;
    let metadata = fs::metadata(&canonical_sidecar).map_err(|_| SidecarFailure::startup())?;

    // Bundle directory 밖 symlink와 executable bit가 없는 artifact를 process spawn 전에 거부한다.
    if !canonical_sidecar.starts_with(&canonical_directory)
        || !metadata.is_file()
        || metadata.permissions().mode() & 0o111 == 0
    {
        return Err(SidecarFailure::startup());
    }

    Ok(canonical_sidecar)
}

/// 함수 이름: create_parent_writer_child_reader()
/// 기능: parent write/child read anonymous pipe를 만들고 child end를 collision-free staging FD로 옮긴다.
/// 인자: 없음
/// 반환값: parent File과 staged child OwnedFd
/// 작성 날짜: 2026/08/24
fn create_parent_writer_child_reader() -> io::Result<(File, OwnedFd)> {
    let (reader, writer) = create_anonymous_pipe()?;
    let child_reader = duplicate_staging_fd(reader.as_raw_fd())?;
    drop(reader);
    Ok((File::from(writer), child_reader))
}

/// 함수 이름: create_parent_reader_child_writer()
/// 기능: parent read/child write anonymous pipe를 만들고 child end를 collision-free staging FD로 옮긴다.
/// 인자: 없음
/// 반환값: parent File과 staged child OwnedFd
/// 작성 날짜: 2026/08/24
fn create_parent_reader_child_writer() -> io::Result<(File, OwnedFd)> {
    let (reader, writer) = create_anonymous_pipe()?;
    let child_writer = duplicate_staging_fd(writer.as_raw_fd())?;
    drop(writer);
    Ok((File::from(reader), child_writer))
}

/// 함수 이름: create_anonymous_pipe()
/// 기능: 양 끝에 CLOEXEC를 적용한 macOS anonymous pipe를 만든다.
/// 인자: 없음
/// 반환값: read/write OwnedFd pair
/// 작성 날짜: 2026/08/24
fn create_anonymous_pipe() -> io::Result<(OwnedFd, OwnedFd)> {
    let mut descriptors = [-1; 2];
    if unsafe { libc::pipe(descriptors.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error());
    }

    // Raw FD를 즉시 OwnedFd로 감싸 이후 모든 early return에서 자동 close되게 한다.
    let reader = unsafe { OwnedFd::from_raw_fd(descriptors[0]) };
    let writer = unsafe { OwnedFd::from_raw_fd(descriptors[1]) };
    set_close_on_exec(reader.as_raw_fd())?;
    set_close_on_exec(writer.as_raw_fd())?;
    Ok((reader, writer))
}

/// 함수 이름: set_close_on_exec()
/// 기능: exec에서 명시적으로 dup하지 않은 pipe end가 sidecar에 남지 않게 FD_CLOEXEC를 설정한다.
/// 인자: descriptor -> 설정할 raw FD
/// 반환값: OS 설정 결과
/// 작성 날짜: 2026/08/24
fn set_close_on_exec(descriptor: RawFd) -> io::Result<()> {
    let existing_flags = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
    if existing_flags == -1 {
        return Err(io::Error::last_os_error());
    }
    if unsafe { libc::fcntl(descriptor, libc::F_SETFD, existing_flags | libc::FD_CLOEXEC) } == -1 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// 함수 이름: duplicate_staging_fd()
/// 기능: pre_exec dup2 source를 target 3~6과 겹치지 않는 CLOEXEC FD로 복제한다.
/// 인자: descriptor -> pipe의 원래 child end
/// 반환값: 16 이상 staged OwnedFd
/// 작성 날짜: 2026/08/24
fn duplicate_staging_fd(descriptor: RawFd) -> io::Result<OwnedFd> {
    let staged_descriptor =
        unsafe { libc::fcntl(descriptor, libc::F_DUPFD_CLOEXEC, MINIMUM_STAGING_FD) };
    if staged_descriptor == -1 {
        return Err(io::Error::last_os_error());
    }

    Ok(unsafe { OwnedFd::from_raw_fd(staged_descriptor) })
}

/// 함수 이름: configure_child_file_descriptors()
/// 기능: pre_exec에서 staged pipe를 FD3 token/4 ready/5 stop/6 config로만 상속한다.
/// 인자: command -> sidecar Command, 각 staged descriptor -> child mapping source
/// 반환값: 없음
/// 작성 날짜: 2026/08/24
fn configure_child_file_descriptors(
    command: &mut Command,
    token_descriptor: RawFd,
    ready_descriptor: RawFd,
    stop_descriptor: RawFd,
    config_descriptor: RawFd,
) {
    // pre_exec closure는 fork 뒤 async-signal-safe dup2/setrlimit 호출만 수행한다.
    unsafe {
        command.pre_exec(move || {
            set_zero_core_dump_limit()?;
            duplicate_to_child_fd(token_descriptor, TOKEN_CHILD_FD)?;
            duplicate_to_child_fd(ready_descriptor, READY_CHILD_FD)?;
            duplicate_to_child_fd(stop_descriptor, STOP_CHILD_FD)?;
            duplicate_to_child_fd(config_descriptor, CONFIG_CHILD_FD)?;
            Ok(())
        });
    }
}

/// 함수 이름: duplicate_to_child_fd()
/// 기능: staged descriptor를 고정 child FD로 원자적으로 복제한다.
/// 인자: source -> 16 이상 source FD, target -> ADR fixed FD
/// 반환값: OS dup 결과
/// 작성 날짜: 2026/08/24
fn duplicate_to_child_fd(source: RawFd, target: RawFd) -> io::Result<()> {
    if unsafe { libc::dup2(source, target) } == -1 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// 함수 이름: set_zero_core_dump_limit()
/// 기능: 현재 process 또는 pre_exec child의 core dump soft/hard limit를 모두 0으로 만든다.
/// 인자: 없음
/// 반환값: OS 설정 결과
/// 작성 날짜: 2026/08/24
fn set_zero_core_dump_limit() -> io::Result<()> {
    let limit = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    if unsafe { libc::setrlimit(libc::RLIMIT_CORE, &limit) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// 함수 이름: write_pipe_payload()
/// 기능: bounded payload를 anonymous pipe에 전부 쓰고 File drop으로 EOF를 전달한다.
/// 인자: writer -> parent pipe File, payload -> token 또는 strict config bytes
/// 반환값: write 결과
/// 작성 날짜: 2026/08/24
fn write_pipe_payload(mut writer: File, payload: &[u8]) -> io::Result<()> {
    writer.write_all(payload)
}

/// 함수 이름: read_ready_descriptor()
/// 기능: nonblocking FD4에서 bounded framing을 보존하며 exact JSON ready descriptor를 timeout 내 읽는다.
/// 인자: reader -> parent ready File, payload -> timeout 사이에도 보존할 framing buffer,
///      timeout -> 이번 read attempt의 bounded wait
/// 반환값: strict ready wire, retryable timeout 또는 terminal failure
/// 작성 날짜: 2026/08/24
fn read_ready_descriptor(
    reader: &mut File,
    payload: &mut Zeroizing<Vec<u8>>,
    timeout: Duration,
) -> Result<ReadyDescriptorWire, ReadyDescriptorReadFailure> {
    set_nonblocking(reader.as_raw_fd())
        .map_err(|_| ReadyDescriptorReadFailure::Terminal(SidecarFailure::startup()))?;
    let deadline = Instant::now() + timeout;
    let mut chunk = [0_u8; 512];

    // Timeout에서는 payload를 지우지 않아 split JSON의 framing과 late READY recovery를 그대로 이어 간다.
    loop {
        if Instant::now() >= deadline {
            return Err(ReadyDescriptorReadFailure::Timeout);
        }
        match reader.read(&mut chunk) {
            Ok(0) => {
                payload.zeroize();
                return Err(ReadyDescriptorReadFailure::Terminal(
                    SidecarFailure::startup(),
                ));
            }
            Ok(read_count) => {
                payload.extend_from_slice(&chunk[..read_count]);
                if payload.len() > MAXIMUM_READY_BYTES {
                    payload.zeroize();
                    return Err(ReadyDescriptorReadFailure::Terminal(
                        SidecarFailure::descriptor_rejected(),
                    ));
                }
                if let Some(newline_index) = payload.iter().position(|byte| *byte == b'\n') {
                    let trailing_is_whitespace = payload[newline_index + 1..]
                        .iter()
                        .all(u8::is_ascii_whitespace);
                    if !trailing_is_whitespace {
                        payload.zeroize();
                        return Err(ReadyDescriptorReadFailure::Terminal(
                            SidecarFailure::descriptor_rejected(),
                        ));
                    }
                    let parsed = parse_ready_descriptor(&payload[..newline_index]);
                    payload.zeroize();
                    return parsed.map_err(ReadyDescriptorReadFailure::Terminal);
                }
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(10));
            }
            Err(_) => {
                payload.zeroize();
                return Err(ReadyDescriptorReadFailure::Terminal(
                    SidecarFailure::startup(),
                ));
            }
        }
    }
}

/// 함수 이름: set_nonblocking()
/// 기능: ready FD wait에 native hard timeout을 적용할 수 있도록 O_NONBLOCK을 설정한다.
/// 인자: descriptor -> parent ready FD
/// 반환값: OS 설정 결과
/// 작성 날짜: 2026/08/24
fn set_nonblocking(descriptor: RawFd) -> io::Result<()> {
    let existing_flags = unsafe { libc::fcntl(descriptor, libc::F_GETFL) };
    if existing_flags == -1 {
        return Err(io::Error::last_os_error());
    }
    if unsafe { libc::fcntl(descriptor, libc::F_SETFL, existing_flags | libc::O_NONBLOCK) } == -1 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// 함수 이름: parse_ready_descriptor()
/// 기능: unknown field와 invalid port/session/schema를 raw value 반사 없이 거부한다.
/// 인자: payload -> newline을 제외한 ready JSON bytes
/// 반환값: strict ready wire 또는 descriptor failure
/// 작성 날짜: 2026/08/24
fn parse_ready_descriptor(payload: &[u8]) -> Result<ReadyDescriptorWire, SidecarFailure> {
    let ready: ReadyDescriptorWire =
        serde_json::from_slice(payload).map_err(|_| SidecarFailure::descriptor_rejected())?;
    if ready.port == 0
        || ready.schema_version != BACKEND_SCHEMA_VERSION
        || !crate::is_canonical_uuid(&ready.session_id)
    {
        return Err(SidecarFailure::descriptor_rejected());
    }
    Ok(ready)
}

/// 함수 이름: replace_production_csp()
/// 기능: production CSP의 두 sentinel을 validated random loopback origin으로 정확히 한 번 교체한다.
/// 인자: csp -> Tauri가 nonce/hash를 보강한 CSP, assigned_port -> backend port
/// 반환값: exact-port CSP 또는 malformed sentinel이면 None
/// 작성 날짜: 2026/08/24
fn replace_production_csp(csp: &str, assigned_port: u16) -> Option<String> {
    if assigned_port == 0
        || csp.matches(PRODUCTION_HTTP_CSP_SENTINEL).count() != 1
        || csp.matches(PRODUCTION_WS_CSP_SENTINEL).count() != 1
    {
        return None;
    }
    let exact_http_origin = format!("http://127.0.0.1:{assigned_port}");
    let exact_ws_origin = format!("ws://127.0.0.1:{assigned_port}");

    Some(
        csp.replace(PRODUCTION_HTTP_CSP_SENTINEL, &exact_http_origin)
            .replace(PRODUCTION_WS_CSP_SENTINEL, &exact_ws_origin),
    )
}

/// 함수 이름: normalize_exit_timeout()
/// 기능: 인자 없는 UI 호출에는 default를 적용하고 1ms~hard max 범위만 허용한다.
/// 인자: timeout_ms -> optional renderer value
/// 반환값: bounded Duration 또는 validation failure
/// 작성 날짜: 2026/08/24
fn normalize_exit_timeout(timeout_ms: Option<u64>) -> Result<Duration, SidecarFailure> {
    let selected_timeout = timeout_ms.unwrap_or(DEFAULT_EXIT_TIMEOUT_MS);
    if selected_timeout == 0 || selected_timeout > MAXIMUM_EXIT_TIMEOUT_MS {
        return Err(SidecarFailure::invalid_timeout());
    }
    Ok(Duration::from_millis(selected_timeout))
}

/// 함수 이름: classify_expected_exit()
/// 기능: native가 FD5 release intent를 먼저 publish한 exit만 expected로 분류한다.
/// 인자: expected_exit_requested -> FD5 release 요청 publication 여부,
///      code -> payload shape 검토용 exit code 또는 signal None
/// 반환값: 정상 shutdown으로 분류할 수 있으면 true
/// 작성 날짜: 2026/08/24
fn classify_expected_exit(expected_exit_requested: bool, _code: Option<i32>) -> bool {
    expected_exit_requested
}

/// 함수 이름: receipt_from_exit_record()
/// 기능: expected clean code 0만 final window destroy를 허용하는 exact receipt로 변환한다.
/// 인자: exit_record -> monitor가 publish한 exit payload
/// 반환값: clean receipt 또는 unexpected/non-zero failure
/// 작성 날짜: 2026/08/24
fn receipt_from_exit_record(
    exit_record: &BackendSidecarExitPayload,
) -> Result<BackendSidecarExitReceipt, SidecarFailure> {
    if !exit_record.expected {
        return Err(SidecarFailure::unexpected_exit());
    }
    if exit_record.code != Some(0) {
        return Err(SidecarFailure::nonzero_exit());
    }

    Ok(BackendSidecarExitReceipt {
        exited: true,
        code: exit_record.code,
    })
}

/// 함수 이름: recover_child_lock()
/// 기능: monitor-only Child mutex poison에서도 handle을 잃지 않고 process 관찰을 계속한다.
/// 인자: child_handle -> shared child handle
/// 반환값: usable Child guard
/// 작성 날짜: 2026/08/24
fn recover_child_lock(child_handle: &Arc<Mutex<Child>>) -> MutexGuard<'_, Child> {
    match child_handle.lock() {
        Ok(child) => child,
        Err(poisoned) => poisoned.into_inner(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    const TEST_SESSION_ID: &str = "3c73d583-c1c8-4830-8393-cc31639a40fd";

    /// 함수 이름: generated_token_has_required_base64url_shape()
    /// 기능: CSPRNG token이 32-byte base64url no-padding wire shape를 만족하는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn generated_token_has_required_base64url_shape() {
        let mut token = generate_session_token().expect("CSPRNG token generation must succeed");

        assert_eq!(token.len(), 43);
        assert!(token
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-'));
        token.zeroize();
    }

    /// 함수 이름: ready_descriptor_rejects_unknown_fields_and_schema_drift()
    /// 기능: ready FD가 secret field나 다른 schema를 받아들이지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn ready_descriptor_rejects_unknown_fields_and_schema_drift() {
        let unknown_field = format!(
            "{{\"port\":41000,\"session_id\":\"{TEST_SESSION_ID}\",\"schema_version\":2,\"token\":\"forbidden\"}}"
        );
        let wrong_schema =
            format!("{{\"port\":41000,\"session_id\":\"{TEST_SESSION_ID}\",\"schema_version\":3}}");

        assert!(parse_ready_descriptor(unknown_field.as_bytes()).is_err());
        assert!(parse_ready_descriptor(wrong_schema.as_bytes()).is_err());
    }

    /// 함수 이름: bootstrap_configuration_is_exact_read_only_contract()
    /// 기능: FD6 JSON이 agreed exact keys와 false/null production gate만 가지는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn bootstrap_configuration_is_exact_read_only_contract() {
        let configuration = SidecarBootstrapConfiguration {
            schema_version: BACKEND_SCHEMA_VERSION,
            allowed_origin: PRODUCTION_UI_ORIGIN,
            history_path: "/private/tmp/binance-auto-test/history.jsonl",
            api_key: "fixture-api-key",
            api_secret: "fixture-api-secret",
            allow_testnet_orders: false,
            max_notional: None,
        };
        let mut payload = serialize_bootstrap_configuration(&configuration)
            .expect("strict fixture configuration must serialize");
        let decoded: Value = serde_json::from_slice(&payload)
            .expect("serialized bootstrap configuration must be valid JSON");
        let object = decoded
            .as_object()
            .expect("bootstrap configuration must be an object");

        assert_eq!(object.len(), 7);
        assert_eq!(decoded["schema_version"], BACKEND_SCHEMA_VERSION);
        assert_eq!(decoded["allowed_origin"], PRODUCTION_UI_ORIGIN);
        assert_eq!(decoded["allow_testnet_orders"], false);
        assert!(decoded["max_notional"].is_null());
        assert!(object.get("runtime_mode").is_none());
        payload.zeroize();
    }

    /// 함수 이름: production_csp_replaces_only_exact_random_loopback_sentinels()
    /// 기능: wildcard 없이 HTTP/WS sentinel이 assigned port로 한 번씩 바뀌는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn production_csp_replaces_only_exact_random_loopback_sentinels() {
        let source = "default-src 'self'; connect-src 'self' http://127.0.0.1:0 ws://127.0.0.1:0";
        let replaced = replace_production_csp(source, 41_321)
            .expect("valid sentinels and port must produce a CSP");

        assert!(replaced.contains("http://127.0.0.1:41321"));
        assert!(replaced.contains("ws://127.0.0.1:41321"));
        assert!(!replaced.contains("127.0.0.1:*"));
        assert!(!replaced.contains(PRODUCTION_HTTP_CSP_SENTINEL));
        assert!(replace_production_csp(source, 0).is_none());
    }

    /// 함수 이름: exit_receipt_requires_expected_clean_code()
    /// 기능: unexpected, signal과 non-zero exit가 window final receipt로 승격되지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn exit_receipt_requires_expected_clean_code() {
        let clean = BackendSidecarExitPayload {
            expected: true,
            code: Some(0),
        };
        let unexpected = BackendSidecarExitPayload {
            expected: false,
            code: Some(0),
        };
        let signaled = BackendSidecarExitPayload {
            expected: true,
            code: None,
        };

        assert_eq!(
            receipt_from_exit_record(&clean).expect("expected code zero must be clean"),
            BackendSidecarExitReceipt {
                exited: true,
                code: Some(0),
            }
        );
        assert_eq!(
            receipt_from_exit_record(&unexpected)
                .expect_err("unexpected exit must fail")
                .code,
            "BACKEND_SIDECAR_EXITED_UNEXPECTEDLY"
        );
        assert_eq!(
            receipt_from_exit_record(&signaled)
                .expect_err("signal exit must fail")
                .code,
            "BACKEND_SIDECAR_EXIT_FAILED"
        );
    }

    /// 함수 이름: expected_exit_requires_native_fd5_release_intent()
    /// 기능: spontaneous code 0을 unexpected로 유지하고 native intent 뒤 status만 expected payload로 분류한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn expected_exit_requires_native_fd5_release_intent() {
        assert!(!classify_expected_exit(false, Some(0)));
        assert!(classify_expected_exit(true, Some(0)));
        assert!(classify_expected_exit(true, None));
        assert!(!classify_expected_exit(false, Some(1)));
        assert!(!classify_expected_exit(false, None));
    }

    /// 함수 이름: exit_timeout_has_default_and_hard_max_without_kill_policy()
    /// 기능: 인자 없는 UI 호출과 hard max validation을 deterministic하게 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn exit_timeout_has_default_and_hard_max_without_kill_policy() {
        assert_eq!(
            normalize_exit_timeout(None).expect("default timeout must be valid"),
            Duration::from_millis(DEFAULT_EXIT_TIMEOUT_MS)
        );
        assert!(normalize_exit_timeout(Some(0)).is_err());
        assert!(normalize_exit_timeout(Some(MAXIMUM_EXIT_TIMEOUT_MS + 1)).is_err());
    }

    /// 함수 이름: ready_wait_ambiguity_never_allows_forced_child_abort()
    /// 기능: READY timeout/invalid/EOF가 exposure 없음으로 오판되어 timed kill로 연결되지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn ready_wait_ambiguity_never_allows_forced_child_abort() {
        assert_eq!(
            select_startup_child_recovery_policy(true),
            StartupChildRecoveryPolicy::RetainAmbiguousExposure
        );
        assert_eq!(
            select_startup_child_recovery_policy(false),
            StartupChildRecoveryPolicy::AbortProvenPreRuntime
        );
    }

    /// 함수 이름: ready_timeout_preserves_fd_and_recovers_late_descriptor()
    /// 기능: initial timeout이 FD4/token/framing을 보존해 late READY write와 descriptor 복구를 허용하는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn ready_timeout_preserves_fd_and_recovers_late_descriptor() {
        let (reader_descriptor, writer_descriptor) =
            create_anonymous_pipe().expect("anonymous READY pipe must be created");
        let mut reader = File::from(reader_descriptor);
        let mut writer = File::from(writer_descriptor);
        let mut framing = Zeroizing::new(Vec::with_capacity(256));

        assert!(matches!(
            read_ready_descriptor(&mut reader, &mut framing, Duration::from_millis(1)),
            Err(ReadyDescriptorReadFailure::Timeout)
        ));
        let late_payload = format!(
            "{{\"port\":41000,\"session_id\":\"{TEST_SESSION_ID}\",\"schema_version\":2}}\n"
        );
        writer
            .write_all(late_payload.as_bytes())
            .expect("preserved FD4 reader must prevent late READY BrokenPipe");
        let ready = read_ready_descriptor(&mut reader, &mut framing, Duration::from_millis(100))
            .expect("late READY must reuse the preserved parser state");
        let mut token = Zeroizing::new("A".repeat(43));
        let descriptor = build_connection_descriptor(ready, &mut token);

        assert_eq!(descriptor.port, 41_000);
        assert_eq!(descriptor.session_id, TEST_SESSION_ID);
        assert_eq!(descriptor.schema_version, BACKEND_SCHEMA_VERSION);
        assert_eq!(descriptor.token.len(), 43);
        assert!(token.is_empty());
    }

    /// 함수 이름: ready_timeout_preserves_partial_framing()
    /// 기능: timeout 전 수신한 split JSON bytes가 late continuation과 합쳐져 exact READY로 복구되는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn ready_timeout_preserves_partial_framing() {
        let (reader_descriptor, writer_descriptor) =
            create_anonymous_pipe().expect("anonymous READY pipe must be created");
        let mut reader = File::from(reader_descriptor);
        let mut writer = File::from(writer_descriptor);
        let mut framing = Zeroizing::new(Vec::with_capacity(256));
        writer
            .write_all(b"{\"port\":41000,")
            .expect("partial READY prefix must be written");

        assert!(matches!(
            read_ready_descriptor(&mut reader, &mut framing, Duration::from_millis(1)),
            Err(ReadyDescriptorReadFailure::Timeout)
        ));
        assert!(!framing.is_empty());
        let suffix = format!("\"session_id\":\"{TEST_SESSION_ID}\",\"schema_version\":2}}\n");
        writer
            .write_all(suffix.as_bytes())
            .expect("late READY suffix must be written");
        let ready = read_ready_descriptor(&mut reader, &mut framing, Duration::from_millis(100))
            .expect("preserved partial framing must parse after continuation");

        assert_eq!(ready.port, 41_000);
        assert_eq!(ready.session_id, TEST_SESSION_ID);
    }

    /// 함수 이름: ready_eof_is_terminal_and_not_retryable()
    /// 기능: writer EOF를 timeout과 구분해 token을 late recovery에 무기한 보유하지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn ready_eof_is_terminal_and_not_retryable() {
        let (reader_descriptor, writer_descriptor) =
            create_anonymous_pipe().expect("anonymous READY pipe must be created");
        let mut reader = File::from(reader_descriptor);
        let mut framing = Zeroizing::new(Vec::with_capacity(256));
        drop(writer_descriptor);

        let failure = read_ready_descriptor(&mut reader, &mut framing, Duration::from_millis(100));

        assert!(matches!(
            failure,
            Err(ReadyDescriptorReadFailure::Terminal(SidecarFailure {
                code: "BACKEND_SIDECAR_STARTUP_FAILED",
                ..
            }))
        ));
    }

    /// 함수 이름: exit_event_payload_has_exact_secret_free_shape()
    /// 기능: abnormal exit event가 expected/code 두 field 외 값을 공개하지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn exit_event_payload_has_exact_secret_free_shape() {
        let payload = BackendSidecarExitPayload {
            expected: false,
            code: Some(17),
        };
        let serialized =
            serde_json::to_value(payload).expect("secret-free exit payload must serialize");
        let object = serialized
            .as_object()
            .expect("exit payload must be an object");

        assert_eq!(object.len(), 2);
        assert_eq!(serialized["expected"], false);
        assert_eq!(serialized["code"], 17);
    }

    /// 함수 이름: sidecar_exit_before_listener_is_flushed_once_on_arm()
    /// 기능: JS listener 전 exit record가 유실되지 않고 arm에서 한 번만 publication되는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn sidecar_exit_before_listener_is_flushed_once_on_arm() {
        let state = SidecarProcessState::default();
        let (recorded, should_emit) = state.record_exit(Some(17));

        assert!(!should_emit);
        assert_eq!(recorded.expected, false);
        assert_eq!(
            state
                .arm_exit_event_delivery()
                .expect("sidecar exit event bridge must arm"),
            Some(recorded)
        );
        assert_eq!(
            state
                .arm_exit_event_delivery()
                .expect("repeated arm must be idempotent"),
            None
        );
    }

    /// 함수 이름: armed_listener_receives_future_sidecar_exit_immediately()
    /// 기능: listener arm 후 exit는 pending query 없이 monitor emit 경로로 전달되는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn armed_listener_receives_future_sidecar_exit_immediately() {
        let state = SidecarProcessState::default();

        assert_eq!(
            state
                .arm_exit_event_delivery()
                .expect("sidecar exit event bridge must arm"),
            None
        );
        let (recorded, should_emit) = state.record_exit(None);

        assert!(should_emit);
        assert_eq!(recorded.code, None);
        assert!(!recorded.expected);
    }

    /// 함수 이름: private_app_data_directory_enforces_owner_only_mode()
    /// 기능: 기존 directory에 group/other permission이 있어도 startup 준비가 0700으로 축소하는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn private_app_data_directory_enforces_owner_only_mode() {
        let mut unique_component =
            generate_session_token().expect("temporary directory token generation must succeed");
        let test_directory =
            std::env::temp_dir().join(format!("binance-auto-sidecar-app-data-{unique_component}"));
        unique_component.zeroize();
        fs::create_dir(&test_directory).expect("temporary app-data directory must be created");
        fs::set_permissions(&test_directory, fs::Permissions::from_mode(0o755))
            .expect("fixture permissions must be applied");

        ensure_private_app_data_directory(&test_directory)
            .expect("existing app-data directory must be hardened");
        let mode = fs::metadata(&test_directory)
            .expect("hardened directory metadata must exist")
            .permissions()
            .mode()
            & 0o777;

        assert_eq!(mode, 0o700);
        fs::remove_dir(&test_directory).expect("temporary app-data directory must be removed");
    }
}
