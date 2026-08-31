# Phase 13 deterministic fault matrix

`canonical_fault_trace.json`은 credential이 없는 P13-05 입력 trace와 각 scenario의 정규화
SHA-256을 함께 보존한다. `scripts/phase13_deterministic_replay.py`는 고정 clock과 공개
`Order`, `Position`, `TradeHistory`, `BackendEventStream` API만 사용한다. 결과의
`OFFLINE_CONTRACT_REPLAY` 범위는 실제 Testnet, production market-event E2E 또는 private BUY
trigger 성공 증거가 아니다. network mutation port 호출 수와 private Action 호출 수는 모든 행에서
0으로 기록한다.

| Fault | Owner | Expected state | 신규 주문 | Recovery action |
|---|---|---|---:|---|
| `REST_TIMEOUT` | `APIGateway/TradingController` | `RECONCILIATION_REQUIRED` | 차단 | `QUERY_SAME_CLIENT_ORDER_ID` |
| `REST_5XX` | `APIGateway/TradingController` | `RECONCILIATION_REQUIRED` | 차단 | `QUERY_SAME_CLIENT_ORDER_ID` |
| `REST_RATE_LIMIT` | `APIGateway/TradingController` | `RECONCILIATION_REQUIRED` | 차단 | `HONOR_RETRY_AFTER_THEN_QUERY_SAME_ID` |
| `REST_RESPONSE_DECODE_FAILURE` | `APIGateway/TradingController` | `RECONCILIATION_REQUIRED` | 차단 | `QUERY_SAME_CLIENT_ORDER_ID` |
| `WS_DISCONNECT_RECONNECT` | `WebSocketGateway/TradingController` | `RECONCILIATION_REQUIRED` | 차단 | `REST_SNAPSHOT_THEN_NEW_GENERATION` |
| `WS_STALE_GENERATION` | `WebSocketGateway` | `RUNNING` | 허용 | `DROP_STALE_GENERATION` |
| `WS_SEQUENCE_GAP` | `WebSocketGateway/TradingController` | `RECONCILIATION_REQUIRED` | 차단 | `FULL_SNAPSHOT_RESYNC` |
| `WS_DUPLICATE_EVENT` | `Order/TradeHistory` | `RUNNING` | 허용 | `DEDUPLICATE_BY_ORDER_AND_FILL_ID` |
| `WS_OUT_OF_ORDER_EVENT` | `Order` | `RUNNING` | 허용 | `PRESERVE_MONOTONIC_ORDER_STATE` |
| `CRASH_BEFORE_SUBMIT` | `TradingController/TradeHistoryRepository` | `RECONCILIATION_REQUIRED` | 차단 | `QUERY_PREPARED_IDENTITY_BEFORE_SUBMIT` |
| `CRASH_AFTER_SUBMIT` | `TradingController/TradeHistoryRepository` | `RECONCILIATION_REQUIRED` | 차단 | `QUERY_SAME_CLIENT_ORDER_ID` |
| `PENDING_JOURNAL_FSYNC_FAILURE` | `TradeHistoryRepository/TradingController` | `RECONCILIATION_REQUIRED` | 차단 | `REPAIR_JOURNAL_BEFORE_SUBMIT` |
| `PENDING_JOURNAL_REMOVE_FAILURE` | `TradeHistoryRepository/TradingController` | `RECONCILIATION_REQUIRED` | 차단 | `RETRY_DURABLE_REMOVE` |
| `HISTORY_APPEND_FAILURE` | `TradeHistoryRepository/TradingController` | `RECONCILIATION_REQUIRED` | 차단 | `RETRY_HISTORY_APPEND_THEN_REMOVE` |
| `UNKNOWN_RECENT_APP_ORDER` | `TradingController/APIGateway` | `RECONCILIATION_REQUIRED` | 차단 | `QUERY_AND_CLASSIFY_APP_ORDER` |
| `TESTNET_RESET` | `TradingController/APIGateway` | `RECONCILIATION_REQUIRED` | 차단 | `REQUIRE_OPERATOR_RECONCILIATION` |
| `BALANCE_DECREASE` | `TradingController/Account` | `RECONCILIATION_REQUIRED` | 차단 | `RECONCILE_ACCOUNT_AND_POSITION` |
| `FEE_DUST_MISMATCH` | `TradingController/Position` | `RECONCILIATION_REQUIRED` | 차단 | `RECONCILE_FILL_FEE_AND_FREE_BALANCE` |
| `SIDECAR_PRE_READY_EXIT` | `NativeSidecarLifecycle` | `RECONCILIATION_REQUIRED` | 차단 | `REAP_CHILD_AND_SCAN_OWNERSHIP` |
| `SIDECAR_LATE_READY` | `NativeSidecarLifecycle` | `WAITING_READY` | 차단 | `WAIT_WITHIN_READY_DEADLINE` |
| `SIDECAR_ABNORMAL_EXIT` | `NativeSidecarLifecycle` | `RECONCILIATION_REQUIRED` | 차단 | `REAP_CHILD_AND_SCAN_OWNERSHIP` |
| `MAIN_CRASH_LISTENER_ORPHAN` | `ProcessWatchdog/TradingController` | `RECONCILIATION_REQUIRED` | 차단 | `VERIFY_PID_START_IDENTITY_AND_LOCK` |
| `SHUTDOWN_POSITION_DRIFT` | `TradingController` | `RECONCILIATION_REQUIRED` | 차단 | `RECONCILE_POSITION_BEFORE_CLOSED` |
| `SHUTDOWN_ORDER_DRIFT` | `TradingController` | `RECONCILIATION_REQUIRED` | 차단 | `RECONCILE_ORDER_BEFORE_CLOSED` |
| `SHUTDOWN_CLOSED_ACK_LOSS` | `NativeSidecarLifecycle` | `RECONCILIATION_REQUIRED` | 차단 | `VERIFY_CHILD_EXIT_AND_OWNERSHIP_ARTIFACT` |

검증 명령은 다음과 같다.

```sh
PYTHONPATH=backend/src:. backend/.venv/bin/python \
  scripts/phase13_deterministic_replay.py \
  backend/tests/fixtures/phase13/canonical_fault_trace.json \
  --repeat 5 --quiet
```

현재 canonical aggregate digest는
`a5f17e96f60e825faf4ccf89e0788133f8bfcac6a3654504a4f16c5c2f2f70f2`다.
