//! UI↔backend 최초 오류를 backend 통신 없이 저장한다. 자유 형식 payload·오류 원문은 거부한다.
use crate::chart_diagnostics::ChartLogWriter;
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Manager, State, WebviewWindow};

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ConnectionEvent {
    ConnectionStarted, SocketOpened, AuthenticationSent, Heartbeat, SocketClosed, SocketError,
    RequestStarted, RequestSucceeded, RequestFailed, FirstFailure, Failure, RetryScheduled,
    ConnectionReady, TerminalFailure, ShutdownStarted, ShutdownCompleted, Stopped, EnvironmentChanged,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ConnectionStage { Connect, Authenticate, Receive, Decode, Map, Publish, Resync, Request, Shutdown, Lifecycle }

#[derive(Deserialize, Serialize)]
pub enum ConnectionErrorType { TypeError, RangeError, SyntaxError, AbortError, ContractError, AdapterError, CommandError, Error, Unknown }

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ConnectionOperation { Snapshot, ShutdownState, Shutdown, Trading, Regime, History, Csv, ConnectionStatus, Other }

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectionDiagnostic {
    event: ConnectionEvent,
    session_id: String,
    adapter_id: String,
    renderer_id: String,
    at_ms: u64,
    sequence: u64,
    dropped_before: u64,
    generation: u64,
    last_sequence: u64,
    last_received_at_ms: Option<u64>,
    incident_id: Option<String>,
    error_code: Option<String>,
    error_type: Option<ConnectionErrorType>,
    stage: Option<ConnectionStage>,
    origin: Option<String>,
    validation_field: Option<String>,
    retryable: Option<bool>,
    request_id: Option<String>,
    operation: Option<ConnectionOperation>,
    http_status: Option<u16>,
    elapsed_ms: Option<u64>,
    close_code: Option<u16>,
    clean: Option<bool>,
    attempt: Option<u64>,
    delay_ms: Option<u64>,
    visible: Option<bool>,
    online: Option<bool>,
}

fn valid_origin(origin: &str) -> bool {
    let parts: Vec<_> = origin.split(':').collect();
    parts.len() == 3 && matches!(parts[0], "BackendUiAdapter.ts" | "BackendUiAdapter.js"
        | "backendEventMapper.ts" | "backendEventMapper.js" | "tradingIndicatorValidation.ts" | "tradingIndicatorValidation.js"
        | "UiApplicationFacade.ts" | "UiApplicationFacade.js" | "UiApplicationStore.ts" | "UiApplicationStore.js"
        | "createLiveUiApplication.ts" | "createLiveUiApplication.js")
        && parts[1..].iter().all(|part| !part.is_empty() && part.len() < 8 && part.bytes().all(|byte| byte.is_ascii_digit()))
}

fn validate_record(record: &ConnectionDiagnostic) -> bool {
    [&record.session_id, &record.adapter_id, &record.renderer_id].iter().all(|id| crate::is_canonical_uuid(id))
        && record.incident_id.as_ref().is_none_or(|id| crate::is_canonical_uuid(id))
        && record.request_id.as_ref().is_none_or(|id| crate::is_canonical_uuid(id))
        && record.origin.as_ref().is_none_or(|origin| valid_origin(origin))
        && record.validation_field.as_ref().is_none_or(|field| ALLOWED_VALIDATION_FIELDS.contains(&field.as_str()))
        && record.error_code.as_ref().is_none_or(|code| ALLOWED_ERROR_CODES.contains(&code.as_str()))
        && record.http_status.is_none_or(|status| (100..=599).contains(&status))
        && record.close_code.is_none_or(|code| code <= 4999)
}

#[derive(Default)]
pub struct BackendConnectionDiagnosticsState(Mutex<Option<ChartLogWriter>>);

/// main 창에서 온 고정 schema만 검증하고 append+sync가 성공한 뒤 수신 확인을 반환한다.
#[tauri::command]
pub async fn record_backend_connection_diagnostics(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, BackendConnectionDiagnosticsState>,
    sidecar: State<'_, crate::sidecar::SidecarProcessState>,
    records: Vec<ConnectionDiagnostic>,
) -> Result<(), &'static str> {
    let url = window.url().map_err(|_| "BACKEND_CONNECTION_DIAGNOSTIC_INVALID_WINDOW")?;
    if window.label() != "main" || !crate::sidecar::is_trusted_renderer_url(&url, tauri::is_dev())
        || records.is_empty() || records.len() > 32 || !records.iter().all(validate_record) {
        return Err("BACKEND_CONNECTION_DIAGNOSTIC_INVALID_BATCH");
    }
    let now = SystemTime::now().duration_since(UNIX_EPOCH)
        .map_err(|_| "BACKEND_CONNECTION_DIAGNOSTIC_CLOCK_FAILED")?.as_millis();
    let identity = sidecar.diagnostic_runtime_identity();
    let bytes = encode_records(records, now, identity)?;
    let mut writer = state.0.lock().map_err(|_| "BACKEND_CONNECTION_DIAGNOSTIC_STATE_FAILED")?;
    if writer.is_none() {
        let directory = if cfg!(debug_assertions) {
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../../Log_History/backend_connection")
        } else {
            app.path().app_log_dir().map_err(|_| "BACKEND_CONNECTION_DIAGNOSTIC_DIRECTORY_FAILED")?.join("backend_connection")
        };
        *writer = Some(ChartLogWriter::new(directory, format!("connection_{}_{}", now, std::process::id())));
    }
    writer.as_mut().ok_or("BACKEND_CONNECTION_DIAGNOSTIC_STATE_FAILED")?.append(&bytes)
        .map_err(|_| "BACKEND_CONNECTION_DIAGNOSTIC_WRITE_FAILED")
}

