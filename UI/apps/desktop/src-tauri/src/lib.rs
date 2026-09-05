//! Tauri 데스크톱 앱과 sidecar를 연결하며, 현재 메인 화면의 인증 연결 복구와 native lifecycle을 소유한다.
mod dialog;
mod exit_bridge;
#[cfg(target_os = "macos")]
mod macos_quit_guard;
mod sidecar;

use serde::Serialize;
use std::error::Error;
use std::sync::Mutex;
use tauri::{AppHandle, Manager, State, WebviewWindow};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use zeroize::Zeroize;

/// 함수 이름: choose_csv_export_directory_for_current_host_smoke()
/// 기능: opt-in current-host harness가 production native picker command를 동일한 Tauri plugin에서 실행한다.
/// 인자: app -> dialog plugin을 설치한 최소 smoke application handle
/// 반환값: 선택 directory, 취소 null 또는 path-free failure code
/// 작성 날짜: 2026/08/29
#[cfg(feature = "native-picker-smoke")]
pub async fn choose_csv_export_directory_for_current_host_smoke(
    app: AppHandle,
) -> Result<Option<String>, &'static str> {
    // Harness가 production command를 복제하지 않고 같은 command implementation을 직접 호출한다.
    dialog::choose_csv_export_directory(app)
        .await
        .map_err(|failure| failure.code)
}

const BACKEND_SCHEMA_VERSION: u32 = 3; // Python transport schema와 native descriptor gate를 맞춘다.
const RELEASE_PROVENANCE_MARKER: &str = env!("BINANCE_AUTO_RELEASE_PROVENANCE");

/// 함수 이름: retain_release_provenance_marker()
/// 기능: build-time clean Git commit marker가 최종 native executable에 남도록 linker-visible 참조를 유지한다.
/// 인자: 없음
/// 반환값: 없음
/// 작성 날짜: 2026/08/24
#[inline(never)]
fn retain_release_provenance_marker() {
    std::hint::black_box(RELEASE_PROVENANCE_MARKER);
}

/// 현재 backend 실행 동안 native 메모리에만 보존하는 loopback 연결 descriptor이다.
#[derive(Clone, Serialize)]
pub struct BackendConnectionDescriptor {
    pub port: u16,
    pub session_id: String,
    pub schema_version: u32,
    pub token: String,
}

impl BackendConnectionDescriptor {
    /// 함수 이름: new()
    /// 기능: sidecar ready 값을 renderer 연결 descriptor로 옮기기 전에 strict shape를 검증한다.
    /// 인자: port -> 127.0.0.1 random port
    ///      session_id -> backend session canonical UUID
    ///      schema_version -> transport major schema
    ///      token -> CSPRNG 32-byte base64url no-padding token
    /// 반환값: 검증된 descriptor 또는 secret 없는 typed failure
    /// 작성 날짜: 2026/08/21
    pub fn new(
        port: u16,
        session_id: String,
        schema_version: u32,
        mut token: String,
    ) -> Result<Self, BackendDescriptorFailure> {
        let has_valid_token = token.len() == 43
            && token
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-');

        if port == 0
            || !is_canonical_uuid(&session_id)
            || schema_version != BACKEND_SCHEMA_VERSION
            || !has_valid_token
        {
            token.zeroize(); // 검증 실패 token도 allocator drop에만 맡기지 않고 즉시 덮어쓴다.
            return Err(BackendDescriptorFailure::invalid());
        }

        Ok(Self {
            port,
            session_id,
            schema_version,
            token,
        })
    }
}

impl Drop for BackendConnectionDescriptor {
    /// 함수 이름: drop()
    /// 기능: IPC 응답 복사본과 종료된 native 연결 정보의 token 메모리를 덮어쓴다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    fn drop(&mut self) {
        self.token.zeroize();
    }
}

/// Tauri command가 renderer에 반환하는 secret 없는 typed descriptor failure이다.
#[derive(Serialize)]
pub struct BackendDescriptorFailure {
    pub code: &'static str,
    pub message: &'static str,
}

impl BackendDescriptorFailure {
    /// 함수 이름: invalid()
    /// 기능: raw descriptor 값을 반사하지 않는 validation failure를 만든다.
    /// 인자: 없음
    /// 반환값: INVALID_BACKEND_DESCRIPTOR failure
    /// 작성 날짜: 2026/08/21
    fn invalid() -> Self {
        Self {
            code: "INVALID_BACKEND_DESCRIPTOR",
            message: "Backend connection descriptor is invalid.",
        }
    }

    /// 함수 이름: unavailable()
    /// 기능: 현재 backend의 연결 정보가 준비되지 않은 상태를 공개 가능한 오류로 반환한다.
    /// 인자: 없음
    /// 반환값: BACKEND_DESCRIPTOR_UNAVAILABLE failure
    /// 작성 날짜: 2026/08/21
    fn unavailable() -> Self {
        Self {
            code: "BACKEND_DESCRIPTOR_UNAVAILABLE",
            message: "Backend connection descriptor is unavailable.",
        }
    }

