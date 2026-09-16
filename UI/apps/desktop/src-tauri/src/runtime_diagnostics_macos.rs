//! AppKit 메인 스레드에서만 activity와 알림 observer를 소유한다.
use super::{Environment, Shared};
use block2::RcBlock;
use objc2::{rc::Retained, runtime::ProtocolObject, MainThreadMarker};
use objc2_app_kit::{
    NSApplication, NSApplicationDidBecomeActiveNotification,
    NSApplicationDidResignActiveNotification, NSWindow,
    NSWindowDidChangeOcclusionStateNotification, NSWindowOcclusionState, NSWorkspace,
    NSWorkspaceDidWakeNotification, NSWorkspaceScreensDidSleepNotification,
    NSWorkspaceScreensDidWakeNotification, NSWorkspaceWillSleepNotification,
};
use objc2_foundation::{
    ns_string, NSActivityOptions, NSNotification, NSNotificationCenter, NSObjectProtocol,
    NSProcessInfo, NSProcessInfoPowerStateDidChangeNotification,
    NSProcessInfoThermalStateDidChangeNotification,
};
use serde_json::json;
use std::{cell::RefCell, ptr::NonNull, sync::Arc};
use tauri::{AppHandle, Manager};

type Observer = (
    Retained<NSNotificationCenter>,
    Retained<ProtocolObject<dyn NSObjectProtocol>>,
);
thread_local! {
    static ACTIVITY: RefCell<Option<Retained<ProtocolObject<dyn NSObjectProtocol>>>> = const { RefCell::new(None) };
    static OBSERVERS: RefCell<Vec<Observer>> = const { RefCell::new(Vec::new()) };
}

pub fn install(app: AppHandle, shared: Arc<Shared>) {
    let handle = app.clone();
    let fallback = shared.clone();
    if app.run_on_main_thread(move || {
        let Some(mtm)=MainThreadMarker::new() else {return};
        let info=NSProcessInfo::processInfo();
        ACTIVITY.with(|slot| { if slot.borrow().is_none() { *slot.borrow_mut()=Some(info.beginActivityWithOptions_reason(
            NSActivityOptions::UserInitiatedAllowingIdleSystemSleep,ns_string!("Live market monitoring"))); } });
        if let Ok(mut env)=shared.environment.lock() {env.activity_registered=true;}
        shared.record(json!({"event":"platform_started","os_version":info.operatingSystemVersionString().to_string(),
            "activity_registered":true,"requested_policy":"disabled", "policy_supported":info.operatingSystemVersion().majorVersion>=14}));
        if let Some(window)=handle.get_webview_window("main") {
            let owner=shared.clone();
            let _=window.with_webview(move |webview| {
                let info=NSProcessInfo::processInfo();
                let actual=if info.operatingSystemVersion().majorVersion>=14 {
                    let wk=unsafe {&*(webview.inner() as *const objc2_web_kit::WKWebView)};
                    let policy=unsafe {wk.configuration().preferences().inactiveSchedulingPolicy()};
                    Some(match policy.0 {0=>"suspend",1=>"throttle",2=>"disabled",_=>"unknown"})
                } else {None};
                if let Ok(mut env)=owner.environment.lock() {env.actual_policy=actual;}
                owner.record(json!({"event":"execution_policy_readback","requested_policy":"disabled","actual_policy":actual,
                    "status":if actual==Some("disabled"){"verified"}else if actual.is_none(){"unsupported"}else{"mismatch"}}));
            });
        }
        let workspace=NSWorkspace::sharedWorkspace();
        let center=workspace.notificationCenter();
        for (name,event) in unsafe {[(NSWorkspaceWillSleepNotification,"system_will_sleep"),
            (NSWorkspaceDidWakeNotification,"system_did_wake"),(NSWorkspaceScreensDidSleepNotification,"display_did_sleep"),
            (NSWorkspaceScreensDidWakeNotification,"display_did_wake")]} {
            let owner=shared.clone(); let app=handle.clone();
            let block=RcBlock::new(move |_notification:NonNull<NSNotification>| {
                owner.record(json!({"event":event,"source":"NSWorkspace"}));
                if event=="system_did_wake" || event=="display_did_wake" {
                    if let Some(w)=app.get_webview_window("main") { let _=w.eval("window.dispatchEvent(new Event('desktop-resumed'))"); }
                }
            });
            let token=unsafe {center.addObserverForName_object_queue_usingBlock(Some(name),None,None,&block)};
            OBSERVERS.with(|v|v.borrow_mut().push((center.clone(),token)));
        }
        let center=NSNotificationCenter::defaultCenter();
        for (name,event) in unsafe {[(NSApplicationDidBecomeActiveNotification,"app_became_active"),
            (NSApplicationDidResignActiveNotification,"app_resigned_active"),
            (NSProcessInfoPowerStateDidChangeNotification,"power_state_changed"),
            (NSProcessInfoThermalStateDidChangeNotification,"thermal_state_changed")]} {
            let owner=shared.clone(); let app=handle.clone();
            let block=RcBlock::new(move |_notification:NonNull<NSNotification>| {
                observe_environment(&app, &owner, event);
            });
            let token=unsafe {center.addObserverForName_object_queue_usingBlock(Some(name),None,None,&block)};
            OBSERVERS.with(|list|list.borrow_mut().push((center.clone(),token)));
        }
        if let Some(window)=handle.get_webview_window("main") {
            if let Ok(pointer)=window.ns_window() {
                let native=unsafe {&*(pointer as *const NSWindow)};
                let owner=shared.clone(); let app=handle.clone();
                let block=RcBlock::new(move |_n:NonNull<NSNotification>| {
                    observe_environment(&app, &owner, "occlusion_changed");
                });
                let token=unsafe {center.addObserverForName_object_queue_usingBlock(Some(NSWindowDidChangeOcclusionStateNotification),Some(native),None,&block)};
                OBSERVERS.with(|v|v.borrow_mut().push((center.clone(),token)));
            }
        }
        let _=mtm;
    }).is_err() { fallback.record(json!({"event":"platform_registration_failed"})); }
}

