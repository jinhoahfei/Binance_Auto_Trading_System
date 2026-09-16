//! 별도 identifier·주문 없는 fixture·독립 산출물만 사용하는 실제 WebView 검증 앱.
use crate::{
    backend_connection_diagnostics, chart_diagnostics, runtime_diagnostics, sidecar,
    BackendConnectionDescriptor, BackendConnectionDescriptorState,
};
use serde::Deserialize;
use serde_json::json;
use std::{
    io::{BufRead, BufReader},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::Mutex,
    time::{Duration, Instant},
};
use tauri::{Manager, State, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

struct Fixture(Mutex<Child>);
impl Drop for Fixture {
    fn drop(&mut self) {
        if let Ok(child) = self.0.get_mut() {
            child.stdin.take();
            let start = Instant::now();
            while start.elapsed() < Duration::from_secs(2) {
                if matches!(child.try_wait(), Ok(Some(_))) {
                    return;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

#[tauri::command]
fn get_background_soak_descriptor(
    window: WebviewWindow,
    state: State<'_, BackendConnectionDescriptorState>,
) -> Result<BackendConnectionDescriptor, &'static str> {
    let url = window.url().map_err(|_| "INVALID_WINDOW")?;
    if window.label() != "main" || !sidecar::is_trusted_renderer_url(&url, false) {
        return Err("INVALID_WINDOW");
    }
    state.get().map_err(|_| "FIXTURE_UNAVAILABLE")
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SoakSummary {
    elapsed_ms: u64,
    events: u64,
    recoveries: u64,
    chart_live: bool,
    errors: u64,
}
#[tauri::command]
fn record_background_soak_summary(
    window: WebviewWindow,
    state: State<'_, runtime_diagnostics::RuntimeDiagnosticsState>,
    summary: SoakSummary,
) -> Result<(), &'static str> {
    let url = window.url().map_err(|_| "INVALID_WINDOW")?;
    if window.label() != "main" || !sidecar::is_trusted_renderer_url(&url, false) {
        return Err("INVALID_WINDOW");
    }
    state.0.record(json!({"event":"soak_summary", "elapsed_ms":summary.elapsed_ms,"received_events":summary.events,
        "recoveries":summary.recoveries,"chart_live":summary.chart_live,"errors":summary.errors,"orders_enabled":false}));
    Ok(())
}

pub fn run() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let option = |key: &str| {
        args.windows(2)
            .find(|pair| pair[0] == key)
            .map(|pair| pair[1].clone())
    };
    let output =
        PathBuf::from(option("--output").expect("--output NEW_ABSOLUTE_DIRECTORY is required"));
    assert!(output.is_absolute(), "output must be absolute");
    std::fs::create_dir(&output).expect("output must not exist");
    let seconds: u64 = option("--seconds")
        .unwrap_or_else(|| "86400".into())
        .parse()
        .expect("invalid seconds");
    assert!((20..=604800).contains(&seconds));
    let phase = option("--phase").unwrap_or_else(|| "visible".into());
    assert!([
        "visible",
        "hidden",
        "minimized",
        "fullscreen-cover",
        "other-desktop"
    ]
    .contains(&phase.as_str()));
    let freeze: u64 = option("--freeze-seconds")
        .unwrap_or_else(|| "0".into())
        .parse()
        .expect("invalid freeze duration");
    assert!(freeze <= 120 && freeze + 15 < seconds);
    let cycle_seconds: u64 = option("--cycle-seconds")
        .unwrap_or_else(|| "0".into())
        .parse()
        .expect("invalid cycle seconds");
    assert!(cycle_seconds == 0 || (30..=3600).contains(&cycle_seconds));
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../..");
    let stderr = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output.join("fixture-stderr.log"))
        .unwrap();
    let mut child = Command::new(root.join("backend/.venv/bin/python"))
        .arg(root.join("scripts/backend_connection_soak_fixture.py"))
        .arg(output.join("backend"))
        .env("BINANCE_RUN_TESTNET", "0")
        .env("BINANCE_RUN_TESTNET_ORDERS", "0")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(stderr)
        .spawn()
        .expect("fixture spawn failed");
    let stdout = child.stdout.take().unwrap();
    let fixture = Fixture(Mutex::new(child));
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut line = String::new();
        let result = BufReader::new(stdout).read_line(&mut line).map(|_| line);
        let _ = tx.send(result);
    });
    let line = rx
        .recv_timeout(Duration::from_secs(10))
        .expect("fixture startup timed out")
        .expect("fixture pipe failed");
    let value: serde_json::Value =
        serde_json::from_str(&line).unwrap_or_else(|_| panic!("invalid fixture descriptor"));
    let descriptor = BackendConnectionDescriptor::new(
        value["port"].as_u64().unwrap() as u16,
        value["session_id"].as_str().unwrap().into(),
        value["schema_version"].as_u64().unwrap() as u32,
        value["token"].as_str().unwrap().into(),
    )
    .unwrap_or_else(|_| panic!("invalid fixture descriptor"));
    let state = BackendConnectionDescriptorState::default();
    state
        .stage(descriptor)
        .unwrap_or_else(|_| panic!("fixture state unavailable"));
    let output_for_setup = output.clone();
    let app = tauri::Builder::default()
        .manage(fixture).manage(state).manage(sidecar::SidecarProcessState::default())
        .manage(runtime_diagnostics::RuntimeDiagnosticsState::default())
        .manage(backend_connection_diagnostics::BackendConnectionDiagnosticsState::in_directory(output.join("ui")))
        .manage(chart_diagnostics::ChartDiagnosticsState::in_directory(output.join("chart")))
        .invoke_handler(tauri::generate_handler![get_background_soak_descriptor,
            runtime_diagnostics::record_renderer_heartbeat, backend_connection_diagnostics::record_backend_connection_diagnostics,
            chart_diagnostics::record_chart_diagnostics, record_background_soak_summary])
        .setup(move |app| {
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("연결 유지 검증 · 주문 기능 없음").inner_size(640.0, 400.0)
                .background_throttling(tauri::utils::config::BackgroundThrottlingPolicy::Disabled)
                .visible(phase != "hidden").build()?;
            if phase == "minimized" { window.minimize()?; }
            let state = app.state::<runtime_diagnostics::RuntimeDiagnosticsState>();
            state.start_in_directory(app.handle(), output_for_setup.join("native"));
            state.0.record(json!({"event":"soak_started","phase":phase,"target_seconds":seconds,"injected_freeze_seconds":freeze,"orders_enabled":false}));
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let began=Instant::now();
                let mut freeze_issued=false;
                let mut cycle_number=0;
                loop {
                    let state=handle.state::<runtime_diagnostics::RuntimeDiagnosticsState>();
                    if state.is_stopped() { return; }
                    let elapsed=began.elapsed().as_secs();
                    if elapsed >= seconds { break; }
                    if freeze>0 && !freeze_issued && elapsed>=10 {
                        freeze_issued=true;
                        state.0.record(json!({"event":"soak_fault_injected","kind":"renderer_busy_loop","seconds":freeze}));
                        if let Some(window)=handle.get_webview_window("main") {
                            let _=window.eval(format!("{{const end=performance.now()+{};while(performance.now()<end){{}}}}",freeze*1000));
                        }
                    }
                    if cycle_seconds>0 && elapsed/cycle_seconds>cycle_number {
                        cycle_number=elapsed/cycle_seconds;
                        let hidden=cycle_number%2==0;
                        if let Some(window)=handle.get_webview_window("main") {
                            let result=if hidden {window.hide()} else {window.show()};
                            state.0.record(json!({"event":"soak_window_cycle","hidden":hidden,"success":result.is_ok()}));
                        }
                    }
                    std::thread::sleep(Duration::from_millis(100));
                }
                handle.state::<runtime_diagnostics::RuntimeDiagnosticsState>().0.record(json!({"event":"soak_duration_completed","target_seconds":seconds}));
                handle.exit(0);
            });
            Ok(())
        })
        .build(tauri::generate_context!("tauri.soak.conf.json")).expect("soak WebView failed");
    app.run_return(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            handle
                .state::<runtime_diagnostics::RuntimeDiagnosticsState>()
                .stop();
        }
    });
}
