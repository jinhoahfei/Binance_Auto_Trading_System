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
    /// 함수 이름: drop()
    /// 기능: 소유 fixture의 표준 입력을 닫고 제한 시간 안에 끝나지 않으면 해당 자식만 정리한다.
    /// 인자: self -> 검증용 자식 프로세스 소유자
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/17
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


/// 함수 이름: get_background_soak_descriptor()
/// 기능: 신뢰한 검증 창에 주문 없는 fixture의 연결 정보를 제공한다.
/// 인자: window -> 호출 창, state -> 연결 정보 상태
/// 반환값: 검증한 연결 정보 또는 오류
/// 작성 날짜: 2026/09/17
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


/// 함수 이름: record_background_soak_summary()
/// 기능: 신뢰한 main 창의 soak 통계를 주문 비활성 표시와 함께 기록한다.
/// 인자: window -> 호출 창, state -> 진단 상태, summary -> renderer 통계
/// 반환값: 기록 수락 또는 고정 오류 코드
/// 작성 날짜: 2026/09/17
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


/// 함수 이름: run()
/// 기능: 명시한 새 출력 디렉터리에서 주문 없는 fixture와 실제 WebView 장시간 검증 앱을 실행한다.
/// 인자: 없음; 명령줄에서 실행 시간·화면 단계·출력 경로를 읽음
/// 반환값: 앱 이벤트 루프 종료 후 반환
/// 작성 날짜: 2026/09/17
pub fn run() {
    // 검증 시간·화면 단계·장애 주입 옵션을 읽고 허용 범위를 확인한다.
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

    // 주문이 비활성화된 로컬 fixture를 독립 자식으로 실행한다.
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

    // 별도 reader로 준비 응답을 받고 10초 제한 안에 연결 정보를 확보한다.
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

    // fixture 연결 정보를 검증한 뒤 renderer에 전달할 상태로 보관한다.
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

    // 검증 전용 IPC와 진단 writer를 실제 WebView 앱 수명에 연결한다.
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

                    // 요청한 경우에만 renderer 지연을 한 번 주입한다.
                    if freeze>0 && !freeze_issued && elapsed>=10 {
                        freeze_issued=true;
                        state.0.record(json!({"event":"soak_fault_injected","kind":"renderer_busy_loop","seconds":freeze}));
                        if let Some(window)=handle.get_webview_window("main") {
                            let _=window.eval(format!("{{const end=performance.now()+{};while(performance.now()<end){{}}}}",freeze*1000));
                        }
                    }

                    // 지정한 주기에 맞춰 표시·숨김 상태를 반복해 복귀 동작을 관찰한다.
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

    // 검증 앱이 끝날 때 독립 감시도 함께 정리한다.
    app.run_return(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            handle
                .state::<runtime_diagnostics::RuntimeDiagnosticsState>()
                .stop();
        }
    });
}