fn observe_environment(app: &AppHandle, shared: &Shared, event: &str) {
    let mut environment = shared
        .environment
        .try_lock()
        .ok()
        .map(|state| state.clone())
        .unwrap_or_default();
    sample(app, &mut environment);
    shared.record(json!({"event":event,"environment":environment}));
    if let Ok(mut state) = shared.environment.try_lock() {
        *state = environment;
    }
}

pub fn sample(app: &AppHandle, env: &mut Environment) {
    let Some(mtm) = MainThreadMarker::new() else {
        return;
    };
    let info = NSProcessInfo::processInfo();
    env.app_active = Some(NSApplication::sharedApplication(mtm).isActive());
    env.thermal_state = Some(info.thermalState().0 as i64);
    if info.operatingSystemVersion().majorVersion >= 12 {
        env.low_power = Some(info.isLowPowerModeEnabled());
    }
    if let Some(window) = app.get_webview_window("main") {
        if let Ok(pointer) = window.ns_window() {
            let native = unsafe { &*(pointer as *const NSWindow) };
            env.occluded = Some(
                !native
                    .occlusionState()
                    .contains(NSWindowOcclusionState::Visible),
            );
            env.minimized = Some(native.isMiniaturized());
        }
    }
}

pub fn stop() {
    if MainThreadMarker::new().is_none() {
        return;
    }
    OBSERVERS.with(|list| {
        for (center, token) in list.borrow_mut().drain(..) {
            unsafe {
                center.removeObserver(AsRef::<objc2::runtime::AnyObject>::as_ref(&*token));
            }
        }
    });
    ACTIVITY.with(|slot| {
        if let Some(activity) = slot.borrow_mut().take() {
            unsafe {
                NSProcessInfo::processInfo().endActivity(&activity);
            }
        }
    });
}
