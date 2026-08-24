//! Renderer listener 준비 전에도 macOS close/quit intent를 잃지 않는 native bridge를 소유한다.

use serde::Serialize;
use std::sync::{Arc, Mutex, MutexGuard};
use tauri::{AppHandle, Emitter, State};

pub const NATIVE_EXIT_REQUESTED_EVENT: &str = "native-exit-requested";

/// Main window close와 application quit을 renderer에 exact string으로 전달하는 source이다.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum NativeExitIntentSource {
    Window,
    Application,
}

/// Native close/quit event에 사용하는 secret-free exact payload이다.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct NativeExitIntentPayload {
    pub source: NativeExitIntentSource,
}

/// Renderer가 event listener를 설치했음을 확인하는 exact command receipt이다.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct NativeExitIntentArmReceipt {
    pub armed: bool,
}

/// Bridge lock/event 오류 상세를 renderer에 반사하지 않는 typed failure이다.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct NativeExitIntentFailure {
    pub code: &'static str,
    pub message: &'static str,
}

impl NativeExitIntentFailure {
    /// 함수 이름: unavailable()
    /// 기능: latch 또는 event emit 실패를 고정 typed failure로 만든다.
    /// 인자: 없음
    /// 반환값: NATIVE_EXIT_INTENT_BRIDGE_UNAVAILABLE failure
    /// 작성 날짜: 2026/08/24
    fn unavailable() -> Self {
        Self {
            code: "NATIVE_EXIT_INTENT_BRIDGE_UNAVAILABLE",
            message: "Native exit intent bridge is unavailable.",
        }
    }
}

/// Listener arm 여부와 pre-bootstrap intent 하나를 같은 lock으로 보호한다.
#[derive(Default)]
struct NativeExitIntentLifecycle {
    armed: bool,
    pending_source: Option<NativeExitIntentSource>,
}

/// Tauri window/app callback과 renderer arm command가 공유하는 clone 가능 bridge state이다.
#[derive(Clone, Default)]
pub struct NativeExitIntentBridgeState {
    lifecycle: Arc<Mutex<NativeExitIntentLifecycle>>,
}

impl NativeExitIntentBridgeState {
    /// 함수 이름: request()
    /// 기능: listener arm 전 intent는 latch하고 arm 후 intent는 main renderer에 즉시 emit한다.
    /// 인자: app_handle -> main window event emitter, source -> window/application source
    /// 반환값: latch/emit 성공 또는 typed failure
    /// 작성 날짜: 2026/08/24
    pub fn request(
        &self,
        app_handle: &AppHandle,
        source: NativeExitIntentSource,
    ) -> Result<(), NativeExitIntentFailure> {
        let should_emit = {
            let mut lifecycle = self.lock_lifecycle()?;
            if lifecycle.armed {
                true
            } else {
                // Listener 전 연속 OS intent는 첫 source 하나로 합쳐 bootstrap 후 한 번만 전달한다.
                lifecycle.pending_source.get_or_insert(source);
                false
            }
        };

        if should_emit {
            self.emit_or_restore(app_handle, source)?;
        }
        Ok(())
    }

    /// 함수 이름: arm()
    /// 기능: renderer listener를 armed로 publish하고 pre-bootstrap pending intent를 event로 flush한다.
    /// 인자: app_handle -> main window event emitter
    /// 반환값: exact armed receipt 또는 typed failure
    /// 작성 날짜: 2026/08/24
    fn arm(
        &self,
        app_handle: &AppHandle,
    ) -> Result<NativeExitIntentArmReceipt, NativeExitIntentFailure> {
        let pending_source = {
            let mut lifecycle = self.lock_lifecycle()?;
            lifecycle.armed = true;
            lifecycle.pending_source.take()
        };

        // Renderer가 listen을 먼저 완료한 command에서만 latched event를 emit해 startup race를 닫는다.
        if let Some(source) = pending_source {
            self.emit_or_restore(app_handle, source)?;
        }
        Ok(NativeExitIntentArmReceipt { armed: true })
    }

    /// 함수 이름: emit_or_restore()
    /// 기능: main renderer event emit이 실패하면 intent를 latch에 복원해 재-arm으로 복구할 수 있게 한다.
    /// 인자: app_handle -> Tauri emitter, source -> 전달할 exact source
    /// 반환값: emit 성공 또는 typed failure
    /// 작성 날짜: 2026/08/24
    fn emit_or_restore(
        &self,
        app_handle: &AppHandle,
        source: NativeExitIntentSource,
    ) -> Result<(), NativeExitIntentFailure> {
        let payload = NativeExitIntentPayload { source };
        if app_handle
            .emit_to("main", NATIVE_EXIT_REQUESTED_EVENT, payload)
            .is_ok()
        {
            return Ok(());
        }

        let mut lifecycle = self.lock_lifecycle()?;
        lifecycle.pending_source.get_or_insert(source);
        Err(NativeExitIntentFailure::unavailable())
    }

