//! Renderer와 디스크 writer에서 독립된 생존 감시. 관측은 거래 제어를 호출하지 않는다.
use crate::chart_diagnostics::ChartLogWriter;
use crate::{BackendConnectionDescriptor, BackendConnectionDescriptorState};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    mpsc::{sync_channel, SyncSender},
    Arc, Mutex,
};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Manager, State, WebviewWindow};
use zeroize::Zeroize;

#[path = "os_evidence.rs"]
mod os_evidence;
#[cfg(target_os = "macos")]
#[path = "runtime_diagnostics_macos.rs"]
mod platform;


/// 함수 이름: now_ms()
/// 기능: UTC epoch 기준의 현재 시각을 진단용 밀리초로 읽는다.
/// 인자: 없음
/// 반환값: 현재 epoch 밀리초; 시스템 시각 오류이면 0
/// 작성 날짜: 2026/09/17
pub(crate) fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}


/// 함수 이름: new_id()
/// 기능: 난수 UUID를 만들고 난수 실패 시 시각·PID·원자적 순번으로 진단 식별자를 구성한다.
/// 인자: 없음
/// 반환값: UUID 형식의 진단 식별자
/// 작성 날짜: 2026/09/17
pub(crate) fn new_id() -> String {
    let mut bytes = [0u8; 16];
    if getrandom::fill(&mut bytes).is_err() {
        bytes[..8].copy_from_slice(&now_ms().to_le_bytes());
        bytes[8..12].copy_from_slice(&std::process::id().to_le_bytes());
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        bytes[12..]
            .copy_from_slice(&(COUNTER.fetch_add(1, Ordering::Relaxed) as u32).to_le_bytes());
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let s: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &s[..8],
        &s[8..12],
        &s[12..16],
        &s[16..20],
        &s[20..]
    )
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RendererHeartbeat {
    renderer_id: String,
    session_id: Option<String>,
    sequence: u64,
    at_ms: u64,
    monotonic_ms: u64,
    timer_lag_ms: u64,
    visible: bool,
    online: bool,
    last_received_at_ms: Option<u64>,
    last_applied_at_ms: Option<u64>,
    last_sequence: u64,
    last_chart_received_at_ms: Option<u64>,
    ipc_failures: u64,
}

#[derive(Clone, Default, Serialize)]
pub(crate) struct Environment {
    pub occluded: Option<bool>,
    pub minimized: Option<bool>,
    pub app_active: Option<bool>,
    pub low_power: Option<bool>,
    pub thermal_state: Option<i64>,
    pub actual_policy: Option<&'static str>,
    pub activity_registered: bool,
}

pub(crate) struct Shared {
    started: AtomicBool,
    stopped: AtomicBool,
    epoch: Instant,
    renderer: Mutex<Option<(Instant, RendererHeartbeat)>>,
    environment: Mutex<Environment>,
    sender: Mutex<Option<SyncSender<Value>>>,
    main_pending: AtomicBool,
    main_ack_ms: AtomicU64,
    dropped: AtomicU64,
    write_failures: AtomicU64,
    write_duration_ms: AtomicU64,
    written_bytes: AtomicU64,
    evidence_busy: AtomicBool,
    writer_done: AtomicBool,
    pub native_run_id: String,
}

#[derive(Clone)]
pub struct RuntimeDiagnosticsState(pub(crate) Arc<Shared>);
impl Default for RuntimeDiagnosticsState {
    /// 함수 이름: default()
    /// 기능: 독립 감시 상태와 공유 카운터·채널 슬롯을 초기화한다.
    /// 인자: 없음
    /// 반환값: 새 RuntimeDiagnosticsState
    /// 작성 날짜: 2026/09/17
    fn default() -> Self {
        Self(Arc::new(Shared {
            started: AtomicBool::new(false),
            stopped: AtomicBool::new(false),
            epoch: Instant::now(),
            renderer: Mutex::new(None),
            environment: Mutex::new(Environment::default()),
            sender: Mutex::new(None),
            main_pending: AtomicBool::new(false),
            main_ack_ms: AtomicU64::new(0),
            dropped: AtomicU64::new(0),
            write_failures: AtomicU64::new(0),
            write_duration_ms: AtomicU64::new(0),
            written_bytes: AtomicU64::new(0),
            evidence_busy: AtomicBool::new(false),
            writer_done: AtomicBool::new(false),
            native_run_id: new_id(),
        }))
    }
}

impl Shared {
    /// 함수 이름: record()
    /// 기능: 네이티브 식별자·시각·누락 개수를 붙여 제한된 진단 채널에 비차단 전송한다.
    /// 인자: self -> 공유 감시 상태, record -> 기록할 진단 객체
    /// 반환값: 없음; 채널이 가득 차면 누락 개수를 보존
    /// 작성 날짜: 2026/09/17
    pub(crate) fn record(&self, mut record: Value) {
        record["schema_version"] = json!(2);
        record["native_run_id"] = json!(self.native_run_id);
        record["native_pid"] = json!(std::process::id());
        record["at_ms"] = record
            .get("sampled_at_ms")
            .cloned()
            .unwrap_or_else(|| json!(now_ms()));
        record["monotonic_ms"] = record
            .get("sample_monotonic_ms")
            .cloned()
            .unwrap_or_else(|| json!(self.epoch.elapsed().as_millis() as u64));
        let dropped = self.dropped.swap(0, Ordering::Relaxed);
        record["dropped_before"] = json!(dropped);
        let accepted = self
            .sender
            .lock()
            .ok()
            .and_then(|sender| sender.as_ref().map(|s| s.try_send(record).is_ok()))
            .unwrap_or(false);
        if !accepted {
            self.dropped.fetch_add(1 + dropped, Ordering::Relaxed);
        }
    }
}


/// 함수 이름: record_renderer_heartbeat()
/// 기능: 신뢰한 main 창의 heartbeat를 검증하고 최신 순번과 지연 복귀 정보를 보관한다.
/// 인자: window -> 호출 창, state -> 진단 상태, record -> renderer 생존 신호
/// 반환값: 수신 처리 성공 또는 고정 오류 코드
/// 작성 날짜: 2026/09/17
#[tauri::command]
pub fn record_renderer_heartbeat(
    window: WebviewWindow,
    state: State<'_, RuntimeDiagnosticsState>,
    record: RendererHeartbeat,
) -> Result<(), &'static str> {
    let url = window.url().map_err(|_| "LIVENESS_INVALID_WINDOW")?;
    if window.label() != "main"
        || !crate::sidecar::is_trusted_renderer_url(&url, tauri::is_dev())
        || !crate::is_canonical_uuid(&record.renderer_id)
        || record
            .session_id
            .as_ref()
            .is_some_and(|id| !crate::is_canonical_uuid(id))
    {
        return Err("LIVENESS_INVALID_RECORD");
    }
    let mut latest = state
        .0
        .renderer
        .lock()
        .map_err(|_| "LIVENESS_STATE_UNAVAILABLE")?;
    if latest.as_ref().is_some_and(|(_, old)| {
        old.renderer_id == record.renderer_id && old.sequence >= record.sequence
    }) {
        return Ok(());
    }
    let delivery_gap_ms = latest
        .as_ref()
        .map(|(received, _)| received.elapsed().as_millis() as u64);
    if record.timer_lag_ms >= 15_000 || delivery_gap_ms.is_some_and(|gap| gap >= 15_000) {
        state.0.record(json!({"event":"renderer_delivery_resumed","renderer_id":record.renderer_id,
            "renderer_sequence":record.sequence,"timer_lag_ms":record.timer_lag_ms,"delivery_gap_ms":delivery_gap_ms}));
    }
    *latest = Some((Instant::now(), record));
    Ok(())
}

