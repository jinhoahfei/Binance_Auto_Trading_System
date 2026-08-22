"""ExecutionSummary의 중복 fill identity 거부 회귀를 검증한다."""

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading import DuplicateFillConflictError
from binance_auto_trader.domain.trading.order import ExecutionSummary, Fill
from binance_auto_trader.domain.trading.states import (
    OrderSide,
    StrategyType,
)


EXECUTED_AT = datetime(2026, 8, 22, 6, 0, tzinfo=timezone.utc)


class ExecutionSummaryDuplicateFillTests(unittest.TestCase):
    """
    클래스 이름: ExecutionSummaryDuplicateFillTests
    기능: 동일 exchange order·trade ID가 요약 금액을 중복 증가시키지 못하게 한다.
    작성 날짜: 2026/08/22
    """

    def test_rejects_duplicate_fill_keys_before_publishing_summary(self) -> None:
        """
        함수 이름: test_rejects_duplicate_fill_keys_before_publishing_summary()
        기능: 내용이 같은 fill이라도 같은 dedup key를 두 번 집계하는 요약을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        fill = Fill(
            exchange_order_id="7001",
            trade_id="duplicate-trade",
            quantity=Decimal("1"),
            price=Decimal("100"),
            fee_amount=Decimal("0.10"),
            fee_asset="USDT",
            fee_quote_amount=Decimal("0.10"),
            executed_at=EXECUTED_AT,
        )

        # Aggregate 숫자를 중복 tuple에 맞춰도 identity 중복은 독립 검증해야 한다.
        with self.assertRaises(DuplicateFillConflictError):
            ExecutionSummary(
                exchange_order_id="7001",
                client_order_id="client-7001",
                symbol="ETHUSDT",
                side=OrderSide.BUY,
                strategy=StrategyType.CASE_B,
                regime_type=RegimeType.TYPE_0,
                exit_reason=None,
                requested_quantity=Decimal("2"),
                submitted_quantity=Decimal("2"),
                executed_quantity=Decimal("2"),
                executed_amount=Decimal("200"),
                average_fill_price=Decimal("100"),
                fee_amount=Decimal("0.20"),
                fee_asset="USDT",
                fee_quote_amount=Decimal("0.20"),
                executed_at=EXECUTED_AT,
                fills=(fill, fill),
            )


if __name__ == "__main__":
    unittest.main()
