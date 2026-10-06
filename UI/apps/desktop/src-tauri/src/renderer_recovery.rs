//! 비정상 WebView2 renderer 종료를 기록하고 같은 backend session의 기존 bootstrap만 제한적으로 다시 실행한다.

#[cfg(all(feature = "renderer-recovery-smoke", target_os = "windows"))]
pub(crate) mod smoke;

use std::sync::Mutex;
use tauri::{State, WebviewWindow};

#[cfg(target_os = "windows")]
use serde_json::json;
#[cfg(target_os = "windows")]
use tauri::{AppHandle, Manager};

#[cfg(any(target_os = "windows", test))]
const MAX_RENDERER_RELOADS: u8 = 3;

/// 클래스 이름: RecoveryPolicy
/// 기능: native 실행 동안 복구 상한과 renderer 교체 확인·종료 차단 상태를 보존한다.
/// 작성 날짜: 2026/10/04
#[derive(Default)]
struct RecoveryPolicy {
    shutdown_in_progress: bool,
    #[cfg(any(target_os = "windows", test))]
    reload_count: u8,
    #[cfg(any(target_os = "windows", test))]
    reload_pending: bool,
    #[cfg(any(target_os = "windows", test))]
    process_failed_count: u64,
    #[cfg(any(target_os = "windows", test))]
    last_renderer_id: Option<String>,
    #[cfg(any(target_os = "windows", test))]
    failed_renderer_ids: Vec<String>,
    #[cfg(target_os = "windows")]
    installed: bool,
}

#[cfg(any(target_os = "windows", test))]
impl RecoveryPolicy {
    /// 함수 이름: reserve_reload()
    /// 기능: 중복·반복 실패와 backend 종료를 차단하며 renderer 종료당 복구 한 번을 예약한다.
    /// 인자: main_renderer_exited -> main renderer 종료 여부, backend_available -> 종료하지 않는 기존 backend 여부
    /// 반환값: 예약 또는 복구 거절의 고정 진단 코드
    /// 작성 날짜: 2026/10/04
    fn reserve_reload(
        &mut self,
        main_renderer_exited: bool,
        backend_available: bool,
    ) -> &'static str {
        self.process_failed_count = self.process_failed_count.saturating_add(1);
        if !main_renderer_exited {
            return "observe_only";
        }
        if self.shutdown_in_progress || !backend_available {
            return "backend_stopping_or_unavailable";
        }
        if self.last_renderer_id.is_none() {
            return "renderer_not_ready"; // 최초 snapshot 검증 전에는 교체될 document identity를 확정할 수 없다.
        }
        if self.reload_pending {
            return "reload_already_pending";
        }
        if self.reload_count >= MAX_RENDERER_RELOADS {
            return "reload_limit_reached";
        }
        self.reload_count += 1;
        self.reload_pending = true;
        if let Some(renderer_id) = self.last_renderer_id.as_ref() {
            self.failed_renderer_ids.push(renderer_id.clone()); // 전체 세 번의 복구 상한으로 보관량도 제한한다.
        }
        "reload_reserved"
    }

    /// 함수 이름: observe_ready()
    /// 기능: 새 renderer의 같은 session heartbeat로만 복구 대기를 해제하며 총 재시도 상한은 유지한다.
    /// 인자: renderer_id -> 검증된 현재 renderer UUID
    /// 반환값: pending 복구가 새 renderer로 확인됐으면 true
    /// 작성 날짜: 2026/10/04
    fn observe_ready(&mut self, renderer_id: &str) -> bool {
        if self.last_renderer_id.as_deref() == Some(renderer_id)
            || self
                .failed_renderer_ids
                .iter()
                .any(|failed_id| failed_id == renderer_id)
        {
            return false; // 종료 직전에 전송된 이전 renderer heartbeat는 복구 성공이 아니다.
        }
        self.last_renderer_id = Some(renderer_id.to_owned());
        let recovered = self.reload_pending;
        self.reload_pending = false;
        recovered
    }
}

/// 클래스 이름: RendererRecoveryState
/// 기능: IPC 종료 gate와 WebView2 callback 사이에서 복구 정책을 단일 lock으로 공유한다.
/// 작성 날짜: 2026/10/04
#[derive(Default)]
pub struct RendererRecoveryState(Mutex<RecoveryPolicy>);