/// 고정 DTO만 허용하며 응답의 알 수 없는 필드를 저장하지 않는다.
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct BackendLiveness {
    diagnostic_schema_version: u32,
    session_id: String,
    process_id: u32,
    process_start_id: String,
    runtime_status: String,
    sampled_at_ms: u64,
    monotonic_ms: u64,
    last_runtime_cycle_at_ms: Option<u64>,
    last_runtime_cycle_monotonic_ms: Option<u64>,
    last_market_input_at_ms: Option<u64>,
    last_strategy_evaluation_at_ms: Option<u64>,
    last_ui_send_at_ms: Option<u64>,
    last_ui_send_sequence: Option<u64>,
    last_ui_send_duration_ms: Option<u64>,
    market_stream: String,
    account_stream: String,
    api: String,
    api_checked_at_ms: Option<u64>,
    streams_checked_at_ms: Option<u64>,
    last_exchange_error_code: Option<String>,
}
impl BackendLiveness {
    /// 함수 이름: valid()
    /// 기능: backend 생존 응답의 schema·session·상태·오류 코드가 허용 계약인지 검사한다.
    /// 인자: self -> 생존 응답, descriptor -> 현재 연결 정보
    /// 반환값: 허용된 응답이면 true
    /// 작성 날짜: 2026/09/17
    fn valid(&self, descriptor: &BackendConnectionDescriptor) -> bool {
        self.diagnostic_schema_version == 2
            && self.session_id == descriptor.session_id
            && crate::is_canonical_uuid(&self.process_start_id)
            && [
                self.market_stream.as_str(),
                self.account_stream.as_str(),
                self.api.as_str(),
            ]
            .iter()
            .all(|s| matches!(*s, "online" | "offline" | "unknown"))
            && matches!(
                self.runtime_status.as_str(),
                "unknown"
                    | "running"
                    | "not_started"
                    | "stopping"
                    | "reconciliation_required"
                    | "terminated"
            )
            && self.last_exchange_error_code.as_deref().is_none_or(|c| {
                matches!(
                    c,
                    "API_REQUEST_FAILED"
                        | "MARKET_STREAM_UNAVAILABLE"
                        | "ACCOUNT_STREAM_UNAVAILABLE"
                )
            })
    }
}