    /// 함수 이름: state_unavailable()
    /// 기능: poisoned native mutex를 내부 상세 없이 typed state failure로 변환한다.
    /// 인자: 없음
    /// 반환값: BACKEND_DESCRIPTOR_STATE_UNAVAILABLE failure
    /// 작성 날짜: 2026/08/21
    fn state_unavailable() -> Self {
        Self {
            code: "BACKEND_DESCRIPTOR_STATE_UNAVAILABLE",
            message: "Backend connection descriptor state is unavailable.",
        }
    }

    /// 함수 이름: backend_exited()
    /// 기능: 종료된 backend의 token을 다시 반환하지 않고 창 닫기가 가능한 상태를 알린다.
    /// 인자: 없음
    /// 반환값: BACKEND_SIDECAR_EXITED failure
    /// 작성 날짜: 2026/09/05
    fn backend_exited() -> Self {
        Self {
            code: "BACKEND_SIDECAR_EXITED",
            message: "Backend sidecar has exited.",
        }
    }
}

/// 새로고침한 메인 화면이 같은 backend로 재연결할 수 있게 연결 정보를 native 메모리에 보존한다.
#[derive(Default)]
pub struct BackendConnectionDescriptorState {
    session_descriptor: Mutex<Option<BackendConnectionDescriptor>>,
}

impl BackendConnectionDescriptorState {
    /// 함수 이름: stage()
    /// 기능: Phase 12 launcher가 만든 descriptor를 아직 비어 있는 memory-only slot에 소유권 이전한다.
    /// 인자: descriptor -> strict constructor를 통과한 launch descriptor
    /// 반환값: stage 성공 또는 secret 없는 typed failure
    /// 작성 날짜: 2026/08/21
    pub fn stage(
        &self,
        descriptor: BackendConnectionDescriptor,
    ) -> Result<(), BackendDescriptorFailure> {
        let mut session_descriptor = match self.session_descriptor.lock() {
            Ok(session_descriptor) => session_descriptor,
            Err(poisoned) => poisoned.into_inner(),
        };

        if session_descriptor.is_some() {
            return Err(BackendDescriptorFailure::unavailable());
        }

        // Descriptor와 token은 filesystem, environment 또는 log를 거치지 않고 memory slot으로 이동한다.
        *session_descriptor = Some(descriptor);
        Ok(())
    }

    /// 함수 이름: get()
    /// 기능: 현재 backend의 연결 정보를 복사해 새 renderer와 복구 재시도에 전달한다.
    /// 인자: 없음
    /// 반환값: renderer에 직렬화할 descriptor 또는 unavailable failure
    /// 작성 날짜: 2026/08/21
    fn get(&self) -> Result<BackendConnectionDescriptor, BackendDescriptorFailure> {
        let session_descriptor = self
            .session_descriptor
            .lock()
            .map_err(|_| BackendDescriptorFailure::state_unavailable())?;

        session_descriptor
            .as_ref()
            .cloned()
            .ok_or_else(BackendDescriptorFailure::unavailable)
    }

    /// 함수 이름: clear()
    /// 기능: backend 종료 시 native 원본을 제거하고 Drop에서 token을 즉시 덮어쓴다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/05
    pub(crate) fn clear(&self) {
        // Poison 상태에서도 종료된 session의 인증 정보를 메모리에 남기지 않는다.
        let mut descriptor = self
            .session_descriptor
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        *descriptor = None; // 원본 descriptor의 Drop이 token을 zeroize한다.
    }
}

/// 함수 이름: is_canonical_uuid()
/// 기능: dependency 추가 없이 lowercase RFC 4122 variant canonical UUID shape를 검증한다.
/// 인자: value -> session ID 후보
/// 반환값: canonical UUID shape 여부
/// 작성 날짜: 2026/08/21
fn is_canonical_uuid(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.len() != 36 {
        return false;
    }

    for (index, byte) in bytes.iter().enumerate() {
        if matches!(index, 8 | 13 | 18 | 23) {
            if *byte != b'-' {
                return false;
            }
        } else if !byte.is_ascii_hexdigit() || byte.is_ascii_uppercase() {
            return false;
        }
    }

    matches!(bytes[14], b'1'..=b'8') && matches!(bytes[19], b'8' | b'9' | b'a' | b'b')
}

/// 함수 이름: get_backend_connection_descriptor()
/// 기능: 허용된 메인 창에만 실행 중 backend의 연결 정보를 제공해 화면 새로고침을 복구한다.
/// 인자: window -> IPC 호출 창, state -> native 연결 정보, sidecar_state -> 현재 backend 수명주기
/// 반환값: 메모리에서만 직렬화한 descriptor 또는 공개 가능한 오류
/// 작성 날짜: 2026/09/05
#[tauri::command]
fn get_backend_connection_descriptor(
    window: WebviewWindow,
    state: State<'_, BackendConnectionDescriptorState>,
    sidecar_state: State<'_, sidecar::SidecarProcessState>,
) -> Result<BackendConnectionDescriptor, BackendDescriptorFailure> {
    // Capability에 더해 실제 호출 창과 URL도 확인해 외부 문서의 token 재발급을 막는다.
    let url = window
        .url()
        .map_err(|_| BackendDescriptorFailure::unavailable())?;
    if window.label() != "main" || !sidecar::is_trusted_renderer_url(&url, tauri::is_dev()) {
        return Err(BackendDescriptorFailure::unavailable());
    }
    if !sidecar_state.is_running() {
        state.clear();
        return Err(BackendDescriptorFailure::backend_exited());
    }
    state.get()
}