/// 함수 이름: set_renderer_recovery_shutdown()
/// 기능: HTTP 종료 준비 전에 신뢰한 renderer가 native 화면 복구를 차단하고 취소 시 해제한다.
/// 인자: window -> IPC 호출 창, state -> 복구 정책, in_progress -> 종료 준비 여부
/// 반환값: gate 저장 성공 또는 secret 없는 고정 오류 코드
/// 작성 날짜: 2026/10/04
#[tauri::command]
pub fn set_renderer_recovery_shutdown(
    window: WebviewWindow,
    state: State<'_, RendererRecoveryState>,
    in_progress: bool,
) -> Result<(), &'static str> {
    let url = window
        .url()
        .map_err(|_| "RENDERER_RECOVERY_INVALID_WINDOW")?;
    if window.label() != "main" || !crate::sidecar::is_trusted_renderer_url(&url, tauri::is_dev()) {
        return Err("RENDERER_RECOVERY_INVALID_WINDOW");
    }
    state
        .0
        .lock()
        .map_err(|_| "RENDERER_RECOVERY_STATE_UNAVAILABLE")?
        .shutdown_in_progress = in_progress;
    Ok(())
}

/// 함수 이름: existing_backend_is_available()
/// 기능: native 원본 descriptor와 종료하지 않는 backend child가 모두 존재하는지 확인한다.
/// 인자: app_handle -> 기존 Tauri application
/// 반환값: 기존 session bootstrap 재사용 가능 여부
/// 작성 날짜: 2026/10/04
#[cfg(target_os = "windows")]
fn existing_backend_is_available(app_handle: &AppHandle) -> bool {
    app_handle
        .state::<crate::sidecar::SidecarProcessState>()
        .permits_renderer_recovery()
        && app_handle
            .state::<crate::BackendConnectionDescriptorState>()
            .get()
            .is_ok()
}

/// 함수 이름: observe_renderer_ready()
/// 기능: 검증된 현재 backend session의 새 renderer heartbeat를 복구 완료 증거로 기록한다.
/// 인자: app_handle -> native state owner, renderer_id -> renderer UUID, session_id -> renderer가 읽은 session UUID
/// 반환값: 없음
/// 작성 날짜: 2026/10/04
#[cfg(target_os = "windows")]
pub(crate) fn observe_renderer_ready(app_handle: &AppHandle, renderer_id: &str, session_id: &str) {
    let matches_session = app_handle
        .state::<crate::BackendConnectionDescriptorState>()
        .get()
        .is_ok_and(|descriptor| descriptor.session_id == session_id);
    if !matches_session || !existing_backend_is_available(app_handle) {
        return;
    }
    let recovered = app_handle
        .state::<RendererRecoveryState>()
        .0
        .lock()
        .map(|mut policy| policy.observe_ready(renderer_id))
        .unwrap_or(false);
    if recovered {
        app_handle.state::<crate::runtime_diagnostics::RuntimeDiagnosticsState>().0.record(json!({
            "event": "renderer_recovery_ready", "renderer_id": renderer_id, "session_id": session_id,
        }));
    }
}

/// 함수 이름: dispatch_reload()
/// 기능: 실패 callback 밖의 main event loop에서 backend·종료 gate를 재확인하고 기존 창만 reload한다.
/// 인자: app_handle -> 기존 Tauri application
/// 반환값: 없음; 실패는 재시도 없이 진단하고 원래 오류 화면을 유지
/// 작성 날짜: 2026/10/04
#[cfg(target_os = "windows")]
fn dispatch_reload(app_handle: &AppHandle) {
    let reload_app = app_handle.clone();
    let dispatch_result = app_handle.run_on_main_thread(move || {
        let permitted = existing_backend_is_available(&reload_app)
            && reload_app
                .state::<RendererRecoveryState>()
                .0
                .lock()
                .is_ok_and(|policy| policy.reload_pending && !policy.shutdown_in_progress);
        let outcome = if !permitted {
            "backend_stopping_or_unavailable"
        } else if reload_app
            .get_webview_window("main")
            .is_some_and(|window| window.reload().is_ok())
        {
            "reload_dispatched"
        } else {
            "reload_failed"
        };
        // Reload는 기존 descriptor -> snapshot 검증 -> 단일 stream bootstrap을 다시 사용한다.
        // Backend 생성·매매 명령·이전 IPC payload를 복원하거나 재전송하지 않는다.
        reload_app
            .state::<crate::runtime_diagnostics::RuntimeDiagnosticsState>()
            .0
            .record(json!({
                "event": "renderer_recovery_reload", "outcome": outcome,
            }));
    });
    if dispatch_result.is_err() {
        app_handle
            .state::<crate::runtime_diagnostics::RuntimeDiagnosticsState>()
            .0
            .record(json!({
                "event": "renderer_recovery_reload", "outcome": "dispatch_failed",
            }));
    }
}

