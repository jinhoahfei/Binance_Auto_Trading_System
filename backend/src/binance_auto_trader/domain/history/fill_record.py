"""Trade v3의 실제 fill과 BNB 평가 근거를 lossless JSON으로 보존한다."""

from datetime import datetime
from decimal import Decimal
import re
from ..trading.order import Fill
from ..trading.fee_valuation import BnbFeeValuation

_FIELDS = frozenset({"exchange_order_id", "trade_id", "quantity", "price", "fee_amount", "fee_asset", "fee_quote_amount", "executed_at", "fee_valuation"})


def fill_to_record(fill: Fill) -> dict[str, object]:
    """
    함수 이름: fill_to_record()
    기능: 검증된 Fill과 평가 근거를 plain Decimal 문자열로 직렬화한다.
    인자: fill -> 실제 불변 Fill
    반환값: JSON object
    작성 날짜: 2026/09/09
    """
    if not isinstance(fill, Fill):
        raise TypeError("fee evidence must be Fill")
    valuation = fill.fee_valuation
    # 원 수수료와 평가값은 별도 필드이며 원래 체결 ID·시각에 결속된다.
    return {
        "exchange_order_id": fill.exchange_order_id, "trade_id": fill.trade_id,
        "quantity": format(fill.quantity, "f"), "price": format(fill.price, "f"),
        "fee_amount": format(fill.fee_amount, "f"), "fee_asset": fill.fee_asset,
        "fee_quote_amount": format(fill.fee_quote_amount, "f"),
        "executed_at": fill.executed_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "fee_valuation": None if valuation is None else {
            "source": valuation.source, "open_time_ms": valuation.open_time_ms,
            "close_time_ms": valuation.close_time_ms, "rate": format(valuation.rate, "f"),
        },
    }


def _decimal(value: object) -> Decimal:
    """
    함수 이름: _decimal()
    기능: JSON 숫자·지수·비유한 값 없이 plain 양수 또는 0 Decimal을 읽는다.
    인자: value -> 저장 문자열
    반환값: Decimal
    작성 날짜: 2026/09/09
    """
    if not isinstance(value, str) or re.fullmatch(r"(0|[1-9][0-9]*)(\.[0-9]+)?", value) is None:
        raise ValueError("fee evidence requires plain decimal strings")
    return Decimal(value)  # 최종 양수·수량 관계는 Fill에서 재검증한다.


def fill_from_record(record: object) -> Fill:
    """
    함수 이름: fill_from_record()
    기능: exact shape와 타입을 검증하고 네트워크 없이 저장 Fill을 복원한다.
    인자: record -> JSON object
    반환값: 검증된 Fill
    작성 날짜: 2026/09/09
    """
    if not isinstance(record, dict) or set(record) != _FIELDS:
        raise ValueError("invalid fill evidence fields")
    evidence = record["fee_valuation"]
    valuation = None
    if evidence is not None:
        if not isinstance(evidence, dict) or set(evidence) != {"source", "open_time_ms", "close_time_ms", "rate"}:
            raise ValueError("invalid BNB valuation fields")
        valuation = BnbFeeValuation(evidence["open_time_ms"], evidence["close_time_ms"], _decimal(evidence["rate"]), evidence["source"])
    timestamp = record["executed_at"]
    if not isinstance(timestamp, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", timestamp) is None:
        raise ValueError("fill evidence requires canonical UTC timestamp")

    # 저장값도 최초 live Fill과 동일한 수수료·평가 시간 불변식을 적용한다.
    return Fill(
        exchange_order_id=record["exchange_order_id"], trade_id=record["trade_id"],
        quantity=_decimal(record["quantity"]), price=_decimal(record["price"]),
        fee_amount=_decimal(record["fee_amount"]), fee_asset=record["fee_asset"],
        fee_quote_amount=_decimal(record["fee_quote_amount"]),
        executed_at=datetime.fromisoformat(timestamp.replace("Z", "+00:00")), fee_valuation=valuation,
    )
