//! 별도 프로필·읽기 전용 fixture에서 실제 ProcessFailed와 기존 bootstrap 복구를 검증한다.

#[path = "smoke_capture.rs"]
mod smoke_capture;

use super::{install, RendererRecoveryState};
use crate::{backend_connection_diagnostics, chart_diagnostics, runtime_diagnostics, sidecar};
use serde::Deserialize;
use serde_json::json;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder};
use zeroize::Zeroizing;

/// 클래스 이름: RendererSoakSample
/// 기능: payload·속성 값 없이 제한된 수치만 받는 렌더링 부하 검증 DTO이다.
/// 작성 날짜: 2026/10/04
#[derive(Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RendererSoakSample {
    elapsed_ms: u64,
    ticks: u64,
    candle_count: u64,
    dom_nodes: u64,
    measure_count: u64,
    generated_measure_detail_characters: u64,
    js_heap_used_bytes: Option<u64>,
    js_heap_total_bytes: Option<u64>,
    cleanup_enabled: bool,
    development_build: bool,
    errors: u64,
}

/// 함수 이름: record_renderer_soak_sample()
/// 기능: opt-in 검증 창의 수치형 sample을 기존 제한 진단 큐로 전달한다.
/// 인자: window -> 검증 창, state -> 진단 상태, sample -> 수치만 가진 계측
/// 반환값: 검증·전송 성공 또는 고정 오류 코드
/// 작성 날짜: 2026/10/04
#[tauri::command]
pub(crate) fn record_renderer_soak_sample(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, runtime_diagnostics::RuntimeDiagnosticsState>,
    sample: RendererSoakSample,
) -> Result<(), &'static str> {
    if window.label() != "main"
        || !sidecar::is_trusted_renderer_url(
            &window.url().map_err(|_| "INVALID_SMOKE_WINDOW")?,
            false,
        )
    {
        return Err("INVALID_SMOKE_WINDOW");
    }
    state
        .0
        .record(json!({"event":"renderer_soak_sample","sample":sample}));
    Ok(())
}

/// 클래스 이름: FixtureOwner
/// 기능: 검증 시작 실패 시에도 이 모듈이 생성한 Python 자식만 정리한다.
/// 작성 날짜: 2026/10/04
struct FixtureOwner(Option<Child>);

impl Drop for FixtureOwner {
    /// 함수 이름: drop()
    /// 기능: lifecycle로 인계하지 못한 검증 자식을 정리한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/10/04
    fn drop(&mut self) {
        if let Some(child) = self.0.as_mut() {
            child.stdin.take();
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// 클래스 이름: FixtureCleanup
/// 기능: event loop 종료·실패 후 검증 자식의 정리를 보장한다.
/// 작성 날짜: 2026/10/04
struct FixtureCleanup(sidecar::SidecarProcessState);

impl Drop for FixtureCleanup {
    /// 함수 이름: drop()
    /// 기능: 검증 전용 lifecycle의 직접 자식을 정리한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/10/04
    fn drop(&mut self) {
        self.0.stop_recovery_fixture();
    }
}

/// 함수 이름: start_fixture()
/// 기능: 외부 거래 클라이언트 없는 기존 fixture를 실행하고 비공개 pipe에서 descriptor를 읽는다.
/// 인자: output -> 새 검증 결과 디렉터리
/// 반환값: cleanup owner, 검증된 descriptor, 직접 자식 PID
/// 작성 날짜: 2026/10/04
fn start_fixture(output: &Path) -> (FixtureOwner, crate::BackendConnectionDescriptor, u32) {
    use std::os::windows::process::CommandExt;
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../..");
    let stderr = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output.join("fixture-stderr.log"))
        .expect("fixture log unavailable");
    let mut command = Command::new(root.join("backend/.venv/Scripts/python.exe"));
    command
        .arg(root.join("scripts/backend_connection_soak_fixture.py"))
        .arg(output.join("backend"))
        .env_clear()
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(stderr)
        .creation_flags(0x08000000);
    for name in ["SystemRoot", "WINDIR", "TEMP", "TMP"] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    command
        .env("BINANCE_RUN_TESTNET", "0")
        .env("BINANCE_RUN_TESTNET_ORDERS", "0");
    let mut fixture = FixtureOwner(Some(command.spawn().expect("fixture spawn failed")));
    let child = fixture.0.as_mut().unwrap();
    let fixture_pid = child.id();
    let stdout = child.stdout.take().unwrap();
    let (sender, receiver) = std::sync::mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut line = Zeroizing::new(String::new());
        let result = BufReader::new(stdout).read_line(&mut line).map(|_| line);
        let _ = sender.send(result);
    });
    let line = receiver
        .recv_timeout(Duration::from_secs(10))
        .expect("fixture timeout")
        .expect("fixture descriptor unavailable");
    let mut value: serde_json::Value = serde_json::from_str(&line).expect("invalid fixture shape");
    let descriptor = crate::BackendConnectionDescriptor::new(
        value["port"].as_u64().unwrap() as u16,
        value["session_id"].as_str().unwrap().to_owned(),
        value["schema_version"].as_u64().unwrap() as u32,
        value["token"].take().as_str().unwrap().to_owned(),
    )
    .unwrap_or_else(|_| panic!("invalid fixture descriptor"));
    (fixture, descriptor, fixture_pid)
}

