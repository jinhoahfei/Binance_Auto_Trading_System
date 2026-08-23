"""Trade History query parser와 상세 조회 route의 fail-closed 계약을 검증한다."""

from datetime import date, datetime, timezone
from decimal import Decimal
from threading import RLock
from types import SimpleNamespace
import unittest

from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.history import HistoryPeriod, TradeSide
from binance_auto_trader.domain.trading.states import OrderSide, StrategyType
from binance_auto_trader.transport.contracts import (
    MAX_TRADE_PAGE_SIZE,
    TransportContractError,
    parse_trade_history_query_parameters,
)
from binance_auto_trader.transport.event_stream import BackendEventStream
from binance_auto_trader.transport.routes import RouteContext
from binance_auto_trader.transport.routes.trade_history import get_trades


TEST_REQUEST_ID = "3c73d583-c1c8-4830-8393-cc31639a40fd"
TEST_EXECUTED_AT = datetime(2026, 8, 23, 1, 2, 3, tzinfo=timezone.utc)


class _TrackingLock:
    """
    클래스 이름: _TrackingLock
    기능: route가 Controller query를 application 임계 구역에서 실행하는지 기록한다.
    작성 날짜: 2026/08/23
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 재진입 lock과 현재 진입 깊이를 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        self._lock = RLock()
        self._depth = 0

    @property
    def held(self) -> bool:
        """
        함수 이름: held()
        기능: 현재 test thread가 route 임계 구역에 있는지 반환한다.
        인자: 없음
        반환값: application lock 진입 여부
        작성 날짜: 2026/08/23
        """
        return self._depth > 0  # Controller 호출 시점의 lock 소유 여부를 직접 관찰한다.

    def __enter__(self) -> object:
        """
        함수 이름: __enter__()
        기능: 내부 재진입 lock을 획득하고 진입 깊이를 증가시킨다.
        인자: 없음
        반환값: lock context 자신
        작성 날짜: 2026/08/23
        """
        self._lock.acquire()
        self._depth += 1
        return self

    def __exit__(
        self,
        exception_type: object,
        exception_value: object,
        traceback: object,
    ) -> None:
        """
        함수 이름: __exit__()
        기능: 진입 깊이를 감소시키고 내부 재진입 lock을 해제한다.
        인자: exception_type -> 발생한 예외 타입 또는 None
            exception_value -> 발생한 예외 값 또는 None
            traceback -> 발생한 traceback 또는 None
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        self._depth -= 1
        self._lock.release()