/// Late READY recovery task 설치 결과를 native operator surface 경로와 분리한다.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum LateReadyStartupRoute {
    AwaitWithNativeStatus,
    FatalRecovery,
}

/// 함수 이름: select_late_ready_startup_route()
/// 기능: recovery task가 시작되고 child가 live인 경우에만 waiting status를 허용한다.
/// 인자: recovery_started -> FD4 background recovery scheduling 성공 여부,
///      child_is_running -> scheduling 직후 lifecycle의 live child 여부
/// 반환값: native waiting status 또는 fatal recovery route
/// 작성 날짜: 2026/08/24
fn select_late_ready_startup_route(
    recovery_started: bool,
    child_is_running: bool,
) -> LateReadyStartupRoute {
    if recovery_started && child_is_running {
        LateReadyStartupRoute::AwaitWithNativeStatus
    } else {
        LateReadyStartupRoute::FatalRecovery
    }
}

/// 함수 이름: setup_backend_and_window()
/// 기능: AppKit quit gate, core dump 차단, stale owner 조정과 sidecar ready/stage/monitor 뒤에만 deferred main window를 만든다.
/// 인자: app -> startup 중인 trusted native Tauri application
/// 반환값: startup 성공 또는 secret 없는 boxed native failure
/// 작성 날짜: 2026/08/24
fn setup_backend_and_window(app: &mut tauri::App) -> Result<(), Box<dyn Error>> {
    #[cfg(target_os = "macos")]
    if macos_quit_guard::install_macos_quit_guard(app.handle()).is_err() {
        // Guard 없는 child를 시작하지 않고 기존 secret-free pre-READY operator surface로 종료한다.
        schedule_pre_ready_startup_failure(app.handle().clone(), "BACKEND_SIDECAR_STARTUP_FAILED");
        return Ok(());
    }

    // AppKit quit gate 설치가 확정된 뒤에만 credential 조회와 sidecar process 시작을 허용한다.
    if let Err(failure) = sidecar::disable_process_core_dumps() {
        schedule_pre_ready_startup_failure(app.handle().clone(), failure.code);
        return Ok(());
    }
    continue_backend_startup(app.handle());
    Ok(())
}

/// 함수 이름: continue_backend_startup()
/// 기능: 최초 실행과 만료 소유권 해제 뒤의 준비를 동일 프로세스에서 계속한다.
/// 인자: app_handle -> 기존 event loop와 개발 서버 연결을 유지하는 앱 handle
/// 반환값: 없음
/// 작성 날짜: 2026/09/05
fn continue_backend_startup(app_handle: &AppHandle) {
    // 화면 서버가 없을 때는 backend를 먼저 띄우거나 빈 WebView를 만들지 않는다.
    if tauri::is_dev() && !sidecar::development_renderer_is_available() {
        schedule_renderer_startup_recovery(app_handle.clone());
        return;
    }
    match sidecar::inspect_stale_runtime_owner(app_handle) {
        Ok(Some(attestation)) => {
            // Python startup보다 먼저 stale identity를 공개해 새 owner가 기록을 덮어쓰지 않게 한다.
            schedule_stale_runtime_owner_release(app_handle.clone(), attestation);
            return;
        }
        Ok(None) => {}
        Err(failure) => {
            schedule_pre_ready_startup_failure(app_handle.clone(), failure.code);
            return;
        }
    }
    let preparation = match sidecar::prepare_backend_sidecar(app_handle) {
        Ok(preparation) => preparation,
        Err(failure) => {
            schedule_pre_ready_startup_failure(app_handle.clone(), failure.code);
            return;
        }
    };
    let prepared_sidecar = match preparation {
        sidecar::BackendSidecarPreparation::Ready(prepared_sidecar) => prepared_sidecar,
        sidecar::BackendSidecarPreparation::Ambiguous(ambiguous_sidecar) => {
            let sidecar_state = app_handle
                .state::<sidecar::SidecarProcessState>()
                .inner()
                .clone();
            let sidecar::AmbiguousBackendSidecar {
                child,
                stop_writer,
                late_ready_recovery,
            } = ambiguous_sidecar;
            if sidecar_state
                .install(child, stop_writer, app_handle.clone())
                .is_err()
            {
                schedule_ready_fatal_recovery(app_handle.clone());
                return;
            }
            if let Some(recovery) = late_ready_recovery {
                let recovery_started = sidecar::start_late_ready_recovery(
                    recovery,
                    app_handle.clone(),
                    sidecar_state.clone(),
                )
                .is_ok();
                let child_is_running = sidecar_state.is_running();

                // Task 시작 failure나 child exit race는 headless return 없이 fatal surface가 안전 종료를 결정한다.
                match select_late_ready_startup_route(recovery_started, child_is_running) {
                    LateReadyStartupRoute::AwaitWithNativeStatus => {
                        schedule_late_ready_status(app_handle.clone(), sidecar_state);
                    }
                    LateReadyStartupRoute::FatalRecovery => {
                        schedule_ready_fatal_recovery(app_handle.clone());
                    }
                }
            } else {
                schedule_ready_fatal_recovery(app_handle.clone());
            }
            return;
        }
    };
    let sidecar::PreparedBackendSidecar {
        descriptor,
        child,
        stop_writer,
        port,
    } = prepared_sidecar;

    // READY child를 먼저 native lifecycle에 유지해 후속 UI 실패가 process orphan/kill로 바뀌지 않게 한다.
    let sidecar_state = app_handle
        .state::<sidecar::SidecarProcessState>()
        .inner()
        .clone();
    if sidecar_state
        .install(child, stop_writer, app_handle.clone())
        .is_err()
    {
        schedule_ready_fatal_recovery(app_handle.clone());
        return;
    }

    // 검증된 session 정보를 native 메모리에 보존한 뒤에만 renderer window를 만든다.
    let descriptor_state = app_handle.state::<BackendConnectionDescriptorState>();
    if descriptor_state.stage(descriptor).is_err() {
        schedule_ready_fatal_recovery(app_handle.clone());
        return;
    }

    // Window build 실패에는 READY process를 kill하지 않고 native dialog에서 복구를 계속 시도한다.
    if sidecar::create_ready_main_window(app_handle, port).is_err() {
        schedule_ready_window_recovery(app_handle.clone(), port);
    }
}