/// 함수 이름: probe()
/// 기능: 단일 제한 시간으로 loopback 생존 조회를 수행하고 인증 토큰은 메모리에서 지운다.
/// 인자: descriptor -> backend 연결 정보
/// 반환값: 검증한 생존 응답 또는 고정 실패 코드
/// 작성 날짜: 2026/09/17
fn probe(descriptor: &BackendConnectionDescriptor) -> Result<BackendLiveness, &'static str> {
    // 연결·송신·수신이 같은 deadline을 공유하도록 남은 시간을 계산한다.
    let deadline = Instant::now() + Duration::from_secs(2);
    let address = SocketAddr::from(([127, 0, 0, 1], descriptor.port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(2))
        .map_err(|_| "connect_failed")?;
    let remaining = || {
        deadline
            .checked_duration_since(Instant::now())
            .filter(|d| !d.is_zero())
            .ok_or("timeout")
    };
    stream
        .set_write_timeout(Some(remaining()?))
        .map_err(|_| "timeout")?;
    let request_id = new_id();
    let origin = if tauri::is_dev() {
        "http://127.0.0.1:5173"
    } else {
        "tauri://localhost"
    };
    let mut request = format!("GET /v1/diagnostics/liveness HTTP/1.1\r\nHost: {address}\r\nOrigin: {origin}\r\nAuthorization: Bearer {}\r\nX-Request-Id: {request_id}\r\nConnection: close\r\n\r\n", descriptor.token);
    // 인증 헤더는 전송 직후 메모리에서 지우며 진단 기록에 원문을 남기지 않는다.
    let sent = stream.write_all(request.as_bytes());
    request.zeroize();
    sent.map_err(|_| "send_failed")?;

    // 응답 크기를 제한하면서 연결이 닫힐 때까지 읽는다.
    let mut response = Vec::new();
    let mut buf = [0u8; 2048];
    loop {
        stream
            .set_read_timeout(Some(remaining()?))
            .map_err(|_| "timeout")?;
        let n = stream.read(&mut buf).map_err(|_| "timeout")?;
        if n == 0 {
            break;
        }
        response.extend_from_slice(&buf[..n]);
        if response.len() > 16_384 {
            return Err("response_too_large");
        }
    }
    let text = std::str::from_utf8(&response).map_err(|_| "invalid_response")?;
    let (headers, body) = text.split_once("\r\n\r\n").ok_or("invalid_response")?;
    if headers
        .lines()
        .next()
        .and_then(|s| s.split_whitespace().nth(1))
        != Some("200")
    {
        return Err("http_failed");
    }

    // HTTP 성공 이후에도 request ID·schema·생존 데이터 계약을 검증한다.
    let envelope: Value = serde_json::from_str(body).map_err(|_| "invalid_response")?;
    if envelope["ok"] != true
        || envelope["schema_version"] != crate::BACKEND_SCHEMA_VERSION
        || envelope["request_id"] != request_id
    {
        return Err("invalid_response");
    }
    let data: BackendLiveness =
        serde_json::from_value(envelope["data"].clone()).map_err(|_| "invalid_response")?;
    if !data.valid(descriptor) {
        return Err("invalid_response");
    }
    Ok(data)
}