/// 함수 이름: install()
/// 기능: main WebView2에 단일 ProcessFailed listener를 연결하고 종료 종류·원인·PID 후보만 기록한다.
/// 인자: app_handle -> main 창과 진단 상태를 설치한 기존 application
/// 반환값: 없음; 등록 실패가 정상 backend 수명주기에 영향을 주지 않음
/// 작성 날짜: 2026/10/04
#[cfg(target_os = "windows")]
pub(crate) fn install(app_handle: &AppHandle) {
    use webview2_com::Microsoft::Web::WebView2::Win32::{
        ICoreWebView2ProcessFailedEventArgs2, COREWEBVIEW2_PROCESS_FAILED_KIND,
        COREWEBVIEW2_PROCESS_FAILED_KIND_RENDER_PROCESS_EXITED, COREWEBVIEW2_PROCESS_FAILED_REASON,
    };
    use webview2_com::{take_pwstr, ProcessFailedEventHandler};
    use windows::core::{Interface, PWSTR};

    let Some(window) = app_handle.get_webview_window("main") else {
        return;
    };
    let reserved = app_handle
        .state::<RendererRecoveryState>()
        .0
        .lock()
        .map(|mut policy| {
            if policy.installed {
                return false;
            }
            policy.installed = true;
            true
        })
        .unwrap_or(false);
    if !reserved {
        return;
    }

    let install_app = app_handle.clone();
    let dispatch_result = window.with_webview(move |platform_webview| {
        let registration = (|| -> windows::core::Result<()> {
            // COM objects remain inside the WebView's main-thread callback; no raw handles cross threads.
            let webview = unsafe { platform_webview.controller().CoreWebView2()? };
            let mut version_buffer = PWSTR::null();
            let runtime_version = unsafe {
                let version_result = platform_webview
                    .environment()
                    .BrowserVersionString(&mut version_buffer);
                let version = take_pwstr(version_buffer);
                version_result.ok().map(|_| version)
            };
            let event_app = install_app.clone();
            let handler = ProcessFailedEventHandler::create(Box::new(move |_, arguments| {
                let Some(arguments) = arguments else {
                    return Ok(());
                };
                let mut kind = COREWEBVIEW2_PROCESS_FAILED_KIND(-1);
                if unsafe { arguments.ProcessFailedKind(&mut kind) }.is_err() {
                    return Ok(());
                }
                let (reason, exit_code) = arguments
                    .cast::<ICoreWebView2ProcessFailedEventArgs2>()
                    .map(|details| {
                        let mut reason = COREWEBVIEW2_PROCESS_FAILED_REASON(-1);
                        let mut exit_code = 0;
                        unsafe {
                            (
                                details.Reason(&mut reason).ok().map(|_| reason.0),
                                details.ExitCode(&mut exit_code).ok().map(|_| exit_code),
                            )
                        }
                    })
                    .unwrap_or((None, None));
                let outcome = event_app
                    .state::<RendererRecoveryState>()
                    .0
                    .lock()
                    .map(|mut policy| {
                        policy.reserve_reload(
                            kind == COREWEBVIEW2_PROCESS_FAILED_KIND_RENDER_PROCESS_EXITED,
                            existing_backend_is_available(&event_app),
                        )
                    })
                    .unwrap_or("recovery_state_unavailable");
                let diagnostics =
                    event_app.state::<crate::runtime_diagnostics::RuntimeDiagnosticsState>();
                diagnostics.0.record(json!({
                    "event": "webview_process_failed", "process_failed_kind": kind.0,
                    "process_failed_reason": reason, "exit_code": exit_code,
                    "webview2_version": runtime_version, "recovery": outcome,
                    // ProcessFailed does not expose the failed PID. Preserve honest, timestamped candidates.
                    "renderer_pid_candidates": diagnostics.renderer_process_candidates(),
                    "renderer_pid_source": "last_webview_process_sample",
                }));
                if outcome == "reload_reserved" {
                    dispatch_reload(&event_app);
                }
                Ok(())
            }));
            let mut registration_token = 0;
            unsafe {
                webview.add_ProcessFailed(&handler, &mut registration_token)?;
            }
            // The single main WebView owns this callback until its controller is closed.
            Ok(())
        })();
        install_app
            .state::<crate::runtime_diagnostics::RuntimeDiagnosticsState>()
            .0
            .record(json!({
                "event": "renderer_recovery_installed", "succeeded": registration.is_ok(),
            }));
    });
    if dispatch_result.is_err() {
        app_handle
            .state::<crate::runtime_diagnostics::RuntimeDiagnosticsState>()
            .0
            .record(json!({
                "event": "renderer_recovery_installed", "succeeded": false,
            }));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 함수 이름: recovery_rejects_other_failures_missing_backend_and_shutdown()
    /// 기능: 일반 종료·hang·GPU 등 renderer 종료가 아닌 사건에서 reload를 예약하지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/10/04
    #[test]
    fn recovery_rejects_other_failures_missing_backend_and_shutdown() {
        let mut policy = RecoveryPolicy::default();
        assert_eq!(policy.reserve_reload(false, true), "observe_only");
        assert_eq!(
            policy.reserve_reload(true, false),
            "backend_stopping_or_unavailable"
        );
        policy.shutdown_in_progress = true;
        assert_eq!(
            policy.reserve_reload(true, true),
            "backend_stopping_or_unavailable"
        );
        assert_eq!(policy.reload_count, 0);
        policy.shutdown_in_progress = false;
        assert_eq!(policy.reserve_reload(true, true), "renderer_not_ready");
        assert_eq!(policy.reload_count, 0);
    }

    /// 함수 이름: old_renderer_heartbeat_cannot_acknowledge_or_duplicate_reload()
    /// 기능: 중복 실패와 이전 renderer heartbeat가 reload를 반복 예약하지 못하는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/10/04
    #[test]
    fn old_renderer_heartbeat_cannot_acknowledge_or_duplicate_reload() {
        let mut policy = RecoveryPolicy::default();
        assert!(!policy.observe_ready("old-renderer"));
        assert_eq!(policy.reserve_reload(true, true), "reload_reserved");
        assert!(!policy.observe_ready("old-renderer"));
        assert_eq!(policy.reserve_reload(true, true), "reload_already_pending");
        assert!(policy.observe_ready("new-renderer"));
        assert!(!policy.observe_ready("new-renderer"));
        assert_eq!(policy.reload_count, 1);
        assert_eq!(policy.reserve_reload(true, true), "reload_reserved");
        assert!(!policy.observe_ready("old-renderer"));
        assert!(!policy.observe_ready("new-renderer"));
        assert_eq!(policy.reserve_reload(true, true), "reload_already_pending");
        assert!(policy.observe_ready("third-renderer"));
    }

    /// 함수 이름: successful_bootstrap_never_resets_native_lifetime_reload_limit()
    /// 기능: 부팅 직후 반복 충돌이 성공 heartbeat를 보내도 native 실행당 세 번 상한을 지키는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/10/04
    #[test]
    fn successful_bootstrap_never_resets_native_lifetime_reload_limit() {
        let mut policy = RecoveryPolicy::default();
        assert!(!policy.observe_ready("initial-renderer"));
        for attempt in 0..MAX_RENDERER_RELOADS {
            assert_eq!(policy.reserve_reload(true, true), "reload_reserved");
            assert!(policy.observe_ready(&format!("renderer-{attempt}")));
        }
        assert_eq!(policy.reserve_reload(true, true), "reload_limit_reached");
        assert_eq!(policy.reload_count, MAX_RENDERER_RELOADS);
        assert!(policy.failed_renderer_ids.len() <= MAX_RENDERER_RELOADS as usize);
    }
}