/// 함수 이름: schedule_renderer_startup_recovery()
/// 기능: 개발 화면 서버 부재를 빈 창 대신 재시도·종료가 가능한 native 안내로 표시한다.
/// 인자: app_handle -> backend를 아직 시작하지 않은 앱 handle
/// 반환값: 없음
/// 작성 날짜: 2026/09/05
fn schedule_renderer_startup_recovery(app_handle: AppHandle) {
    let retry_handle = app_handle.clone();
    // 서버 재개 후 확인하면 같은 앱에서 시작하므로 개발 서버의 수명주기를 끊지 않는다.
    app_handle.dialog()
        .message("화면 서버에 연결할 수 없습니다. 개발 서버를 실행한 뒤 다시 시도하세요. 백엔드는 아직 시작하지 않았습니다.")
        .title("화면을 불러올 수 없습니다")
        .kind(MessageDialogKind::Error)
        .buttons(MessageDialogButtons::OkCancelCustom("다시 시도".to_owned(), "종료".to_owned()))
        .show(move |retry| {
            if retry {
                resume_backend_startup(retry_handle);
            } else {
                retry_handle.exit(0);  // 시작 전이므로 종료할 거래·backend가 없다.
            }
        });
}

/// 함수 이름: resume_backend_startup()
/// 기능: native 대화상자 callback에서 기존 main event loop로 시작 작업을 돌려보낸다.
/// 인자: app_handle -> 동일 프로세스에서 준비를 계속할 앱 handle
/// 반환값: 없음
/// 작성 날짜: 2026/09/05
fn resume_backend_startup(app_handle: AppHandle) {
    let resume_handle = app_handle.clone();
    if app_handle
        .run_on_main_thread(move || continue_backend_startup(&resume_handle))
        .is_err()
    {
        schedule_pre_ready_startup_failure(app_handle, "BACKEND_SIDECAR_STARTUP_FAILED");
    }
}

/// 함수 이름: pre_ready_failure_copy()
/// 기능: credential 부재와 기타 pre-READY 실패를 secret/path 없는 native 안내로 변환한다.
/// 인자: failure_code -> fixed native SidecarFailure code
/// 반환값: dialog title/message pair
/// 작성 날짜: 2026/08/24
fn pre_ready_failure_copy(failure_code: &str) -> (&'static str, &'static str) {
    if failure_code == "BACKEND_CREDENTIALS_UNAVAILABLE" {
        return (
            "Keychain 인증 정보가 필요합니다",
            "macOS Keychain에 testnet API key와 secret을 안전하게 등록한 뒤 애플리케이션을 다시 시작하세요.",
        );
    }
    if failure_code == "BACKEND_OWNERSHIP_RECONCILIATION_REQUIRED" {
        return (
            "백엔드 소유권을 자동 확인할 수 없습니다",
            "기록된 프로세스가 실행 중이거나 소유권 기록을 안전하게 잠그고 검증할 수 없습니다. 자동 해제하지 않았습니다. 다른 백엔드 실행 여부를 운영 절차로 확인한 뒤 다시 시작하세요.",
        );
    }
    if failure_code == "BACKEND_OWNERSHIP_RELEASE_FAILED" {
        return (
            "백엔드 소유권을 해제하지 못했습니다",
            "확인 이후 프로세스 또는 소유권 기록이 바뀌어 자동 해제하지 않았습니다. 현재 실행 상태를 다시 확인한 뒤 애플리케이션을 다시 시작하세요.",
        );
    }

    (
        "백엔드를 준비하지 못했습니다",
        "보안 준비 단계에서 실패했습니다. 애플리케이션을 종료한 뒤 설치 상태를 확인하고 다시 시작하세요.",
    )
}