impl RuntimeDiagnosticsState {
    /// 함수 이름: start()
    /// 기능: 개발·배포 환경의 진단 디렉터리를 선택해 독립 감시를 시작한다.
    /// 인자: self -> 진단 수명 상태, app -> Tauri 앱 핸들
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/17
    pub fn start(&self, app: &AppHandle) {
        let directory = if cfg!(debug_assertions) {
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../../Log_History/runtime_health")
        } else {
            match app.path().app_log_dir() {
                Ok(p) => p.join("runtime_health"),
                Err(_) => {
                    self.0.started.store(false, Ordering::SeqCst);
                    return;
                }
            }
        };
        self.start_in_directory(app, directory);
    }

    /// 함수 이름: start_in_directory()
    /// 기능: 지정 디렉터리의 저장 worker와 독립 감시 worker를 최초 한 번 시작한다.
    /// 인자: self -> 진단 수명 상태, app -> 앱 핸들, directory -> 진단 저장 위치
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/17
    pub(crate) fn start_in_directory(&self, app: &AppHandle, directory: PathBuf) {
        if self.0.started.swap(true, Ordering::SeqCst) {
            return;
        }
        self.0
            .main_ack_ms
            .store(self.0.epoch.elapsed().as_millis() as u64, Ordering::Relaxed);
        // 디스크 저장과 감시를 분리하고 제한된 채널로 대기열의 무한 증가를 막는다.
        let (sender, receiver) = sync_channel::<Value>(256);
        *self.0.sender.lock().unwrap() = Some(sender);
        let shared = self.0.clone();
        let writer_spawn = thread::Builder::new()
            .name("runtime-diagnostic-writer".into())
            .spawn(move || {
                let mut writer = ChartLogWriter::new(
                    directory,
                    format!("runtime_{}_{}", now_ms(), std::process::id()),
                );
                while let Ok(record) = receiver.recv() {
                    let began = Instant::now();
                    if let Ok(mut bytes) = serde_json::to_vec(&record) {
                        bytes.push(b'\n');
                        if writer.append(&bytes).is_err() {
                            shared.write_failures.fetch_add(1, Ordering::Relaxed);
                            shared.dropped.fetch_add(1, Ordering::Relaxed);
                        } else {
                            shared
                                .written_bytes
                                .fetch_add(bytes.len() as u64, Ordering::Relaxed);
                        }
                        shared
                            .write_duration_ms
                            .store(began.elapsed().as_millis() as u64, Ordering::Relaxed);
                    }
                }
                shared.writer_done.store(true, Ordering::SeqCst);
            });
        if writer_spawn.is_err() {
            self.0.write_failures.fetch_add(1, Ordering::Relaxed);
            self.0.writer_done.store(true, Ordering::SeqCst);
            eprintln!("RUNTIME_DIAGNOSTIC_WRITER_UNAVAILABLE");
        }
        #[cfg(target_os = "macos")]
        platform::install(app.clone(), self.0.clone());
        self.0.record(
            json!({"event":"runtime_started", "app_version":app.package_info().version.to_string(),
            "webview_version":tauri::webview_version().ok(), "os":std::env::consts::OS,
            "requested_policy":"disabled", "sample_interval_ms":5000}),
        );
        let shared = self.0.clone();
        let app = app.clone();
        if thread::Builder::new()
            .name("runtime-liveness-monitor".into())
            .spawn(move || monitor(app, shared))
            .is_err()
        {
            self.0.record(json!({"event":"monitor_unavailable"}));
        }
    }

    /// 함수 이름: is_stopped()
    /// 기능: 검증 앱에서 독립 감시의 종료 상태를 확인한다.
    /// 인자: self -> 진단 수명 상태
    /// 반환값: 종료 표시가 설정되었으면 true
    /// 작성 날짜: 2026/09/17
    #[cfg(feature = "background-liveness-smoke")]
    pub(crate) fn is_stopped(&self) -> bool {
        self.0.stopped.load(Ordering::SeqCst)
    }

