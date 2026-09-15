//! Backend와 독립적인 chart 진단을 크기 제한 JSONL 파일로 보존한다.
use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Manager, State, WebviewWindow};

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ChartEvent {
    ConnectionStarted,
    SocketOpened,
    RestStarted,
    RestCompleted,
    RestFailed,
    RestTimeout,
    FirstKline,
    Heartbeat,
    StreamStale,
    SocketClosed,
    SocketError,
    ParseError,
    ReconnectScheduled,
    ConnectionReady,
    Stopped,
    BrowserOnline,
    VisibilityChanged,
    ConnectTimeout,
    RenderApplied,
    RenderFailed,
}

#[derive(Deserialize, Serialize)]
pub enum ChartInterval {
    #[serde(rename = "1m")]
    OneMinute,
    #[serde(rename = "30m")]
    ThirtyMinutes,
    #[serde(rename = "4h")]
    FourHours,
    #[serde(rename = "1d")]
    OneDay,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ChartErrorKind {
    Timeout,
    HttpError,
    InvalidJson,
    InvalidPayload,
    TypeError,
    RangeError,
    Unknown,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct IntervalDiagnostic {
    interval: ChartInterval,
    received_at_ms: Option<u64>,
    open_time_ms: Option<u64>,
    event_time_ms: Option<u64>,
    close: Option<f64>,
    count: u64,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ChartDiagnostic {
    event: ChartEvent,
    renderer_id: String,
    at_ms: u64,
    sequence: u64,
    dropped_before: u64,
    error_kind: Option<ChartErrorKind>,
    http_status: Option<u16>,
    connection_id: Option<u64>,
    interval: Option<ChartInterval>,
    elapsed_ms: Option<u64>,
    close_code: Option<u16>,
    clean: Option<bool>,
    attempt: Option<u64>,
    delay_ms: Option<u64>,
    visible: Option<bool>,
    online: Option<bool>,
    intervals: Option<Vec<IntervalDiagnostic>>,
}

#[derive(Default)]
pub struct ChartDiagnosticsState(Mutex<Option<ChartLogWriter>>);

pub(crate) struct ChartLogWriter {
    directory: PathBuf,
    run_name: String,
    part: u32,
    size: u64,
}

impl ChartLogWriter {
    pub(crate) fn new(directory: PathBuf, run_name: String) -> Self {
        Self { directory, run_name, part: 1, size: 0 }
    }

    /// 함수 이름: append()
    /// 기능: 한 batch를 즉시 기록하고 5MiB마다 새 파일로 분할하고 과거 파일을 보존한다.
    /// 인자: bytes -> 검증·직렬화한 JSONL
    /// 반환값: 파일 처리 결과
    /// 작성 날짜: 2026/09/11
    pub(crate) fn append(&mut self, bytes: &[u8]) -> std::io::Result<()> {
        fs::create_dir_all(&self.directory)?;
        if self.size + bytes.len() as u64 > 5 * 1024 * 1024 {
            self.part += 1;
            self.size = 0;
        }
        let path = self
            .directory
            .join(format!("{}_part{:04}.log", self.run_name, self.part));
        let mut options = OpenOptions::new();
        options.create(true).append(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(path)?;
        // 직전 write/sync 실패가 남긴 부분 기록은 마지막 확정 byte 이후에서 복구한다.
        file.set_len(self.size)?;
        file.write_all(bytes)?;
        file.sync_data()?;
        self.size += bytes.len() as u64;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 함수 이름: rejects_arbitrary_payload_and_unknown_event()
    /// 기능: 로그 schema가 원본 오류·token·임의 event 문자열을 받아들이지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/11
    #[test]
    fn rejects_arbitrary_payload_and_unknown_event() {
        let base = serde_json::json!({"event":"heartbeat", "renderer_id":"00000000-0000-4000-8000-000000000001",
            "at_ms":1000, "sequence":1, "dropped_before":0});
        assert!(serde_json::from_value::<ChartDiagnostic>(base.clone()).is_ok());
        let mut unknown_field = base.clone();
        unknown_field["token"] = serde_json::json!("should-not-be-recorded");
        assert!(serde_json::from_value::<ChartDiagnostic>(unknown_field).is_err());
        let mut unknown_event = base;
        unknown_event["event"] = serde_json::json!("arbitrary message");
        assert!(serde_json::from_value::<ChartDiagnostic>(unknown_event).is_err());
    }

    /// 함수 이름: persists_rotates_and_repairs_partial_tail()
    /// 기능: 저장·부분 append 복구·크기별 분할이 과거 로그를 보존하는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/11
    #[test]
    fn persists_rotates_and_repairs_partial_tail() {
        let directory = std::env::temp_dir().join(format!(
            "chart-log-test-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let mut writer = ChartLogWriter {
            directory: directory.clone(),
            run_name: "test".into(),
            part: 1,
            size: 0,
        };
        writer.append(b"{\"sequence\":1}\n").unwrap();
        let first_path = directory.join("test_part0001.log");
        OpenOptions::new()
            .append(true)
            .open(&first_path)
            .unwrap()
            .write_all(b"partial")
            .unwrap();
        writer.append(b"{\"sequence\":2}\n").unwrap();
        assert_eq!(
            fs::read_to_string(&first_path).unwrap(),
            "{\"sequence\":1}\n{\"sequence\":2}\n"
        );
        writer.size = 5 * 1024 * 1024;
        writer.append(b"{\"sequence\":3}\n").unwrap();
        assert!(first_path.exists());
        assert_eq!(
            fs::read_to_string(directory.join("test_part0002.log")).unwrap(),
            "{\"sequence\":3}\n"
        );
        fs::remove_dir_all(directory).unwrap();
    }
}

/// 함수 이름: record_chart_diagnostics()
/// 기능: main renderer의 고정 schema 진단만 native 시각·backend session과 함께 파일에 기록한다.
/// 인자: window/app -> 현재 native 실행, state -> writer, backend -> 상관관계, records -> 최대 32건
/// 반환값: 저장 성공 또는 원문·경로 없는 오류 코드
/// 작성 날짜: 2026/09/11
#[tauri::command]
pub async fn record_chart_diagnostics(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, ChartDiagnosticsState>,
    backend: State<'_, crate::BackendConnectionDescriptorState>,
    sidecar: State<'_, crate::sidecar::SidecarProcessState>,
    records: Vec<ChartDiagnostic>,
) -> Result<(), &'static str> {
    if window.label() != "main" || records.is_empty() || records.len() > 32 {
        return Err("CHART_DIAGNOSTIC_INVALID_BATCH");
    }
    for record in &records {
        if !crate::is_canonical_uuid(&record.renderer_id)
            || record
                .intervals
                .as_ref()
                .is_some_and(|items| items.len() > 4)
        {
            return Err("CHART_DIAGNOSTIC_INVALID_RECORD");
        }
    }
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "CHART_DIAGNOSTIC_CLOCK_FAILED")?
        .as_millis();
    let session_id = backend
        .session_descriptor
        .lock()
        .ok()
        .and_then(|descriptor| descriptor.as_ref().map(|value| value.session_id.clone()));
    let mut bytes = Vec::new();
    let runtime_identity = sidecar.diagnostic_runtime_identity();
    for record in records {
        let envelope = serde_json::json!({
            "schema_version": 1, "native_at_ms": now, "native_pid": std::process::id(),
            "backend_session_id": session_id, "chart": record,
            "backend_pid": runtime_identity.as_ref().map(|identity| identity.0),
            "backend_process_start_id": runtime_identity.as_ref().map(|identity| &identity.1),
        });
        serde_json::to_writer(&mut bytes, &envelope)
            .map_err(|_| "CHART_DIAGNOSTIC_ENCODE_FAILED")?;
        bytes.push(b'\n');
    }
    let mut writer = state
        .0
        .lock()
        .map_err(|_| "CHART_DIAGNOSTIC_STATE_FAILED")?;
    if writer.is_none() {
        let directory = if cfg!(debug_assertions) {
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../../Log_History/chart")
        } else {
            app.path()
                .app_log_dir()
                .map_err(|_| "CHART_DIAGNOSTIC_DIRECTORY_FAILED")?
                .join("chart")
        };
        *writer = Some(ChartLogWriter {
            directory,
            run_name: format!("chart_{}_{}", now, std::process::id()),
            part: 1,
            size: 0,
        });
    }
    writer
        .as_mut()
        .ok_or("CHART_DIAGNOSTIC_STATE_FAILED")?
        .append(&bytes)
        .map_err(|_| "CHART_DIAGNOSTIC_WRITE_FAILED")
}