/// 함수 이름: stale_runtime_owner_prompt_copy()
/// 기능: exact stale process identity와 state를 secret/path 없는 operator attestation 문구로 만든다.
/// 인자: owner_state -> ACTIVE 또는 ORPHANED artifact state,
///      runtime_pid -> artifact가 기록한 actual Python PID,
///      process_start_id -> artifact가 기록한 canonical process launch UUID
/// 반환값: dialog title과 exact identity를 포함한 message
/// 작성 날짜: 2026/08/29
fn stale_runtime_owner_prompt_copy(
    owner_state: sidecar::RuntimeOwnershipState,
    runtime_pid: u32,
    process_start_id: &str,
) -> (&'static str, String) {
    let message = format!(
        "이전 백엔드 소유권 기록을 발견했습니다.\n\n상태: {}\n런타임 PID: {}\n프로세스 시작 ID: {}\n\n현재 PID 부재를 확인했지만 자동으로 소유권을 해제하지 않았습니다. 계속하면 이 exact 기록의 상태만 RELEASED로 저장한 뒤 현재 애플리케이션에서 시작을 계속합니다. 실행 중인 프로세스 종료, 주문 취소 또는 포지션 청산은 수행하지 않습니다. 이 실행을 직접 확인한 운영자만 계속하세요.",
        owner_state.as_str(),
        runtime_pid,
        process_start_id,
    );
    ("이전 백엔드 소유권 확인", message)
}

/// 함수 이름: schedule_stale_runtime_owner_release()
/// 기능: stale identity를 확인받아 같은 inode를 RELEASED fsync한 뒤 동일 프로세스에서 시작을 계속한다.
/// 인자: app_handle -> native dialog와 현재 시작 작업의 owner,
///      attestation -> lock·PID 검증을 통과해 화면에 고정할 stale identity snapshot
/// 반환값: 없음
/// 작성 날짜: 2026/08/29
fn schedule_stale_runtime_owner_release(
    app_handle: AppHandle,
    attestation: sidecar::StaleRuntimeOwnershipAttestation,
) {
    let (title, message) = stale_runtime_owner_prompt_copy(
        attestation.owner_state,
        attestation.runtime_pid,
        &attestation.process_start_id,
    );
    let release_handle = app_handle.clone();

    // 취소는 artifact를 그대로 두며, 확인도 dialog 이후 exact inode·identity·PID를 다시 검증한다.
    app_handle
        .dialog()
        .message(message)
        .title(title)
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(
            "확인 후 해제·계속".to_owned(),
            "취소".to_owned(),
        ))
        .show(move |confirmed| {
            if !confirmed {
                release_handle.exit(1);
                return;
            }
            match sidecar::release_stale_runtime_owner(&release_handle, &attestation) {
                Ok(()) => resume_backend_startup(release_handle),
                Err(failure) => {
                    schedule_pre_ready_startup_failure(release_handle, failure.code);
                }
            }
        });
}

/// 함수 이름: schedule_pre_ready_startup_failure()
/// 기능: child가 없는 startup 실패를 native dialog로 보인 뒤 process를 안전하게 종료한다.
/// 인자: app_handle -> dialog/exit owner, failure_code -> fixed failure discriminator
/// 반환값: 없음
/// 작성 날짜: 2026/08/24
fn schedule_pre_ready_startup_failure(app_handle: AppHandle, failure_code: &'static str) {
    let (title, message) = pre_ready_failure_copy(failure_code);
    let exit_handle = app_handle.clone();

    // Child가 시작되지 않은 경로이므로 dialog 확인 후 exit은 lifecycle/position을 우회하지 않는다.
    app_handle
        .dialog()
        .message(message)
        .title(title)
        .kind(MessageDialogKind::Error)
        .show(move |_| {
            exit_handle.exit(1);
        });
}

