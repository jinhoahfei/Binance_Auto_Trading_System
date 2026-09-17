//! AppKit quit AppleEvent를 기존 renderer 안전 종료 lifecycle 앞에서 차단한다.

use crate::exit_bridge::{NativeExitIntentBridgeState, NativeExitIntentSource};
use objc2::ffi::class_addMethod;
use objc2::runtime::{AnyClass, AnyObject, Imp, Sel};
use objc2::{sel, MainThreadMarker};
use objc2_app_kit::{NSApplication, NSApplicationTerminateReply};
use std::error::Error;
use std::ffi::CStr;
use std::fmt;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::OnceLock;
use tauri::{AppHandle, Manager};

// macOS 11 이상은 64-bit NSUInteger를 사용하므로 반환/receiver/selector/인자 encoding은 Q@:@이다.
const APPLICATION_SHOULD_TERMINATE_TYPES: &CStr = c"Q@:@";

/// AppKit delegate callback이 Tauri event emitter를 다시 찾을 때 사용하는 process-lifetime context이다.
struct MacOsQuitGuardContext {
    app_handle: AppHandle,
    exit_intent_bridge: NativeExitIntentBridgeState,
}

/// 한 process에 정확히 한 번 설치하는 AppKit quit gate context이다.
static MACOS_QUIT_GUARD_CONTEXT: OnceLock<MacOsQuitGuardContext> = OnceLock::new();

/// AppKit quit gate 설치 실패 상세를 노출하지 않는 fixed native failure이다.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct MacOsQuitGuardInstallFailure;

impl fmt::Display for MacOsQuitGuardInstallFailure {
    /// 함수 이름: fmt()
    /// 기능: Objective-C class나 selector 상세를 숨긴 고정 설치 실패 문구를 쓴다.
    /// 인자: formatter -> Rust error display target
    /// 반환값: formatting 결과
    /// 작성 날짜: 2026/08/24
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("macOS quit guard could not be installed safely")
    }
}

impl Error for MacOsQuitGuardInstallFailure {}


/// 함수 이름: cancel_after_routing()
/// 기능: routing 성공·실패·panic과 무관하게 AppKit 정상 종료를 fail closed한다.
/// 인자: route_exit_intent -> 기존 renderer 안전 종료 bridge로 intent를 전달하는 작업
/// 반환값: NSTerminateCancel
/// 작성 날짜: 2026/08/24
fn cancel_after_routing<RouteExitIntent>(
    route_exit_intent: RouteExitIntent,
) -> NSApplicationTerminateReply
where
    RouteExitIntent: FnOnce(),
{
    // Rust panic이 Objective-C ABI를 넘어가지 않게 격리하고 종료 취소 결정을 항상 보존한다.
    let _ = catch_unwind(AssertUnwindSafe(route_exit_intent));
    NSApplicationTerminateReply::TerminateCancel
}


/// 함수 이름: application_should_terminate()
/// 기능: Command-Q, Dock Quit, AppleEvent Quit을 취소하고 기존 application exit intent로 단일화한다.
/// 인자: delegate -> 현재 Tao NSApplicationDelegate,
///      selector -> applicationShouldTerminate: selector,
///      application -> 종료를 요청받은 NSApplication
/// 반환값: renderer가 정상 sidecar shutdown을 끝낼 때까지 NSTerminateCancel
/// 작성 날짜: 2026/08/24
unsafe extern "C-unwind" fn application_should_terminate(
    _delegate: &AnyObject,
    _selector: Sel,
    _application: &NSApplication,
) -> NSApplicationTerminateReply {
    cancel_after_routing(|| {
        let Some(context) = MACOS_QUIT_GUARD_CONTEXT.get() else {
            return;
        };

        // AppKit quit도 window close와 동일한 native latch를 거쳐 renderer 상태 머신만 실행한다.
        let _ = context
            .exit_intent_bridge
            .request(&context.app_handle, NativeExitIntentSource::Application);
    })
}


/// 함수 이름: method_implementation()
/// 기능: typed Objective-C callback을 class_addMethod가 요구하는 opaque IMP로 변환한다.
/// 인자: 없음
/// 반환값: applicationShouldTerminate: callback IMP
/// 작성 날짜: 2026/08/24
fn method_implementation() -> Imp {
    let callback: unsafe extern "C-unwind" fn(
        &AnyObject,
        Sel,
        &NSApplication,
    ) -> NSApplicationTerminateReply = application_should_terminate;

    // IMP는 같은 Objective-C ABI 함수 포인터의 type-erased 표현이며 selector encoding을 별도로 등록한다.
    unsafe { std::mem::transmute(callback) }
}


