"""전략 입력과 전이 원인을 파일 구현과 독립된 진단 값으로 구성한다."""

from dataclasses import replace
import re

from ..domain.trading.conditions import evaluate_condition
from ..domain.trading.context import TradingContextView
from ..domain.trading.states import TradingStateConfiguration
from .trading_indicator_snapshot import select_indicator_slots


# Adapter가 이미 분류한 고정 원인만 허용하며 거래소의 자유 형식 메시지는 보존하지 않는다.
_ORDER_FAILURE_PATTERN = re.compile(
    r"(BINANCE_(?:TRANSPORT_UNKNOWN|PAYLOAD_RECONCILIATION_REQUIRED|ORDER_NOT_VISIBLE|"
    r"QUERY_UNKNOWN|QUERY_TRANSPORT_UNKNOWN|QUERY_RECONCILIATION_REQUIRED|"
    r"CANCEL_UNKNOWN|CANCEL_TRANSPORT_UNKNOWN|CANCEL_RECONCILIATION_REQUIRED|"
    r"SUBMISSION_UNKNOWN|SUBMISSION_REJECTED))(?:_(-?[0-9]{1,10}))?"
)
_STREAM_REASONS = frozenset({
    "market_stream_initializing", "market_stream_initialization_failed", "kline_stream_closed",
    "kline_stream_disconnected", "kline_stream_invalid", "account_stream_disconnected",
    "account_stream_closed", "account_stream_processing_failed",
    "market_input_stalled", "market_evaluation_stalled",
})
# 2026/09/09 Binance 공식 User Data Stream의 Order Reject Reason 표를 확인한 고정 값이다.
# https://developers.binance.com/en/docs/products/spot/user-data-stream#order-reject-reason
_ORDER_REJECT_REASONS = frozenset({
    "INSUFFICIENT_BALANCES", "STOP_PRICE_WOULD_TRIGGER_IMMEDIATELY",
    "WOULD_MATCH_IMMEDIATELY", "OCO_BAD_PRICES",
})


def normalize_stream_reason(reason: str) -> str:
    """
    함수 이름: normalize_stream_reason()
    기능: stream callback이 전달한 고정 분류만 기록하고 임의 close 메시지를 제외한다.
    인자: reason -> 기존 Gateway의 장애 분류
    반환값: 허용된 분류 또는 고정 미분류 표식
    작성 날짜: 2026/09/09
    """
    return reason if reason in _STREAM_REASONS else "UNCLASSIFIED_STREAM_REASON"  # 서버 close 원문을 반사하지 않는다.


def normalize_order_failure(reason: str | None) -> dict[str, object] | None:
    """
    함수 이름: normalize_order_failure()
    기능: 정규화된 주문 거부 사유에서 고정 분류와 정수 Binance code만 보존한다.
    인자: reason -> 기존 adapter의 내부 failure reason 또는 None
    반환값: 안전한 원인과 API code 또는 성공의 None
    작성 날짜: 2026/09/09
    """
    if reason is None:
        return None
    if reason in _ORDER_REJECT_REASONS:
        return {"code": reason, "api_code": None}  # 공식 enum만 보존하고 자유 형식 서버 message는 기록하지 않는다.

    # Whitelist 밖 문자열은 그대로 기록하지 않아 가짜 응답·예외에 포함된 비밀도 차단한다.
    match = _ORDER_FAILURE_PATTERN.fullmatch(reason)
    if match is None:
        return {"code": "UNCLASSIFIED_ORDER_FAILURE", "api_code": None}
    return {"code": match[1], "api_code": None if match[2] is None else int(match[2])}


def describe_trading_evaluation(state: TradingStateConfiguration, context: TradingContextView) -> dict[str, object]:
    """
    함수 이름: describe_trading_evaluation()
    기능: 현재 단계의 공통 비교 함수로 입력값·기준·미충족 원인과 확정봉 적용 여부를 구성한다.
    인자: state -> 판단 직전 STM 상태, context -> 같은 microstep의 불변 입력
    반환값: 원본 시장·runtime·포지션과 조건 비교 목록
    작성 날짜: 2026/09/09
    """
    slots, phase_key, notice = select_indicator_slots(state, context)
    comparisons = []

    # 전이 Guard와 같은 순수 비교만 사용하며 비확정봉에는 종가 조건 충족을 주장하지 않는다.
    for slot in slots:
        condition = evaluate_condition(slot.condition_id, context)
        applicable = (
            condition.source != "close_30m" or context.market.confirmed_30m_close
        ) and (condition.source != "close_1m" or context.market.confirmed_1m_close)
        if not applicable:
            condition = replace(condition, satisfied=None)
        comparisons.append({"phase": slot.phase, "strategy": slot.strategy, "applicable": applicable, "condition": condition})

    # 값은 재계산한 다른 tick이 아닌 STM이 실제 사용한 Context에서 가져온다.
    return {
        "phase_key": phase_key,
        "notice": notice,
        "evaluated_at": context.evaluated_at,
        "context_version": context.version,
        "state": state,
        "market": context.market,
        "runtime": context.runtime,
        "position": context.position,
        "pending_order": context.pending_order,
        "condition_comparisons": tuple(comparisons),
    }