/// 함수 이름: wait_until()
/// 기능: 한정된 시간 동안 검증 조건을 기다린다.
/// 인자: predicate -> native state 검사, seconds -> 최대 대기 시간
/// 반환값: 제한 내 조건 충족 여부
/// 작성 날짜: 2026/10/04
fn wait_until(mut predicate: impl FnMut() -> bool, seconds: u64) -> bool {
    let started = Instant::now();
    while started.elapsed() < Duration::from_secs(seconds) {
        if predicate() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    false
}

/// 함수 이름: crash_fixture_renderer()
/// 기능: 검증 전용 WebView의 CDP Page.crash로 실제 renderer 종료를 일으킨다.
/// 인자: app -> 격리된 검증 앱
/// 반환값: main 스레드 예약 성공 여부
/// 작성 날짜: 2026/10/04
fn crash_fixture_renderer(app: &AppHandle) -> bool {
    use webview2_com::CallDevToolsProtocolMethodCompletedHandler;
    use windows::core::w;
    let fault_state = app
        .state::<runtime_diagnostics::RuntimeDiagnosticsState>()
        .inner()
        .clone();
    app.get_webview_window("main").is_some_and(|window| {
        window
            .with_webview(move |platform| {
                if let Ok(webview) = unsafe { platform.controller().CoreWebView2() } {
                    // Crash는 응답 전에 renderer를 종료하므로 completion callback은 결과 판정에 사용하지 않는다.
                    let completion_state = fault_state.clone();
                    let callback = CallDevToolsProtocolMethodCompletedHandler::create(Box::new(move |result, _| {
                        completion_state.0.record(json!({"event":"smoke_crash_command_completed",
                            "succeeded":result.is_ok(),"error_code":result.err().map(|error|error.code().0)}));
                        Ok(())
                    }));
                    let result = unsafe {
                        webview.CallDevToolsProtocolMethod(w!("Page.crash"), w!("{}"), &callback)
                    };
                    fault_state.0.record(json!({"event":"smoke_crash_command_dispatched",
                        "succeeded":result.is_ok(),"error_code":result.err().map(|error|error.code().0)}));
                }
            })
            .is_ok()
    })
}

/// 함수 이름: current_renderer()
/// 기능: 실제 같은-session heartbeat로 확인한 renderer ID만 읽는다.
/// 인자: app -> 검증 앱
/// 반환값: 현재 renderer ID와 복구 시도 횟수·대기 상태
/// 작성 날짜: 2026/10/04
fn current_renderer(app: &AppHandle) -> (Option<String>, u8, bool) {
    let state = app.state::<RendererRecoveryState>();
    let policy = state.0.lock().unwrap();
    (
        policy.last_renderer_id.clone(),
        policy.reload_count,
        policy.reload_pending,
    )
}

/// 함수 이름: verify_scenario()
/// 기능: 실제 crash·정상 종료 gate·종료된 backend·재시도 상한을 격리 프로필에서 검사한다.
/// 인자: app -> 검증 앱, scenario -> 지정 시나리오, seconds -> soak 실행 시간, output -> 검증 결과 경로, capture -> 고정 화면 저장 여부
/// 반환값: 검증 성공 여부와 실패 단계
/// 작성 날짜: 2026/10/04
fn verify_scenario(
    app: &AppHandle,
    scenario: &str,
    seconds: u64,
    output: &Path,
    capture: bool,
) -> (bool, &'static str) {
    if !wait_until(|| current_renderer(app).0.is_some(), 25) {
        return (false, "initial_bootstrap_timeout");
    }
    if capture {
        std::thread::sleep(Duration::from_secs(2));
        if !app
            .get_webview_window("main")
            .is_some_and(|window| smoke_capture::capture_fixture(&window, output.to_owned()))
        {
            return (false, "fixture_capture_failed");
        }
    }
    if scenario == "soak" {
        let started = Instant::now();
        while started.elapsed() < Duration::from_secs(seconds) {
            if output.join("stop-request.json").is_file() {
                return (false, "soak_stopped_by_guard");
            }
            std::thread::sleep(Duration::from_secs(1));
        }
        let (_, count, pending) = current_renderer(app);
        return (count == 0 && !pending, "soak_completed");
    }
    if scenario == "shutdown" {
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.eval("window.__TAURI_INTERNALS__.invoke('set_renderer_recovery_shutdown',{inProgress:true})");
        }
        if !wait_until(
            || {
                app.state::<RendererRecoveryState>()
                    .0
                    .lock()
                    .unwrap()
                    .shutdown_in_progress
            },
            5,
        ) {
            return (false, "shutdown_gate_timeout");
        }
    }
    if scenario == "stopped" {
        app.state::<sidecar::SidecarProcessState>()
            .stop_recovery_fixture();
        if app.state::<sidecar::SidecarProcessState>().is_running() {
            return (false, "fixture_exit_timeout");
        }
    }
    let crashes = if scenario == "limit" { 4 } else { 1 };
    for attempt in 1..=crashes {
        let previous_renderer = current_renderer(app).0;
        let previous_failures = app
            .state::<RendererRecoveryState>()
            .0
            .lock()
            .unwrap()
            .process_failed_count;
        if !crash_fixture_renderer(app) {
            return (false, "fault_dispatch_failed");
        }
        if !wait_until(
            || {
                app.state::<RendererRecoveryState>()
                    .0
                    .lock()
                    .unwrap()
                    .process_failed_count
                    > previous_failures
            },
            25,
        ) {
            return (false, "process_failed_event_timeout");
        }
        if scenario == "recover" || (scenario == "limit" && attempt <= 3) {
            if !wait_until(
                || {
                    let (renderer, count, pending) = current_renderer(app);
                    renderer.is_some()
                        && renderer != previous_renderer
                        && count == attempt
                        && !pending
                },
                25,
            ) {
                return (false, "recovery_timeout");
            }
        } else {
            std::thread::sleep(Duration::from_secs(8));
            let (renderer, count, _) = current_renderer(app);
            let expected_count = if scenario == "limit" { 3 } else { 0 };
            if renderer != previous_renderer || count != expected_count {
                return (false, "unexpected_reload");
            }
        }
    }
    (true, "scenario_completed")
}