class _TradeDetailsController:
    """
    클래스 이름: _TradeDetailsController
    기능: 상세 조회 인자와 application lock 소유 여부를 기록하는 Controller test double이다.
    작성 날짜: 2026/08/23
    """

    def __init__(
        self,
        application_lock: _TrackingLock,
        result: object,
        error: Exception | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 반환 결과 또는 발생시킬 repository 오류와 호출 기록을 준비한다.
        인자: application_lock -> query 호출의 임계 구역 여부를 확인할 lock
            result -> 정상 조회에서 반환할 TradeDetailsResult 대역
            error -> 조회 시 발생시킬 optional 오류
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        self._application_lock = application_lock
        self._result = result
        self._error = error
        self.calls: list[tuple[HistoryPeriod, TradeSide]] = []

    def get_trade_details(
        self,
        period: HistoryPeriod,
        side: TradeSide,
    ) -> object:
        """
        함수 이름: get_trade_details()
        기능: canonical filter 호출을 기록하고 설정된 결과 또는 오류를 반환한다.
        인자: period -> canonical history 기간
            side -> canonical 거래 방향
        반환값: TradeDetailsResult 대역
        작성 날짜: 2026/08/23
        """
        if not self._application_lock.held:
            raise AssertionError("query must run under the application lock")

        self.calls.append((period, side))  # route가 parser의 enum을 그대로 전달했는지 보존한다.
        if self._error is not None:
            raise self._error
        return self._result


def _create_trade_details_result() -> SimpleNamespace:
    """
    함수 이름: _create_trade_details_result()
    기능: Decimal 정밀도와 조회 범위를 모두 포함한 상세 조회 결과 대역을 생성한다.
    인자: 없음
    반환값: TradeDetailsResult shape의 test fixture
    작성 날짜: 2026/08/23
    """
    # Trade DTO의 모든 금융값이 JSON number가 아닌 plain string으로 유지되게 구성한다.
    trade = SimpleNamespace(
        trade_id="trade-1",
        order_id="123",
        client_order_id="client-1",
        symbol="ETHUSDT",
        executed_at=TEST_EXECUTED_AT,
        side=OrderSide.BUY,
        regime_type=RegimeType.TYPE_2,
        strategy=StrategyType.CASE_B,
        requested_quantity=Decimal("0.1000"),
        executed_quantity=Decimal("0.1000"),
        executed_amount=Decimal("432.1500"),
        average_fill_price=Decimal("4321.5000"),
        market_price_at_decision=Decimal("4320.0000"),
        fee_amount=Decimal("0.0001"),
        fee_asset="ETH",
        fee_quote_amount=Decimal("0.4321500"),
        allocated_cost_basis=None,
        realized_pnl=None,
        realized_return_rate=None,
        exit_reason=None,
    )

    # Summary는 filtered row와 별개인 authoritative Account·Performance 값을 사용한다.
    performance = SimpleNamespace(
        daily_return_rate=Decimal("0.01000000"),
        cumulative_return_rate=Decimal("0.02000000"),
        realized_pnl=Decimal("12.3400"),
        daily_fee=Decimal("0.4321500"),
        total_fee=Decimal("1.2345000"),
        average_sell_return_rate=Decimal("0.03000000"),
        total_profit=Decimal("12.3400"),
        winning_sell_count=2,
        losing_sell_count=1,
        breakeven_sell_count=0,
        completed_sell_count=3,
        win_rate=Decimal("0.66666667"),
    )
    query = SimpleNamespace(
        start_date=date(2026, 8, 17),
        end_date=date(2026, 8, 23),
        side=TradeSide.BUY,
    )
    return SimpleNamespace(
        query=query,
        rows=(trade,),
        holdings_asset="ETH",
        holdings=Decimal("1.25000000"),
        account_version=17,
        performance=performance,
    )


def _create_route_context(
    *,
    ready: bool = True,
    query_error: Exception | None = None,
) -> tuple[RouteContext, _TradeDetailsController]:
    """
    함수 이름: _create_route_context()
    기능: readiness와 query 결과를 선택할 수 있는 trade-history route context를 만든다.
    인자: ready -> application startup 완료 여부
        query_error -> Controller가 발생시킬 optional 조회 오류
    반환값: RouteContext와 호출 기록용 Controller tuple
    작성 날짜: 2026/08/23
    """
    application_lock = _TrackingLock()
    controller = _TradeDetailsController(
        application_lock,
        _create_trade_details_result(),
        query_error,
    )
    runtime = SimpleNamespace(
        application_lock=application_lock,
        ready=ready,
        trade_history_controller=controller,
    )
    context = RouteContext(runtime, BackendEventStream())
    return context, controller


class TradeHistoryQueryParserTests(unittest.TestCase):
    """
    클래스 이름: TradeHistoryQueryParserTests
    기능: period와 side query string의 exact cardinality 및 canonical value 계약을 검증한다.
    작성 날짜: 2026/08/23
    """

    def test_accepts_every_canonical_period_and_side_pair(self) -> None:
        """
        함수 이름: test_accepts_every_canonical_period_and_side_pair()
        기능: 네 기간과 세 방향의 모든 wire 조합을 canonical domain enum으로 변환한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        period_pairs = (
            ("today", HistoryPeriod.TODAY),
            ("last7days", HistoryPeriod.LAST_7_DAYS),
            ("last30days", HistoryPeriod.LAST_30_DAYS),
            ("all", HistoryPeriod.ALL),
        )
        side_pairs = (
            ("all", TradeSide.ALL),
            ("buy", TradeSide.BUY),
            ("sell", TradeSide.SELL),
        )

        # Query field 순서에는 의미를 부여하지 않고 각 canonical pair만 정확히 허용한다.
        for period_wire, expected_period in period_pairs:
            for side_wire, expected_side in side_pairs:
                with self.subTest(period=period_wire, side=side_wire):
                    self.assertEqual(
                        parse_trade_history_query_parameters(
                            f"side={side_wire}&period={period_wire}"
                        ),
                        (expected_period, expected_side),
                    )

    def test_rejects_missing_blank_duplicate_unknown_and_invalid_queries(
        self,
    ) -> None:
        """
        함수 이름: test_rejects_missing_blank_duplicate_unknown_and_invalid_queries()
        기능: fallback 가능한 모든 비정규 query shape와 값을 typed 400으로 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        invalid_queries: tuple[object, ...] = (
            "",
            "period=today",
            "side=all",
            "period=&side=all",
            "period=today&side=",
            "period=today&period=all&side=all",
            "period=today&side=all&side=buy",
            "period=today&side=all&unknown=value",
            "period=TODAY&side=all",
            "period=weekly&side=all",
            "period=today&side=BUY",
            "period=today&side=buy%20",
            "period=today&broken&side=all",
            "period=today&side=all&",
            "period=today&side=%FF",
            None,
        )

        # Invalid input은 모두 같은 공개 code와 retry 불가 400 계약으로 수렴해야 한다.
        for invalid_query in invalid_queries:
            with self.subTest(invalid_query=invalid_query):
                with self.assertRaises(TransportContractError) as raised:
                    parse_trade_history_query_parameters(invalid_query)
                self.assertEqual(raised.exception.code, "MALFORMED_REQUEST")
                self.assertEqual(raised.exception.status, 400)
                self.assertFalse(raised.exception.retryable)


class TradeHistoryRouteTests(unittest.TestCase):
    """
    클래스 이름: TradeHistoryRouteTests
    기능: 상세 조회 성공 DTO와 readiness·validation·repository failure 응답을 검증한다.
    작성 날짜: 2026/08/23
    """

    def test_returns_composite_details_with_plain_decimal_strings(self) -> None:
        """
        함수 이름: test_returns_composite_details_with_plain_decimal_strings()
        기능: canonical query와 rows 및 별도 summary를 한 versioned 성공 응답으로 반환한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        context, controller = _create_route_context()

        response = get_trades(
            TEST_REQUEST_ID,
            context,
            "period=last7days&side=buy",
        )

        self.assertEqual(response.status, 200)
        self.assertTrue(response.payload["ok"])
        details = response.payload["data"]
        self.assertEqual(
            details["query"],
            {
                "period": "last7days",
                "side": "buy",
                "start_date": "2026-08-17",
                "end_date": "2026-08-23",
            },
        )
        self.assertEqual(details["row_count"], 1)
        self.assertEqual(details["rows"][0]["executed_amount"], "432.1500")
        self.assertEqual(
            details["rows"][0]["average_fill_price"],
            "4321.5000",
        )
        self.assertEqual(details["summary"]["holdings_asset"], "ETH")
        self.assertEqual(details["summary"]["holdings"], "1.25000000")
        self.assertEqual(details["summary"]["account_version"], 17)
        self.assertEqual(
            details["summary"]["performance"]["win_rate"],
            "0.66666667",
        )
        self.assertEqual(
            controller.calls,
            [(HistoryPeriod.LAST_7_DAYS, TradeSide.BUY)],
        )

    def test_rejects_invalid_query_without_calling_controller(self) -> None:
        """
        함수 이름: test_rejects_invalid_query_without_calling_controller()
        기능: unknown query field를 typed 400으로 반환하고 application query를 실행하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        context, controller = _create_route_context()

        response = get_trades(
            TEST_REQUEST_ID,
            context,
            "period=today&side=all&page=1",
        )

        self.assertEqual(response.status, 400)
        self.assertFalse(response.payload["ok"])
        self.assertEqual(response.payload["error"]["code"], "MALFORMED_REQUEST")
        self.assertEqual(controller.calls, [])

    def test_returns_retryable_not_ready_before_controller_query(self) -> None:
        """
        함수 이름: test_returns_retryable_not_ready_before_controller_query()
        기능: startup 미완료 runtime을 공통 BACKEND_NOT_READY 응답으로 닫는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        context, controller = _create_route_context(ready=False)

        response = get_trades(
            TEST_REQUEST_ID,
            context,
            "period=today&side=all",
        )

        self.assertEqual(response.status, 503)
        self.assertEqual(response.payload["error"]["code"], "BACKEND_NOT_READY")
        self.assertTrue(response.payload["error"]["retryable"])
        self.assertEqual(controller.calls, [])

    def test_redacts_repository_failure_as_retryable_query_error(self) -> None:
        """
        함수 이름: test_redacts_repository_failure_as_retryable_query_error()
        기능: 상세 조회 내부 오류를 노출하지 않고 재시도 가능한 안정적 503으로 변환한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        secret_text = "database credential=private-secret"
        context, controller = _create_route_context(
            query_error=OSError(secret_text),
        )

        response = get_trades(
            TEST_REQUEST_ID,
            context,
            "period=all&side=sell",
        )

        self.assertEqual(response.status, 503)
        self.assertEqual(
            response.payload["error"]["code"],
            "TRADE_HISTORY_QUERY_FAILED",
        )
        self.assertTrue(response.payload["error"]["retryable"])
        self.assertNotIn(secret_text, str(response.payload))
        self.assertEqual(controller.calls, [(HistoryPeriod.ALL, TradeSide.SELL)])

    def test_rejects_trade_page_over_transport_row_limit(self) -> None:
        """
        함수 이름: test_rejects_trade_page_over_transport_row_limit()
        기능: ADR-005의 1,000행 상한을 조용한 절삭 없이 typed 413으로 강제한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        context, controller = _create_route_context()
        controller._result.rows = (
            controller._result.rows * (MAX_TRADE_PAGE_SIZE + 1)
        )

        response = get_trades(
            TEST_REQUEST_ID,
            context,
            "period=all&side=buy",
        )

        self.assertEqual(response.status, 413)
        self.assertEqual(
            response.payload["error"]["code"],
            "TRADE_HISTORY_PAGE_TOO_LARGE",
        )
        self.assertFalse(response.payload["error"]["retryable"])
        self.assertEqual(
            response.payload["error"]["details"],
            {"maximum_rows": MAX_TRADE_PAGE_SIZE},
        )  # 행을 잘라 ALL 의미를 바꾸지 않고 더 좁은 필터를 요구한다.
