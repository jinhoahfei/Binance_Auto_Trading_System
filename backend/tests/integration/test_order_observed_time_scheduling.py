"""Order reconciliation의 후속 due가 실제 관측 시각을 기준으로 하는지 검증한다."""

from datetime import timedelta
import unittest

from tests.integration.test_order_reconciliation_flow import (
    STARTED_AT,
    MutableUtcClock,
    ScriptedOrderRESTClient,
    _create_started_controller,
    _OrderResponseKind,
    _submit_case_b_buy,
)


class OrderObservedTimeSchedulingTests(unittest.TestCase):
    """
    클래스 이름: OrderObservedTimeSchedulingTests
    기능: 늦게 관찰한 query의 후속 backoff가 이전 wall clock으로 역산되지 않게 한다.
    작성 날짜: 2026/08/22
    """

    def test_unknown_query_schedules_next_delay_from_observed_time(self) -> None:
        """
        함수 이름: test_unknown_query_schedules_next_delay_from_observed_time()
        기능: occurred_at이 clock보다 늦을 때 다음 2초 query를 occurred_at 기준으로 예약한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        clock = MutableUtcClock()
        client = ScriptedOrderRESTClient(
            clock,
            submit_steps=(_OrderResponseKind.UNKNOWN,),
            query_steps=(
                _OrderResponseKind.UNKNOWN,
                _OrderResponseKind.UNKNOWN,
            ),
        )
        controller, _, _, _ = _create_started_controller(client, clock)
        self.assertEqual((), _submit_case_b_buy(controller))

        # Scheduler가 첫 1초 due를 10초 늦게 관찰해도 query는 이번 관측에서 한 번만 실행한다.
        first_observed_at = STARTED_AT + timedelta(seconds=10)
        self.assertEqual(
            (),
            controller.trigger_order_reconciliation(
                occurred_at=first_observed_at,
            ),
        )
        self.assertEqual(1, len(client.queried_orders))

        # 두 번째 backoff는 stale injected clock이 아니라 첫 실제 query 관측 시각 뒤 2초여야 한다.
        controller.trigger_order_reconciliation(
            occurred_at=first_observed_at
            + timedelta(seconds=2, microseconds=-1),
        )
        self.assertEqual(1, len(client.queried_orders))

        controller.trigger_order_reconciliation(
            occurred_at=first_observed_at + timedelta(seconds=2),
        )
        self.assertEqual(2, len(client.queried_orders))


if __name__ == "__main__":
    unittest.main()