/// 함수 이름: install_macos_quit_guard()
/// 기능: sidecar 시작 전에 현재 Tao application delegate에 fail-closed quit callback을 추가한다.
/// 인자: app_handle -> managed exit bridge와 Tauri emitter owner
/// 반환값: 정확히 한 번 설치 성공 또는 secret 없는 fixed failure
/// 작성 날짜: 2026/08/24
pub fn install_macos_quit_guard(
    app_handle: &AppHandle,
) -> Result<(), MacOsQuitGuardInstallFailure> {
    // AppKit main thread와 현재 Tao delegate, 기존 native exit bridge를 설치 입력으로 고정한다.
    let main_thread_marker = MainThreadMarker::new().ok_or(MacOsQuitGuardInstallFailure)?;
    let application = NSApplication::sharedApplication(main_thread_marker);
    let delegate = application.delegate().ok_or(MacOsQuitGuardInstallFailure)?;
    let exit_intent_bridge = app_handle
        .state::<NativeExitIntentBridgeState>()
        .inner()
        .clone();
    let context = MacOsQuitGuardContext {
        app_handle: app_handle.clone(),
        exit_intent_bridge,
    };

    // 같은 main-thread setup turn에서 selector를 먼저 추가해 실패한 install이 global context를 남기지 않게 한다.
    let delegate_object: &AnyObject = AsRef::<AnyObject>::as_ref(&*delegate);
    let delegate_class = delegate_object.class() as *const AnyClass as *mut AnyClass;
    let method_was_added = unsafe {
        class_addMethod(
            delegate_class,
            sel!(applicationShouldTerminate:),
            method_implementation(),
            APPLICATION_SHOULD_TERMINATE_TYPES.as_ptr(),
        )
        .as_bool()
    };

    // Upstream 또는 다른 plugin이 같은 selector를 소유하면 조용히 덮어쓰지 않고 startup을 차단한다.
    if !method_was_added {
        return Err(MacOsQuitGuardInstallFailure);
    }

    // Main-thread turn을 반환하기 전에 context를 publish해 다음 AppKit quit callback이 exact bridge를 찾게 한다.
    MACOS_QUIT_GUARD_CONTEXT
        .set(context)
        .map_err(|_| MacOsQuitGuardInstallFailure)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use objc2::ffi::NSUInteger;
    use std::sync::atomic::{AtomicBool, Ordering};

    /// 함수 이름: routing_always_returns_terminate_cancel()
    /// 기능: 정상 route helper가 실행돼도 terminate gate를 열지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn routing_always_returns_terminate_cancel() {
        // Route 실행 여부와 helper 반환값을 독립적으로 관찰할 fixture를 준비한다.
        let route_was_called = AtomicBool::new(false);

        // 정상 route를 한 번 실행한 뒤 AppKit terminate reply를 수집한다.
        let reply = cancel_after_routing(|| {
            route_was_called.store(true, Ordering::SeqCst);
        });

        // Route 실행과 fail-closed cancel 결정을 함께 고정한다.
        assert!(route_was_called.load(Ordering::SeqCst));
        assert_eq!(reply, NSApplicationTerminateReply::TerminateCancel);
    }

    /// 함수 이름: routing_panic_cannot_open_the_appkit_exit_gate()
    /// 기능: bridge routing panic도 Objective-C 경계를 넘거나 즉시 종료 허용으로 바뀌지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn routing_panic_cannot_open_the_appkit_exit_gate() {
        // Panic을 발생시키는 route도 unwind 격리 뒤 cancel reply를 반환해야 한다.
        let reply = cancel_after_routing(|| panic!("simulated exit routing panic"));

        assert_eq!(reply, NSApplicationTerminateReply::TerminateCancel);
    }

    /// 함수 이름: configured_method_encoding_uses_64_bit_terminate_shape()
    /// 기능: 등록할 Objective-C encoding과 NSUInteger 크기의 64-bit 전제를 고정한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/24
    #[test]
    fn configured_method_encoding_uses_64_bit_terminate_shape() {
        // Target word size와 고정 CStr을 함께 검사해 서로 다른 ABI 전제의 혼입을 막는다.
        assert_eq!(std::mem::size_of::<NSUInteger>(), 8);
        assert_eq!(APPLICATION_SHOULD_TERMINATE_TYPES.to_bytes(), b"Q@:@");
    }
}