    /// 함수 이름: lock_lifecycle()
    /// 기능: poison 상세를 반사하지 않고 bridge lifecycle guard를 얻는다.
    /// 인자: 없음
    /// 반환값: mutex guard 또는 typed failure
    /// 작성 날짜: 2026/08/24
    fn lock_lifecycle(
        &self,
    ) -> Result<MutexGuard<'_, NativeExitIntentLifecycle>, NativeExitIntentFailure> {
        self.lifecycle
            .lock()
            .map_err(|_| NativeExitIntentFailure::unavailable())
    }
}

/// 함수 이름: arm_native_exit_intent_bridge()
/// 기능: renderer event listener 설치를 publish하고 latched close/quit intent를 순서대로 전달한다.
/// 인자: app_handle -> event emitter, state -> native intent bridge
/// 반환값: exact armed receipt 또는 typed failure
/// 작성 날짜: 2026/08/24
#[tauri::command]
pub fn arm_native_exit_intent_bridge(
    app_handle: AppHandle,
    state: State<'_, NativeExitIntentBridgeState>,
) -> Result<NativeExitIntentArmReceipt, NativeExitIntentFailure> {
    state.arm(&app_handle)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 함수 이름: record_for_test()
    /// 기능: Tauri emitter 없이 pre-bootstrap latch rule를 deterministic하게 검증할 intent를 기록한다.
    /// 인자: state -> bridge state, source -> test intent source
    /// 반환값: armed 상태에서 emit이 필요하면 true
    /// 작성 날짜: 2026/08/24
    fn record_for_test(
        state: &NativeExitIntentBridgeState,
        source: NativeExitIntentSource,
    ) -> bool {
        let mut lifecycle = state
            .lifecycle
            .lock()
            .expect("test bridge lock must remain available");
        if lifecycle.armed {
            true
        } else {
            lifecycle.pending_source.get_or_insert(source);
            false
        }
    }

    /// 함수 이름: arm_for_test()
    /// 기능: Tauri emitter 없이 listener arm이 pending source를 한 번만 꺼내는지 검증한다.
    /// 인자: state -> bridge state
    /// 반환값: arm 시 flush할 optional source
    /// 작성 날짜: 2026/08/24
    fn arm_for_test(state: &NativeExitIntentBridgeState) -> Option<NativeExitIntentSource> {
        let mut lifecycle = state
            .lifecycle
            .lock()
            .expect("test bridge lock must remain available");
        lifecycle.armed = true;
        lifecycle.pending_source.take()
    }

    /// 함수 이름: pre_bootstrap_window_close_is_latched_until_arm()
    /// 기능: JS listener 전 window close가 잃히지 않고 arm 후 한 번만 flush되는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn pre_bootstrap_window_close_is_latched_until_arm() {
        let state = NativeExitIntentBridgeState::default();

        assert!(!record_for_test(&state, NativeExitIntentSource::Window));
        assert_eq!(arm_for_test(&state), Some(NativeExitIntentSource::Window));
        assert_eq!(arm_for_test(&state), None);
    }

    /// 함수 이름: command_q_before_arm_keeps_first_pending_intent()
    /// 기능: pre-bootstrap close/Command-Q burst가 latch를 overwrite하거나 중복 workflow를 만들지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn command_q_before_arm_keeps_first_pending_intent() {
        let state = NativeExitIntentBridgeState::default();

        assert!(!record_for_test(
            &state,
            NativeExitIntentSource::Application
        ));
        assert!(!record_for_test(&state, NativeExitIntentSource::Window));
        assert_eq!(
            arm_for_test(&state),
            Some(NativeExitIntentSource::Application)
        );
        assert!(record_for_test(&state, NativeExitIntentSource::Window));
    }

    /// 함수 이름: payload_has_exact_secret_free_shape()
    /// 기능: renderer event payload가 source 하나의 exact shape만 가지는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn payload_has_exact_secret_free_shape() {
        let payload = NativeExitIntentPayload {
            source: NativeExitIntentSource::Application,
        };
        let serialized =
            serde_json::to_value(payload).expect("native exit intent payload must serialize");
        let object = serialized
            .as_object()
            .expect("native exit intent payload must be an object");

        assert_eq!(object.len(), 1);
        assert_eq!(serialized["source"], "application");
    }
}