/// 함수 이름: schedule_ready_fatal_recovery()
/// 기능: READY child를 안전하게 보존한 채 renderer startup을 금지하고 operator manual recovery 안내를 유지한다.
/// 인자: app_handle -> nonblocking native dialog owner
/// 반환값: 없음
/// 작성 날짜: 2026/08/24
pub(crate) fn schedule_ready_fatal_recovery(app_handle: AppHandle) {
    let sidecar_state = app_handle
        .state::<sidecar::SidecarProcessState>()
        .inner()
        .clone();
    if !sidecar_state.is_running() {
        app_handle.exit(1);
        return;
    }
    let repeat_handle = app_handle.clone();
    let repeat_state = sidecar_state.clone();

    // READY exposure를 timed kill하거나 descriptor 없는 renderer를 열지 않고 native operator surface를 계속 유지한다.
    app_handle
        .dialog()
        .message(
            "백엔드는 실행 중이며 자동 종료되지 않았습니다. 애플리케이션을 강제 종료하지 말고 운영자에게 수동 복구를 요청하세요.",
        )
        .title("네이티브 안전 복구가 필요합니다")
        .kind(MessageDialogKind::Error)
        .show(move |_| {
            if repeat_state.is_running() {
                schedule_ready_fatal_recovery(repeat_handle);
            } else {
                repeat_handle.exit(1);
            }
        });
}

/// 함수 이름: schedule_late_ready_status()
/// 기능: recoverable READY timeout 중 native status를 유지하고 valid late READY window가 생기면 반복을 멈춘다.
/// 인자: app_handle -> native dialog/window owner, sidecar_state -> pending recovery guard
/// 반환값: 없음
/// 작성 날짜: 2026/08/24
fn schedule_late_ready_status(app_handle: AppHandle, sidecar_state: sidecar::SidecarProcessState) {
    let repeat_handle = app_handle.clone();
    let repeat_state = sidecar_state.clone();

    // 이 안내는 FD4 parser가 token/framing을 보존한 동안만 반복되며 fatal recovery와 동시에 반복되지 않는다.
    app_handle
        .dialog()
        .message(
            "백엔드의 안전 상태 확인이 예상보다 오래 걸리고 있습니다. 자동 종료하지 말고 이 창을 유지해 주세요. 준비가 완료되면 메인 창이 자동으로 열립니다.",
        )
        .title("백엔드 안전 상태 확인 중")
        .kind(MessageDialogKind::Warning)
        .show(move |_| {
            if repeat_state.is_late_ready_terminal() {
                schedule_ready_fatal_recovery(repeat_handle);
            } else if repeat_state.is_late_ready_pending()
                && repeat_handle.get_webview_window("main").is_none()
            {
                schedule_late_ready_status(repeat_handle, repeat_state);
            } else if !repeat_state.is_running() {
                repeat_handle.exit(1);
            }
        });
}

/// 함수 이름: schedule_ready_window_recovery()
/// 기능: READY 후 main window build 실패를 operator에게 보이고 확인할 때마다 window를 kill 없이 재생성한다.
/// 인자: app_handle -> native dialog/window manager, port -> validated backend loopback port
/// 반환값: 없음
/// 작성 날짜: 2026/08/24
pub(crate) fn schedule_ready_window_recovery(app_handle: AppHandle, port: u16) {
    let retry_handle = app_handle.clone();
    let retry_state = app_handle
        .state::<sidecar::SidecarProcessState>()
        .inner()
        .clone();

    // Nonblocking native dialog는 event loop를 시작해 Command-Q guard와 child monitor가 계속 작동하게 한다.
    app_handle
        .dialog()
        .message(
            "백엔드는 안전하게 실행 중이며 자동 종료되지 않았습니다. 확인을 누르면 메인 창 복구를 다시 시도합니다.",
        )
        .title("메인 창을 준비하지 못했습니다")
        .kind(MessageDialogKind::Error)
        .show(move |_| {
            if !retry_state.is_running() {
                retry_handle.exit(1);
            } else if sidecar::create_ready_main_window(&retry_handle, port).is_err() {
                schedule_ready_window_recovery(retry_handle, port);
            }
        });
}

