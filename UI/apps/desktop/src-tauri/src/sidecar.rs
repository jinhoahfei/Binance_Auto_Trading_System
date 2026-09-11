//! Python backend sidecar의 native-only 보안 handshake와 process lifecycle을 소유한다.

#[cfg(target_os = "macos")]
mod macos;
#[cfg(target_os = "macos")]
mod macos_profile;
#[cfg(target_os = "macos")]
use macos::*;
#[cfg(target_os = "windows")]
mod windows;
#[cfg(target_os = "windows")]
use windows::*;
#[cfg(any(target_os = "windows", test))]
mod framed;

use crate::{BackendConnectionDescriptor, BACKEND_SCHEMA_VERSION};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use serde::{Deserialize, Serialize};
use std::error::Error;
use std::fmt::{Display, Formatter};
#[cfg(target_os = "macos")]
use std::fs::File;
#[cfg(target_os = "macos")]
use std::io;
use std::io::{Read, Seek, SeekFrom, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager, State, WebviewWindowBuilder};
use zeroize::{Zeroize, Zeroizing};

// OS pipe owner의 native read/write 구현을 유지해 Windows overlapped handle을 일반 File로 바꾸지 않는다.
#[cfg(target_os = "macos")]
type ControlWriter = File;
#[cfg(target_os = "macos")]
type ReadyReader = File;
#[cfg(target_os = "windows")]
type ControlWriter = std::process::ChildStdin;
#[cfg(target_os = "windows")]
type ReadyReader = std::process::ChildStdout;

pub const BACKEND_SIDECAR_EXIT_EVENT: &str = "backend-sidecar-exited";

const DEVELOPMENT_UI_ORIGIN: &str = "http://127.0.0.1:5173";
const PRODUCTION_UI_ORIGIN: &str = "tauri://localhost";
const HISTORY_FILE_NAME: &str = "history.jsonl";
const PRODUCTION_HTTP_CSP_SENTINEL: &str = "http://127.0.0.1:0";
const PRODUCTION_WS_CSP_SENTINEL: &str = "ws://127.0.0.1:0";
const MAXIMUM_SECRET_BYTES: usize = 512;
const MAXIMUM_CONFIG_BYTES: usize = 8 * 1024;
const MAXIMUM_READY_BYTES: usize = 4 * 1024;
const RUNTIME_OWNERSHIP_FILE_NAME: &str = ".backend-runtime.lock";
const MAXIMUM_RUNTIME_OWNERSHIP_BYTES: u64 = 1024;
const SIDECAR_READY_TIMEOUT: Duration = Duration::from_secs(20);
const LATE_READY_ATTEMPT_TIMEOUT: Duration = Duration::from_secs(1);
const SIDECAR_MONITOR_INTERVAL: Duration = Duration::from_millis(50);
const PROVEN_PRE_RUNTIME_ABORT_GRACE: Duration = Duration::from_millis(500);
const DEFAULT_EXIT_TIMEOUT_MS: u64 = 30_000;
const MAXIMUM_EXIT_TIMEOUT_MS: u64 = 120_000;

/// 함수 이름: is_trusted_renderer_url()
/// 기능: 현재 실행 환경의 메인 화면 origin만 navigation과 descriptor 요청에 허용한다.
/// 인자: url -> 요청·이동 대상 URL, is_development -> 개발 서버 사용 여부
/// 반환값: 정확한 origin과 인증 정보 없는 URL이면 true
/// 작성 날짜: 2026/09/05
pub(crate) fn is_trusted_renderer_url(url: &tauri::Url, is_development: bool) -> bool {
    if !url.username().is_empty() || url.password().is_some() {
        return false;
    }
    // HTTP와 custom protocol의 기본 port 해석을 섞지 않고 각 환경을 따로 검증한다.
    if is_development {
        url.scheme() == "http" && url.host_str() == Some("127.0.0.1") && url.port() == Some(5173)
    } else {
        cfg!(target_os = "macos")
            && url.scheme() == "tauri"
            && url.host_str() == Some("localhost")
            && url.port().is_none()
    }
}

/// 함수 이름: development_renderer_is_available()
/// 기능: 실제 개발 화면의 HTML 응답을 확인해 서버 없는 흰 창 생성을 방지한다.
/// 인자: 없음
/// 반환값: 고정된 개발 서버가 HTML을 제공하면 true
/// 작성 날짜: 2026/09/05
pub(crate) fn development_renderer_is_available() -> bool {
    probe_renderer_server(SocketAddr::from(([127, 0, 0, 1], 5173)))
}