/// 함수 이름: run()
/// 기능: 운영 앱과 분리한 read-only fixture·프로필·결과 경로에서 opt-in 실제 복구 검증을 실행한다.
/// 인자: --output NEW_ABSOLUTE_DIRECTORY, --scenario recover|limit|shutdown|stopped|soak, optional --seconds --entry --devtools --capture
/// 반환값: 실패 시 exit code 1, 성공 시 0
/// 작성 날짜: 2026/10/04
pub fn run() {
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    let option = |key: &str| {
        arguments
            .windows(2)
            .find(|pair| pair[0] == key)
            .map(|pair| pair[1].clone())
    };
    let output = PathBuf::from(option("--output").expect("--output required"));
    assert!(output.is_absolute(), "output must be absolute");
    std::fs::create_dir(&output).expect("output must be new");
    let scenario = option("--scenario").unwrap_or_else(|| "recover".to_owned());
    assert!(["recover", "limit", "shutdown", "stopped", "soak"].contains(&scenario.as_str()));
    let seconds: u64 = option("--seconds")
        .unwrap_or_else(|| "60".to_owned())
        .parse()
        .unwrap();
    assert!((20..=172800).contains(&seconds));
    let entry = option("--entry").unwrap_or_else(|| "index.html".to_owned());
    assert!(["index.html", "renderer-soak.html"].contains(&entry.as_str()));
    let devtools = arguments.iter().any(|argument| argument == "--devtools");
    let capture = arguments.iter().any(|argument| argument == "--capture");
    let (mut fixture, descriptor, fixture_pid) = start_fixture(&output);
    let backend_identity_before =
        runtime_diagnostics::probe_backend_identity_for_smoke(&descriptor)
            .expect("fixture identity unavailable");
    let session_id = descriptor.session_id.clone();
    let descriptor_state = crate::BackendConnectionDescriptorState::default();
    descriptor_state
        .stage(descriptor)
        .unwrap_or_else(|_| panic!("fixture stage failed"));
    let process_state = sidecar::SidecarProcessState::default();
    let cleanup = FixtureCleanup(process_state.clone());
    let output_for_setup = output.clone();
    let result_path = output.join("result.json");
    let fixture_child = Mutex::new(fixture.0.take());
    let app = tauri::Builder::default()
        .manage(descriptor_state).manage(process_state).manage(RendererRecoveryState::default())
        .manage(runtime_diagnostics::RuntimeDiagnosticsState::default())
        .manage(backend_connection_diagnostics::BackendConnectionDiagnosticsState::in_directory(output.join("ui")))
        .manage(chart_diagnostics::ChartDiagnosticsState::in_directory(output.join("chart")))
        .invoke_handler(tauri::generate_handler![crate::get_backend_connection_descriptor,
            runtime_diagnostics::record_renderer_heartbeat,
            backend_connection_diagnostics::record_backend_connection_diagnostics,
            chart_diagnostics::record_chart_diagnostics,
            record_renderer_soak_sample,
            super::set_renderer_recovery_shutdown])
        .setup(move |app| {
            app.state::<sidecar::SidecarProcessState>().install_recovery_fixture(
                fixture_child.lock().unwrap().take().unwrap(), app.handle().clone())?;
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App(entry.into()))
                .title("주문 없는 renderer 복구 검증").inner_size(1440.0, 1024.0)
                .data_directory(output_for_setup.join("webview-profile"))
                .additional_browser_args(sidecar::WINDOWS_BROWSER_ARGUMENTS)
                .initialization_script(if capture { "window.__RENDERER_SOAK_FREEZE_AFTER_TICKS__ = 4;" } else { "" })
                .on_navigation(|url| sidecar::is_trusted_renderer_url(url, false))
                .visible(false).build()?;
            if devtools { window.open_devtools(); }
            app.state::<runtime_diagnostics::RuntimeDiagnosticsState>()
                .start_in_directory(app.handle(), output_for_setup.join("native"));
            install(app.handle());
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let (passed, stage) = verify_scenario(&handle, &scenario, seconds, &output_for_setup, capture);
                let (renderer_id, reload_count, reload_pending) = current_renderer(&handle);
                let same_session = handle.state::<crate::BackendConnectionDescriptorState>().get()
                    .is_ok_and(|current| current.session_id == session_id);
                let backend_alive = handle.state::<sidecar::SidecarProcessState>().is_running();
                let backend_identity_after = handle.state::<crate::BackendConnectionDescriptorState>().get().ok()
                    .and_then(|current|runtime_diagnostics::probe_backend_identity_for_smoke(&current).ok());
                let same_backend_identity = backend_identity_after.as_ref() == Some(&backend_identity_before);
                let passed = passed && (scenario == "stopped" || same_session && backend_alive && same_backend_identity);
                let result = json!({"passed":passed, "stage":stage, "scenario":scenario,
                    "launcher_child_pid":fixture_pid,"backend_pid":backend_identity_before.0,
                    "backend_process_start_id":backend_identity_before.1,"same_backend_identity":same_backend_identity,
                    "session_id":session_id,"same_session":same_session,
                    "backend_alive":backend_alive,"renderer_id":renderer_id,"reload_count":reload_count,
                    "reload_pending":reload_pending,"devtools":devtools,
                    "devtools_open":handle.get_webview_window("main").map(|window| window.is_devtools_open()),
                    "orders_enabled":false});
                handle.state::<runtime_diagnostics::RuntimeDiagnosticsState>().0.record(json!({
                    "event":"renderer_recovery_smoke_result","result":result,
                }));
                std::fs::write(result_path, serde_json::to_vec_pretty(&result).unwrap()).expect("result write failed");
                handle.exit(if passed { 0 } else { 1 });
            });
            Ok(())
        })
        .build(tauri::generate_context!("tauri.renderer-recovery.conf.json")).expect("smoke WebView failed");
    let exit_code = app.run_return(|handle, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            handle
                .state::<runtime_diagnostics::RuntimeDiagnosticsState>()
                .stop();
        }
    });
    drop(cleanup);
    std::process::exit(exit_code);
}