fn encode_records(records: Vec<ConnectionDiagnostic>, now: u128, identity: Option<(u32, String)>) -> Result<Vec<u8>, &'static str> {
    let mut bytes = Vec::new();
    for record in records {
        if !validate_record(&record) { return Err("BACKEND_CONNECTION_DIAGNOSTIC_INVALID_BATCH"); }
        let envelope = serde_json::json!({"schema_version":1, "native_at_ms":now,
            "native_pid":std::process::id(), "backend_pid":identity.as_ref().map(|value| value.0),
            "backend_process_start_id":identity.as_ref().map(|value| &value.1), "connection":record});
        serde_json::to_writer(&mut bytes, &envelope).map_err(|_| "BACKEND_CONNECTION_DIAGNOSTIC_ENCODE_FAILED")?;
        bytes.push(b'\n');
    }
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn sample() -> serde_json::Value {
        serde_json::json!({"event":"first_failure", "session_id":"00000000-0000-4000-8000-000000000001",
            "adapter_id":"00000000-0000-4000-8000-000000000002", "renderer_id":"00000000-0000-4000-8000-000000000003",
            "at_ms":1000, "sequence":1, "dropped_before":0, "generation":2, "last_sequence":100,
            "stage":"decode", "error_code":"MALFORMED_BACKEND_PAYLOAD", "error_type":"SyntaxError"})
    }
    #[test]
    fn rejects_free_text_and_accepts_fixed_failure_context() {
        let value = sample();
        assert!(validate_record(&serde_json::from_value(value.clone()).unwrap()));
        let mut publication = value.clone();
        publication["stage"] = serde_json::json!("publish");
        publication["error_type"] = serde_json::json!("Error");
        publication["error_code"] = serde_json::json!("UI_STATE_PUBLICATION_FAILED");
        publication["origin"] = serde_json::json!("UiApplicationStore.ts:212:18");
        assert!(validate_record(&serde_json::from_value(publication).unwrap()));
        for key in ["token", "payload", "message", "headers"] {
            let mut unsafe_value = value.clone(); unsafe_value[key] = serde_json::json!("SECRET_CANARY");
            assert!(serde_json::from_value::<ConnectionDiagnostic>(unsafe_value).is_err());
        }
        for key in ["origin", "error_code", "validation_field", "session_id"] {
            let mut unsafe_value = value.clone(); unsafe_value[key] = serde_json::json!("SECRET_CANARY");
            assert!(!validate_record(&serde_json::from_value(unsafe_value).unwrap()));
        }
    }
    #[test]
    fn persists_first_failure_with_native_and_backend_identity_without_backend_access() {
        let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let directory = std::env::temp_dir().join(format!("backend-connection-log-{}-{now}", std::process::id()));
        let records = vec![serde_json::from_value(sample()).unwrap()];
        let bytes = encode_records(records, 2000, Some((1234, "test-process-start".into()))).unwrap();
        let mut writer = ChartLogWriter::new(directory.clone(), "test".into());
        writer.append(&bytes).unwrap();
        let saved = std::fs::read(directory.join("test_part0001.log")).unwrap();
        let envelope: serde_json::Value = serde_json::from_slice(&saved).unwrap();
        assert_eq!(envelope["connection"]["event"], "first_failure");
        assert_eq!(envelope["connection"]["stage"], "decode");
        assert_eq!(envelope["native_at_ms"], 2000);
        assert_eq!(envelope["backend_pid"], 1234);
        assert_eq!(envelope["backend_process_start_id"], "test-process-start");
        // Backend 프로세스가 사라진 후에도 마지막 사건을 저장할 수 있어야 한다.
        writer.append(&encode_records(vec![serde_json::from_value(sample()).unwrap()], 3000, None).unwrap()).unwrap();
        assert_eq!(std::fs::read_to_string(directory.join("test_part0001.log")).unwrap().lines().count(), 2);
        std::fs::remove_dir_all(directory).unwrap();
    }
}