    /// 함수 이름: stop()
    /// 기능: 감시 종료를 표시하고 플랫폼 관찰·채널을 정리하며 writer 완료를 제한 시간만 기다린다.
    /// 인자: self -> 진단 수명 상태
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/17
    pub fn stop(&self) {
        self.0.record(json!({"event":"runtime_stopped"}));
        self.0.stopped.store(true, Ordering::SeqCst);
        #[cfg(target_os = "macos")]
        platform::stop();
        // 송신 채널을 닫아 writer가 끝나게 하되 앱 종료를 무기한 막지 않는다.
        self.0.sender.lock().ok().map(|mut s| s.take());
        let began = Instant::now();
        while !self.0.writer_done.load(Ordering::SeqCst)
            && began.elapsed() < Duration::from_millis(250)
        {
            thread::sleep(Duration::from_millis(5));
        }
    }

    /// 함수 이름: backend_exited()
    /// 기능: backend 프로세스의 종료 코드를 독립 진단 기록에 남긴다.
    /// 인자: self -> 진단 상태, code -> 관찰한 종료 코드 또는 None
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/17
    pub fn backend_exited(&self, code: Option<i32>) {
        self.0
            .record(json!({"event":"backend_exited","exit_code":code}));
    }

    /// 함수 이름: window_event()
    /// 기능: 창의 가시성·크기 등 변경 시점에 고정된 진단 이벤트를 남긴다.
    /// 인자: self -> 진단 상태
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/17
    pub fn window_event(&self) {
        self.0.record(json!({"event":"window_changed"}));
    }
}


/// 함수 이름: collect_evidence()
/// 기능: 동시에 한 worker만 OS 증거를 수집하게 하고 사건 식별자와 결과를 기록한다.
/// 인자: shared -> 공유 감시 상태, incident -> 사건 ID, backend_pid -> 선택적 backend PID
/// 반환값: 없음
/// 작성 날짜: 2026/09/17
fn collect_evidence(shared: &Arc<Shared>, incident: &str, backend_pid: Option<u32>) {
    if shared.evidence_busy.swap(true, Ordering::SeqCst) {
        shared.record(
            json!({"event":"os_evidence", "incident_id":incident,"status":"already_collecting"}),
        );
        return;
    }
    let owner = shared.clone();
    let incident = incident.to_owned();
    let spawn = thread::Builder::new()
        .name("runtime-os-evidence".into())
        .spawn(move || {
            let result = os_evidence::collect(std::process::id(), backend_pid);
            owner.record(json!({"event":"os_evidence", "incident_id":incident, "result":result}));
            owner.evidence_busy.store(false, Ordering::SeqCst);
        });
    if spawn.is_err() {
        shared.evidence_busy.store(false, Ordering::SeqCst);
        shared.record(json!({"event":"os_evidence", "status":"worker_unavailable"}));
    }
}


/// 함수 이름: process_resources()
/// 기능: 지원되는 OS에서 네이티브 프로세스의 CPU 사용 시간과 최대 RSS를 읽는다.
/// 인자: 없음
/// 반환값: 자원 관찰값 또는 사용 불가 상태의 JSON
/// 작성 날짜: 2026/09/17
fn process_resources() -> Value {
    #[cfg(unix)]
    {
        let mut usage = std::mem::MaybeUninit::<libc::rusage>::uninit();
        if unsafe { libc::getrusage(libc::RUSAGE_SELF, usage.as_mut_ptr()) } == 0 {
            let usage = unsafe { usage.assume_init() };
            let cpu_ms = (usage.ru_utime.tv_sec + usage.ru_stime.tv_sec) as i64 * 1000
                + (usage.ru_utime.tv_usec + usage.ru_stime.tv_usec) as i64 / 1000;
            let multiplier = if cfg!(target_os = "macos") { 1 } else { 1024 };
            return json!({"cpu_total_ms":cpu_ms,"peak_rss_bytes":usage.ru_maxrss as i64 * multiplier,"scope":"native_process_only"});
        }
    }
    json!({"status":"unavailable"})
}


