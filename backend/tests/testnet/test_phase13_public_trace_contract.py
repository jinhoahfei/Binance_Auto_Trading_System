"""Phase 13 public Case 2 trace의 exact schema, redaction와 hash 계약을 검증한다."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import unittest

from tests.testnet._phase13_trace import (
    PHASE13_PUBLIC_TRACE_RECORD_TYPE,
    PHASE13_PUBLIC_TRACE_SCHEMA_VERSION,
    PhaseThirteenPublicTraceValidationError,
    canonical_actual_phase13_public_trace_bytes,
    canonical_phase13_public_trace_bytes,
    seal_actual_phase13_public_trace,
    seal_phase13_public_trace,
    validate_actual_phase13_public_trace,
    validate_phase13_public_trace,
)


API_KEY_CANARY = "actual-testnet-key-canary-4d8e7931"
API_SECRET_CANARY = "actual-testnet-secret-canary-f8f22391"
SESSION_TOKEN_CANARY = "local-session-token-canary-84c57b0a"


def _create_success_order_execution_trace(
    *,
    sequence: int,
    intent_id: str,
    client_order_id: str,
    side: str,
    evaluation_id: str,
    exchange_order_id: str,
    context_version: int,
) -> dict[str, object]:
    """
    함수 이름: _create_success_order_execution_trace()
    기능: Contract fixture용 immediate BUY/SELL Communication 1~14 complete sequence를 만든다.
    인자: sequence -> 주문 순서
        intent_id -> attempt intent identity
        client_order_id -> application order identity
        side -> BUY 또는 SELL
        evaluation_id -> 최초 message 1의 실제 source event identity
        exchange_order_id -> message 7 이후 확인된 exchange identity
        context_version -> 단조 Context version fixture 값
    반환값: exact order_execution_traces entry
    작성 날짜: 2026/08/31
    """
    if side == "BUY":
        message_ids = (
            "1",
            "2",
            "3",
            "4",
            "5.1",
            "5",
            "6",
            "6.1",
            "7",
            "10",
            "12",
            "13",
            "13.2",
            "13.3",
            "13.4",
            "13.5",
            "13.5.1",
            "14",
        )
    elif side == "SELL":
        message_ids = (
            "1",
            "2",
            "3",
            "4",
            "5",
            "6",
            "6.1",
            "7",
            "10",
            "11",
            "12",
            "13",
            "13.1",
            "13.2",
            "13.3",
            "13.4",
            "13.5",
            "13.5.1",
            "14",
        )
    else:
        raise ValueError("side must be BUY or SELL")

    # Message 7 전에는 exchange ID가 아직 없고 이후에는 terminal result와 같은 ID만 기록한다.
    return {
        "sequence": sequence,
        "intent_id": intent_id,
        "client_order_id": client_order_id,
        "side": side,
        "entries": [
            {
                "sequence": entry_sequence,
                "message_id": message_id,
                "command_event_id": evaluation_id,
                "order_id": (
                    exchange_order_id
                    if message_ids.index(message_id) >= message_ids.index("7")
                    else None
                ),
                "context_version_before": context_version,
                "context_version_after": context_version,
                "result": "SUCCESS",
                "failure_code": None,
            }
            for entry_sequence, message_id in enumerate(message_ids, start=1)
        ],
    }


class PhaseThirteenPublicTraceContractTests(unittest.TestCase):
    """
    클래스 이름: PhaseThirteenPublicTraceContractTests
    기능: actual public Case 2 evidence의 strict schema, provenance, redaction와 digest를 검증한다.
    작성 날짜: 2026/08/31
    """

    def _success_trace_body(self) -> dict[str, object]:
        """
        함수 이름: _success_trace_body()
        기능: BUY와 same-run STOP recovery SELL을 가진 secret-free 성공 trace body를 만든다.
        인자: 없음
        반환값: trace_sha256를 제외한 mutable trace dictionary
        작성 날짜: 2026/08/31
        """
        # 한 source Kline과 version chain을 decision, BUY attempt와 public event에 반복해 결속한다.
        decision_fingerprint = {
            "source_event_id": "kline-event-0001",
            "source_kline_identity": "ETHUSDT:30m:1788134400000",
            "source_event_time": "2026-08-31T00:00:00.000Z",
            "evaluation_id": "evaluation-0001",
            "market_version": 7,
            "account_version": 3,
            "context_version": 12,
            "policy_version": 2,
            "regime": "TYPE_0",
            "action_type": "SUBMIT_ORDER",
            "side": "BUY",
            "strategy": "CASE_C",
            "intent_id": "case2-buy-intent-0001",
            "client_order_id": "bat-phase13-buy-0001",
            "decision_price": "2500",
            "final_submitted_quantity": "0.004",
            "final_notional": "10.000",
            "configured_cap": "100",
        }
        public_market_events = []
        for sequence, message_id, event_type in (
            (1, "1L.1", "KLINE_OBSERVED"),
            (2, "1L.2", "MARKET_EVALUATED"),
            (3, "1L.3", "ACTION_EMITTED"),
        ):
            action_event = message_id == "1L.3"
            public_market_events.append(
                {
                    "sequence": sequence,
                    "message_id": message_id,
                    "event_type": event_type,
                    "source_event_id": "kline-event-0001",
                    "source_kline_identity": "ETHUSDT:30m:1788134400000",
                    "source_event_time": "2026-08-31T00:00:00.000Z",
                    "market_version": 7,
                    "context_version": 12,
                    "evaluation_id": "evaluation-0001",
                    "regime": "TYPE_0",
                    "action_type": "SUBMIT_ORDER" if action_event else None,
                    "side": "BUY" if action_event else None,
                    "strategy": "CASE_C" if action_event else None,
                }
            )
        public_account_events = [
            {
                "sequence": 1,
                "message_id": "2.2.1",
                "event_type": "ACCOUNT_POSITION_APPLIED",
                "source_event_id": "account-event-0001",
                "source_event_time": "2026-08-31T00:00:00.100Z",
                "account_version": 3,
                "asset": "ETH",
                "free_quantity": "1.004",
                "locked_quantity": "0",
            }
        ]

        # Attempt에는 filter 뒤 최종 수량과 Decimal notional만 남기고 signed request는 남기지 않는다.
        order_attempts = [
            {
                "sequence": 1,
                "attempted_at": "2026-08-31T00:00:01.000Z",
                "evaluation_id": "evaluation-0001",
                "intent_id": "case2-buy-intent-0001",
                "client_order_id": "bat-phase13-buy-0001",
                "submission_attempt": 0,
                "symbol": "ETHUSDT",
                "regime": "TYPE_0",
                "strategy": "CASE_C",
                "action_type": "SUBMIT_ORDER",
                "side": "BUY",
                "exit_reason": None,
                "decision_price": "2500",
                "final_submitted_quantity": "0.004",
                "final_notional": "10.000",
                "configured_cap": "100",
                "policy_version": 2,
            },
            {
                "sequence": 2,
                "attempted_at": "2026-08-31T00:00:20.000Z",
                "evaluation_id": "recovery-evaluation-0001",
                "intent_id": "recovery-sell-intent-0001",
                "client_order_id": "bat-phase13-sell-0001",
                "submission_attempt": 0,
                "symbol": "ETHUSDT",
                "regime": "TYPE_0",
                "strategy": "CASE_C",
                "action_type": "SUBMIT_ORDER",
                "side": "SELL",
                "exit_reason": "STOP",
                "decision_price": "2505",
                "final_submitted_quantity": "0.004",
                "final_notional": "10.020",
                "configured_cap": "100",
                "policy_version": 2,
            },
        ]
        order_results = [
            {
                "sequence": 1,
                "observed_at": "2026-08-31T00:00:02.000Z",
                "intent_id": "case2-buy-intent-0001",
                "client_order_id": "bat-phase13-buy-0001",
                "exchange_order_id": "9100001",
                "status": "FILLED",
                "failure_code": None,
                "incremental_fills": [
                    {
                        "fill_id": "7100001",
                        "durable_trade_id": "trade-buy-0001",
                        "event_time": "2026-08-31T00:00:01.500Z",
                        "price": "2500",
                        "quantity": "0.004",
                        "quote_amount": "10.000",
                        "fee_amount": "0",
                        "fee_asset": "USDT",
                        "fee_quote_amount": "0",
                    }
                ],
            },
            {
                "sequence": 2,
                "observed_at": "2026-08-31T00:00:21.000Z",
                "intent_id": "recovery-sell-intent-0001",
                "client_order_id": "bat-phase13-sell-0001",
                "exchange_order_id": "9100002",
                "status": "FILLED",
                "failure_code": None,
                "incremental_fills": [
                    {
                        "fill_id": "7100002",
                        "durable_trade_id": "trade-sell-0001",
                        "event_time": "2026-08-31T00:00:20.500Z",
                        "price": "2505",
                        "quantity": "0.004",
                        "quote_amount": "10.020",
                        "fee_amount": "0",
                        "fee_asset": "USDT",
                        "fee_quote_amount": "0",
                    }
                ],
            },
        ]
        transport_ui_event_batch = {
            "transport_session_id": "00000000-0000-4000-8000-000000000050",
            "events": [
                {
                    "event_id": "00000000-0000-4000-8000-000000000101",
                    "transport_sequence": 9,
                    "aggregate": "ACCOUNT",
                    "event_type": "ACCOUNT_UPDATED",
                    "aggregate_version": 3,
                    "related_id": None,
                    "published_at": "2026-08-31T00:00:00.200Z",
                },
                {
                    "event_id": "00000000-0000-4000-8000-000000000102",
                    "transport_sequence": 10,
                    "aggregate": "TRADE_HISTORY",
                    "event_type": "ORDER_EXECUTED",
                    "aggregate_version": None,
                    "related_id": "trade-buy-0001",
                    "published_at": "2026-08-31T00:00:03.000Z",
                },
                {
                    "event_id": "00000000-0000-4000-8000-000000000103",
                    "transport_sequence": 11,
                    "aggregate": "TRADE_HISTORY",
                    "event_type": "PERFORMANCE_UPDATED",
                    "aggregate_version": None,
                    "related_id": "trade-buy-0001",
                    "published_at": "2026-08-31T00:00:03.000Z",
                },
                {
                    "event_id": "00000000-0000-4000-8000-000000000104",
                    "transport_sequence": 12,
                    "aggregate": "TRADE_HISTORY",
                    "event_type": "ORDER_EXECUTED",
                    "aggregate_version": None,
                    "related_id": "trade-sell-0001",
                    "published_at": "2026-08-31T00:00:22.000Z",
                },
                {
                    "event_id": "00000000-0000-4000-8000-000000000105",
                    "transport_sequence": 13,
                    "aggregate": "TRADE_HISTORY",
                    "event_type": "PERFORMANCE_UPDATED",
                    "aggregate_version": None,
                    "related_id": "trade-sell-0001",
                    "published_at": "2026-08-31T00:00:22.000Z",
                },
                {
                    "event_id": "00000000-0000-4000-8000-000000000106",
                    "transport_sequence": 14,
                    "aggregate": "TRADING_SESSION",
                    "event_type": "TRADING_SESSION_UPDATED",
                    "aggregate_version": 2,
                    "related_id": "bat-phase13-sell-0001",
                    "published_at": "2026-08-31T00:00:23.000Z",
                },
            ],
        }
        fresh_filter_rules = {
            "symbol_status": "TRADING",
            "base_asset": "ETH",
            "quote_asset": "USDT",
            "base_asset_precision": 8,
            "is_spot_trading_allowed": True,
            "market_order_allowed": True,
            "lot_size_minimum_quantity": "0.0001",
            "lot_size_maximum_quantity": "1000",
            "lot_size_step_size": "0.0001",
            "market_lot_size_minimum_quantity": "0",
            "market_lot_size_maximum_quantity": "1000",
            "market_lot_size_step_size": "0.0001",
            "minimum_notional": "5",
            "maximum_notional": None,
            "maximum_position": None,
        }
        account_asset_filters = [
            {
                "filter_type": "MAX_ASSET",
                "asset": "ETH",
                "maximum_quantity": "100",
            }
        ]
        account_relevant_filters = {
            "symbol": "ETHUSDT",
            "exchange_order_count_filters": [
                {
                    "filter_type": "EXCHANGE_MAX_NUM_ORDERS",
                    "maximum_count": 1000,
                }
            ],
            "symbol_order_count_filters": [
                {
                    "filter_type": "MAX_NUM_ORDERS",
                    "maximum_count": 100,
                }
            ],
            "symbol_quantity_filters": [
                {
                    "filter_type": "LOT_SIZE",
                    "minimum_quantity": "0.0001",
                    "maximum_quantity": "1000",
                    "step_size": "0.0001",
                },
                {
                    "filter_type": "MARKET_LOT_SIZE",
                    "minimum_quantity": "0",
                    "maximum_quantity": "1000",
                    "step_size": "0.0001",
                },
            ],
            "symbol_notional_filters": [
                {
                    "filter_type": "MIN_NOTIONAL",
                    "minimum_notional": "5",
                    "maximum_notional": None,
                    "apply_minimum_to_market": True,
                    "apply_maximum_to_market": False,
                    "average_price_minutes": 5,
                }
            ],
            "symbol_maximum_position": None,
            "passive_symbol_filter_types": [
                "ICEBERG_PARTS",
                "PERCENT_PRICE",
                "PRICE_FILTER",
                "TRAILING_DELTA",
            ],
            "asset_filters": deepcopy(account_asset_filters),
        }
        public_relevant_filters = deepcopy(account_relevant_filters)
        public_relevant_filters["asset_filters"] = []
        submit_time_filter_evidence = [
            {
                "sequence": 1,
                "intent_id": "case2-buy-intent-0001",
                "client_order_id": "bat-phase13-buy-0001",
                "side": "BUY",
                "observed_at": "2026-08-31T00:00:00.900Z",
                "rules": deepcopy(fresh_filter_rules),
                "account_asset_filters": deepcopy(account_asset_filters),
                "account_relevant_filters": deepcopy(
                    account_relevant_filters
                ),
                "public_relevant_filters": deepcopy(
                    public_relevant_filters
                ),
                "account_filters_observed_at": (
                    "2026-08-31T00:00:00.850Z"
                ),
                "account_open_orders_observed_at": (
                    "2026-08-31T00:00:00.910Z"
                ),
                "account_open_orders_verified_empty": True,
                "account_open_order_lists_observed_at": (
                    "2026-08-31T00:00:00.920Z"
                ),
                "account_open_order_lists_verified_empty": True,
                "reference_price": {
                    "symbol": "ETHUSDT",
                    "price": "2500",
                    "exchange_timestamp": 1788134400900,
                },
                "reference_price_observed_at": (
                    "2026-08-31T00:00:00.950Z"
                ),
            },
            {
                "sequence": 2,
                "intent_id": "recovery-sell-intent-0001",
                "client_order_id": "bat-phase13-sell-0001",
                "side": "SELL",
                "observed_at": "2026-08-31T00:00:19.900Z",
                "rules": deepcopy(fresh_filter_rules),
                "account_asset_filters": deepcopy(account_asset_filters),
                "account_relevant_filters": deepcopy(
                    account_relevant_filters
                ),
                "public_relevant_filters": deepcopy(
                    public_relevant_filters
                ),
                "account_filters_observed_at": (
                    "2026-08-31T00:00:19.850Z"
                ),
                "account_open_orders_observed_at": (
                    "2026-08-31T00:00:19.910Z"
                ),
                "account_open_orders_verified_empty": True,
                "account_open_order_lists_observed_at": (
                    "2026-08-31T00:00:19.920Z"
                ),
                "account_open_order_lists_verified_empty": True,
                "reference_price": {
                    "symbol": "ETHUSDT",
                    "price": "2505",
                    "exchange_timestamp": 1788134419900,
                },
                "reference_price_observed_at": (
                    "2026-08-31T00:00:19.950Z"
                ),
            },
        ]

        # Preflight snapshot과 두 production prepare-time rule을 분리해 submit 산술의 권위를 보존한다.
        return {
            "schema_version": PHASE13_PUBLIC_TRACE_SCHEMA_VERSION,
            "record_type": PHASE13_PUBLIC_TRACE_RECORD_TYPE,
            "outcome": "SUCCESS",
            "typed_reason": None,
            "run_id": "00000000-0000-4000-8000-000000000013",
            "timestamps": {
                "started_at": "2026-08-31T00:00:00.000Z",
                "completed_at": "2026-08-31T00:00:30.000Z",
            },
            "preflight": {
                "endpoint_set": "BINANCE_SPOT_TESTNET",
                "symbol": "ETHUSDT",
                "can_trade": True,
                "account_stream_status": "READY",
                "market_stream_status": "READY",
                "account_version": 3,
                "verified_at": "2026-08-31T00:00:00.050Z",
                "commission_policy": {
                    "symbol": "ETHUSDT",
                    "enabled_for_account": False,
                    "enabled_for_symbol": True,
                    "discount_asset": None,
                    "discount_rate": "0",
                    "standard_market_buy_rate": "0",
                    "special_market_buy_rate": "0",
                    "tax_market_buy_rate": "0",
                    "market_buy_received_asset_commission_rate": "0",
                    "can_charge_discount_asset": False,
                },
                "fresh_filters": {
                    "observed_at": "2026-08-31T00:00:00.040Z",
                    **fresh_filter_rules,
                },
                "account_asset_filters": deepcopy(account_asset_filters),
                "account_relevant_filters": deepcopy(
                    account_relevant_filters
                ),
                "public_relevant_filters": deepcopy(
                    public_relevant_filters
                ),
                "account_filters_observed_at": (
                    "2026-08-31T00:00:00.030Z"
                ),
                "account_open_orders_observed_at": (
                    "2026-08-31T00:00:00.041Z"
                ),
                "account_open_orders_verified_empty": True,
                "account_open_order_lists_observed_at": (
                    "2026-08-31T00:00:00.042Z"
                ),
                "account_open_order_lists_verified_empty": True,
                "reference_price": {
                    "symbol": "ETHUSDT",
                    "price": "2500",
                    "exchange_timestamp": 1788134400040,
                },
                "reference_price_observed_at": (
                    "2026-08-31T00:00:00.045Z"
                ),
                "position_quantity": "0",
                "pending_order_count": 0,
                "unknown_order_count": 0,
                "matching_open_order_count": 0,
            },
            "immutable_decision_fingerprint": decision_fingerprint,
            "public_market_events": public_market_events,
            "public_account_events": public_account_events,
            "order_attempts": order_attempts,
            "order_execution_traces": [
                _create_success_order_execution_trace(
                    sequence=1,
                    intent_id="case2-buy-intent-0001",
                    client_order_id="bat-phase13-buy-0001",
                    side="BUY",
                    evaluation_id="evaluation-0001",
                    exchange_order_id="9100001",
                    context_version=12,
                ),
                _create_success_order_execution_trace(
                    sequence=2,
                    intent_id="recovery-sell-intent-0001",
                    client_order_id="bat-phase13-sell-0001",
                    side="SELL",
                    evaluation_id="recovery-evaluation-0001",
                    exchange_order_id="9100002",
                    context_version=20,
                ),
            ],
            "submit_time_filter_evidence": submit_time_filter_evidence,
            "order_results": order_results,
            "baseline_history_sha256": (
                "e3b0c44298fc1c149afbf4c8996fb924"
                "27ae41e4649b934ca495991b7852b855"
            ),
            "baseline_history_count": 0,
            "run_durable_trades": [
                {
                    "trade_id": "trade-buy-0001",
                    "client_order_id": "bat-phase13-buy-0001",
                    "exchange_order_id": "9100001",
                    "symbol": "ETHUSDT",
                    "side": "BUY",
                    "regime": "TYPE_0",
                    "strategy": "CASE_C",
                    "executed_quantity": "0.004",
                    "executed_amount": "10.000",
                    "average_fill_price": "2500",
                    "fee_amount": "0",
                    "fee_asset": "USDT",
                    "fee_quote_amount": "0",
                    "realized_profit_loss": None,
                    "exit_reason": None,
                    "executed_at": "2026-08-31T00:00:01.500Z",
                },
                {
                    "trade_id": "trade-sell-0001",
                    "client_order_id": "bat-phase13-sell-0001",
                    "exchange_order_id": "9100002",
                    "symbol": "ETHUSDT",
                    "side": "SELL",
                    "regime": "TYPE_0",
                    "strategy": "CASE_C",
                    "executed_quantity": "0.004",
                    "executed_amount": "10.020",
                    "average_fill_price": "2505",
                    "fee_amount": "0",
                    "fee_asset": "USDT",
                    "fee_quote_amount": "0",
                    "realized_profit_loss": "0.020",
                    "exit_reason": "STOP",
                    "executed_at": "2026-08-31T00:00:20.500Z",
                },
            ],
            "transport_ui_event_batch": transport_ui_event_batch,
            "recovery": {
                "required": True,
                "attempted": True,
                "outcome": "SUCCESS",
                "intent_id": "recovery-sell-intent-0001",
                "client_order_id": "bat-phase13-sell-0001",
                "authoritative_position_quantity": "0.004",
                "effective_free_quantity": "0.004",
                "submitted_quantity": "0.004",
                "final_position_quantity": "0",
                "pending_order_count": 0,
                "matching_open_order_count": 0,
                "duplicate_order_count": 0,
                "duplicate_trade_count": 0,
            },
            "final_state": {
                "position_quantity": "0",
                "pending_order_count": 0,
                "unknown_order_count": 0,
                "matching_open_order_count": 0,
                "actual_order_count": 2,
                "duplicate_order_count": 0,
                "duplicate_trade_count": 0,
                "verified_at": "2026-08-31T00:00:29.000Z",
                "fresh_runtime_session_id": (
                    "00000000-0000-4000-8000-000000000200"
                ),
                "performance": {
                    "baseline_trade_count": 0,
                    "run_trade_count": 2,
                    "total_trade_count": 2,
                    "baseline_realized_profit_loss": "0",
                    "run_realized_profit_loss": "0.020",
                    "total_realized_profit_loss": "0.020",
                    "baseline_fee_quote": "0",
                    "run_fee_quote": "0",
                    "total_fee_quote": "0",
                },
            },
        }

    def _no_signal_trace_body(self) -> dict[str, object]:
        """
        함수 이름: _no_signal_trace_body()
        기능: production signal이 없어 주문을 만들지 않은 normalized NO_SIGNAL trace를 만든다.
        인자: 없음
        반환값: 주문·decision evidence가 비어 있는 mutable trace dictionary
        작성 날짜: 2026/08/31
        """
        trace_body = self._success_trace_body()

        # Public 관측은 보존하되 성공 decision과 모든 mutation evidence를 제거한다.
        trace_body["outcome"] = "NO_SIGNAL"
        trace_body["typed_reason"] = None
        trace_body["immutable_decision_fingerprint"] = None
        market_event = trace_body["public_market_events"][0]
        market_event["evaluation_id"] = None
        market_event["action_type"] = None
        market_event["side"] = None
        market_event["strategy"] = None
        trace_body["public_market_events"] = [market_event]
        trace_body["order_attempts"] = []
        trace_body["order_execution_traces"] = []
        trace_body["submit_time_filter_evidence"] = []
        trace_body["order_results"] = []
        trace_body["run_durable_trades"] = []
        trace_body["transport_ui_event_batch"]["events"] = [
            event
            for event in trace_body["transport_ui_event_batch"]["events"]
            if event["event_type"]
            not in {"ORDER_EXECUTED", "PERFORMANCE_UPDATED"}
        ]
        trace_body["recovery"] = {
            "required": False,
            "attempted": False,
            "outcome": "NOT_REQUIRED",
            "intent_id": None,
            "client_order_id": None,
            "authoritative_position_quantity": "0",
            "effective_free_quantity": "0",
            "submitted_quantity": None,
            "final_position_quantity": "0",
            "pending_order_count": 0,
            "matching_open_order_count": 0,
            "duplicate_order_count": 0,
            "duplicate_trade_count": 0,
        }
        trace_body["final_state"] = {
            "position_quantity": "0",
            "pending_order_count": 0,
            "unknown_order_count": 0,
            "matching_open_order_count": 0,
            "actual_order_count": 0,
            "duplicate_order_count": 0,
            "duplicate_trade_count": 0,
            "verified_at": "2026-08-31T00:00:29.000Z",
            "fresh_runtime_session_id": (
                "00000000-0000-4000-8000-000000000200"
            ),
            "performance": {
                "baseline_trade_count": 0,
                "run_trade_count": 0,
                "total_trade_count": 0,
                "baseline_realized_profit_loss": "0",
                "run_realized_profit_loss": "0",
                "total_realized_profit_loss": "0",
                "baseline_fee_quote": "0",
                "run_fee_quote": "0",
                "total_fee_quote": "0",
            },
        }

        return trace_body  # NO_SIGNAL도 public input과 UI batch를 버리지 않는다.

    def _legacy_v2_trace_body(self) -> dict[str, object]:
        """
        함수 이름: _legacy_v2_trace_body()
        기능: v3 fixture에서 새 account filter/state 필드만 제거한
            보존용 v2 body를 만든다.
        인자: 없음
        반환값: 기존 exact v2 nested schema를 가진 mutable trace dictionary
        작성 날짜: 2026/08/31
        """
        trace_body = self._success_trace_body()
        trace_body["schema_version"] = 2
        v3_only_fields = (
            "account_relevant_filters",
            "public_relevant_filters",
            "account_open_orders_observed_at",
            "account_open_orders_verified_empty",
            "account_open_order_lists_observed_at",
            "account_open_order_lists_verified_empty",
        )

        # Preflight와 각 prepare evidence에서 동일한 v3 확장만 제거해
        # v2 exact schema를 재현한다.
        for field_name in v3_only_fields:
            del trace_body["preflight"][field_name]
        for evidence in trace_body["submit_time_filter_evidence"]:
            for field_name in v3_only_fields:
                del evidence[field_name]

        return trace_body  # V2 causal order를 유지해 backward validator만 검증한다.

    def _assert_body_is_rejected(self, trace_body: dict[str, object]) -> None:
        """
        함수 이름: _assert_body_is_rejected()
        기능: 잘못된 trace body가 seal 전에 typed validation 오류로 차단되는지 검증한다.
        인자: trace_body -> 의도적으로 변조한 trace body
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with self.assertRaises(PhaseThirteenPublicTraceValidationError):
            seal_phase13_public_trace(trace_body)

    def test_success_trace_is_canonical_and_digest_is_byte_stable(self) -> None:
        """
        함수 이름: test_success_trace_is_canonical_and_digest_is_byte_stable()
        기능: 성공 trace의 exact SHA-256와 complete document bytes가 반복 실행에서 일치하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        trace_body = self._success_trace_body()

        # Seal은 입력 body를 수정하지 않고 body canonical bytes에만 digest를 결합해야 한다.
        self.assertEqual(3, PHASE13_PUBLIC_TRACE_SCHEMA_VERSION)
        sealed_trace = seal_phase13_public_trace(trace_body)
        first_bytes = canonical_phase13_public_trace_bytes(sealed_trace)
        second_bytes = canonical_phase13_public_trace_bytes(deepcopy(sealed_trace))
        recorded_digest = validate_phase13_public_trace(sealed_trace)
        expected_body_bytes = (
            json.dumps(
                trace_body,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

        self.assertNotIn("trace_sha256", trace_body)
        self.assertEqual(recorded_digest, sealed_trace["trace_sha256"])
        self.assertEqual(recorded_digest, hashlib.sha256(expected_body_bytes).hexdigest())
        self.assertEqual(first_bytes, second_bytes)
        self.assertTrue(first_bytes.endswith(b"\n"))
        self.assertEqual(json.loads(first_bytes), sealed_trace)

    def test_preserved_schema_v2_trace_remains_verifiable(self) -> None:
        """
        함수 이름: test_preserved_schema_v2_trace_remains_verifiable()
        기능: v3 도입 뒤에도 exact v2 body·digest를 offline validator가
            계속 수용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        legacy_trace_body = self._legacy_v2_trace_body()

        # Legacy seal과 validation은 v3 필드를 합성하지 않고
        # 원 v2 canonical bytes를 그대로 유지한다.
        sealed_trace = seal_phase13_public_trace(legacy_trace_body)
        recorded_digest = validate_phase13_public_trace(sealed_trace)

        self.assertEqual(2, sealed_trace["schema_version"])
        self.assertNotIn(
            "account_relevant_filters",
            sealed_trace["preflight"],
        )
        self.assertEqual(recorded_digest, sealed_trace["trace_sha256"])

    def test_all_normalized_outcomes_are_supported_without_synthesizing_orders(
        self,
    ) -> None:
        """
        함수 이름: test_all_normalized_outcomes_are_supported_without_synthesizing_orders()
        기능: SUCCESS, NO_SIGNAL, BLOCKED와 FAILED가 각 mutation 계약에 맞게 seal되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        trace_bodies = {
            "SUCCESS": self._success_trace_body(),
            "NO_SIGNAL": self._no_signal_trace_body(),
            "BLOCKED": self._no_signal_trace_body(),
            "FAILED": self._no_signal_trace_body(),
        }
        trace_bodies["BLOCKED"]["outcome"] = "BLOCKED"
        trace_bodies["BLOCKED"]["typed_reason"] = "RISK_POLICY_BLOCKED"
        trace_bodies["FAILED"]["outcome"] = "FAILED"
        trace_bodies["FAILED"]["typed_reason"] = "TRACE_CAPTURE_FAILED"

        # Order 없는 종료를 성공으로 바꾸지 않고 네 normalized outcome만 exact enum으로 허용한다.
        for expected_outcome, trace_body in trace_bodies.items():
            with self.subTest(outcome=expected_outcome):
                sealed_trace = seal_phase13_public_trace(trace_body)
                self.assertEqual(sealed_trace["outcome"], expected_outcome)
                self.assertEqual(
                    validate_phase13_public_trace(sealed_trace),
                    sealed_trace["trace_sha256"],
                )

    def test_unknown_missing_and_version_drift_fields_fail_closed(self) -> None:
        """
        함수 이름: test_unknown_missing_and_version_drift_fields_fail_closed()
        기능: root·nested schema 확장, 필수 field 누락과 schema/record drift를 모두 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        invalid_trace_bodies: list[tuple[str, dict[str, object]]] = []

        # Root unknown, nested missing과 버전 drift를 별도 사본으로 만들어 실패 위치를 독립시킨다.
        unknown_root = self._success_trace_body()
        unknown_root["unexpected_field"] = "UNSUPPORTED"
        invalid_trace_bodies.append(("unknown_root", unknown_root))
        missing_nested = self._success_trace_body()
        del missing_nested["public_market_events"][0]["context_version"]
        invalid_trace_bodies.append(("missing_nested", missing_nested))
        missing_account_filters = self._success_trace_body()
        del missing_account_filters["preflight"]["account_asset_filters"]
        invalid_trace_bodies.append(
            ("missing_account_filters", missing_account_filters)
        )
        missing_reference_price = self._success_trace_body()
        del missing_reference_price["submit_time_filter_evidence"][0][
            "reference_price"
        ]
        invalid_trace_bodies.append(
            ("missing_reference_price", missing_reference_price)
        )
        unknown_nested = self._success_trace_body()
        unknown_nested["final_state"]["unexpected_field"] = 0
        invalid_trace_bodies.append(("unknown_nested", unknown_nested))
        unknown_account_filter = self._success_trace_body()
        unknown_account_filter["preflight"]["account_asset_filters"][0][
            "current_balance"
        ] = "0"
        invalid_trace_bodies.append(
            ("unknown_account_filter", unknown_account_filter)
        )
        unknown_reference_price = self._success_trace_body()
        unknown_reference_price["preflight"]["reference_price"][
            "average_price_minutes"
        ] = 5
        invalid_trace_bodies.append(
            ("unknown_reference_price", unknown_reference_price)
        )
        schema_drift = self._success_trace_body()
        schema_drift["schema_version"] = PHASE13_PUBLIC_TRACE_SCHEMA_VERSION + 1
        invalid_trace_bodies.append(("schema_drift", schema_drift))
        legacy_schema = self._success_trace_body()
        legacy_schema["schema_version"] = 1
        invalid_trace_bodies.append(("legacy_schema", legacy_schema))
        record_drift = self._success_trace_body()
        record_drift["record_type"] = "phase13_public_case2_future_trace"
        invalid_trace_bodies.append(("record_drift", record_drift))
        outcome_drift = self._success_trace_body()
        outcome_drift["outcome"] = "SKIPPED"
        invalid_trace_bodies.append(("outcome_drift", outcome_drift))

        for case_name, trace_body in invalid_trace_bodies:
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

    def test_float_nonfinite_and_non_plain_decimal_values_are_rejected(self) -> None:
        """
        함수 이름: test_float_nonfinite_and_non_plain_decimal_values_are_rejected()
        기능: JSON float, NaN/Infinity 문자열과 exponent 금융 문자열을 digest 전에 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        invalid_trace_bodies: list[tuple[str, dict[str, object]]] = []

        # Float는 finite 여부와 무관하게 금지하고 nonfinite/exponent 문자열도 Decimal schema로 거부한다.
        finite_float = self._success_trace_body()
        finite_float["public_market_events"][0]["market_version"] = 7.0
        invalid_trace_bodies.append(("finite_float", finite_float))
        nan_float = self._success_trace_body()
        nan_float["public_account_events"][0]["account_version"] = float("nan")
        invalid_trace_bodies.append(("nan_float", nan_float))
        for case_name, invalid_decimal in (
            ("nan_string", "NaN"),
            ("infinity_string", "Infinity"),
            ("exponent_string", "1e1"),
        ):
            invalid_body = self._success_trace_body()
            invalid_body["immutable_decision_fingerprint"][
                "decision_price"
            ] = invalid_decimal
            invalid_trace_bodies.append((case_name, invalid_body))

        for case_name, trace_body in invalid_trace_bodies:
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

    def test_v3_account_filter_and_open_state_schema_fail_closed(self) -> None:
        """
        함수 이름: test_v3_account_filter_and_open_state_schema_fail_closed()
        기능: Full account filter projection과 두 account-wide empty 관찰의
            drift를 모두 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        invalid_trace_bodies: list[tuple[str, dict[str, object]]] = []

        # V3 필수 object·nested field 누락과 unknown raw 확장을
        # exact schema 단계에서 차단한다.
        missing_composite = self._success_trace_body()
        del missing_composite["preflight"]["account_relevant_filters"]
        invalid_trace_bodies.append(("missing_composite", missing_composite))
        missing_public_composite = self._success_trace_body()
        del missing_public_composite["preflight"]["public_relevant_filters"]
        invalid_trace_bodies.append(
            ("missing_public_composite", missing_public_composite)
        )
        missing_nested = self._success_trace_body()
        del missing_nested["preflight"]["account_relevant_filters"][
            "passive_symbol_filter_types"
        ]
        invalid_trace_bodies.append(("missing_nested", missing_nested))
        unknown_nested = self._success_trace_body()
        unknown_nested["preflight"]["account_relevant_filters"][
            "raw_filter_payload"
        ] = {}
        invalid_trace_bodies.append(("unknown_nested", unknown_nested))

        # Symbol binding, scope, count type·경계와 passive canonical order를
        # 각각 독립 변조한다.
        wrong_symbol = self._success_trace_body()
        wrong_symbol["preflight"]["account_relevant_filters"][
            "symbol"
        ] = "BTCUSDT"
        invalid_trace_bodies.append(("wrong_symbol", wrong_symbol))
        wrong_count_scope = self._success_trace_body()
        wrong_count_scope["preflight"]["account_relevant_filters"][
            "exchange_order_count_filters"
        ][0]["filter_type"] = "MAX_NUM_ORDERS"
        invalid_trace_bodies.append(("wrong_count_scope", wrong_count_scope))
        boolean_count = self._success_trace_body()
        boolean_count["preflight"]["account_relevant_filters"][
            "symbol_order_count_filters"
        ][0]["maximum_count"] = True
        invalid_trace_bodies.append(("boolean_count", boolean_count))
        zero_normal_order_limit = self._success_trace_body()
        zero_normal_order_limit["preflight"]["account_relevant_filters"][
            "symbol_order_count_filters"
        ][0]["maximum_count"] = 0
        invalid_trace_bodies.append(
            ("zero_normal_order_limit", zero_normal_order_limit)
        )
        duplicate_count = self._success_trace_body()
        duplicate_count["preflight"]["account_relevant_filters"][
            "symbol_order_count_filters"
        ].append(
            deepcopy(
                duplicate_count["preflight"]["account_relevant_filters"][
                    "symbol_order_count_filters"
                ][0]
            )
        )
        invalid_trace_bodies.append(("duplicate_count", duplicate_count))
        unsorted_passive = self._success_trace_body()
        unsorted_passive["preflight"]["account_relevant_filters"][
            "passive_symbol_filter_types"
        ].reverse()
        invalid_trace_bodies.append(("unsorted_passive", unsorted_passive))
        unknown_passive = self._success_trace_body()
        unknown_passive["preflight"]["account_relevant_filters"][
            "passive_symbol_filter_types"
        ].append("T_PLUS_SELL")
        invalid_trace_bodies.append(("unknown_passive", unknown_passive))

        # DTO scalar schema와 v2 asset projection이 달라져도
        # normalized evidence로 봉인하지 않는다.
        negative_quantity = self._success_trace_body()
        negative_quantity["preflight"]["account_relevant_filters"][
            "symbol_quantity_filters"
        ][0]["step_size"] = "-0.0001"
        invalid_trace_bodies.append(("negative_quantity", negative_quantity))
        wrong_notional_flag = self._success_trace_body()
        wrong_notional_flag["preflight"]["account_relevant_filters"][
            "symbol_notional_filters"
        ][0]["apply_minimum_to_market"] = 1
        invalid_trace_bodies.append(
            ("wrong_notional_flag", wrong_notional_flag)
        )
        mismatched_asset_projection = self._success_trace_body()
        mismatched_asset_projection["preflight"]["account_relevant_filters"][
            "asset_filters"
        ][0]["maximum_quantity"] = "99"
        invalid_trace_bodies.append(
            ("mismatched_asset_projection", mismatched_asset_projection)
        )
        public_asset_scope = self._success_trace_body()
        public_asset_scope["preflight"]["public_relevant_filters"][
            "asset_filters"
        ].append(
            deepcopy(public_asset_scope["preflight"]["account_asset_filters"][0])
        )
        invalid_trace_bodies.append(("public_asset_scope", public_asset_scope))

        # Account-wide state는 두 exact True와
        # openOrders→openOrderList UTC 순서를 모두 요구한다.
        nonempty_orders = self._success_trace_body()
        nonempty_orders["preflight"][
            "account_open_orders_verified_empty"
        ] = False
        invalid_trace_bodies.append(("nonempty_orders", nonempty_orders))
        non_boolean_lists = self._success_trace_body()
        non_boolean_lists["preflight"][
            "account_open_order_lists_verified_empty"
        ] = 1
        invalid_trace_bodies.append(("non_boolean_lists", non_boolean_lists))
        reversed_open_state = self._success_trace_body()
        reversed_open_state["preflight"][
            "account_open_orders_observed_at"
        ] = "2026-08-31T00:00:00.043Z"
        invalid_trace_bodies.append(
            ("reversed_open_state", reversed_open_state)
        )
        late_preflight_state = self._success_trace_body()
        late_preflight_state["preflight"][
            "account_open_orders_observed_at"
        ] = "2026-08-31T00:00:00.051Z"
        late_preflight_state["preflight"][
            "account_open_order_lists_observed_at"
        ] = "2026-08-31T00:00:00.052Z"
        late_preflight_state["preflight"][
            "reference_price_observed_at"
        ] = "2026-08-31T00:00:00.053Z"
        invalid_trace_bodies.append(
            ("late_preflight_state", late_preflight_state)
        )
        late_submit_state = self._success_trace_body()
        late_submit_state["submit_time_filter_evidence"][0][
            "account_open_orders_observed_at"
        ] = "2026-08-31T00:00:01.010Z"
        late_submit_state["submit_time_filter_evidence"][0][
            "account_open_order_lists_observed_at"
        ] = "2026-08-31T00:00:01.020Z"
        late_submit_state["submit_time_filter_evidence"][0][
            "reference_price_observed_at"
        ] = "2026-08-31T00:00:01.030Z"
        invalid_trace_bodies.append(("late_submit_state", late_submit_state))

        for case_name, trace_body in invalid_trace_bodies:
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

        # V2 body에 v3 nested field를 섞으면
        # version downgrade로 새 증거를 우회할 수 없다.
        legacy_with_v3_fields = self._success_trace_body()
        legacy_with_v3_fields["schema_version"] = 2
        self._assert_body_is_rejected(legacy_with_v3_fields)

    def test_hash_tamper_and_noncanonical_digest_are_rejected(self) -> None:
        """
        함수 이름: test_hash_tamper_and_noncanonical_digest_are_rejected()
        기능: seal 이후 safe field tamper와 uppercase/잘못된 길이 digest가 검증을 통과하지 못하는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        sealed_trace = seal_phase13_public_trace(self._success_trace_body())

        # Schema가 여전히 유효한 event type 변경도 원 body SHA-256와 달라져야 한다.
        tampered_trace = deepcopy(sealed_trace)
        tampered_trace["transport_ui_event_batch"]["events"][0][
            "related_id"
        ] = "account-safe-tamper"
        with self.assertRaisesRegex(
            PhaseThirteenPublicTraceValidationError,
            "digest does not match",
        ):
            validate_phase13_public_trace(tampered_trace)

        # Digest field 자체는 lowercase 64자리 외 표현을 허용하지 않는다.
        for invalid_digest in ("0" * 63, "A" * 64):
            with self.subTest(invalid_digest=invalid_digest[:4]):
                invalid_trace = deepcopy(sealed_trace)
                invalid_trace["trace_sha256"] = invalid_digest
                with self.assertRaises(PhaseThirteenPublicTraceValidationError):
                    validate_phase13_public_trace(invalid_trace)

    def test_transport_truth_uses_envelope_sequence_and_atomic_trade_pairs(self) -> None:
        """
        함수 이름: test_transport_truth_uses_envelope_sequence_and_atomic_trade_pairs()
        기능: 실제 event/session identity와 ORDER/PERFORMANCE N/N+1 pair만 transport 증거가 되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        trace_body = self._success_trace_body()

        # Atomic Trade event에는 synthetic state version이 없고 실제 envelope sequence만 남는다.
        sealed_trace = seal_phase13_public_trace(trace_body)
        self.assertEqual(
            validate_phase13_public_trace(sealed_trace),
            sealed_trace["trace_sha256"],
        )
        transport_events = sealed_trace["transport_ui_event_batch"]["events"]
        self.assertNotIn("state_version", transport_events[1])
        self.assertIsNone(transport_events[1]["aggregate_version"])
        self.assertIsNone(transport_events[2]["aggregate_version"])
        self.assertEqual(
            transport_events[1]["transport_sequence"] + 1,
            transport_events[2]["transport_sequence"],
        )
        self.assertEqual(
            transport_events[1]["published_at"],
            transport_events[2]["published_at"],
        )

        # 지연 domain event의 occurred_at은 감소할 수 있고 envelope sequence만 transport ordering이다.
        delayed_event_body = self._success_trace_body()
        delayed_event_body["transport_ui_event_batch"]["events"][0][
            "published_at"
        ] = "2026-08-31T00:00:04.000Z"
        seal_phase13_public_trace(delayed_event_body)

        # Pair sequence, occurred_at, nullable version과 Trade ID 중 하나라도 바뀌면 fail closed한다.
        for field_name, invalid_value in (
            ("transport_sequence", 12),
            ("published_at", "2026-08-31T00:00:03.001Z"),
            ("aggregate_version", 1),
            ("related_id", "trade-other"),
        ):
            with self.subTest(field_name=field_name):
                invalid_body = deepcopy(trace_body)
                invalid_body["transport_ui_event_batch"]["events"][2][
                    field_name
                ] = invalid_value
                self._assert_body_is_rejected(invalid_body)

    def test_secret_like_keys_values_and_actual_canaries_are_rejected(self) -> None:
        """
        함수 이름: test_secret_like_keys_values_and_actual_canaries_are_rejected()
        기능: raw key/header/query 표현과 주입 credential canary가 seal·오류에 노출되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        secret_like_trace_bodies: list[tuple[str, dict[str, object]]] = []

        # Unknown field라도 schema 검사보다 먼저 secret scanner가 전체 tree에서 차단한다.
        secret_key_body = self._success_trace_body()
        secret_key_body["public_market_events"][0]["api_key"] = "redacted"
        secret_like_trace_bodies.append(("secret_key", secret_key_body))
        raw_header_body = self._success_trace_body()
        raw_header_body["public_account_events"][0]["raw_headers"] = {}
        secret_like_trace_bodies.append(("raw_header", raw_header_body))
        signature_value_body = self._success_trace_body()
        signature_value_body["transport_ui_event_batch"]["events"][0][
            "related_id"
        ] = "signature=0123456789abcdef"
        secret_like_trace_bodies.append(("signature_value", signature_value_body))
        query_value_body = self._success_trace_body()
        query_value_body["transport_ui_event_batch"]["events"][0][
            "related_id"
        ] = "https://testnet.invalid/order?timestamp=1&signature=2"
        secret_like_trace_bodies.append(("query_value", query_value_body))

        for case_name, trace_body in secret_like_trace_bodies:
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

        # 실제 credential/session canary는 어떤 safe-looking field에 섞여도 고정 오류만 남긴다.
        for canary_value in (
            API_KEY_CANARY,
            API_SECRET_CANARY,
            SESSION_TOKEN_CANARY,
        ):
            with self.subTest(canary_kind=canary_value.split("-")[1]):
                canary_body = self._success_trace_body()
                canary_body["public_market_events"][0][
                    "source_event_id"
                ] = f"event-{canary_value}"
                with self.assertRaises(
                    PhaseThirteenPublicTraceValidationError
                ) as raised_context:
                    seal_phase13_public_trace(
                        canary_body,
                        forbidden_values=(
                            API_KEY_CANARY,
                            API_SECRET_CANARY,
                            SESSION_TOKEN_CANARY,
                        ),
                    )

                self.assertNotIn(canary_value, str(raised_context.exception))
                self.assertNotIn(canary_value, repr(raised_context.exception))

    def test_decision_event_attempt_and_notional_provenance_mismatch_is_rejected(
        self,
    ) -> None:
        """
        함수 이름: test_decision_event_attempt_and_notional_provenance_mismatch_is_rejected()
        기능: source event, BUY attempt 또는 price×quantity claim이 fingerprint와 다르면 차단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        invalid_trace_bodies: list[tuple[str, dict[str, object]]] = []

        # 각 tamper는 자체 schema와 타입은 유지해 cross-object provenance 검증을 직접 통과해야만 한다.
        source_mismatch = self._success_trace_body()
        source_mismatch["public_market_events"][0][
            "source_event_id"
        ] = "kline-event-other"
        invalid_trace_bodies.append(("source_mismatch", source_mismatch))
        attempt_mismatch = self._success_trace_body()
        attempt_mismatch["order_attempts"][0]["decision_price"] = "2000"
        attempt_mismatch["order_attempts"][0]["final_notional"] = "8.000"
        invalid_trace_bodies.append(("attempt_mismatch", attempt_mismatch))
        notional_mismatch = self._success_trace_body()
        notional_mismatch["immutable_decision_fingerprint"][
            "final_notional"
        ] = "9.999"
        invalid_trace_bodies.append(("notional_mismatch", notional_mismatch))
        cap_exceeded = self._success_trace_body()
        cap_exceeded["immutable_decision_fingerprint"][
            "configured_cap"
        ] = "100.000000000000000001"
        invalid_trace_bodies.append(("cap_exceeded", cap_exceeded))

        for case_name, trace_body in invalid_trace_bodies:
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

    def test_preflight_endpoint_readiness_filters_and_zero_state_are_exact(
        self,
    ) -> None:
        """
        함수 이름: test_preflight_endpoint_readiness_filters_and_zero_state_are_exact()
        기능: fixed Testnet·ETHUSDT·stream READY·commission/filter·zero-state drift를 모두 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        invalid_trace_bodies: list[tuple[str, dict[str, object]]] = []

        # Endpoint, symbol, permission, 두 stream과 mutation 전 account state를 독립 변조한다.
        for case_name, field_name, invalid_value in (
            ("live_endpoint", "endpoint_set", "BINANCE_SPOT_LIVE"),
            ("wrong_symbol", "symbol", "BTCUSDT"),
            ("cannot_trade", "can_trade", False),
            ("account_stream", "account_stream_status", "CONNECTING"),
            ("market_stream", "market_stream_status", "FAILED"),
            ("account_version", "account_version", 0),
            ("open_position", "position_quantity", "0.001"),
            ("pending_order", "pending_order_count", 1),
            ("unknown_order", "unknown_order_count", 1),
            ("open_order", "matching_open_order_count", 1),
        ):
            invalid_body = self._success_trace_body()
            invalid_body["preflight"][field_name] = invalid_value
            invalid_trace_bodies.append((case_name, invalid_body))

        # Dust-producing commission과 음수 quantity filter도 preflight 완료로 승격하지 않는다.
        commission_dust = self._success_trace_body()
        commission_policy = commission_dust["preflight"]["commission_policy"]
        commission_policy["standard_market_buy_rate"] = "0.001"
        commission_policy["market_buy_received_asset_commission_rate"] = "0.001"
        invalid_trace_bodies.append(("commission_dust", commission_dust))
        negative_step = self._success_trace_body()
        negative_step["preflight"]["fresh_filters"][
            "market_lot_size_step_size"
        ] = "-0.0001"
        invalid_trace_bodies.append(("negative_step", negative_step))
        invalid_range = self._success_trace_body()
        invalid_range["preflight"]["fresh_filters"][
            "lot_size_maximum_quantity"
        ] = "0.00001"
        invalid_trace_bodies.append(("invalid_range", invalid_range))
        unsupported_precision = self._success_trace_body()
        unsupported_precision["preflight"]["fresh_filters"][
            "base_asset_precision"
        ] = 19
        invalid_trace_bodies.append(("unsupported_precision", unsupported_precision))

        # Phase 13 BUY의 MAX_POSITION, signed MAX_ASSET·reference price schema와 preflight 시각 drift를 거부한다.
        maximum_position = self._success_trace_body()
        maximum_position["preflight"]["fresh_filters"][
            "maximum_position"
        ] = "10"
        invalid_trace_bodies.append(("maximum_position", maximum_position))
        duplicate_account_asset = self._success_trace_body()
        duplicate_filter = deepcopy(
            duplicate_account_asset["preflight"]["account_asset_filters"][0]
        )
        duplicate_account_asset["preflight"]["account_asset_filters"].append(
            deepcopy(duplicate_filter)
        )
        duplicate_account_asset["preflight"]["account_relevant_filters"][
            "asset_filters"
        ].append(duplicate_filter)
        invalid_trace_bodies.append(
            ("duplicate_account_asset", duplicate_account_asset)
        )
        wrong_reference_symbol = self._success_trace_body()
        wrong_reference_symbol["preflight"]["reference_price"][
            "symbol"
        ] = "BTCUSDT"
        invalid_trace_bodies.append(
            ("wrong_reference_symbol", wrong_reference_symbol)
        )
        late_account_filters = self._success_trace_body()
        late_account_filters["preflight"][
            "account_filters_observed_at"
        ] = "2026-08-31T00:00:00.060Z"
        invalid_trace_bodies.append(
            ("late_account_filters", late_account_filters)
        )
        late_reference_price = self._success_trace_body()
        late_reference_price["preflight"][
            "reference_price_observed_at"
        ] = "2026-08-31T00:00:00.060Z"
        invalid_trace_bodies.append(
            ("late_reference_price", late_reference_price)
        )
        for case_name, trace_body in invalid_trace_bodies:
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

        # Binance의 stepSize 0은 rule disablement이므로 두 filter가 0이어도 precision fallback을 허용한다.
        disabled_steps = self._success_trace_body()
        disabled_steps["preflight"]["fresh_filters"][
            "lot_size_step_size"
        ] = "0"
        disabled_steps["preflight"]["fresh_filters"][
            "market_lot_size_step_size"
        ] = "0"
        for filter_scope in (
            "account_relevant_filters",
            "public_relevant_filters",
        ):
            for quantity_filter in disabled_steps["preflight"][filter_scope][
                "symbol_quantity_filters"
            ]:
                quantity_filter["step_size"] = "0"
        seal_phase13_public_trace(disabled_steps)

        # Earlier preflight snapshot은 submit-time rules와 다를 수 있고 최종 수량 권위를 대신하지 않는다.
        changed_after_preflight = self._success_trace_body()
        changed_after_preflight["preflight"]["fresh_filters"][
            "lot_size_step_size"
        ] = "0.003"
        for filter_scope in (
            "account_relevant_filters",
            "public_relevant_filters",
        ):
            changed_after_preflight["preflight"][filter_scope][
                "symbol_quantity_filters"
            ][0]["step_size"] = "0.003"
        seal_phase13_public_trace(changed_after_preflight)

    def test_submit_time_filters_are_authoritative_and_causally_bound(
        self,
    ) -> None:
        """
        함수 이름: test_submit_time_filters_are_authoritative_and_causally_bound()
        기능: prepare-time rules·final 산술·source→filter→POST-start→fill/result 순서를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        invalid_trace_bodies: list[tuple[str, dict[str, object]]] = []

        # 정상 fixture는 public source→filter→POST 직전 start→fill→terminal result를 따른다.
        valid_trace_body = self._success_trace_body()
        seal_phase13_public_trace(valid_trace_body)
        self.assertEqual(
            valid_trace_body["order_attempts"][0]["attempted_at"],
            "2026-08-31T00:00:01.000Z",
        )

        # Evidence 누락·identity drift·POST start 뒤 filter fetch는 실제 prepare provenance가 아니다.
        missing_sell_evidence = self._success_trace_body()
        missing_sell_evidence["submit_time_filter_evidence"].pop()
        invalid_trace_bodies.append(("missing_sell", missing_sell_evidence))
        wrong_intent = self._success_trace_body()
        wrong_intent["submit_time_filter_evidence"][0][
            "intent_id"
        ] = "other-buy-intent"
        invalid_trace_bodies.append(("wrong_intent", wrong_intent))
        sequence_identity_drift = self._success_trace_body()
        first_evidence = sequence_identity_drift[
            "submit_time_filter_evidence"
        ][0]
        second_evidence = sequence_identity_drift[
            "submit_time_filter_evidence"
        ][1]
        for field_name in ("intent_id", "client_order_id", "side"):
            first_evidence[field_name], second_evidence[field_name] = (
                second_evidence[field_name],
                first_evidence[field_name],
            )
        invalid_trace_bodies.append(
            ("sequence_identity_drift", sequence_identity_drift)
        )
        late_filter = self._success_trace_body()
        late_filter["submit_time_filter_evidence"][0]["observed_at"] = (
            "2026-08-31T00:00:01.100Z"
        )
        invalid_trace_bodies.append(("late_filter", late_filter))
        stale_sell_filter = self._success_trace_body()
        stale_sell_filter["submit_time_filter_evidence"][1]["observed_at"] = (
            "2026-08-31T00:00:00.800Z"
        )
        invalid_trace_bodies.append(("stale_sell_filter", stale_sell_filter))
        clock_regression = self._success_trace_body()
        clock_regression["order_attempts"][0]["attempted_at"] = (
            "2026-08-31T00:00:00.940Z"
        )
        with self.assertRaisesRegex(
            PhaseThirteenPublicTraceValidationError,
            "safety observation follows POST submission start",
        ):
            seal_phase13_public_trace(clock_regression)

        # Source 이후 시작된 composite라도 첫 fetch부터 POST까지
        # production 고정 freshness 경계를 넘기면 stale evidence다.
        stale_composite = self._success_trace_body()
        stale_composite["timestamps"]["started_at"] = (
            "2026-08-30T23:59:00.000Z"
        )
        stale_composite["immutable_decision_fingerprint"]["source_event_time"] = (
            "2026-08-30T23:59:00.000Z"
        )
        for market_event in stale_composite["public_market_events"]:
            market_event["source_event_time"] = "2026-08-30T23:59:00.000Z"
        stale_composite["submit_time_filter_evidence"][0][
            "account_filters_observed_at"
        ] = "2026-08-30T23:59:30.000Z"
        with self.assertRaisesRegex(
            PhaseThirteenPublicTraceValidationError,
            "filter evidence exceeded its maximum age",
        ):
            seal_phase13_public_trace(stale_composite)

        # Production 경계와 동일한 exact age는 stale이 아니므로 seal을 허용한다.
        boundary_composite = self._success_trace_body()
        boundary_composite["timestamps"]["started_at"] = (
            "2026-08-30T23:59:00.000Z"
        )
        boundary_composite["immutable_decision_fingerprint"][
            "source_event_time"
        ] = "2026-08-30T23:59:00.000Z"
        for market_event in boundary_composite["public_market_events"]:
            market_event["source_event_time"] = "2026-08-30T23:59:00.000Z"
        boundary_composite["submit_time_filter_evidence"][0][
            "account_filters_observed_at"
        ] = "2026-08-30T23:59:31.000Z"
        seal_phase13_public_trace(boundary_composite)

        # 느지막 polling 관찰을 attempted_at으로 쓰면 fill·terminal result보다 뒤이므로 거부한다.
        polling_observation_as_attempt = self._success_trace_body()
        polling_observation_as_attempt["order_attempts"][0]["attempted_at"] = (
            "2026-08-31T00:00:02.100Z"
        )
        invalid_trace_bodies.append(
            ("polling_observation_as_attempt", polling_observation_as_attempt)
        )

        # Preflight가 유효해도 submit-time grid·notional이 BUY final claim을 거부하면 seal할 수 없다.
        off_grid_buy = self._success_trace_body()
        off_grid_buy["submit_time_filter_evidence"][0]["rules"][
            "lot_size_step_size"
        ] = "0.003"
        invalid_trace_bodies.append(("off_grid_buy", off_grid_buy))
        below_submit_notional = self._success_trace_body()
        below_submit_notional["submit_time_filter_evidence"][0]["rules"][
            "minimum_notional"
        ] = "11"
        invalid_trace_bodies.append(("below_submit_notional", below_submit_notional))

        # Runtime overlap과 동일하게 signed common 값, passive subset과 bilateral count drift를 거부한다.
        signed_quantity_drift = self._success_trace_body()
        signed_quantity_drift["submit_time_filter_evidence"][0][
            "account_relevant_filters"
        ]["symbol_quantity_filters"][0]["step_size"] = "0.0002"
        invalid_trace_bodies.append(("signed_quantity_drift", signed_quantity_drift))
        signed_notional_drift = self._success_trace_body()
        signed_notional_drift["submit_time_filter_evidence"][0][
            "account_relevant_filters"
        ]["symbol_notional_filters"][0]["minimum_notional"] = "6"
        invalid_trace_bodies.append(("signed_notional_drift", signed_notional_drift))
        signed_count_drift = self._success_trace_body()
        signed_count_drift["submit_time_filter_evidence"][0][
            "account_relevant_filters"
        ]["exchange_order_count_filters"][0]["maximum_count"] = 999
        invalid_trace_bodies.append(("signed_count_drift", signed_count_drift))
        signed_passive_drift = self._success_trace_body()
        signed_passive_types = signed_passive_drift[
            "submit_time_filter_evidence"
        ][0]["account_relevant_filters"]["passive_symbol_filter_types"]
        signed_passive_types.append("PERCENT_PRICE_BY_SIDE")
        signed_passive_types.sort()
        invalid_trace_bodies.append(("signed_passive_drift", signed_passive_drift))

        # Signed/public을 함께 변조해도 public scalar rule binding을 우회할 수 없어야 한다.
        bilateral_quantity_drift = self._success_trace_body()
        for filter_scope in (
            "account_relevant_filters",
            "public_relevant_filters",
        ):
            bilateral_quantity_drift["submit_time_filter_evidence"][0][
                filter_scope
            ]["symbol_quantity_filters"][0]["step_size"] = "0.0002"
        invalid_trace_bodies.append(
            ("bilateral_quantity_drift", bilateral_quantity_drift)
        )

        # Official MARKET notional은 decision price가 아닌 submit-time referencePrice로 검증해야 한다.
        reference_below_minimum = self._success_trace_body()
        reference_below_minimum["submit_time_filter_evidence"][0][
            "reference_price"
        ]["price"] = "1000"
        invalid_trace_bodies.append(
            ("reference_below_minimum", reference_below_minimum)
        )
        submit_maximum_position = self._success_trace_body()
        submit_maximum_position["submit_time_filter_evidence"][0]["rules"][
            "maximum_position"
        ] = "100"
        invalid_trace_bodies.append(
            ("submit_maximum_position", submit_maximum_position)
        )

        # Base MAX_ASSET은 final quantity와 비교하고 BUY·SELL 양쪽의 초과를 차단한다.
        for case_name, evidence_index, maximum_quantity in (
            ("buy_base_max_asset", 0, "0.003"),
            ("sell_base_max_asset", 1, "0.003"),
        ):
            exceeded_max_asset = self._success_trace_body()
            exceeded_max_asset["submit_time_filter_evidence"][evidence_index][
                "account_asset_filters"
            ][0]["maximum_quantity"] = maximum_quantity
            exceeded_max_asset["submit_time_filter_evidence"][evidence_index][
                "account_relevant_filters"
            ]["asset_filters"][0]["maximum_quantity"] = maximum_quantity
            invalid_trace_bodies.append((case_name, exceeded_max_asset))

        # Quantity MARKET의 quote MAX_ASSET은 공식 환산 가격식이 없어 limit 값과 무관하게 fail-close한다.
        for case_name, evidence_index in (
            ("buy_quote_max_asset", 0),
            ("sell_quote_max_asset", 1),
        ):
            quote_max_asset = self._success_trace_body()
            quote_filter = {
                "filter_type": "MAX_ASSET",
                "asset": "USDT",
                "maximum_quantity": "100000",
            }
            quote_max_asset["submit_time_filter_evidence"][evidence_index][
                "account_asset_filters"
            ].append(deepcopy(quote_filter))
            quote_max_asset["submit_time_filter_evidence"][evidence_index][
                "account_relevant_filters"
            ]["asset_filters"].append(quote_filter)
            invalid_trace_bodies.append((case_name, quote_max_asset))

        # Submit-time symbol·timestamp drift는 matching attempt의 fresh safety provenance가 아니다.
        reference_symbol_drift = self._success_trace_body()
        reference_symbol_drift["submit_time_filter_evidence"][0][
            "reference_price"
        ]["symbol"] = "BTCUSDT"
        invalid_trace_bodies.append(
            ("reference_symbol_drift", reference_symbol_drift)
        )
        late_account_filter = self._success_trace_body()
        late_account_filter["submit_time_filter_evidence"][0][
            "account_filters_observed_at"
        ] = "2026-08-31T00:00:01.100Z"
        invalid_trace_bodies.append(("late_account_filter", late_account_filter))
        late_reference_price = self._success_trace_body()
        late_reference_price["submit_time_filter_evidence"][0][
            "reference_price_observed_at"
        ] = "2026-08-31T00:00:01.100Z"
        invalid_trace_bodies.append(("late_reference_price", late_reference_price))

        # Public source가 POST submission-start보다 늦으면 causal order를 충족하지 못한다.
        source_after_attempt = self._success_trace_body()
        source_after_attempt["immutable_decision_fingerprint"][
            "source_event_time"
        ] = "2026-08-31T00:00:01.100Z"
        for market_event in source_after_attempt["public_market_events"]:
            market_event["source_event_time"] = "2026-08-31T00:00:01.100Z"
        invalid_trace_bodies.append(("source_after_attempt", source_after_attempt))

        for case_name, trace_body in invalid_trace_bodies:
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

    def test_success_requires_exact_public_1l_case_c_buy_path(self) -> None:
        """
        함수 이름: test_success_requires_exact_public_1l_case_c_buy_path()
        기능: 1L.1→1L.2→1L.3와 SUBMIT_ORDER/BUY/CASE_C action drift를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        invalid_trace_bodies: list[tuple[str, dict[str, object]]] = []

        # Message 순서와 마지막 Action type, side, strategy는 각각 독립적인 public-path 사실이다.
        for case_name, field_name, invalid_value in (
            ("message_path", "message_id", "1L.3"),
            ("action_type", "action_type", "DIRECT_ORDER"),
            ("side", "side", "SELL"),
            ("strategy", "strategy", "CASE_B"),
        ):
            invalid_body = self._success_trace_body()
            target_index = 1 if field_name == "message_id" else 2
            invalid_body["public_market_events"][target_index][
                field_name
            ] = invalid_value
            invalid_trace_bodies.append((case_name, invalid_body))
        extra_path_event = self._success_trace_body()
        extra_event = deepcopy(extra_path_event["public_market_events"][2])
        extra_event["sequence"] = 4
        extra_path_event["public_market_events"].append(extra_event)
        invalid_trace_bodies.append(("extra_path_event", extra_path_event))

        for case_name, trace_body in invalid_trace_bodies:
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

    def test_order_execution_trace_requires_complete_identity_version_and_result(
        self,
    ) -> None:
        """
        함수 이름: test_order_execution_trace_requires_complete_identity_version_and_result()
        기능: Artifact의 주문 1~14 누락·중복·불법 branch와 identity·version·result 변조를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        missing_step_body = self._success_trace_body()
        missing_entries = missing_step_body["order_execution_traces"][0][
            "entries"
        ]
        missing_entries.pop(6)
        for sequence, entry in enumerate(missing_entries, start=1):
            entry["sequence"] = sequence

        duplicate_step_body = self._success_trace_body()
        duplicate_entries = duplicate_step_body["order_execution_traces"][0][
            "entries"
        ]
        duplicate_entries.insert(2, deepcopy(duplicate_entries[1]))
        for sequence, entry in enumerate(duplicate_entries, start=1):
            entry["sequence"] = sequence

        illegal_branch_body = self._success_trace_body()
        illegal_branch_body["order_execution_traces"][0]["entries"][9][
            "message_id"
        ] = "8"
        mismatched_identity_body = self._success_trace_body()
        mismatched_identity_body["order_execution_traces"][0][
            "client_order_id"
        ] = "bat-phase13-unrelated-order"
        regressed_version_body = self._success_trace_body()
        regressed_version_body["order_execution_traces"][0]["entries"][3][
            "context_version_after"
        ] = 11
        cross_entry_regression_body = self._success_trace_body()
        cross_entry_entries = cross_entry_regression_body[
            "order_execution_traces"
        ][0]["entries"]
        cross_entry_entries[0]["context_version_before"] = 8
        cross_entry_entries[0]["context_version_after"] = 10
        cross_entry_entries[1]["context_version_before"] = 9
        cross_entry_entries[1]["context_version_after"] = 10
        for trace_entry in cross_entry_entries[2:]:
            trace_entry["context_version_before"] = 10
            trace_entry["context_version_after"] = 10
        failed_result_body = self._success_trace_body()
        failed_result_entry = failed_result_body["order_execution_traces"][0][
            "entries"
        ][5]
        failed_result_entry["result"] = "FAILURE"
        failed_result_entry["failure_code"] = "GATEWAY_REQUEST_FAILED"

        # Entry 내부·각 축 monotonic뿐 아니라 직전 after보다 작은 다음 before도 독립 차단한다.
        for case_name, trace_body in (
            ("missing-step", missing_step_body),
            ("duplicate-step", duplicate_step_body),
            ("illegal-branch", illegal_branch_body),
            ("mismatched-identity", mismatched_identity_body),
            ("regressed-version", regressed_version_body),
            ("cross-entry-version-regression", cross_entry_regression_body),
            ("failed-result", failed_result_body),
        ):
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

    def test_order_execution_trace_requires_monotonic_terminal_exchange_identity(
        self,
    ) -> None:
        """
        함수 이름: test_order_execution_trace_requires_monotonic_terminal_exchange_identity()
        기능: SUCCESS trace의 exchange ID 전체 누락과 최초 관찰 후 None 회귀를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        all_none_body = self._success_trace_body()
        all_none_entries = all_none_body["order_execution_traces"][0][
            "entries"
        ]
        for trace_entry in all_none_entries:
            trace_entry["order_id"] = None

        none_regression_body = self._success_trace_body()
        none_regression_entries = none_regression_body[
            "order_execution_traces"
        ][0]["entries"]
        message_ten_index = next(
            index
            for index, trace_entry in enumerate(none_regression_entries)
            if trace_entry["message_id"] == "10"
        )

        # Message 7의 ID 관찰 후 query 8~9가 None으로 후퇴하는 branch를 정상 grammar 사이에 삽입한다.
        query_entries: list[dict[str, object]] = []
        for message_id in ("8", "8.1", "8.2", "9"):
            query_entry = deepcopy(none_regression_entries[message_ten_index])
            query_entry["message_id"] = message_id
            query_entry["order_id"] = None
            query_entries.append(query_entry)
        none_regression_entries[message_ten_index:message_ten_index] = (
            query_entries
        )
        for sequence, trace_entry in enumerate(
            none_regression_entries,
            start=1,
        ):
            trace_entry["sequence"] = sequence

        # Old validator가 허용하던 all-None과 matching ID 관찰 후 None 회귀를 독립 거부한다.
        for case_name, trace_body in (
            ("all-order-ids-none", all_none_body),
            ("order-id-none-regression", none_regression_body),
        ):
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

    def test_duplicate_fill_and_missing_durable_trade_are_rejected(self) -> None:
        """
        함수 이름: test_duplicate_fill_and_missing_durable_trade_are_rejected()
        기능: incremental fill 중복과 final History에 없는 durable Trade 대응을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        duplicate_fill_body = self._success_trace_body()
        duplicate_fill_body["order_results"][0]["incremental_fills"].append(
            deepcopy(
                duplicate_fill_body["order_results"][0]["incremental_fills"][0]
            )
        )
        missing_trade_body = self._success_trace_body()
        missing_trade_body["run_durable_trades"] = [
            missing_trade_body["run_durable_trades"][0]
        ]
        missing_performance = missing_trade_body["final_state"]["performance"]
        missing_performance["run_trade_count"] = 1
        missing_performance["total_trade_count"] = 1
        missing_performance["run_realized_profit_loss"] = "0"
        missing_performance["total_realized_profit_loss"] = "0"

        # Fill dedup과 durable history correlation은 서로 다른 failure boundary다.
        for case_name, trace_body in (
            ("duplicate_fill", duplicate_fill_body),
            ("missing_trade", missing_trade_body),
        ):
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

    def test_failed_terminal_partial_fill_still_requires_one_durable_trade(
        self,
    ) -> None:
        """
        함수 이름: test_failed_terminal_partial_fill_still_requires_one_durable_trade()
        기능: CANCELED terminal의 실제 partial fill도 정확히 한 Trade와 Performance를 갖는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        trace_body = self._success_trace_body()

        # BUY partial 뒤 recovery 전 중단된 FAILED를 한 attempt·terminal fill·Trade로 정규화한다.
        trace_body["outcome"] = "FAILED"
        trace_body["typed_reason"] = "RECOVERY_BLOCKED"
        trace_body["order_attempts"] = [trace_body["order_attempts"][0]]
        trace_body["order_execution_traces"] = [
            trace_body["order_execution_traces"][0]
        ]
        trace_body["submit_time_filter_evidence"] = [
            trace_body["submit_time_filter_evidence"][0]
        ]
        trace_body["order_results"] = [trace_body["order_results"][0]]
        trace_body["order_results"][0]["status"] = "CANCELED"
        partial_fill = trace_body["order_results"][0]["incremental_fills"][0]
        partial_fill["quantity"] = "0.002"
        partial_fill["quote_amount"] = "5.000"
        buy_trade = trace_body["run_durable_trades"][0]
        buy_trade["executed_quantity"] = "0.002"
        buy_trade["executed_amount"] = "5.000"
        trace_body["run_durable_trades"] = [buy_trade]
        trace_body["transport_ui_event_batch"]["events"] = [
            trace_body["transport_ui_event_batch"]["events"][0],
            trace_body["transport_ui_event_batch"]["events"][1],
            trace_body["transport_ui_event_batch"]["events"][2],
            trace_body["transport_ui_event_batch"]["events"][5],
        ]
        trace_body["transport_ui_event_batch"]["events"][3][
            "related_id"
        ] = "bat-phase13-buy-0001"
        trace_body["recovery"] = {
            "required": True,
            "attempted": False,
            "outcome": "BLOCKED",
            "intent_id": None,
            "client_order_id": None,
            "authoritative_position_quantity": "0.002",
            "effective_free_quantity": "0.002",
            "submitted_quantity": None,
            "final_position_quantity": "0.002",
            "pending_order_count": 0,
            "matching_open_order_count": 0,
            "duplicate_order_count": 0,
            "duplicate_trade_count": 0,
        }
        final_state = trace_body["final_state"]
        final_state["position_quantity"] = "0.002"
        final_state["actual_order_count"] = 1
        final_state["performance"]["run_trade_count"] = 1
        final_state["performance"]["total_trade_count"] = 1
        final_state["performance"]["run_realized_profit_loss"] = "0"
        final_state["performance"]["total_realized_profit_loss"] = "0"

        # Terminal status가 FILLED가 아니어도 fill aggregate와 durable Trade가 있으면 valid evidence다.
        sealed_trace = seal_phase13_public_trace(trace_body)
        self.assertEqual(
            validate_phase13_public_trace(sealed_trace),
            sealed_trace["trace_sha256"],
        )

    def test_filled_identity_fill_math_quantity_and_terminal_state_are_exact(
        self,
    ) -> None:
        """
        함수 이름: test_filled_identity_fill_math_quantity_and_terminal_state_are_exact()
        기능: FILLED exchange/fill, global ID 1:1, quote math, final quantity와 terminal 단조성을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        invalid_trace_bodies: list[tuple[str, dict[str, object]]] = []

        # FILLED 자체가 exchange identity와 적어도 한 incremental fill을 소유해야 한다.
        missing_exchange = self._success_trace_body()
        missing_exchange["order_results"][0]["exchange_order_id"] = None
        invalid_trace_bodies.append(("missing_exchange", missing_exchange))
        missing_fills = self._success_trace_body()
        missing_fills["order_results"][0]["incremental_fills"] = []
        invalid_trace_bodies.append(("missing_fills", missing_fills))

        # Exchange ID 재사용, fill quote 오차와 submitted quantity 미달을 별도 tamper로 만든다.
        reused_exchange = self._success_trace_body()
        reused_exchange["order_results"][1]["exchange_order_id"] = "9100001"
        invalid_trace_bodies.append(("reused_exchange", reused_exchange))
        noncanonical_exchange = self._success_trace_body()
        noncanonical_exchange["order_results"][0]["exchange_order_id"] = "09100001"
        invalid_trace_bodies.append(("noncanonical_exchange", noncanonical_exchange))
        changed_exchange = self._success_trace_body()
        changed_exchange["order_results"].insert(
            1,
            {
                "sequence": 2,
                "observed_at": "2026-08-31T00:00:02.100Z",
                "intent_id": "case2-buy-intent-0001",
                "client_order_id": "bat-phase13-buy-0001",
                "exchange_order_id": "9100009",
                "status": "FILLED",
                "failure_code": None,
                "incremental_fills": [],
            },
        )
        changed_exchange["order_results"][2]["sequence"] = 3
        invalid_trace_bodies.append(("changed_exchange", changed_exchange))
        quote_mismatch = self._success_trace_body()
        quote_mismatch["order_results"][0]["incremental_fills"][0][
            "quote_amount"
        ] = "9.999"
        invalid_trace_bodies.append(("quote_mismatch", quote_mismatch))
        quantity_mismatch = self._success_trace_body()
        quantity_fill = quantity_mismatch["order_results"][0][
            "incremental_fills"
        ][0]
        quantity_fill["quantity"] = "0.003"
        quantity_fill["quote_amount"] = "7.500"
        invalid_trace_bodies.append(("quantity_mismatch", quantity_mismatch))
        executed_at_mismatch = self._success_trace_body()
        executed_at_mismatch["run_durable_trades"][0]["executed_at"] = (
            "2026-08-31T00:00:01.600Z"
        )
        invalid_trace_bodies.append(("executed_at_mismatch", executed_at_mismatch))

        # Terminal FILLED 뒤 같은 client가 NEW로 돌아가는 concrete result는 monotonicity 위반이다.
        terminal_regression = self._success_trace_body()
        terminal_regression["order_results"].append(
            {
                "sequence": 3,
                "observed_at": "2026-08-31T00:00:22.500Z",
                "intent_id": "case2-buy-intent-0001",
                "client_order_id": "bat-phase13-buy-0001",
                "exchange_order_id": "9100001",
                "status": "NEW",
                "failure_code": None,
                "incremental_fills": [],
            }
        )
        invalid_trace_bodies.append(("terminal_regression", terminal_regression))

        for case_name, trace_body in invalid_trace_bodies:
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

    def test_success_order_budget_and_recovery_identity_are_exact(self) -> None:
        """
        함수 이름: test_success_order_budget_and_recovery_identity_are_exact()
        기능: SUCCESS가 BUY 1+STOP SELL 1, 모두 FILLED와 same-run exact recovery만 허용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        invalid_trace_bodies: list[tuple[str, dict[str, object]]] = []

        # 세 번째 attempt, STOP provenance 상실과 non-terminal SELL은 실제 주문 budget을 위반한다.
        extra_attempt = self._success_trace_body()
        copied_attempt = deepcopy(extra_attempt["order_attempts"][1])
        copied_attempt["sequence"] = 3
        copied_attempt["intent_id"] = "unexpected-third-intent"
        copied_attempt["client_order_id"] = "bat-unexpected-third"
        extra_attempt["order_attempts"].append(copied_attempt)
        extra_attempt["final_state"]["actual_order_count"] = 3
        invalid_trace_bodies.append(("extra_attempt", extra_attempt))
        missing_stop = self._success_trace_body()
        missing_stop["order_attempts"][1]["exit_reason"] = "TAKE_PROFIT"
        invalid_trace_bodies.append(("missing_stop", missing_stop))
        non_terminal_sell = self._success_trace_body()
        non_terminal_sell["order_results"][1]["status"] = "PARTIALLY_FILLED"
        invalid_trace_bodies.append(("non_terminal_sell", non_terminal_sell))
        repeated_submission = self._success_trace_body()
        repeated_submission["order_attempts"][0]["submission_attempt"] = 1
        invalid_trace_bodies.append(("repeated_submission", repeated_submission))
        reused_intent = self._success_trace_body()
        reused_intent["order_attempts"][1]["intent_id"] = (
            reused_intent["order_attempts"][0]["intent_id"]
        )
        reused_intent["order_results"][1]["intent_id"] = (
            reused_intent["order_attempts"][0]["intent_id"]
        )
        reused_intent["submit_time_filter_evidence"][1]["intent_id"] = (
            reused_intent["order_attempts"][0]["intent_id"]
        )
        reused_intent["recovery"]["intent_id"] = (
            reused_intent["order_attempts"][0]["intent_id"]
        )
        invalid_trace_bodies.append(("reused_intent", reused_intent))

        # Recovery intent/client와 authoritative/free/submitted 수량은 STOP SELL과 exact identity다.
        wrong_recovery_client = self._success_trace_body()
        wrong_recovery_client["recovery"]["client_order_id"] = "bat-other-sell"
        invalid_trace_bodies.append(("wrong_recovery_client", wrong_recovery_client))
        free_quantity_drift = self._success_trace_body()
        free_quantity_drift["recovery"]["effective_free_quantity"] = "0.003"
        invalid_trace_bodies.append(("free_quantity_drift", free_quantity_drift))

        for case_name, trace_body in invalid_trace_bodies:
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

    def test_run_trades_baseline_and_performance_are_exactly_correlated(self) -> None:
        """
        함수 이름: test_run_trades_baseline_and_performance_are_exactly_correlated()
        기능: terminal order/fill↔Trade와 baseline+run Performance 산술 drift를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        invalid_trace_bodies: list[tuple[str, dict[str, object]]] = []

        # Trade exchange identity, fill aggregate amount와 required CASE_C/STOP provenance를 변조한다.
        exchange_mismatch = self._success_trace_body()
        exchange_mismatch["run_durable_trades"][0]["exchange_order_id"] = "9199999"
        invalid_trace_bodies.append(("exchange_mismatch", exchange_mismatch))
        trade_amount_mismatch = self._success_trace_body()
        trade_amount_mismatch["run_durable_trades"][0]["executed_amount"] = "9.999"
        invalid_trace_bodies.append(("trade_amount_mismatch", trade_amount_mismatch))
        strategy_mismatch = self._success_trace_body()
        strategy_mismatch["run_durable_trades"][1]["strategy"] = "CASE_B"
        invalid_trace_bodies.append(("strategy_mismatch", strategy_mismatch))
        exit_mismatch = self._success_trace_body()
        exit_mismatch["run_durable_trades"][1]["exit_reason"] = "TAKE_PROFIT"
        invalid_trace_bodies.append(("exit_mismatch", exit_mismatch))

        # Empty baseline digest/count와 Performance의 run·total 합은 서로 독립 field지만 exact하게 결속한다.
        baseline_digest_mismatch = self._success_trace_body()
        baseline_digest_mismatch["baseline_history_sha256"] = "0" * 64
        invalid_trace_bodies.append(("baseline_digest", baseline_digest_mismatch))
        baseline_count_mismatch = self._success_trace_body()
        baseline_count_mismatch["baseline_history_count"] = 1
        baseline_count_mismatch["final_state"]["performance"][
            "baseline_trade_count"
        ] = 1
        baseline_count_mismatch["final_state"]["performance"][
            "total_trade_count"
        ] = 3
        invalid_trace_bodies.append(("baseline_count", baseline_count_mismatch))
        performance_mismatch = self._success_trace_body()
        performance_mismatch["final_state"]["performance"][
            "run_realized_profit_loss"
        ] = "0.019"
        invalid_trace_bodies.append(("performance", performance_mismatch))
        duplicate_count_mismatch = self._success_trace_body()
        duplicate_count_mismatch["final_state"]["duplicate_trade_count"] = 1
        duplicate_count_mismatch["recovery"]["duplicate_trade_count"] = 1
        invalid_trace_bodies.append(("duplicate_count", duplicate_count_mismatch))

        for case_name, trace_body in invalid_trace_bodies:
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

    def test_final_unknown_fresh_session_and_run_causal_time_are_exact(self) -> None:
        """
        함수 이름: test_final_unknown_fresh_session_and_run_causal_time_are_exact()
        기능: final UNKNOWN zero, fresh session identity와 모든 evidence의 run 시간 경계를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        invalid_trace_bodies: list[tuple[str, dict[str, object]]] = []

        # SUCCESS final에는 UNKNOWN이 남을 수 없고 fresh runtime은 원 transport session과 달라야 한다.
        unknown_final = self._success_trace_body()
        unknown_final["final_state"]["unknown_order_count"] = 1
        invalid_trace_bodies.append(("unknown_final", unknown_final))
        reused_session = self._success_trace_body()
        reused_session["final_state"]["fresh_runtime_session_id"] = (
            reused_session["transport_ui_event_batch"]["transport_session_id"]
        )
        invalid_trace_bodies.append(("reused_session", reused_session))

        # Preflight-after-submit, result-before-attempt와 final-before-publication은 causal order 위반이다.
        late_preflight = self._success_trace_body()
        late_preflight["preflight"]["verified_at"] = "2026-08-31T00:00:02.000Z"
        invalid_trace_bodies.append(("late_preflight", late_preflight))
        early_result = self._success_trace_body()
        early_result["order_results"][0]["observed_at"] = (
            "2026-08-31T00:00:00.500Z"
        )
        invalid_trace_bodies.append(("early_result", early_result))
        early_final = self._success_trace_body()
        early_final["final_state"]["verified_at"] = "2026-08-31T00:00:21.500Z"
        invalid_trace_bodies.append(("early_final", early_final))
        late_filter = self._success_trace_body()
        late_filter["preflight"]["fresh_filters"]["observed_at"] = (
            "2026-08-31T00:00:00.060Z"
        )
        invalid_trace_bodies.append(("late_filter", late_filter))
        early_fill = self._success_trace_body()
        early_fill["order_results"][0]["incremental_fills"][0][
            "event_time"
        ] = "2026-08-31T00:00:00.900Z"
        early_fill["run_durable_trades"][0]["executed_at"] = (
            "2026-08-31T00:00:00.900Z"
        )
        invalid_trace_bodies.append(("early_fill", early_fill))
        early_ui_publication = self._success_trace_body()
        early_ui_publication["transport_ui_event_batch"]["events"][1][
            "published_at"
        ] = "2026-08-31T00:00:01.400Z"
        early_ui_publication["transport_ui_event_batch"]["events"][2][
            "published_at"
        ] = "2026-08-31T00:00:01.400Z"
        invalid_trace_bodies.append(("early_ui_publication", early_ui_publication))
        outside_run = self._success_trace_body()
        outside_run["public_account_events"][0]["source_event_time"] = (
            "2026-08-30T23:59:59.999Z"
        )
        invalid_trace_bodies.append(("outside_run", outside_run))

        for case_name, trace_body in invalid_trace_bodies:
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

    def test_blocked_failed_and_no_signal_semantics_require_safe_reasons(self) -> None:
        """
        함수 이름: test_blocked_failed_and_no_signal_semantics_require_safe_reasons()
        기능: BLOCKED/FAILED typed reason과 non-mutating NO_SIGNAL 경계를 fail-closed로 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        invalid_trace_bodies: list[tuple[str, dict[str, object]]] = []

        # BLOCKED/FAILED reason 누락과 SUCCESS/NO_SIGNAL의 synthetic reason을 각각 거부한다.
        for outcome, typed_reason in (
            ("BLOCKED", None),
            ("FAILED", None),
            ("SUCCESS", "SYNTHETIC_SUCCESS"),
            ("NO_SIGNAL", "SYNTHETIC_NO_SIGNAL"),
        ):
            invalid_body = (
                self._success_trace_body()
                if outcome == "SUCCESS"
                else self._no_signal_trace_body()
            )
            invalid_body["outcome"] = outcome
            invalid_body["typed_reason"] = typed_reason
            invalid_trace_bodies.append((outcome, invalid_body))

        # NO_SIGNAL이나 BLOCKED에 order 또는 recovery Position을 합성하면 non-mutating evidence가 아니다.
        no_signal_with_order = self._no_signal_trace_body()
        success_body = self._success_trace_body()
        no_signal_with_order["order_attempts"] = [success_body["order_attempts"][0]]
        no_signal_with_order["order_results"] = [success_body["order_results"][0]]
        no_signal_with_order["run_durable_trades"] = [
            success_body["run_durable_trades"][0]
        ]
        no_signal_with_order["final_state"]["actual_order_count"] = 1
        invalid_trace_bodies.append(("NO_SIGNAL_ORDER", no_signal_with_order))
        blocked_with_order = self._success_trace_body()
        blocked_with_order["outcome"] = "BLOCKED"
        blocked_with_order["typed_reason"] = "RISK_POLICY_BLOCKED"
        invalid_trace_bodies.append(("BLOCKED_ORDER", blocked_with_order))
        no_signal_with_recovery = self._no_signal_trace_body()
        no_signal_with_recovery["recovery"][
            "authoritative_position_quantity"
        ] = "0.001"
        no_signal_with_recovery["recovery"]["effective_free_quantity"] = "0.001"
        invalid_trace_bodies.append(("NO_SIGNAL_RECOVERY", no_signal_with_recovery))

        # FILLED·durable·recovery·zero-state가 모두 완료된 trace를 FAILED로 축소할 수 없다.
        completed_as_failed = self._success_trace_body()
        completed_as_failed["outcome"] = "FAILED"
        completed_as_failed["typed_reason"] = "TRACE_CAPTURE_FAILED"
        invalid_trace_bodies.append(("COMPLETED_AS_FAILED", completed_as_failed))

        for case_name, trace_body in invalid_trace_bodies:
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

    def test_actual_artifact_apis_require_nonempty_redaction_canaries(self) -> None:
        """
        함수 이름: test_actual_artifact_apis_require_nonempty_redaction_canaries()
        기능: actual seal/validate/bytes API가 빈 canary를 거부하고 실제 canary로만 성공하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        trace_body = self._success_trace_body()
        forbidden_values = (
            API_KEY_CANARY,
            API_SECRET_CANARY,
            SESSION_TOKEN_CANARY,
        )

        # Unit용 permissive API와 달리 actual artifact API는 빈 redaction tuple로 호출할 수 없다.
        with self.assertRaises(PhaseThirteenPublicTraceValidationError):
            seal_actual_phase13_public_trace(trace_body, forbidden_values=())
        permissive_trace = seal_phase13_public_trace(trace_body)
        with self.assertRaises(PhaseThirteenPublicTraceValidationError):
            validate_actual_phase13_public_trace(
                permissive_trace,
                forbidden_values=(),
            )
        with self.assertRaises(PhaseThirteenPublicTraceValidationError):
            canonical_actual_phase13_public_trace_bytes(
                permissive_trace,
                forbidden_values=(),
            )

        # 실제 canary 세 개가 있으면 seal→validate→bytes가 동일 digest와 canonical framing을 만든다.
        sealed_trace = seal_actual_phase13_public_trace(
            trace_body,
            forbidden_values=forbidden_values,
        )
        self.assertEqual(
            validate_actual_phase13_public_trace(
                sealed_trace,
                forbidden_values=forbidden_values,
            ),
            sealed_trace["trace_sha256"],
        )
        self.assertTrue(
            canonical_actual_phase13_public_trace_bytes(
                sealed_trace,
                forbidden_values=forbidden_values,
            ).endswith(b"\n")
        )

        # Safe-looking source field에 실제 key가 섞여도 고정 오류가 canary를 반사하지 않는다.
        canary_body = self._success_trace_body()
        canary_body["public_market_events"][0]["source_event_id"] = (
            f"event-{API_KEY_CANARY}"
        )
        with self.assertRaises(
            PhaseThirteenPublicTraceValidationError
        ) as raised_context:
            seal_actual_phase13_public_trace(
                canary_body,
                forbidden_values=forbidden_values,
            )
        self.assertNotIn(API_KEY_CANARY, str(raised_context.exception))

    def test_recovery_and_final_zero_state_must_agree(self) -> None:
        """
        함수 이름: test_recovery_and_final_zero_state_must_agree()
        기능: recovery가 authoritative Position/free를 넘거나 fresh final state와 다르면 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        excessive_recovery = self._success_trace_body()
        excessive_recovery["recovery"]["submitted_quantity"] = "0.005"
        final_position_drift = self._success_trace_body()
        final_position_drift["final_state"]["position_quantity"] = "0.001"
        pending_drift = self._success_trace_body()
        pending_drift["final_state"]["pending_order_count"] = 1

        # 같은 run의 authoritative exposure와 마지막 fresh runtime 사실을 두 경계에서 비교한다.
        for case_name, trace_body in (
            ("excessive_recovery", excessive_recovery),
            ("final_position_drift", final_position_drift),
            ("pending_drift", pending_drift),
        ):
            with self.subTest(case_name=case_name):
                self._assert_body_is_rejected(trace_body)

    def test_exact_one_hundred_buy_is_allowed_and_above_ceiling_is_rejected(
        self,
    ) -> None:
        """
        함수 이름: test_exact_one_hundred_buy_is_allowed_and_above_ceiling_is_rejected()
        기능: immutable decision과 BUY attempt가 exact 100을 허용하고 그 초과를 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        exact_ceiling_body = self._success_trace_body()

        # Fingerprint와 BUY attempt를 함께 exact 100으로 바꿔 correlation과 absolute cap을 동시에 검증한다.
        for decision_claim in (
            exact_ceiling_body["immutable_decision_fingerprint"],
            exact_ceiling_body["order_attempts"][0],
        ):
            decision_claim["decision_price"] = "2500"
            decision_claim["final_submitted_quantity"] = "0.04"
            decision_claim["final_notional"] = "100"
            decision_claim["configured_cap"] = "100"
        buy_fill = exact_ceiling_body["order_results"][0]["incremental_fills"][0]
        buy_fill["quantity"] = "0.04"
        buy_fill["quote_amount"] = "100"
        buy_trade = exact_ceiling_body["run_durable_trades"][0]
        buy_trade["executed_quantity"] = "0.04"
        buy_trade["executed_amount"] = "100"
        sell_attempt = exact_ceiling_body["order_attempts"][1]
        sell_attempt["final_submitted_quantity"] = "0.04"
        sell_attempt["final_notional"] = "100.200"
        sell_fill = exact_ceiling_body["order_results"][1]["incremental_fills"][0]
        sell_fill["quantity"] = "0.04"
        sell_fill["quote_amount"] = "100.200"
        sell_trade = exact_ceiling_body["run_durable_trades"][1]
        sell_trade["executed_quantity"] = "0.04"
        sell_trade["executed_amount"] = "100.200"
        for recovery_field in (
            "authoritative_position_quantity",
            "effective_free_quantity",
            "submitted_quantity",
        ):
            exact_ceiling_body["recovery"][recovery_field] = "0.04"
        sealed_trace = seal_phase13_public_trace(exact_ceiling_body)
        self.assertEqual(
            validate_phase13_public_trace(sealed_trace),
            sealed_trace["trace_sha256"],
        )

        # Exact ceiling에서 한 최소 decimal unit만 넘어도 seal 이전에 fail closed한다.
        exceeded_body = deepcopy(exact_ceiling_body)
        for decision_claim in (
            exceeded_body["immutable_decision_fingerprint"],
            exceeded_body["order_attempts"][0],
        ):
            decision_claim["final_submitted_quantity"] = "0.0401"
            decision_claim["final_notional"] = "100.2500"
        self._assert_body_is_rejected(exceeded_body)

    def test_stop_recovery_sell_can_exceed_buy_cap_but_not_authoritative_exposure(
        self,
    ) -> None:
        """
        함수 이름: test_stop_recovery_sell_can_exceed_buy_cap_but_not_authoritative_exposure()
        기능: STOP SELL cap 예외가 same-run Position/free 수량 상한은 우회하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        recovery_body = self._success_trace_body()
        recovery_attempt = recovery_body["order_attempts"][1]

        # Recovery SELL notional은 100을 넘겨도 submitted quantity가 authoritative/free 2와 같으면 허용한다.
        for decision_claim in (
            recovery_body["immutable_decision_fingerprint"],
            recovery_body["order_attempts"][0],
        ):
            decision_claim["decision_price"] = "50"
            decision_claim["final_submitted_quantity"] = "2"
            decision_claim["final_notional"] = "100"
        buy_fill = recovery_body["order_results"][0]["incremental_fills"][0]
        buy_fill["price"] = "50"
        buy_fill["quantity"] = "2"
        buy_fill["quote_amount"] = "100"
        buy_trade = recovery_body["run_durable_trades"][0]
        buy_trade["executed_quantity"] = "2"
        buy_trade["executed_amount"] = "100"
        buy_trade["average_fill_price"] = "50"
        recovery_attempt["decision_price"] = "100"
        recovery_attempt["final_submitted_quantity"] = "2"
        recovery_attempt["final_notional"] = "200"
        recovery_body["recovery"]["authoritative_position_quantity"] = "2"
        recovery_body["recovery"]["effective_free_quantity"] = "2"
        recovery_body["recovery"]["submitted_quantity"] = "2"
        recovery_fill = recovery_body["order_results"][1]["incremental_fills"][0]
        recovery_fill["price"] = "100"
        recovery_fill["quantity"] = "2"
        recovery_fill["quote_amount"] = "200"
        recovery_trade = recovery_body["run_durable_trades"][1]
        recovery_trade["executed_quantity"] = "2"
        recovery_trade["executed_amount"] = "200"
        recovery_trade["average_fill_price"] = "100"
        sealed_trace = seal_phase13_public_trace(recovery_body)
        self.assertEqual(
            validate_phase13_public_trace(sealed_trace),
            sealed_trace["trace_sha256"],
        )

        # 동일 STOP SELL도 authoritative Position보다 한 unit 크면 recovery validator가 차단한다.
        excessive_recovery = deepcopy(recovery_body)
        excessive_recovery["recovery"]["submitted_quantity"] = "2.1"
        self._assert_body_is_rejected(excessive_recovery)

    def test_forbidden_value_arguments_are_strict_and_secret_free(self) -> None:
        """
        함수 이름: test_forbidden_value_arguments_are_strict_and_secret_free()
        기능: redaction canary API가 문자열 sequence만 받고 입력 원문을 오류에 반사하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        trace_body = self._success_trace_body()

        # 문자열 자체는 character sequence로 오인하지 않고 empty/non-string canary도 즉시 거부한다.
        invalid_forbidden_values = (
            API_KEY_CANARY,
            ("",),
            (API_KEY_CANARY, 1),
        )
        for invalid_value in invalid_forbidden_values:
            with self.subTest(value_type=type(invalid_value).__name__):
                with self.assertRaises((TypeError, ValueError)) as raised_context:
                    seal_phase13_public_trace(
                        trace_body,
                        forbidden_values=invalid_value,
                    )

                self.assertNotIn(API_KEY_CANARY, str(raised_context.exception))


if __name__ == "__main__":
    unittest.main()