// 아래 허용 목록은 UI의 진단 코드 목록과 contract test에서 대조한다.
const ALLOWED_ERROR_CODES: &[&str] = &[
    "ADAPTER_STOPPED",
    "AUTHENTICATION_REQUIRED",
    "BACKEND_CONNECTION_RECOVERING",
    "BACKEND_EVENT_STREAM_FAILED",
    "BACKEND_NOT_READY",
    "BACKEND_REQUEST_CANCELLED",
    "BACKEND_REQUEST_TIMEOUT",
    "BACKEND_UNREACHABLE",
    "CORS_PREFLIGHT_REJECTED",
    "EVENT_STREAM_AUTHENTICATION_FAILED",
    "EVENT_STREAM_CLOSED",
    "EVENT_STREAM_CONNECTION_FAILED",
    "EVENT_STREAM_CONNECT_TIMEOUT",
    "EVENT_STREAM_DECODE_FAILED",
    "EVENT_STREAM_MAPPING_FAILED",
    "EVENT_STREAM_REJECTED",
    "EVENT_STREAM_SOCKET_ERROR",
    "EVENT_STREAM_STALE",
    "FEATURE_NOT_AVAILABLE",
    "IDEMPOTENCY_CONFLICT",
    "IDEMPOTENCY_STATE_EXHAUSTED",
    "INTERNAL_TRANSPORT_ERROR",
    "INVALID_ADAPTER_CONFIGURATION",
    "INVALID_CONNECTION_DESCRIPTOR",
    "INVALID_CSV_EXPORT_OPTIONS",
    "INVALID_REGIME_TYPE",
    "INVALID_SPLIT_RATIO",
    "LOOPBACK_REQUEST_REJECTED",
    "MALFORMED_BACKEND_PAYLOAD",
    "MALFORMED_BACKEND_RESPONSE",
    "MALFORMED_NATIVE_PICKER_RESULT",
    "MALFORMED_REQUEST",
    "METHOD_NOT_ALLOWED",
    "REGIME_SELECTION_REQUIRED",
    "REQUEST_ID_MISMATCH",
    "REQUEST_TOO_LARGE",
    "ROUTE_NOT_FOUND",
    "SESSION_MISMATCH",
    "SHUTDOWN_BLOCKED",
    "SHUTDOWN_NOT_ACCEPTED",
    "SHUTDOWN_OUTCOME_AMBIGUOUS",
    "SHUTDOWN_SAFETY_TIMEOUT",
    "SIDECAR_ABNORMAL_EXIT",
    "SIDECAR_EXIT_TIMEOUT",
    "STALE_BACKEND_RESPONSE",
    "STALE_CONNECTION_RESULT",
    "STALE_STATE",
    "TRADE_HISTORY_PAGE_TOO_LARGE",
    "TRADE_HISTORY_QUERY_FAILED",
    "TRADING_SNAPSHOT_REQUIRED",
    "UI_STATE_PUBLICATION_FAILED",
    "UNCLASSIFIED_CONNECTION_ERROR",
    "UNSUPPORTED_CONTENT_TYPE",
    "UNSUPPORTED_SCHEMA_VERSION",
    "UUID_GENERATION_FAILED",
    "WEBSOCKET_QUERY_NOT_ALLOWED",
    "WEBSOCKET_SUBPROTOCOL_NOT_ALLOWED",
    "WEBSOCKET_UPGRADE_REQUIRED",
];