/// 함수 이름: monitor()
/// 기능: 네이티브·renderer·backend 관찰을 주기적으로 모아 지연 사건과 복구 증거를 기록한다.
/// 인자: app -> Tauri 앱 핸들, shared -> 공유 감시 상태
/// 반환값: 없음; 종료 표시까지 worker에서 반복
/// 작성 날짜: 2026/09/17
fn monitor(app: AppHandle, shared: Arc<Shared>) {
    // 최근 관찰값과 장애·복구 구간의 기록 상태를 준비한다.
    let observation_started = Instant::now();
    let mut ring: VecDeque<Value> = VecDeque::with_capacity(60);
    let mut last_tick = Instant::now();
    let mut last_wall = now_ms();
    let mut next_summary = Instant::now();
    let mut incident: Option<String> = None;
    let mut detail_until = Instant::now();
    let mut recovered_incident: Option<String> = None;
    let mut backend_was_ready = false;
    let mut sample_sequence = 0u64;

    // renderer·메인 스레드·backend를 독립적인 관찰값으로 수집한다.
    while !shared.stopped.load(Ordering::SeqCst) {
        let cycle = Instant::now();
        let jitter = cycle
            .duration_since(last_tick)
            .as_millis()
            .saturating_sub(5000);
        let wall_gap = now_ms() as i64 - last_wall as i64;
        last_wall = now_ms();
        last_tick = cycle;
        if !shared.main_pending.swap(true, Ordering::SeqCst) {
            let state = shared.clone();
            let handle = app.clone();
            if app
                .run_on_main_thread(move || {
                    #[cfg(target_os = "macos")]
                    {
                        let mut env = state
                            .environment
                            .try_lock()
                            .ok()
                            .map(|e| e.clone())
                            .unwrap_or_default();
                        platform::sample(&handle, &mut env);
                        if let Ok(mut stored) = state.environment.try_lock() {
                            *stored = env;
                        }
                    }
                    #[cfg(not(target_os = "macos"))]
                    let _ = handle;
                    state
                        .main_ack_ms
                        .store(state.epoch.elapsed().as_millis() as u64, Ordering::Relaxed);
                    state.main_pending.store(false, Ordering::SeqCst);
                })
                .is_err()
            {
                shared.main_pending.store(false, Ordering::SeqCst);
            }
        }
        let descriptor = app.state::<BackendConnectionDescriptorState>().get().ok();
        let identity = app
            .state::<crate::sidecar::SidecarProcessState>()
            .diagnostic_runtime_identity();
        let probe_started = Instant::now();
        let result = descriptor.as_ref().map(probe);
        let (backend, probe_status) = match result {
            Some(Ok(v)) => (Some(v), "ok"),
            Some(Err(e)) => (None, e),
            None => (None, "not_ready"),
        };
        backend_was_ready |= descriptor.is_some();
        let renderer = shared.renderer.try_lock().ok().and_then(|r| r.clone());
        let renderer_age = renderer
            .as_ref()
            .map(|(at, _)| at.elapsed().as_millis() as u64);
        let main_age = (shared.epoch.elapsed().as_millis() as u64)
            .saturating_sub(shared.main_ack_ms.load(Ordering::Relaxed));
        let backend_stale = backend.as_ref().is_some_and(|b| {
            matches!(b.runtime_status.as_str(), "running" | "stopping")
                && b.last_runtime_cycle_monotonic_ms
                    .is_some_and(|t| b.monotonic_ms.saturating_sub(t) >= 15_000)
        });

        // 개별 관찰 지연을 합쳐 현재 장애 구간의 시작·복구 여부를 판단한다.
        let abnormal = renderer_age.unwrap_or(observation_started.elapsed().as_millis() as u64)
            >= 15_000
            || main_age >= 15_000
            || jitter >= 10_000
            || (backend_was_ready && probe_status != "ok")
            || backend_stale;
        sample_sequence += 1;
        let mut sample = json!({"event":"runtime_sample", "sample_sequence":sample_sequence,
            "sampled_at_ms":now_ms(), "sample_monotonic_ms":shared.epoch.elapsed().as_millis() as u64,
            "renderer":renderer.as_ref().map(|(_, r)| r), "renderer_age_ms":renderer_age,
            "main_thread_age_ms":main_age, "native_timer_lag_ms":jitter, "native_wall_gap_ms":wall_gap, "resources":process_resources(),
            "backend":backend, "probe_status":probe_status, "probe_elapsed_ms":probe_started.elapsed().as_millis() as u64,
            "backend_pid":identity.as_ref().map(|v| v.0).or_else(|| backend.as_ref().map(|b| b.process_id)), "backend_process_start_id":identity.as_ref().map(|v| v.1.as_str()).or_else(|| backend.as_ref().map(|b| b.process_start_id.as_str())),
            "environment":shared.environment.try_lock().ok().map(|e| e.clone()),
            "written_bytes":shared.written_bytes.load(Ordering::Relaxed), "write_failures":shared.write_failures.load(Ordering::Relaxed), "write_duration_ms":shared.write_duration_ms.load(Ordering::Relaxed),
            "incident_id":incident});

        // 첫 장애에는 직전 ring 기록을 보존하고 복구 때도 같은 사건의 증거를 남긴다.
        if abnormal && incident.is_none() {
            let id = new_id();
            shared.record(json!({"event":"incident_started", "incident_id":id}));
            for mut entry in ring.iter().cloned() {
                entry["incident_id"] = json!(id);
                shared.record(entry);
            }
            collect_evidence(&shared, &id, identity.as_ref().map(|v| v.0));
            incident = Some(id);
            recovered_incident = None;
        } else if !abnormal && incident.is_some() {
            let id = incident.take().unwrap();
            shared.record(json!({"event":"incident_recovered", "incident_id":id}));
            collect_evidence(&shared, &id, identity.as_ref().map(|v| v.0));
            detail_until = Instant::now() + Duration::from_secs(60);
            recovered_incident = Some(id);
        }
        sample["incident_id"] = json!(incident.as_ref().or_else(|| if cycle < detail_until {
            recovered_incident.as_ref()
        } else {
            None
        }));

        // 최근 표본 수를 제한하고 장애 구간에서는 상세 기록을 유지한다.
        if ring.len() == 60 {
            ring.pop_front();
        }
        ring.push_back(sample.clone());
        if incident.is_some() || cycle >= next_summary || cycle < detail_until {
            shared.record(sample);
            next_summary = cycle + Duration::from_secs(30);
        }
        // Shutdown을 bounded interval로 관찰하되 5초 주기 자체는 작업 소요시간과 분리한다.
        while cycle.elapsed() < Duration::from_secs(5) && !shared.stopped.load(Ordering::SeqCst) {
            thread::sleep(Duration::from_millis(100));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 함수 이름: renderer_schema_rejects_secrets()
    /// 기능: heartbeat DTO가 알 수 없는 비밀 필드를 거부하는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음; schema 위반을 수락하면 테스트 실패
    /// 작성 날짜: 2026/09/17
    #[test]
    fn renderer_schema_rejects_secrets() {
        let value = json!({"renderer_id":new_id(),"session_id":null,"sequence":1,"at_ms":1,"monotonic_ms":1,
            "timer_lag_ms":0,"visible":false,"online":true,"last_received_at_ms":null,
            "last_applied_at_ms":null,"last_sequence":0,"last_chart_received_at_ms":null,"ipc_failures":0});
        assert!(serde_json::from_value::<RendererHeartbeat>(value.clone()).is_ok());
        let mut unsafe_value = value;
        unsafe_value["token"] = json!("CANARY");
        assert!(serde_json::from_value::<RendererHeartbeat>(unsafe_value).is_err());
    }

    /// 함수 이름: queue_overflow_is_observable_and_never_blocks()
    /// 기능: 가득 찬 진단 큐가 감시를 막지 않고 누락 개수를 보존하는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음; 비차단·누락 계약을 어기면 테스트 실패
    /// 작성 날짜: 2026/09/17
    #[test]
    fn queue_overflow_is_observable_and_never_blocks() {
        let state = RuntimeDiagnosticsState::default();
        let (tx, rx) = sync_channel(1);
        *state.0.sender.lock().unwrap() = Some(tx);
        state.0.record(json!({"event":"first"}));
        state.0.record(json!({"event":"lost"}));
        rx.recv().unwrap();
        state.0.record(json!({"event":"next"}));
        assert_eq!(rx.recv().unwrap()["dropped_before"], 1);
    }
}