/// 함수 이름: run()
/// 기능: 최소 권한으로 Tauri 데스크톱 셸을 시작한다.
/// 인자: 없음
/// 반환값: 없음
/// 작성 날짜: 2026/08/12
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Code signature로 봉인될 provenance를 실행 경로에서도 참조해 release 최적화의 제거를 막는다.
    retain_release_provenance_marker();

    let sidecar_state = sidecar::SidecarProcessState::default();
    let exit_guard_state = sidecar_state.clone();
    let window_guard_state = sidecar_state.clone();
    let exit_intent_bridge = exit_bridge::NativeExitIntentBridgeState::default();
    let application_exit_bridge = exit_intent_bridge.clone();
    let window_exit_bridge = exit_intent_bridge.clone();
    let application_result = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(BackendConnectionDescriptorState::default())
        .manage(sidecar_state)
        .manage(exit_intent_bridge)
        .invoke_handler(tauri::generate_handler![
            get_backend_connection_descriptor,
            dialog::choose_csv_export_directory,
            sidecar::await_backend_sidecar_exit,
            exit_bridge::arm_native_exit_intent_bridge,
            sidecar::arm_sidecar_exit_event_bridge,
        ])
        .on_window_event(move |window, event| {
            if window.label() != "main" || !window_guard_state.is_running() {
                return;
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                // JS listener 준비와 무관하게 live child 중 window teardown를 먼저 native에서 차단한다.
                api.prevent_close();
                let _ = window_exit_bridge.request(
                    window.app_handle(),
                    exit_bridge::NativeExitIntentSource::Window,
                );
            }
        })
        .setup(setup_backend_and_window)
        .build(tauri::generate_context!());
    let application = match application_result {
        Ok(application) => application,
        // Tauri는 event-loop Ready에서 setup을 호출하므로 build failure 시점에는 child/secret publication이 없다.
        Err(_) => return,
    };

    // Live child가 남은 app-level quit은 자동 kill/고아 process 대신 UI shutdown lifecycle을 기다린다.
    application.run(move |app_handle, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            if exit_guard_state.is_running() {
                api.prevent_exit();
                let _ = application_exit_bridge
                    .request(app_handle, exit_bridge::NativeExitIntentSource::Application);
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEST_SESSION_ID: &str = "3c73d583-c1c8-4830-8393-cc31639a40fd";
    const TEST_TOKEN: &str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

    /// 함수 이름: release_provenance_marker_has_fail_closed_shape()
    /// 기능: local build는 UNVERIFIED, release build는 canonical lowercase commit만 native marker에 담는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn release_provenance_marker_has_fail_closed_shape() {
        let release_commit = RELEASE_PROVENANCE_MARKER
            .strip_prefix("BINANCE_AUTO_RELEASE_COMMIT=")
            .expect("release provenance marker prefix must be fixed");
        let canonical_commit = release_commit.len() == 40
            && release_commit
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));

        assert!(release_commit == "UNVERIFIED" || canonical_commit);
    }

    /// 함수 이름: create_descriptor()
    /// 기능: native session state test에 사용할 valid descriptor를 생성한다.
    /// 인자: 없음
    /// 반환값: valid descriptor
    /// 작성 날짜: 2026/08/21
    fn create_descriptor() -> BackendConnectionDescriptor {
        match BackendConnectionDescriptor::new(
            42_123,
            TEST_SESSION_ID.to_owned(),
            BACKEND_SCHEMA_VERSION,
            TEST_TOKEN.to_owned(),
        ) {
            Ok(descriptor) => descriptor,
            Err(_) => panic!("valid test descriptor must be accepted"),
        }
    }

    /// 함수 이름: descriptor_is_absent_until_phase12_stages_it()
    /// 기능: 초기 native state가 demo나 임의 descriptor로 fallback하지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/21
    #[test]
    fn descriptor_is_absent_until_phase12_stages_it() {
        let state = BackendConnectionDescriptorState::default();
        let failure = match state.get() {
            Ok(_) => panic!("empty state must fail closed"),
            Err(failure) => failure,
        };

        assert_eq!(failure.code, "BACKEND_DESCRIPTOR_UNAVAILABLE");
    }

    /// 함수 이름: renderer_reload_reuses_descriptor_until_backend_exit()
    /// 기능: 새 화면이 같은 backend에 재연결하고 종료 후에는 인증 정보가 제거되는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/21
    #[test]
    fn renderer_reload_reuses_descriptor_until_backend_exit() {
        let state = BackendConnectionDescriptorState::default();
        assert!(state.stage(create_descriptor()).is_ok());

        let descriptor = match state.get() {
            Ok(descriptor) => descriptor,
            Err(_) => panic!("staged descriptor must be available"),
        };
        assert_eq!(descriptor.port, 42_123);
        assert_eq!(descriptor.session_id, TEST_SESSION_ID);
        assert_eq!(descriptor.schema_version, BACKEND_SCHEMA_VERSION);
        assert_eq!(descriptor.token, TEST_TOKEN);

        // 이전 renderer의 응답 복사본을 버린 뒤에도 reload가 같은 session에 연결해야 한다.
        drop(descriptor);
        let reloaded_descriptor = state
            .get()
            .unwrap_or_else(|_| panic!("reload must retain connection"));
        assert_eq!(reloaded_descriptor.session_id, TEST_SESSION_ID);
        assert_eq!(reloaded_descriptor.token, TEST_TOKEN);
        state.clear();
        let second_failure = match state.get() {
            Ok(_) => panic!("exited backend descriptor must be unavailable"),
            Err(failure) => failure,
        };
        assert_eq!(second_failure.code, "BACKEND_DESCRIPTOR_UNAVAILABLE");
    }

    /// 함수 이름: invalid_descriptor_never_enters_native_state()
    /// 기능: unknown transport schema가 native state stage 전에 거부되는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/21
    #[test]
    fn invalid_descriptor_never_enters_native_state() {
        // 다른 descriptor 필드는 유효하게 두고 schema mismatch 하나만 격리한다.
        let invalid_result = BackendConnectionDescriptor::new(
            42_123,
            TEST_SESSION_ID.to_owned(),
            BACKEND_SCHEMA_VERSION + 1,
            TEST_TOKEN.to_owned(),
        );
        let failure = match invalid_result {
            Ok(_) => panic!("invalid descriptor must be rejected"),
            Err(failure) => failure,
        };

        assert_eq!(failure.code, "INVALID_BACKEND_DESCRIPTOR");
    }

    /// 함수 이름: duplicate_descriptor_stage_preserves_existing_ready_slot()
    /// 기능: unexpected duplicate stage가 기존 launch descriptor를 덮어쓰지 않고 fatal recovery 분기로 가는 불변식을 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn duplicate_descriptor_stage_preserves_existing_ready_slot() {
        let state = BackendConnectionDescriptorState::default();
        if state.stage(create_descriptor()).is_err() {
            panic!("first descriptor stage must succeed");
        }

        let failure = state
            .stage(create_descriptor())
            .expect_err("duplicate descriptor stage must fail closed");
        let retained = match state.get() {
            Ok(retained) => retained,
            Err(_) => panic!("first staged descriptor must remain available"),
        };

        assert_eq!(failure.code, "BACKEND_DESCRIPTOR_UNAVAILABLE");
        assert_eq!(retained.token, TEST_TOKEN);
    }

    /// 함수 이름: pre_ready_credential_failure_uses_secret_free_operator_copy()
    /// 기능: Keychain item 부재가 panic 원문이 아닌 고정 native 재시작 안내로 변환되는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn pre_ready_credential_failure_uses_secret_free_operator_copy() {
        let (title, message) = pre_ready_failure_copy("BACKEND_CREDENTIALS_UNAVAILABLE");
        let combined = format!("{title} {message}");

        assert!(combined.contains("Keychain"));
        assert!(!combined.contains("api-key"));
        assert!(!combined.contains("api-secret"));
        assert!(!combined.contains("BACKEND_CREDENTIALS_UNAVAILABLE"));
    }

    /// 함수 이름: ownership_failures_use_fixed_fail_closed_operator_copy()
    /// 기능: live/invalid owner와 release race가 path나 artifact 원문 없이 서로 다른 고정 안내를 쓰는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/29
    #[test]
    fn ownership_failures_use_fixed_fail_closed_operator_copy() {
        let (reconciliation_title, reconciliation_message) =
            pre_ready_failure_copy("BACKEND_OWNERSHIP_RECONCILIATION_REQUIRED");
        let (release_title, release_message) =
            pre_ready_failure_copy("BACKEND_OWNERSHIP_RELEASE_FAILED");
        let combined = format!(
            "{reconciliation_title} {reconciliation_message} {release_title} {release_message}"
        );

        assert!(reconciliation_message.contains("자동 해제하지 않았습니다"));
        assert!(release_message.contains("바뀌어 자동 해제하지 않았습니다"));
        assert_ne!(reconciliation_title, release_title);
        assert!(!combined.contains(".backend-runtime.lock"));
        assert!(!combined.contains("BACKEND_OWNERSHIP"));
        assert!(!combined.contains("api-key"));
        assert!(!combined.contains("api-secret"));
    }

    /// 함수 이름: stale_runtime_owner_prompt_binds_exact_nonsecret_identity()
    /// 기능: public 확인 문구가 state/PID/start UUID와 non-mutation 범위를 정확히 표시하는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/29
    #[test]
    fn stale_runtime_owner_prompt_binds_exact_nonsecret_identity() {
        for owner_state in [
            sidecar::RuntimeOwnershipState::Active,
            sidecar::RuntimeOwnershipState::Orphaned,
        ] {
            let (title, message) =
                stale_runtime_owner_prompt_copy(owner_state, 4_321, TEST_SESSION_ID);
            let combined = format!("{title} {message}");

            assert!(combined.contains(owner_state.as_str()));
            assert!(combined.contains("4321"));
            assert!(combined.contains(TEST_SESSION_ID));
            assert!(combined.contains("RELEASED"));
            assert!(combined.contains("프로세스 종료"));
            assert!(combined.contains("주문 취소"));
            assert!(combined.contains("포지션 청산"));
            assert!(combined.contains("현재 애플리케이션에서 시작을 계속"));
            assert!(!combined.contains("애플리케이션을 재시작"));
            assert!(!combined.contains(".backend-runtime.lock"));
            assert!(!combined.contains("api-key"));
            assert!(!combined.contains("api-secret"));
        }
    }

    /// 함수 이름: late_ready_start_failure_never_returns_headless()
    /// 기능: recovery scheduling failure와 child exit race 조합이 항상 fatal native recovery로 가는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn late_ready_start_failure_never_returns_headless() {
        assert_eq!(
            select_late_ready_startup_route(false, false),
            LateReadyStartupRoute::FatalRecovery
        );
        assert_eq!(
            select_late_ready_startup_route(false, true),
            LateReadyStartupRoute::FatalRecovery
        );
        assert_eq!(
            select_late_ready_startup_route(true, false),
            LateReadyStartupRoute::FatalRecovery
        );
        assert_eq!(
            select_late_ready_startup_route(true, true),
            LateReadyStartupRoute::AwaitWithNativeStatus
        );
    }
}