const ALLOWED_VALIDATION_FIELDS: &[&str] = &[
    "trading.last_risk_budget.projected_position_notional",
    "trading.last_risk_budget.current_position_notional",
    "trading.last_risk_budget.candidate_order_notional",
    "trading.last_risk_budget.reserved_buy_notional",
    "trading.manual_kill_activation_policy_version",
    "trading.last_risk_budget.daily_realized_pnl",
    "trading.last_risk_budget.manual_kill_active",
    "trading.last_risk_budget.account_version",
    "trading.last_risk_budget.context_version",
    "trading.last_risk_budget.market_version",
    "trading.last_risk_budget.policy_version",
    "trading.last_risk_budget.unrealized_pnl",
    "trading.manual_kill_activation_behavior",
    "trading.configured_risk_policy_version",
    "trading.manual_kill_cleanup_complete",
    "trading.position_average_entry_price",
    "trading.last_risk_budget.daily_loss",
    "trading.process_ownership_ambiguous",
    "trading.session_risk_policy_version",
    "trading.last_risk_decision_allowed",
    "indicator.source_market_version",
    "indicator.swing.has_higher_high",
    "indicator.swing.has_higher_low",
    "indicator.swing.has_lower_high",
    "trade.market_price_at_decision",
    "indicator.swing.has_lower_low",
    "trading.max_position_notional",
    "trading.manual_kill_behavior",
    "trading.risk_control_version",
    "last_strategy_evaluation_at",
    "projected_position_notional",
    "environment.orders_enabled",
    "indicator.source_candle_id",
    "trade.allocated_cost_basis",
    "trade.realized_return_rate",
    "trading.manual_kill_active",
    "trading.max_order_notional",
    "current_position_notional",
    "trading.has_open_position",
    "trading.risk_block_reason",
    "active_logic.regime_type",
    "average_sell_return_rate",
    "candidate_order_notional",
    "response.error.retryable",
    "trading.daily_loss_scope",
    "trading.last_risk_budget",
    "active_logic.root_state",
    "event.aggregate_version",
    "indicator.calculated_at",
    "indicator.current_price",
    "reconciliation_required",
    "trading.command_enabled",
    "cumulative_return_rate",
    "response.error.details",
    "response.error.message",
    "snapshot.last_sequence",
    "trading.max_daily_loss",
    "account.current_price",
    "reserved_buy_notional",
    "trade.client_order_id",
    "trade_history_summary",
    "breakeven_sell_count",
    "completed_sell_count",
    "event.correlation_id",
    "indicator.ema9_slope",
    "last_market_input_at",
    "market.current_price",
    "performance.win_rate",
    "snapshot.environment",
    "trading.active_logic",
    "event.last_sequence",
    "indicator.live_ema9",
    "residual_cost_basis",
    "response.error.code",
    "response.request_id",
    "snapshot.connection",
    "snapshot.session_id",
    "account.updated_at",
    "average_fill_price",
    "daily_realized_pnl",
    "manual_kill_active",
    "requested_quantity",
    "trade.realized_pnl",
    "trading.session_id",
    "winning_sell_count",
    "account.valuation",
    "daily_return_rate",
    "event.occurred_at",
    "executed_quantity",
    "losing_sell_count",
    "market.updated_at",
    "residual_quantity",
    "trade.entry_price",
    "trade.executed_at",
    "trade.exit_reason",
    "trading.scale_out",
    "connection.ready",
    "event.session_id",
    "fee_quote_amount",
    "trading.recovery",
    "trading.scale_in",
    "account.version",
    "account_version",
    "context_version",
    "executed_amount",
    "indicator.swing",
    "resync_required",
    "snapshot.market",
    "snapshot.regime",
    "trade.fee_asset",
    "trading.version",
    "account_orders",
    "event.event_id",
    "event.sequence",
    "market.version",
    "market_version",
    "policy_version",
    "response.error",
    "trade.fee_note",
    "trade.order_id",
    "trade.trade_id",
    "unrealized_pnl",
    "account_asset",
    "event.payload",
    "realized_pnl",
    "total_profit",
    "market_data",
    "not_started",
    "performance",
    "recommended",
    "unavailable",
    "unsupported",
    "daily_loss",
    "event.type",
    "fee_amount",
    "started_at",
    "terminated",
    "daily_fee",
    "indicator",
    "supported",
    "total_fee",
    "disabled",
    "negative",
    "positive",
    "response",
    "selected",
    "snapshot",
    "stopping",
    "account",
    "blocked",
    "mainnet",
    "neutral",
    "resumed",
    "running",
    "testnet",
    "trading",
    "market",
    "online",
    "event",
    "highs",
    "trade",
    "type0",
    "type1",
    "type2",
    "type3",
    "type4",
    "fake",
    "idle",
    "live",
    "lows",
    "sell",
    "tone",
    "buy",
    "ema",
];