/// 함수 이름: probe_renderer_server()
/// 기능: credential 없는 bounded HEAD 요청으로 화면 서버의 HTTP 상태와 HTML 유형을 확인한다.
/// 인자: address -> localhost 화면 서버 주소
/// 반환값: 정상 HTML 응답이면 true, 연결·응답 오류이면 false
/// 작성 날짜: 2026/09/05
fn probe_renderer_server(address: SocketAddr) -> bool {
    // 첫 연결과 응답 모두 timeout을 두고 최대 4 KiB만 읽어 시작 작업이 무한 대기하지 않게 한다.
    let timeout = Duration::from_millis(750);
    let Ok(mut stream) = TcpStream::connect_timeout(&address, timeout) else {
        return false;
    };
    if stream.set_read_timeout(Some(timeout)).is_err()
        || stream.set_write_timeout(Some(timeout)).is_err()
    {
        return false;
    }
    let request = format!("HEAD / HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = String::new();
    if stream.take(4096).read_to_string(&mut response).is_err() {
        return false;
    }
    let mut lines = response.lines();
    let has_success_status = matches!(
        lines.next().and_then(|line| line.split_whitespace().nth(1)),
        Some("200")
    );
    has_success_status
        && lines.any(|line| {
            let normalized = line.to_ascii_lowercase(); // HTTP header 이름과 media type은 대소문자를 구분하지 않는다.
            normalized.split_once(':').is_some_and(|(name, value)| {
                name == "content-type" && value.trim().starts_with("text/html")
            })
        })
}

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

    /// 함수 이름: ownership_reconciliation_required()
    /// 기능: live·locked·invalid ownership artifact를 자동 해제하지 않는 고정 실패를 만든다.
    /// 인자: 없음
    /// 반환값: BACKEND_OWNERSHIP_RECONCILIATION_REQUIRED failure
    /// 작성 날짜: 2026/08/29
    fn ownership_reconciliation_required() -> Self {
        Self {
            code: "BACKEND_OWNERSHIP_RECONCILIATION_REQUIRED",
            message: "Backend process ownership requires operator reconciliation.",
        }
    }

    /// 함수 이름: ownership_release_failed()
    /// 기능: operator 확인 뒤 identity 재검증 또는 RELEASED fsync 실패를 고정 코드로 만든다.
    /// 인자: 없음
    /// 반환값: BACKEND_OWNERSHIP_RELEASE_FAILED failure
    /// 작성 날짜: 2026/08/29
    fn ownership_release_failed() -> Self {
        Self {
            code: "BACKEND_OWNERSHIP_RELEASE_FAILED",
            message: "Stale backend process ownership could not be released safely.",
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

/// Python interpreter lifetime을 launcher child handle과 별도로 식별하는 secret-free identity이다.
#[derive(Clone, Debug, PartialEq, Eq)]
struct PythonRuntimeIdentity {
    runtime_pid: u32,
    process_start_id: String,
}

/// Python ready FD에서 허용하는 secret 없는 exact descriptor wire shape이다.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ReadyDescriptorWire {
    port: u16,
    session_id: String,
    runtime_pid: u32,
    process_start_id: String,
    schema_version: u32,
}

impl ReadyDescriptorWire {
    /// 함수 이름: runtime_identity()
    /// 기능: strict FD4 wire에서 managed lifecycle에 보존할 Python runtime identity를 복사한다.
    /// 인자: 없음
    /// 반환값: Python PID와 canonical process start UUID
    /// 작성 날짜: 2026/08/25
    fn runtime_identity(&self) -> PythonRuntimeIdentity {
        PythonRuntimeIdentity {
            runtime_pid: self.runtime_pid,
            process_start_id: self.process_start_id.clone(),
        }
    }
}

/// FD4 parser가 재시도 가능한 timeout과 framing을 잃은 terminal failure를 구분한다.
#[derive(Debug)]
enum ReadyDescriptorReadFailure {
    Timeout,
    Terminal(SidecarFailure),
}

/// 클래스 이름: NativeCredentials
/// 기능: OS credential store에서 읽은 두 secret을 Drop 시점에 zeroize하는 native-only container이다.
/// 작성 날짜: 2026/09/06
struct NativeCredentials {
    api_key: String,
    api_secret: String,
}

impl Drop for NativeCredentials {
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

/// 기존 macOS FD6과 Windows 최초 bootstrap frame에 직렬화하는 read-only configuration이다.
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

/// Launcher child handle과 optional Python runtime identity를 분리해 managed state로 옮기는 owner이다.
pub struct OwnedSidecarChild {
    child: Child,
    launcher_child_pid: u32,
    python_runtime_identity: Option<PythonRuntimeIdentity>,
}

impl OwnedSidecarChild {
    /// 함수 이름: new()
    /// 기능: OS가 발급한 launcher child PID를 handle과 묶고 검증된 Python identity를 별도 보존한다.
    /// 인자: child -> spawned launcher child handle,
    ///      python_runtime_identity -> FD4를 아직 받지 못했으면 None인 Python runtime identity
    /// 반환값: managed lifecycle로 한 번 이동할 child owner
    /// 작성 날짜: 2026/08/25
    fn new(child: Child, python_runtime_identity: Option<PythonRuntimeIdentity>) -> Self {
        let launcher_child_pid = child.id();
        Self {
            child,
            launcher_child_pid,
            python_runtime_identity,
        }
    }
}

/// renderer publication 전에 native state로 이전할 child와 descriptor 묶음이다.
pub struct PreparedBackendSidecar {
    pub descriptor: BackendConnectionDescriptor,
    pub child: OwnedSidecarChild,
    pub stop_writer: ControlWriter,
    pub port: u16,
}

/// Token/config publication 후 ready를 확인하지 못해 exposure 여부가 ambiguous한 child이다.
pub struct AmbiguousBackendSidecar {
    pub child: OwnedSidecarChild,
    pub stop_writer: ControlWriter,
    pub late_ready_recovery: Option<LateReadySidecarRecovery>,
}

/// Initial timeout 뒤 FD4 framing과 token을 zeroizing 상태로 보유하는 late READY recovery이다.
pub struct LateReadySidecarRecovery {
    ready_reader: ReadyReader,
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
    launcher_child_pid: Option<u32>,
    python_runtime_identity: Option<PythonRuntimeIdentity>,
    stop_writer: Option<ControlWriter>,
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
            launcher_child_pid: None,
            python_runtime_identity: None,
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
    /// 함수 이름: diagnostic_runtime_identity()
    /// 기능: native 차트 로그와 Python 실행 로그를 연결할 검증된 PID·시작 ID만 반환한다.
    /// 인자: 없음
    /// 반환값: 준비된 Python identity 또는 None
    /// 작성 날짜: 2026/09/11
    pub fn diagnostic_runtime_identity(&self) -> Option<(u32, String)> {
        self.shared.lifecycle.lock().ok().and_then(|lifecycle| {
            lifecycle.python_runtime_identity.as_ref().map(|identity| {
                (identity.runtime_pid, identity.process_start_id.clone())
            })
        })
    }

    /// 함수 이름: install()
    /// 기능: launcher/Python identity를 분리한 child와 stop pipe를 소유하고 exit monitor를 시작한다.
    /// 인자: child -> launcher handle과 optional Python identity를 소유한 child wrapper,
    ///      stop_writer -> parent FD5 writer,
    ///      app_handle -> secret-free exit event를 emit할 Tauri handle
    /// 반환값: lifecycle install 성공 또는 typed state failure
    /// 작성 날짜: 2026/08/24
    pub fn install(
        &self,
        child: OwnedSidecarChild,
        stop_writer: ControlWriter,
        app_handle: AppHandle,
    ) -> Result<(), SidecarFailure> {
        let OwnedSidecarChild {
            child,
            launcher_child_pid,
            python_runtime_identity,
        } = child;

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
        lifecycle.launcher_child_pid = Some(launcher_child_pid);
        lifecycle.python_runtime_identity = python_runtime_identity;
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

    /// 함수 이름: record_python_runtime_identity()
    /// 기능: late READY의 Python identity를 launcher PID와 덮어쓰지 않고 exact-once로 보존한다.
    /// 인자: identity -> strict FD4 parser가 검증한 Python runtime PID/start UUID
    /// 반환값: 최초 또는 동일 identity publication 성공, mismatch/state ambiguity면 typed failure
    /// 작성 날짜: 2026/08/25
    fn record_python_runtime_identity(
        &self,
        identity: PythonRuntimeIdentity,
    ) -> Result<(), SidecarFailure> {
        let mut lifecycle = self.lock_lifecycle()?;
        if lifecycle.launcher_child_pid.is_none() || lifecycle.exit_record.is_some() {
            return Err(SidecarFailure::unavailable());
        }

        // 동일 late result의 멱등 publication만 허용하고 다른 runtime identity는 ambiguous로 유지한다.
        match lifecycle.python_runtime_identity.as_ref() {
            Some(existing_identity) if existing_identity != &identity => {
                Err(SidecarFailure::descriptor_rejected())
            }
            Some(_) => Ok(()),
            None => {
                lifecycle.python_runtime_identity = Some(identity);
                Ok(())
            }
        }
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
            let _ = write_closed_ack(&mut writer);
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

/// Python runtime ownership artifact의 exact state를 native operator workflow에서 공유한다.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum RuntimeOwnershipState {
    Active,
    Orphaned,
    Released,
}

impl RuntimeOwnershipState {
    /// 함수 이름: as_str()
    /// 기능: artifact enum을 operator 표시와 exact 비교에 쓸 canonical 문자열로 반환한다.
    /// 인자: 없음
    /// 반환값: ACTIVE, ORPHANED 또는 RELEASED
    /// 작성 날짜: 2026/08/29
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Active => "ACTIVE",
            Self::Orphaned => "ORPHANED",
            Self::Released => "RELEASED",
        }
    }
}

/// Python writer의 non-secret exact ownership artifact wire를 검증한다.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(deny_unknown_fields)]
struct RuntimeOwnershipArtifactWire {
    schema_version: u32,
    runtime_pid: u32,
    process_start_id: String,
    owner_state: RuntimeOwnershipState,
}

/// 운영자가 화면에서 확인한 stale runtime identity와 상태 snapshot을 보존한다.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StaleRuntimeOwnershipAttestation {
    pub schema_version: u32,
    pub runtime_pid: u32,
    pub process_start_id: String,
    pub owner_state: RuntimeOwnershipState,
    device_id: u64,
    inode: u64,
}

impl StaleRuntimeOwnershipAttestation {
    /// 함수 이름: from_locked_artifact()
    /// 기능: lock 아래 검증한 artifact와 inode를 operator confirmation에 고정할 immutable snapshot으로 복사한다.
    /// 인자: artifact -> exact schema와 runtime identity를 가진 wire,
    ///      device_id -> artifact inode가 속한 device ID,
    ///      inode -> inspect 시점의 artifact inode
    /// 반환값: stale runtime ownership attestation
    /// 작성 날짜: 2026/08/29
    fn from_locked_artifact(
        artifact: &RuntimeOwnershipArtifactWire,
        device_id: u64,
        inode: u64,
    ) -> Self {
        Self {
            schema_version: artifact.schema_version,
            runtime_pid: artifact.runtime_pid,
            process_start_id: artifact.process_start_id.clone(),
            owner_state: artifact.owner_state,
            device_id,
            inode,
        }
    }
}

/// 함수 이름: inspect_stale_runtime_owner_in_directory()
/// 기능: lock이 빈 ACTIVE/ORPHANED artifact와 PID 부재를 결합해 operator attestation 후보를 만든다.
/// 인자: directory -> runtime ownership artifact를 가진 app-data directory,
///      process_exists -> PID 존재 판정을 제공하는 production 함수 또는 test seam
/// 반환값: stale attestation, artifact 없음/RELEASED이면 None, 불명하면 typed failure
/// 작성 날짜: 2026/08/29
fn inspect_stale_runtime_owner_in_directory<F>(
    directory: &Path,
    process_exists: F,
) -> Result<Option<StaleRuntimeOwnershipAttestation>, SidecarFailure>
where
    F: Fn(u32) -> bool,
{
    let Some((_ownership_file, artifact, device_id, inode)) =
        open_locked_runtime_ownership_artifact(directory)?
    else {
        return Ok(None);
    };
    if artifact.owner_state == RuntimeOwnershipState::Released {
        return Ok(None);
    }
    if process_exists(artifact.runtime_pid) {
        return Err(SidecarFailure::ownership_reconciliation_required());
    }

    Ok(Some(
        StaleRuntimeOwnershipAttestation::from_locked_artifact(&artifact, device_id, inode),
    ))
}

/// 함수 이름: inspect_stale_runtime_owner()
/// 기능: native startup 전 app-data에서 확정된 stale ACTIVE/ORPHANED owner만 operator surface에 공개한다.
/// 인자: app_handle -> canonical per-user app-data path owner
/// 반환값: release 확인이 필요한 attestation 또는 None
/// 작성 날짜: 2026/08/29
pub fn inspect_stale_runtime_owner(
    app_handle: &AppHandle,
) -> Result<Option<StaleRuntimeOwnershipAttestation>, SidecarFailure> {
    let app_data_directory = resolve_app_data_directory(app_handle)
        .map_err(|_| SidecarFailure::ownership_reconciliation_required())?;
    if !app_data_directory.exists() {
        return Ok(None);
    }
    ensure_private_app_data_directory(&app_data_directory)?;
    inspect_stale_runtime_owner_in_directory(&app_data_directory, runtime_process_exists)
}

/// 함수 이름: release_stale_runtime_owner_in_directory()
/// 기능: operator가 확인한 exact stale identity를 다시 lock·PID 검증한 뒤 같은 inode에 RELEASED로 fsync한다.
/// 인자: directory -> runtime ownership artifact를 가진 app-data directory,
///      attestation -> operator 화면에 표시한 exact identity/state snapshot,
///      process_exists -> PID 존재 판정을 제공하는 production 함수 또는 test seam
/// 반환값: RELEASED fsync가 완료되면 없음
/// 작성 날짜: 2026/08/29
fn release_stale_runtime_owner_in_directory<F>(
    directory: &Path,
    attestation: &StaleRuntimeOwnershipAttestation,
    process_exists: F,
) -> Result<(), SidecarFailure>
where
    F: Fn(u32) -> bool,
{
    let Some((mut ownership_file, mut artifact, device_id, inode)) =
        open_locked_runtime_ownership_artifact(directory)
            .map_err(|_| SidecarFailure::ownership_release_failed())?
    else {
        return Err(SidecarFailure::ownership_release_failed());
    };

    // Dialog 이후 artifact가 바뀌었거나 PID가 다시 보이면 이전 확인을 재사용하지 않는다.
    if device_id != attestation.device_id
        || inode != attestation.inode
        || artifact.schema_version != attestation.schema_version
        || artifact.runtime_pid != attestation.runtime_pid
        || artifact.process_start_id != attestation.process_start_id
        || (artifact.owner_state != attestation.owner_state
            && artifact.owner_state != RuntimeOwnershipState::Released)
    {
        return Err(SidecarFailure::ownership_release_failed());
    }
    if artifact.owner_state == RuntimeOwnershipState::Released {
        return Ok(());
    }
    if process_exists(artifact.runtime_pid) {
        return Err(SidecarFailure::ownership_release_failed());
    }

    // Identity를 삭제하지 않고 state만 RELEASED로 바꿔 다음 launch의 audit 경계를 남긴다.
    artifact.owner_state = RuntimeOwnershipState::Released;
    let mut released_payload =
        serde_json::to_vec(&artifact).map_err(|_| SidecarFailure::ownership_release_failed())?;
    released_payload.push(b'\n');
    ownership_file
        .set_len(0)
        .and_then(|_| ownership_file.seek(SeekFrom::Start(0)))
        .and_then(|_| ownership_file.write_all(&released_payload))
        .and_then(|_| ownership_file.sync_all())
        .map_err(|_| SidecarFailure::ownership_release_failed())?;

    Ok(())
}

/// 함수 이름: release_stale_runtime_owner()
/// 기능: native operator confirmation에 고정된 stale identity를 RELEASED로 전이한다.
/// 인자: app_handle -> canonical per-user app-data path owner,
///      attestation -> inspect 단계가 반환한 exact identity/state snapshot
/// 반환값: durable RELEASED transition 성공 시 없음
/// 작성 날짜: 2026/08/29
pub fn release_stale_runtime_owner(
    app_handle: &AppHandle,
    attestation: &StaleRuntimeOwnershipAttestation,
) -> Result<(), SidecarFailure> {
    let app_data_directory = resolve_app_data_directory(app_handle)
        .map_err(|_| SidecarFailure::ownership_release_failed())?;
    ensure_private_app_data_directory(&app_data_directory)
        .map_err(|_| SidecarFailure::ownership_release_failed())?;
    release_stale_runtime_owner_in_directory(
        &app_data_directory,
        attestation,
        runtime_process_exists,
    )
}

/// 함수 이름: build_connection_descriptor()
/// 기능: strict READY와 zeroizing token을 복사 없이 native session descriptor로 이전한다.
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
/// 기능: late READY와 preserved token을 main thread에서 session stage한 뒤에만 renderer window를 만든다.
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
    // Renderer staging 전에 Python identity를 managed child lifetime에 결합해 late READY race를 닫는다.
    let python_runtime_identity = ready.runtime_identity();
    if process_state
        .record_python_runtime_identity(python_runtime_identity)
        .is_err()
    {
        schedule_late_ready_terminal_recovery(app_handle, process_state);
        return;
    }

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
fn abort_proven_pre_runtime_child(mut child: Child, mut stop_writer: ControlWriter) {
    debug_assert_eq!(
        select_startup_child_recovery_policy(false),
        StartupChildRecoveryPolicy::AbortProvenPreRuntime
    );
    let _ = write_closed_ack(&mut stop_writer);
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
        .background_color(tauri::utils::config::Color(11, 14, 17, 255))
        .on_navigation(|url| is_trusted_renderer_url(url, tauri::is_dev()))
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
                // 새로고침 복구용 native token도 backend 수명이 끝나는 즉시 폐기한다.
                app_handle
                    .state::<crate::BackendConnectionDescriptorState>()
                    .clear();
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

/// 함수 이름: parse_ready_descriptor()
/// 기능: unknown field와 invalid port/session/runtime identity/schema를 raw value 반사 없이 거부한다.
/// 인자: payload -> newline을 제외한 ready JSON bytes
/// 반환값: strict ready wire 또는 descriptor failure
/// 작성 날짜: 2026/08/24
fn parse_ready_descriptor(payload: &[u8]) -> Result<ReadyDescriptorWire, SidecarFailure> {
    let ready: ReadyDescriptorWire =
        serde_json::from_slice(payload).map_err(|_| SidecarFailure::descriptor_rejected())?;
    if ready.port == 0
        || ready.runtime_pid == 0
        || ready.schema_version != BACKEND_SCHEMA_VERSION
        || !crate::is_canonical_uuid(&ready.session_id)
        || !crate::is_canonical_uuid(&ready.process_start_id)
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
    #[cfg(target_os = "macos")]
    use std::{
        fs::{self, OpenOptions},
        os::{
            fd::AsRawFd,
            unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
        },
    };

    /// 함수 이름: descriptor_access_and_navigation_require_the_exact_renderer_origin()
    /// 기능: 새로고침은 허용하지만 외부 URL과 다른 실행 환경에는 session 정보를 제공하지 않는다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/05
    #[test]
    fn descriptor_access_and_navigation_require_the_exact_renderer_origin() {
        for (url, development, allowed) in [
            ("http://127.0.0.1:5173/", true, true),
            ("http://127.0.0.1:5173/index.html", true, true),
            (
                "tauri://localhost/index.html",
                false,
                cfg!(target_os = "macos"),
            ),
            ("https://example.com/", true, false),
            ("http://localhost:5173/", true, false),
            ("http://127.0.0.1:5174/", true, false),
            ("http://user@127.0.0.1:5173/", true, false),
            ("http://127.0.0.1:5173/", false, false),
            ("tauri://localhost/", true, false),
            ("tauri://external/", false, false),
        ] {
            let parsed = tauri::Url::parse(url).expect("valid test URL");
            assert_eq!(
                is_trusted_renderer_url(&parsed, development),
                allowed,
                "{url}"
            );
        }
    }

    /// 함수 이름: renderer_startup_waits_for_an_actual_html_server()
    /// 기능: 개발 서버 부재·오류·다른 서비스 응답에서는 창 생성을 거부하고 HTML 준비 후 허용한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/05
    #[test]
    fn renderer_startup_waits_for_an_actual_html_server() {
        use std::net::TcpListener;
        // 실제 localhost 응답을 사용해 포트만 열려 있는 상태를 HTML 준비로 오인하지 않는지 검증한다.
        for (response, expected) in [
            (
                "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n",
                true,
            ),
            (
                "HTTP/1.1 503 Unavailable\r\nContent-Type: text/html\r\n\r\n",
                false,
            ),
            (
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n",
                false,
            ),
        ] {
            let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
            let address = listener.local_addr().expect("test address");
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().expect("accept probe");
                let mut request = [0; 1024];
                let count = stream.read(&mut request).expect("read HEAD request");
                assert!(request[..count].starts_with(b"HEAD / HTTP/1.1\r\n"));
                stream
                    .write_all(response.as_bytes())
                    .expect("write test response");
            });
            assert_eq!(probe_renderer_server(address), expected);
            server.join().expect("test server finished");
        }
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind absent server address");
        let address = listener.local_addr().expect("test address");
        drop(listener); // 서버가 닫힌 상태는 흰 창을 열기 전에 감지해야 한다.
        assert!(!probe_renderer_server(address));
    }
    use serde_json::Value;

    const TEST_SESSION_ID: &str = "3c73d583-c1c8-4830-8393-cc31639a40fd";
    const TEST_PROCESS_START_ID: &str = "9cb54fc1-69eb-4d3a-ac28-4ef0efcc01f4";

    /// 함수 이름: create_runtime_ownership_test_directory()
    /// 기능: ownership artifact test에서만 사용할 owner-only 임시 directory를 만든다.
    /// 인자: 없음
    /// 반환값: 유일한 임시 directory path
    /// 작성 날짜: 2026/08/29
    #[cfg(target_os = "macos")]
    fn create_runtime_ownership_test_directory() -> PathBuf {
        let mut unique_component =
            generate_session_token().expect("temporary directory token generation must succeed");
        let test_directory =
            std::env::temp_dir().join(format!("binance-auto-runtime-ownership-{unique_component}"));
        unique_component.zeroize();
        fs::create_dir(&test_directory).expect("ownership test directory must be created");
        fs::set_permissions(&test_directory, fs::Permissions::from_mode(0o700))
            .expect("ownership test directory must be private");
        test_directory
    }

    /// 함수 이름: write_runtime_ownership_test_artifact()
    /// 기능: Python writer와 같은 exact JSON·0600 artifact를 test directory에 기록한다.
    /// 인자: directory -> fixture directory,
    ///      runtime_pid -> fixture Python PID,
    ///      process_start_id -> fixture launch UUID,
    ///      owner_state -> fixture ownership state
    /// 반환값: 생성한 artifact path
    /// 작성 날짜: 2026/08/29
    #[cfg(target_os = "macos")]
    fn write_runtime_ownership_test_artifact(
        directory: &Path,
        runtime_pid: u32,
        process_start_id: &str,
        owner_state: RuntimeOwnershipState,
    ) -> PathBuf {
        let artifact_path = directory.join(RUNTIME_OWNERSHIP_FILE_NAME);
        let artifact = RuntimeOwnershipArtifactWire {
            schema_version: BACKEND_SCHEMA_VERSION,
            runtime_pid,
            process_start_id: process_start_id.to_owned(),
            owner_state,
        };
        let mut artifact_bytes =
            serde_json::to_vec(&artifact).expect("ownership fixture must serialize");
        artifact_bytes.push(b'\n');

        // Fixture도 production의 existing single-link regular file·owner-only mode를 그대로 따른다.
        let mut artifact_file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&artifact_path)
            .expect("ownership fixture file must be created");
        artifact_file
            .write_all(&artifact_bytes)
            .expect("ownership fixture must be written");
        artifact_file
            .sync_all()
            .expect("ownership fixture must be durable");
        artifact_path
    }

    /// 함수 이름: stale_active_and_orphaned_owner_release_preserves_identity_and_inode()
    /// 기능: PID가 사라진 ACTIVE/ORPHANED artifact가 exact attestation 뒤 같은 inode의 RELEASED로 전이되는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/29
    #[test]
    #[cfg(target_os = "macos")]
    fn stale_active_and_orphaned_owner_release_preserves_identity_and_inode() {
        for owner_state in [
            RuntimeOwnershipState::Active,
            RuntimeOwnershipState::Orphaned,
        ] {
            let test_directory = create_runtime_ownership_test_directory();
            let artifact_path = write_runtime_ownership_test_artifact(
                &test_directory,
                4_321,
                TEST_PROCESS_START_ID,
                owner_state,
            );
            let original_inode = fs::metadata(&artifact_path)
                .expect("ownership fixture metadata must exist")
                .ino();

            // 두 PID probe 모두 확정 부재일 때만 화면 snapshot과 durable release를 허용한다.
            let attestation = inspect_stale_runtime_owner_in_directory(&test_directory, |_| false)
                .expect("stale ownership inspection must succeed")
                .expect("ACTIVE or ORPHANED artifact must require attestation");
            assert_eq!(attestation.schema_version, BACKEND_SCHEMA_VERSION);
            assert_eq!(attestation.runtime_pid, 4_321);
            assert_eq!(attestation.process_start_id, TEST_PROCESS_START_ID);
            assert_eq!(attestation.owner_state, owner_state);
            assert!(release_stale_runtime_owner_in_directory(
                &test_directory,
                &attestation,
                |_| false,
            )
            .is_ok());

            let released_bytes =
                fs::read(&artifact_path).expect("released ownership artifact must remain readable");
            let released: RuntimeOwnershipArtifactWire = serde_json::from_slice(&released_bytes)
                .expect("released ownership artifact must remain exact JSON");
            assert_eq!(released.schema_version, BACKEND_SCHEMA_VERSION);
            assert_eq!(released.runtime_pid, 4_321);
            assert_eq!(released.process_start_id, TEST_PROCESS_START_ID);
            assert_eq!(released.owner_state, RuntimeOwnershipState::Released);
            assert_eq!(
                fs::metadata(&artifact_path)
                    .expect("released artifact metadata must exist")
                    .ino(),
                original_inode
            );
            assert!(
                inspect_stale_runtime_owner_in_directory(&test_directory, |_| true)
                    .expect("RELEASED artifact inspection must succeed")
                    .is_none()
            );

            fs::remove_file(&artifact_path).expect("ownership fixture must be removed");
            fs::remove_dir(&test_directory).expect("ownership fixture directory must be removed");
        }
    }

    /// 함수 이름: live_or_locked_runtime_owner_never_reaches_release_attestation()
    /// 기능: recorded PID가 존재하거나 advisory lock이 잡힌 artifact를 stale로 오인하지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/29
    #[test]
    #[cfg(target_os = "macos")]
    fn live_or_locked_runtime_owner_never_reaches_release_attestation() {
        let test_directory = create_runtime_ownership_test_directory();
        let artifact_path = write_runtime_ownership_test_artifact(
            &test_directory,
            4_321,
            TEST_PROCESS_START_ID,
            RuntimeOwnershipState::Active,
        );
        let original_bytes = fs::read(&artifact_path).expect("ownership fixture must be readable");

        // PID가 보이는 경우에는 artifact identity가 valid해도 public 확인 화면을 만들지 않는다.
        let live_failure = inspect_stale_runtime_owner_in_directory(&test_directory, |_| true)
            .expect_err("live runtime owner must fail closed");
        assert_eq!(
            live_failure.code,
            "BACKEND_OWNERSHIP_RECONCILIATION_REQUIRED"
        );
        assert_eq!(
            fs::read(&artifact_path).expect("live artifact must remain readable"),
            original_bytes
        );

        let locked_file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&artifact_path)
            .expect("ownership fixture must open for lock test");
        let lock_result =
            unsafe { libc::flock(locked_file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
        assert_eq!(lock_result, 0);
        let locked_failure = inspect_stale_runtime_owner_in_directory(&test_directory, |_| false)
            .expect_err("locked runtime owner must fail closed");
        assert_eq!(
            locked_failure.code,
            "BACKEND_OWNERSHIP_RECONCILIATION_REQUIRED"
        );
        drop(locked_file);

        fs::remove_file(&artifact_path).expect("ownership fixture must be removed");
        fs::remove_dir(&test_directory).expect("ownership fixture directory must be removed");
    }

    /// 함수 이름: unsafe_or_nonexact_runtime_owner_artifact_is_never_attested()
    /// 기능: owner-only mode 위반과 unknown JSON field가 operator release 후보로 승격되지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/29
    #[test]
    #[cfg(target_os = "macos")]
    fn unsafe_or_nonexact_runtime_owner_artifact_is_never_attested() {
        let test_directory = create_runtime_ownership_test_directory();
        let artifact_path = write_runtime_ownership_test_artifact(
            &test_directory,
            4_321,
            TEST_PROCESS_START_ID,
            RuntimeOwnershipState::Active,
        );

        // Group-readable metadata 하나만으로도 내용을 해석하거나 operator에게 표시하지 않는다.
        fs::set_permissions(&artifact_path, fs::Permissions::from_mode(0o640))
            .expect("ownership fixture mode must change for rejection test");
        let mode_failure = inspect_stale_runtime_owner_in_directory(&test_directory, |_| false)
            .expect_err("non-private ownership artifact must fail closed");
        assert_eq!(
            mode_failure.code,
            "BACKEND_OWNERSHIP_RECONCILIATION_REQUIRED"
        );

        // Permission을 복원해도 exact four-field schema가 아니면 release attestation을 만들지 않는다.
        fs::set_permissions(&artifact_path, fs::Permissions::from_mode(0o600))
            .expect("ownership fixture mode must be restored");
        let schema_version = BACKEND_SCHEMA_VERSION;
        let nonexact_payload = format!(
            "{{\"schema_version\":{schema_version},\"runtime_pid\":4321,\"process_start_id\":\"{TEST_PROCESS_START_ID}\",\"owner_state\":\"ACTIVE\",\"unexpected\":true}}\n"
        );
        fs::write(&artifact_path, nonexact_payload)
            .expect("nonexact ownership fixture must be written");
        let shape_failure = inspect_stale_runtime_owner_in_directory(&test_directory, |_| false)
            .expect_err("unknown ownership field must fail closed");
        assert_eq!(
            shape_failure.code,
            "BACKEND_OWNERSHIP_RECONCILIATION_REQUIRED"
        );

        fs::remove_file(&artifact_path).expect("ownership fixture must be removed");
        fs::remove_dir(&test_directory).expect("ownership fixture directory must be removed");
    }

    /// 함수 이름: release_revalidates_pid_and_same_inode_after_operator_delay()
    /// 기능: dialog 대기 중 PID가 재등장하거나 path inode가 교체되면 이전 attestation을 폐기하는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/29
    #[test]
    #[cfg(target_os = "macos")]
    fn release_revalidates_pid_and_same_inode_after_operator_delay() {
        let test_directory = create_runtime_ownership_test_directory();
        let artifact_path = write_runtime_ownership_test_artifact(
            &test_directory,
            4_321,
            TEST_PROCESS_START_ID,
            RuntimeOwnershipState::Orphaned,
        );
        let attestation = inspect_stale_runtime_owner_in_directory(&test_directory, |_| false)
            .expect("stale ownership inspection must succeed")
            .expect("ORPHANED artifact must require attestation");

        // 확인 직후 PID가 다시 보이면 write를 시작하기 전에 fail closed한다.
        let live_failure =
            release_stale_runtime_owner_in_directory(&test_directory, &attestation, |_| true)
                .expect_err("reappeared PID must prevent stale release");
        assert_eq!(live_failure.code, "BACKEND_OWNERSHIP_RELEASE_FAILED");

        // 같은 내용을 복제해도 확인하지 않은 새 inode에는 RELEASED state를 기록하지 않는다.
        let retained_path = test_directory.join("retained-runtime-owner");
        fs::rename(&artifact_path, &retained_path)
            .expect("inspected inode must remain allocated for race test");
        let replacement_path = write_runtime_ownership_test_artifact(
            &test_directory,
            4_321,
            TEST_PROCESS_START_ID,
            RuntimeOwnershipState::Orphaned,
        );
        let inode_failure =
            release_stale_runtime_owner_in_directory(&test_directory, &attestation, |_| false)
                .expect_err("replacement inode must invalidate stale attestation");
        assert_eq!(inode_failure.code, "BACKEND_OWNERSHIP_RELEASE_FAILED");
        let replacement: RuntimeOwnershipArtifactWire = serde_json::from_slice(
            &fs::read(&replacement_path).expect("replacement artifact must be readable"),
        )
        .expect("replacement artifact must remain exact JSON");
        assert_eq!(replacement.owner_state, RuntimeOwnershipState::Orphaned);

        fs::remove_file(&replacement_path).expect("replacement fixture must be removed");
        fs::remove_file(&retained_path).expect("retained fixture must be removed");
        fs::remove_dir(&test_directory).expect("ownership fixture directory must be removed");
    }

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
        let schema_version = BACKEND_SCHEMA_VERSION;
        let wrong_schema_version = BACKEND_SCHEMA_VERSION + 1;
        let unknown_field = format!(
            "{{\"port\":41000,\"session_id\":\"{TEST_SESSION_ID}\",\"runtime_pid\":4321,\"process_start_id\":\"{TEST_PROCESS_START_ID}\",\"schema_version\":{schema_version},\"token\":\"forbidden\"}}"
        );
        let wrong_schema = format!(
            "{{\"port\":41000,\"session_id\":\"{TEST_SESSION_ID}\",\"runtime_pid\":4321,\"process_start_id\":\"{TEST_PROCESS_START_ID}\",\"schema_version\":{wrong_schema_version}}}"
        );

        assert!(parse_ready_descriptor(unknown_field.as_bytes()).is_err());
        assert!(parse_ready_descriptor(wrong_schema.as_bytes()).is_err());
    }

    /// 함수 이름: ready_descriptor_requires_positive_exact_pid_and_canonical_start_uuid()
    /// 기능: Python runtime PID의 zero/non-integer와 noncanonical process UUID를 strict 거부하는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/25
    #[test]
    fn ready_descriptor_requires_positive_exact_pid_and_canonical_start_uuid() {
        let schema_version = BACKEND_SCHEMA_VERSION;
        let valid = format!(
            "{{\"port\":41000,\"session_id\":\"{TEST_SESSION_ID}\",\"runtime_pid\":4321,\"process_start_id\":\"{TEST_PROCESS_START_ID}\",\"schema_version\":{schema_version}}}"
        );
        let valid_ready = parse_ready_descriptor(valid.as_bytes())
            .expect("strict positive runtime identity must parse");

        assert_eq!(valid_ready.runtime_pid, 4_321);
        assert_eq!(valid_ready.process_start_id, TEST_PROCESS_START_ID);
        for invalid_pid in ["0", "-1", "1.0", "true", "\"4321\""] {
            let invalid = format!(
                "{{\"port\":41000,\"session_id\":\"{TEST_SESSION_ID}\",\"runtime_pid\":{invalid_pid},\"process_start_id\":\"{TEST_PROCESS_START_ID}\",\"schema_version\":{schema_version}}}"
            );
            assert!(parse_ready_descriptor(invalid.as_bytes()).is_err());
        }

        // UUID parser가 받아도 canonical lowercase text가 아닌 uppercase representation은 거부한다.
        let uppercase_start_id = TEST_PROCESS_START_ID.to_uppercase();
        let noncanonical = format!(
            "{{\"port\":41000,\"session_id\":\"{TEST_SESSION_ID}\",\"runtime_pid\":4321,\"process_start_id\":\"{uppercase_start_id}\",\"schema_version\":{schema_version}}}"
        );
        assert!(parse_ready_descriptor(noncanonical.as_bytes()).is_err());
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

    /// 함수 이름: lifecycle_preserves_launcher_and_python_runtime_identities_separately()
    /// 기능: launcher PID와 late Python PID/start UUID를 동일 값으로 추정하지 않고 exit 뒤에도 보존하는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/25
    #[test]
    fn lifecycle_preserves_launcher_and_python_runtime_identities_separately() {
        let state = SidecarProcessState::default();
        {
            let mut lifecycle = state
                .shared
                .lifecycle
                .lock()
                .expect("fixture lifecycle lock must be available");
            lifecycle.launcher_child_pid = Some(7_001);
        }
        let identity = PythonRuntimeIdentity {
            runtime_pid: 8_002,
            process_start_id: TEST_PROCESS_START_ID.to_owned(),
        };

        state
            .record_python_runtime_identity(identity.clone())
            .expect("first late runtime identity must be recorded");
        state.record_exit(Some(0));
        let lifecycle = state
            .shared
            .lifecycle
            .lock()
            .expect("recorded lifecycle lock must be available");

        assert_eq!(lifecycle.launcher_child_pid, Some(7_001));
        assert_eq!(lifecycle.python_runtime_identity, Some(identity));
    }

    /// 함수 이름: lifecycle_rejects_mismatched_late_runtime_identity()
    /// 기능: 같은 launcher lifetime에 다른 Python PID 또는 start UUID가 재게시되면 fail closed하는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/25
    #[test]
    fn lifecycle_rejects_mismatched_late_runtime_identity() {
        let state = SidecarProcessState::default();
        {
            let mut lifecycle = state
                .shared
                .lifecycle
                .lock()
                .expect("fixture lifecycle lock must be available");
            lifecycle.launcher_child_pid = Some(7_001);
        }
        let original = PythonRuntimeIdentity {
            runtime_pid: 8_002,
            process_start_id: TEST_PROCESS_START_ID.to_owned(),
        };
        let mismatch = PythonRuntimeIdentity {
            runtime_pid: 8_003,
            process_start_id: "9a96508f-5585-48de-8464-495943de35f5".to_owned(),
        };

        state
            .record_python_runtime_identity(original.clone())
            .expect("first late runtime identity must be recorded");
        let failure = state
            .record_python_runtime_identity(mismatch)
            .expect_err("different runtime identity must be rejected");
        let lifecycle = state
            .shared
            .lifecycle
            .lock()
            .expect("rejected lifecycle lock must be available");

        assert_eq!(failure.code, "INVALID_BACKEND_DESCRIPTOR");
        assert_eq!(lifecycle.python_runtime_identity, Some(original));
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
    #[cfg(target_os = "macos")]
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
        let schema_version = BACKEND_SCHEMA_VERSION;
        let late_payload = format!(
            "{{\"port\":41000,\"session_id\":\"{TEST_SESSION_ID}\",\"runtime_pid\":4321,\"process_start_id\":\"{TEST_PROCESS_START_ID}\",\"schema_version\":{schema_version}}}\n"
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
    #[cfg(target_os = "macos")]
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
        let schema_version = BACKEND_SCHEMA_VERSION;
        let suffix = format!(
            "\"session_id\":\"{TEST_SESSION_ID}\",\"runtime_pid\":4321,\"process_start_id\":\"{TEST_PROCESS_START_ID}\",\"schema_version\":{schema_version}}}\n"
        );
        writer
            .write_all(suffix.as_bytes())
            .expect("late READY suffix must be written");
        let ready = read_ready_descriptor(&mut reader, &mut framing, Duration::from_millis(100))
            .expect("preserved partial framing must parse after continuation");

        assert_eq!(ready.port, 41_000);
        assert_eq!(ready.session_id, TEST_SESSION_ID);
        assert_eq!(ready.runtime_pid, 4_321);
        assert_eq!(ready.process_start_id, TEST_PROCESS_START_ID);
    }

    /// 함수 이름: ready_eof_is_terminal_and_not_retryable()
    /// 기능: writer EOF를 timeout과 구분해 token을 late recovery에 무기한 보유하지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    #[cfg(target_os = "macos")]
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
        assert!(!recorded.expected);
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
    #[cfg(target_os = "macos")]
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

/// 함수 이름: prepare_backend_sidecar()
/// 기능: 현재 OS adapter의 credential·IPC 준비 결과를 공통 lifecycle에 전달한다.
/// 인자: app_handle -> native app-data와 process owner
/// 반환값: ready 또는 ambiguous child 소유권
/// 작성 날짜: 2026/09/06
pub fn prepare_backend_sidecar(
    app_handle: &AppHandle,
) -> Result<BackendSidecarPreparation, SidecarFailure> {
    platform_prepare_backend_sidecar(app_handle) // OS 별 IPC 세부사항은 renderer에 노출하지 않는다.
}

/// 함수 이름: disable_process_core_dumps()
/// 기능: 현재 OS의 process crash-report 보호를 credential 조회 전에 적용한다.
/// 인자: 없음
/// 반환값: 보호 설정 성공 또는 startup failure
/// 작성 날짜: 2026/09/06
pub fn disable_process_core_dumps() -> Result<(), SidecarFailure> {
    platform_disable_process_core_dumps() // 보호 설정 실패는 startup을 중단한다.
}
