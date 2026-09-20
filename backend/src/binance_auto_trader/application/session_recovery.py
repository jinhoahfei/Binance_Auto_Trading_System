"""자동 세션 복구의 내부 장애 기록과 재시도 분류. 외부 계약은 변경하지 않는다."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

from binance_auto_trader.adapters.binance.spot_rest_client import BinanceAPIError
from binance_auto_trader.application.trade_history_controller import TradeHistoryPersistencePendingError


class SessionRecoveryRetry(RuntimeError):
    """아직 확정되지 않은 주문/스트림 사실을 다음 복구 시도에서 다시 확인한다."""

    def __init__(self, reason: str, *, retry_after: timedelta | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


@dataclass(slots=True)
class RecoveryIssue:
    """동일 원인을 합쳐 보존하는 내부 기록. 주문 식별자는 client order ID다."""
    category: str
    code: str
    client_order_id: str | None
    retryable: bool
    first_seen: datetime
    last_seen: datetime
    occurrences: int = 1


def exception_chain(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


def recovery_retry_after(error: BaseException) -> float:
    """중첩된 요청 오류의 거래소 wait-not-before를 줄이지 않는다."""
    return max((
        delay.total_seconds()
        for item in exception_chain(error)
        if isinstance((delay := getattr(item, "retry_after", None)), timedelta)
    ), default=0.0)


def is_transient_recovery_error(error: BaseException) -> bool:
    for item in exception_chain(error):
        if isinstance(item, (SessionRecoveryRetry, TradeHistoryPersistencePendingError, OSError)):
            return True
        if isinstance(item, BinanceAPIError):
            return item.status_code in (418, 429) or item.status_code >= 500
    return False
