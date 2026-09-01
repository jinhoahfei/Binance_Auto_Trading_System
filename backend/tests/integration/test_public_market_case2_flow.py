"""공개 30분 Kline에서 Case C BUY·체결·History까지의
Case 2 흐름을 검증한다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch as mock_patch

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    WebSocketGateway,
)
from binance_auto_trader.adapters.persistence import TradeHistoryRepository
from binance_auto_trader.application.market_data_controller import (
    MarketDataController,
)
from binance_auto_trader.application.market_evaluation_builder import (
    ThirtyMinuteMarketEvaluationBuilder,
)
from binance_auto_trader.application.regime_controller import RegimeController
from binance_auto_trader.application.trade_history_controller import (
    TradeHistoryController,
)
from binance_auto_trader.application.trading_controller import (
    TradingController,
    TradingSessionStatus,
)
from binance_auto_trader.domain.common import Interval, RegimeType
from binance_auto_trader.domain.history import Performance, Trade
from binance_auto_trader.domain.market import Kline, MarketSnapshot
from binance_auto_trader.domain.regime import RegimeSTM
from binance_auto_trader.domain.trading import (
    Account,
    CaseCPositionState,
    ExitReason,
    Order,
    OrderAttemptKind,
    OrderResult,
    OrderResultFailureKind,
    OrderSide,
    OrderStatus,
    Position,
    PositionReturnState,
    StrategyType,
    SubmitOrder,
    TradingContext,
    TradingEvent,
    TradingEventType,
    TradingPhase,
)
from binance_auto_trader.domain.trading.results import TradingSTMResult
from binance_auto_trader.domain.trading.transitions.helpers import (
    create_exit_order_actions,
)

from tests.integration.phase13_risk_fixture import create_test_risk_policy
from tests.integration.test_account_stream_flow import (
    SynchronousAccountWebSocketClient,
)
from tests.integration.test_buy_sell_flow import (
    FakeOrderRESTClient,
    FakeOrderScenario,
    MutableUtcClock,
)
from tests.integration.test_market_initialization_flow import (
    FakeWebSocketClient,
    NoOpRegimeController,
)


# 모든 REST 기준선과 live Kline은 같은 UTC 30분봉을 사용한다.
# 각 시각은 그 진행봉 안에서 결정적으로 전진한다.
SYMBOL = "ETHUSDT"
CURRENT_THIRTY_MINUTE_OPEN = datetime(
    2026,
    8,
    29,
    12,
    0,
    tzinfo=timezone.utc,
)
INITIAL_TIME = CURRENT_THIRTY_MINUTE_OPEN + timedelta(
    minutes=10,
    seconds=5,
)
INTERVAL_DURATION_MILLISECONDS = {
    Interval.ONE_MINUTE: 60_000,
    Interval.THIRTY_MINUTES: 1_800_000,
    Interval.FOUR_HOURS: 14_400_000,
    Interval.ONE_DAY: 86_400_000,
}
BASE_CLOSED_PRICES = tuple(
    Decimal(100 + index)
    for index in range(20)
)


class PublicCase2OrderScenario(str, Enum):
    """
    클래스 이름: PublicCase2OrderScenario
    기능: Public Case C BUY 뒤 local fake가 재생할 주문 진행을 구분한다.
    작성 날짜: 2026/08/29
    """

    IMMEDIATE_FILLED = "IMMEDIATE_FILLED"
    PARTIALS_THEN_FILLED = "PARTIALS_THEN_FILLED"
    SELL_PARTIAL_THEN_FILLED = "SELL_PARTIAL_THEN_FILLED"
    UNKNOWN_REMAINS_UNRESOLVED = "UNKNOWN_REMAINS_UNRESOLVED"
    SUBMISSION_REJECTION_CONFIRMED = "SUBMISSION_REJECTION_CONFIRMED"


def _create_rest_kline_row(
    interval: Interval,
    open_time: datetime,
    close_price: Decimal,
) -> list[object]:
    """
    함수 이름: _create_rest_kline_row()
    기능: APIGateway가 정규화할 공식 12-field Spot REST Kline 행을 만든다.
    인자: interval -> 행의 canonical Kline 주기
        open_time -> 봉 시작 UTC 시각
        close_price -> OHLC에 사용할 양의 Decimal 종가
    반환값: Binance REST schema와 같은 원시 Kline list
    작성 날짜: 2026/08/29
    """
    open_time_milliseconds = int(open_time.timestamp() * 1_000)
    close_time_milliseconds = (
        open_time_milliseconds
        + INTERVAL_DURATION_MILLISECONDS[interval]
        - 1
    )

    # High·low를 종가에서 한 단위씩 벌린다.
    # 그 결과 Bollinger와 CCI 입력이 유효하게 유지된다.
    return [
        open_time_milliseconds,
        format(close_price, "f"),
        format(close_price + Decimal("1"), "f"),
        format(close_price - Decimal("1"), "f"),
        format(close_price, "f"),
        "10",
        close_time_milliseconds,
        "1000",
        10,
        "5",
        "500",
        "0",
    ]


def _create_market_rest_responses() -> dict[str, list[list[object]]]:
    """
    함수 이름: _create_market_rest_responses()
    기능: 20개 확정 30분봉과 각 주기의 현재 진행봉을
        local REST 기준선으로 만든다.
    인자: 없음
    반환값: interval 문자열별 공식 Kline 행 mapping
    작성 날짜: 2026/08/29
    """
    # 마지막 확정 30분봉은 현재 진행봉 직전에 끝난다.
    # 이 경계를 기준으로 정확한 연속 history를 만든다.
    closed_thirty_minute_rows = [
        _create_rest_kline_row(
            Interval.THIRTY_MINUTES,
            CURRENT_THIRTY_MINUTE_OPEN
            - timedelta(
                minutes=30 * (len(BASE_CLOSED_PRICES) - index),
            ),
            close_price,
        )
        for index, close_price in enumerate(BASE_CLOSED_PRICES)
    ]
    current_thirty_minute_row = _create_rest_kline_row(
        Interval.THIRTY_MINUTES,
        CURRENT_THIRTY_MINUTE_OPEN,
        Decimal("100"),
    )
    current_one_minute_row = _create_rest_kline_row(
        Interval.ONE_MINUTE,
        INITIAL_TIME.replace(second=0, microsecond=0),
        Decimal("100"),
    )
    current_four_hour_row = _create_rest_kline_row(
        Interval.FOUR_HOURS,
        CURRENT_THIRTY_MINUTE_OPEN,
        Decimal("100"),
    )
    current_one_day_row = _create_rest_kline_row(
        Interval.ONE_DAY,
        CURRENT_THIRTY_MINUTE_OPEN.replace(hour=0),
        Decimal("100"),
    )

    return {
        Interval.ONE_MINUTE.value: [current_one_minute_row],
        Interval.THIRTY_MINUTES.value: [
            *closed_thirty_minute_rows,
            current_thirty_minute_row,
        ],
        Interval.FOUR_HOURS.value: [current_four_hour_row],
        Interval.ONE_DAY.value: [current_one_day_row],
    }


class PublicCase2RESTClient(FakeOrderRESTClient):
    """
    클래스 이름: PublicCase2RESTClient
    기능: Local Kline 기준선과 scripted 주문 사실을
        한 fake REST 경계에서 제공한다.
    작성 날짜: 2026/08/29
    """

    def __init__(
        self,
        clock: MutableUtcClock,
        order_scenario: PublicCase2OrderScenario,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 선택한 local 주문 시나리오와 고정된
            네 주기 Kline 응답을 준비한다.
        인자: clock -> 시장·주문 결과가 공유하는 결정론적 UTC clock
            order_scenario -> public Case C BUY 뒤 재생할 주문 상태 진행
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        if not isinstance(order_scenario, PublicCase2OrderScenario):
            raise TypeError("order_scenario must be a PublicCase2OrderScenario")

        # 기존 fill builder가 필요한 두 성공 경로만
        # 대응하는 base 시나리오로 변환한다.
        base_scenario = (
            FakeOrderScenario.PARTIALS_THEN_FILLED
            if order_scenario
            in (
                PublicCase2OrderScenario.PARTIALS_THEN_FILLED,
                PublicCase2OrderScenario.SELL_PARTIAL_THEN_FILLED,
            )
            else FakeOrderScenario.IMMEDIATE_FILLED
        )
        super().__init__(base_scenario, clock)
        self.order_scenario = order_scenario
        self.market_responses = _create_market_rest_responses()
        self.kline_requests: list[tuple[str, str, int]] = []
        self.exchange_order_ids_by_client_id: dict[str, str] = {}

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: Public Case C 주문을 기록하고 선택한
            최초 normalized 결과를 반환한다.
        인자: order -> Controller가 생성한 실제 Case C 주문 aggregate
        반환값: FILLED, PARTIALLY_FILLED, UNKNOWN 또는 typed REJECTED 결과
        작성 날짜: 2026/08/29
        """
        if self.order_scenario in (
            PublicCase2OrderScenario.IMMEDIATE_FILLED,
            PublicCase2OrderScenario.PARTIALS_THEN_FILLED,
        ):
            if not isinstance(order, Order):
                raise TypeError("order must be an Order")

            # BUY와 후속 SELL은 서로 다른 양의 숫자 exchange ID를 가져야
            # durable Trade identity가 충돌하지 않는다.
            self.exchange_order_ids_by_client_id.setdefault(
                order.client_order_id,
                str(1_001 + len(self.exchange_order_ids_by_client_id)),
            )
            return super().submit_order(order=order)
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")

        # Provenance race 시나리오는 BUY를 즉시 열고 SELL만 partial로 남겨
        # terminal query 전에 시장 snapshot을 바꿀 수 있게 한다.
        if (
            self.order_scenario
            is PublicCase2OrderScenario.SELL_PARTIAL_THEN_FILLED
        ):
            self.exchange_order_ids_by_client_id.setdefault(
                order.client_order_id,
                str(1_001 + len(self.exchange_order_ids_by_client_id)),
            )
            self.submitted_orders.append(order)
            return self._build_result(
                order,
                (
                    OrderStatus.FILLED
                    if order.side is OrderSide.BUY
                    else OrderStatus.PARTIALLY_FILLED
                ),
                fill_count=(3 if order.side is OrderSide.BUY else 1),
            )

        # 외부 주문 없이 실제 aggregate identity와
        # 단 한 번의 제출 시도만 보존한다.
        self.submitted_orders.append(order)
        if (
            self.order_scenario
            is PublicCase2OrderScenario.UNKNOWN_REMAINS_UNRESOLVED
        ):
            return OrderResult(
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                status=OrderStatus.UNKNOWN,
                processed_at=self.clock(),
                failure_reason="SCRIPTED_PUBLIC_CASE2_UNKNOWN",
            )

        return OrderResult(
            symbol=order.symbol,
            client_order_id=order.client_order_id,
            status=OrderStatus.REJECTED,
            processed_at=self.clock(),
            failure_reason="SCRIPTED_PUBLIC_CASE2_SUBMISSION_REJECTED",
            failure_kind=OrderResultFailureKind.SUBMISSION_REJECTED,
        )

    def query_order_result(self, *, order: Order) -> OrderResult:
        """
        함수 이름: query_order_result()
        기능: Public Case C의 같은 주문을 조회해
            scripted reconciliation 사실을 반환한다.
        인자: order -> 최초 submit에서 받은 동일 주문 aggregate
        반환값: 누적 partial·FILLED 또는 fill 없는 UNKNOWN 결과
        작성 날짜: 2026/08/29
        """
        if self.order_scenario in (
            PublicCase2OrderScenario.IMMEDIATE_FILLED,
            PublicCase2OrderScenario.PARTIALS_THEN_FILLED,
        ):
            return super().query_order_result(order=order)
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")

        # SELL partial의 same-ID 재조회는 누적 fill 전체를 포함한 terminal로 완결한다.
        if (
            self.order_scenario
            is PublicCase2OrderScenario.SELL_PARTIAL_THEN_FILLED
        ):
            if (
                len(self.submitted_orders) != 2
                or order is not self.submitted_orders[1]
                or order.side is not OrderSide.SELL
            ):
                raise AssertionError("query must target the partial SELL order")
            if self.queried_orders:
                raise AssertionError("partial SELL allows exactly one query")
            self.queried_orders.append(order)
            return self._build_result(
                order,
                OrderStatus.FILLED,
                fill_count=3,
            )
        if not self.submitted_orders or order is not self.submitted_orders[0]:
            raise AssertionError("query must reuse the submitted Order aggregate")
        if len(self.queried_orders) >= 4:
            raise AssertionError("public Case 2 allows at most four queries")

        # 두 실패 시나리오 모두 신규 submit 없이
        # 원 client ID만 네 번 조회한다.
        self.queried_orders.append(order)
        if (
            self.order_scenario
            is PublicCase2OrderScenario.UNKNOWN_REMAINS_UNRESOLVED
        ):
            return OrderResult(
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                status=OrderStatus.UNKNOWN,
                processed_at=self.clock(),
                failure_reason="SCRIPTED_PUBLIC_CASE2_UNKNOWN",
            )

        return OrderResult(
            symbol=order.symbol,
            client_order_id=order.client_order_id,
            status=OrderStatus.UNKNOWN,
            processed_at=self.clock(),
            failure_reason="SCRIPTED_PUBLIC_CASE2_ORDER_NOT_VISIBLE",
            failure_kind=OrderResultFailureKind.ORDER_NOT_VISIBLE,
        )

    def _build_result(
        self,
        order: Order,
        status: OrderStatus,
        *,
        fill_count: int,
    ) -> OrderResult:
        """
        함수 이름: _build_result()
        기능: 각 public BUY·SELL 주문의 독립 exchange ID와
            누적 fill을 normalized 결과로 만든다.
        인자: order -> 결과가 속한 주문 aggregate
            status -> 관찰된 canonical 주문 상태
            fill_count -> 응답에 포함할 앞쪽 누적 fill 개수
        반환값: 주문별 독립 exchange ID를 가진 OrderResult
        작성 날짜: 2026/08/29
        """
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        exchange_order_id = self.exchange_order_ids_by_client_id.get(
            order.client_order_id
        )
        if exchange_order_id is None:
            raise AssertionError("submitted order requires an exchange order ID")

        # Base fill builder를 재사용하되 주문별 ID로 fill key까지 분리한다.
        fills = self._build_fills(order, exchange_order_id)[:fill_count]
        return OrderResult(
            symbol=order.symbol,
            client_order_id=order.client_order_id,
            exchange_order_id=exchange_order_id,
            status=status,
            processed_at=self.clock(),
            fills=fills,
        )

    def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> object:
        """
        함수 이름: get_klines()
        기능: 요청 주기의 local REST Kline 행을 외부 I/O 없이 반환한다.
        인자: symbol -> 요청된 Binance Spot symbol
            interval -> 요청된 공식 interval 문자열
            limit -> Controller가 요구한 최대 행 수
        반환값: 요청 limit 안의 공식 Kline 행 list
        작성 날짜: 2026/08/29
        """
        if symbol != SYMBOL:
            raise AssertionError("public Case 2 supports only ETHUSDT")
        if interval not in self.market_responses:
            raise AssertionError("unexpected Kline interval")

        # Request와 응답은 메모리에만 보존한다.
        # Network client는 만들지 않는다.
        self.kline_requests.append((symbol, interval, limit))
        return self.market_responses[interval][-limit:]


@dataclass(slots=True)
class PublicCase2Fixture:
    """
    클래스 이름: PublicCase2Fixture
    기능: 공개 market path와 주문·Position·History owner를
        한 local fixture로 묶는다.
    작성 날짜: 2026/08/29
    """

    clock: MutableUtcClock
    rest_client: PublicCase2RESTClient
    market_controller: MarketDataController
    trading_controller: TradingController
    position: Position
    history_controller: TradeHistoryController
    repository: TradeHistoryRepository
    history_path: Path
    trade_publications: list[tuple[Trade, Performance]]


def _create_public_case2_fixture(
    temporary_directory: str,
    order_scenario: PublicCase2OrderScenario = (
        PublicCase2OrderScenario.IMMEDIATE_FILLED
    ),
) -> PublicCase2Fixture:
    """
    함수 이름: _create_public_case2_fixture()
    기능: Production builder·Controller와 local fake gateway로
        RUNNING TYPE_0 session을 만든다.
    인자: temporary_directory -> durable JSONL history를 둘 임시 경로
        order_scenario -> Case C submit과 same-order query가 재생할 상태 진행
    반환값: 공개 Kline을 받을 준비가 끝난 PublicCase2Fixture
    작성 날짜: 2026/08/29
    """
    # 시장 snapshot, 주문 결과와 Context가 모두
    # 같은 결정론적 시각을 읽게 한다.
    clock = MutableUtcClock(INITIAL_TIME)
    rest_client = PublicCase2RESTClient(clock, order_scenario)
    api_gateway = APIGateway(rest_client, clock=clock)
    account = Account()
    position = Position()
    market_snapshot = MarketSnapshot(symbol=SYMBOL, clock=clock)
    context = TradingContext(clock=clock)
    trade_publications: list[tuple[Trade, Performance]] = []

    # 실제 repository와 observer를 연결한다.
    # Durable history 뒤 publication까지 관찰한다.
    history_path = Path(temporary_directory) / "public-case2-trades.jsonl"
    repository = TradeHistoryRepository(history_path, clock=clock)
    history_controller = TradeHistoryController(
        repository,
        clock=clock,
        account=account,
        trade_update_observer=(
            lambda trade, performance: trade_publications.append(
                (trade, performance)
            )
        ),
    )
    history_controller.load_trade_history()

    # Account stream과 Kline stream은 각각 production Gateway를 사용한다.
    # 두 외부 경계의 실제 client는 모두 memory fake다.
    account_web_socket_client = SynchronousAccountWebSocketClient([])
    account_web_socket_gateway = WebSocketGateway(
        account_web_socket_client,
        account_snapshot_callback=account.apply_stream_snapshot,
    )
    trading_controller = TradingController(
        api_gateway,
        account_web_socket_gateway,
        account,
        market_snapshot,
        command_gate=True,
        context=context,
        position=position,
        trade_history_controller=history_controller,
        risk_policy_state=create_test_risk_policy(),
        clock=clock,
    )
    market_web_socket_client = FakeWebSocketClient([])
    market_controller = MarketDataController(
        api_gateway,
        WebSocketGateway(market_web_socket_client),
        market_snapshot,
        NoOpRegimeController(),
        kline_limit=21,
        market_evaluation_builder=ThirtyMinuteMarketEvaluationBuilder(
            monotonic_clock=lambda: 0,
        ),
        trading_market_observer=trading_controller,
    )
    market_controller.initialize_market_data()

    # Ready market 뒤 Account·TYPE_0·전량 SELL split을
    # 공개 operation으로 순서대로 설정한다.
    trading_controller.load_account()
    regime_controller = RegimeController(
        RegimeSTM(),
        market_snapshot,
        trading_controller,
    )
    selection = regime_controller.set_regime_type(
        RegimeType.TYPE_0,
        command_id="select-public-case2",
        expected_version=trading_controller.context.version,
    )
    split_result = trading_controller.update_split_ratios(
        command_id="split-public-case2",
        expected_version=selection.version,
        scale_in=Decimal("0.5"),
        scale_out=Decimal("1"),
    )
    trading_controller.start_trading(
        command_id="start-public-case2",
        expected_version=split_result.version,
    )

    return PublicCase2Fixture(
        clock=clock,
        rest_client=rest_client,
        market_controller=market_controller,
        trading_controller=trading_controller,
        position=position,
        history_controller=history_controller,
        repository=repository,
        history_path=history_path,
        trade_publications=trade_publications,
    )


def _create_live_thirty_minute_kline(
    *,
    close_price: Decimal,
    candle_low: Decimal,
    event_time: datetime,
) -> Kline:
    """
    함수 이름: _create_live_thirty_minute_kline()
    기능: 같은 진행 30분봉의 누적 OHLC와
        WebSocket event provenance를 만든다.
    인자: close_price -> 이번 realtime close
        candle_low -> 현재까지 누적된 30분봉 저가
        event_time -> source WebSocket event UTC 시각
    반환값: MarketDataController.observe_kline에 전달할 불변 Kline
    작성 날짜: 2026/08/29
    """
    return Kline(
        symbol=SYMBOL,
        interval=Interval.THIRTY_MINUTES,
        open_time=CURRENT_THIRTY_MINUTE_OPEN,
        open=Decimal("100"),
        high=max(Decimal("101"), close_price),
        low=candle_low,
        close=close_price,
        volume=Decimal("10"),
        closed=False,
        event_time=event_time,
    )  # Test 입력도 production Kline 검증과 계산을 그대로 통과한다.


def _create_closed_one_minute_kline(
    *,
    close_price: Decimal,
    event_time: datetime,
) -> Kline:
    """
    함수 이름: _create_closed_one_minute_kline()
    기능: TP trailing이 확정 EMA slope를 판정할 수 있는 1분 마감봉을 만든다.
    인자: close_price -> 확정 1분 종가
        event_time -> 마감 이후 source WebSocket event UTC 시각
    반환값: MarketDataController에 전달할 불변 Kline
    작성 날짜: 2026/08/29
    """
    one_minute_open = INITIAL_TIME.replace(second=0, microsecond=0)

    # 시장 진행 30분봉 가격은 변경하지 않고 확정 1분 slope source만 제공한다.
    return Kline(
        symbol=SYMBOL,
        interval=Interval.ONE_MINUTE,
        open_time=one_minute_open,
        open=Decimal("100"),
        high=Decimal("101"),
        low=min(Decimal("99"), close_price),
        close=close_price,
        volume=Decimal("10"),
        closed=True,
        event_time=event_time,
    )


def _trigger_public_case_c_buy(
    fixture: PublicCase2Fixture,
) -> tuple[TradingSTMResult, ...]:
    """
    함수 이름: _trigger_public_case_c_buy()
    기능: Public 30분 Kline 세 개로 Case C 최초 BUY 제출까지의
        queue를 처리한다.
    인자: fixture -> 실제 builder와 TradingController를 연결한 local fixture
    반환값: 회복 Kline에서 처리된 TradingSTMResult tuple
    작성 날짜: 2026/08/29
    """
    if not isinstance(fixture, PublicCase2Fixture):
        raise TypeError("fixture must be a PublicCase2Fixture")

    # 첫 tick은 하단 과이탈 setup을 연다.
    # Public queue가 그 사실을 Context에 반영하게 한다.
    fixture.clock.advance(timedelta(seconds=5))
    fixture.market_controller.observe_kline(
        _create_live_thirty_minute_kline(
            close_price=Decimal("90"),
            candle_low=Decimal("89"),
            event_time=fixture.clock(),
        )
    )
    asyncio.run(fixture.trading_controller.drain_events())

    # 둘째 tick은 깊은 flush 저가와 Case C 회복 기준을
    # 같은 진행봉에 보존한다.
    fixture.clock.advance(timedelta(seconds=5))
    fixture.market_controller.observe_kline(
        _create_live_thirty_minute_kline(
            close_price=Decimal("85"),
            candle_low=Decimal("84"),
            event_time=fixture.clock(),
        )
    )
    asyncio.run(fixture.trading_controller.drain_events())

    # 셋째 tick은 +0.06 회복을 충족한다.
    # Real STM과 Controller가 이 평가로 Case C BUY를 제출한다.
    fixture.clock.advance(timedelta(seconds=5))
    fixture.market_controller.observe_kline(
        _create_live_thirty_minute_kline(
            close_price=Decimal("88.6"),
            candle_low=Decimal("84"),
            event_time=fixture.clock(),
        )
    )
    return asyncio.run(fixture.trading_controller.drain_events())


class PublicMarketCase2FlowTests(unittest.TestCase):
    """
    클래스 이름: PublicMarketCase2FlowTests
    기능: Private Action seam 없이 public Kline에서
        Case 2 주문 결과까지의 provenance를 검증한다.
    작성 날짜: 2026/08/29
    """

    def test_public_boundary_retention_evicts_whole_old_evaluations(
        self,
    ) -> None:
        """
        함수 이름: test_public_boundary_retention_evicts_whole_old_evaluations()
        기능: bounded 1L trace가 장기 session을 중단하지 않고 가장 오래된 evaluation 전체만 제거한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_public_case2_fixture(temporary_directory)
            controller = fixture.trading_controller

            # 작은 capacity에서 두 complete evaluation을 채워 실제 4096 경계의 eviction을 빠르게 재현한다.
            with mock_patch(
                "binance_auto_trader.application.trading_controller."
                "_MAX_PUBLIC_MARKET_BOUNDARY_TRACE_ENTRIES",
                4,
            ):
                for market_version in (1, 2):
                    source_event_id = f"source-{market_version}"
                    controller.observe_public_market_boundary(
                        message_id="1L.1",
                        event_type="KLINE_OBSERVED",
                        source_event_id=source_event_id,
                        market_version=market_version,
                    )
                    controller.observe_public_market_boundary(
                        message_id="1L.2",
                        event_type="MARKET_EVALUATED",
                        source_event_id=source_event_id,
                        market_version=market_version,
                    )

                # 세 번째 evaluation은 첫 번째 1L.1/.2를 함께 제거하고 자신의 두 경계를 보존한다.
                controller.observe_public_market_boundary(
                    message_id="1L.1",
                    event_type="KLINE_OBSERVED",
                    source_event_id="source-3",
                    market_version=3,
                )
                controller.observe_public_market_boundary(
                    message_id="1L.2",
                    event_type="MARKET_EVALUATED",
                    source_event_id="source-3",
                    market_version=3,
                )
                retained_evaluation_ids = tuple(
                    entry.evaluation_id
                    for entry in controller.public_market_boundary_trace
                )
                self.assertNotIn("market:1:source-1", retained_evaluation_ids)
                self.assertEqual(
                    2,
                    retained_evaluation_ids.count("market:2:source-2"),
                )
                self.assertEqual(
                    2,
                    retained_evaluation_ids.count("market:3:source-3"),
                )

                # Queue backlog에서 이미 evicted된 가장 오래된 market event가 뒤늦게 Action을 내면 effect 전에 닫힌다.
                controller._active_trace_event_id = "market:1:source-1"
                with self.assertRaisesRegex(
                    RuntimeError,
                    "requires retained Kline and evaluation evidence",
                ):
                    controller._append_public_market_action_boundary(
                        SubmitOrder(
                            strategy=StrategyType.CASE_C,
                            side=OrderSide.BUY,
                            attempt_kind=OrderAttemptKind.INITIAL,
                            idempotency_key="evicted-case-c-buy",
                        )
                    )

                # 현재 evaluation의 Action을 추가할 때는 두 번째 evaluation 전체를 비우고 exact 3단계를 남긴다.
                controller._active_trace_event_id = "market:3:source-3"
                controller._append_public_market_action_boundary(
                    SubmitOrder(
                        strategy=StrategyType.CASE_C,
                        side=OrderSide.BUY,
                        attempt_kind=OrderAttemptKind.INITIAL,
                        idempotency_key="retention-case-c-buy",
                    )
                )
                controller._active_trace_event_id = None

            retained_entries = controller.public_market_boundary_trace
            self.assertEqual(
                ("market:3:source-3",) * 3,
                tuple(entry.evaluation_id for entry in retained_entries),
            )
            self.assertEqual(
                ("1L.1", "1L.2", "1L.3"),
                tuple(entry.message_id for entry in retained_entries),
            )

    def test_public_kline_opens_case_c_and_persists_immediate_buy(self) -> None:
        """
        함수 이름: test_public_kline_opens_case_c_and_persists_immediate_buy()
        기능: 과이탈·flush·회복 Kline이 Case C BUY와
            durable Trade를 한 번 만드는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_public_case2_fixture(temporary_directory)

            # 첫 public tick은 하단 과이탈과 CCI setup으로
            # lower scope와 Case C를 연다.
            fixture.clock.advance(timedelta(seconds=5))
            setup_kline = _create_live_thirty_minute_kline(
                close_price=Decimal("90"),
                candle_low=Decimal("89"),
                event_time=fixture.clock(),
            )
            fixture.market_controller.observe_kline(setup_kline)
            asyncio.run(fixture.trading_controller.drain_events())
            setup_context = fixture.trading_controller.context
            self.assertIsNotNone(setup_context.runtime.lower_event_id)
            self.assertEqual(Decimal("90"), setup_context.market.realtime_price)
            self.assertLessEqual(
                setup_context.market.realtime_pct_b,
                Decimal("-0.15"),
            )
            self.assertLessEqual(
                setup_context.market.cci_30m_realtime,
                Decimal("-140"),
            )

            # 둘째 tick은 %B -0.25 아래의 최초 flush와
            # 3분 회복 기준을 저장한다.
            fixture.clock.advance(timedelta(seconds=5))
            flush_kline = _create_live_thirty_minute_kline(
                close_price=Decimal("85"),
                candle_low=Decimal("84"),
                event_time=fixture.clock(),
            )
            fixture.market_controller.observe_kline(flush_kline)
            asyncio.run(fixture.trading_controller.drain_events())
            flush_context = fixture.trading_controller.context
            self.assertEqual(Decimal("85"), flush_context.runtime.flush_low)
            self.assertIsNotNone(flush_context.runtime.entry_pct_b)
            self.assertEqual([], fixture.rest_client.submitted_orders)

            # 셋째 tick은 +0.06 회복을 만족한다.
            # 공개 event queue에서 production SubmitOrder를 만든다.
            fixture.clock.advance(timedelta(seconds=5))
            recovery_kline = _create_live_thirty_minute_kline(
                close_price=Decimal("88.6"),
                candle_low=Decimal("84"),
                event_time=fixture.clock(),
            )
            fixture.market_controller.observe_kline(recovery_kline)
            processed_results = asyncio.run(
                fixture.trading_controller.drain_events()
            )
            self.assertGreaterEqual(len(processed_results), 1)

            # Fake exchange의 즉시 FILLED 한 건이
            # 같은 evaluation 가격으로 Position을 연다.
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))
            submitted_order = fixture.rest_client.submitted_orders[0]
            self.assertEqual(
                Decimal("88.6"),
                submitted_order.market_price_at_decision,
            )
            self.assertEqual(
                submitted_order.submitted_quantity,
                fixture.position.quantity,
            )
            self.assertEqual(
                submitted_order.filled_amount,
                fixture.position.cost_basis,
            )
            self.assertEqual(
                Decimal("88.6"),
                fixture.position.average_entry_price,
            )
            self.assertIs(fixture.position.owner, StrategyType.CASE_C)
            self.assertIs(
                fixture.trading_controller.status,
                TradingSessionStatus.RUNNING,
            )

            # Terminal Trade는 JSONL commit 뒤 observer에
            # 정확히 한 번 게시된다.
            trades = fixture.history_controller.trade_history.trades
            self.assertEqual(1, len(trades))
            self.assertEqual(trades, fixture.repository.get_trade_history())
            self.assertEqual(1, len(fixture.trade_publications))
            self.assertEqual(trades[0], fixture.trade_publications[0][0])
            self.assertTrue(fixture.history_path.is_file())
            self.assertEqual(
                1,
                len(fixture.history_path.read_text(encoding="utf-8").splitlines()),
            )

            # Immediate fill의 Case 2 trace는 private executor 호출 없이
            # 기존 18개 message를 보존한다.
            expected_trace = (
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
            actual_trace = tuple(
                trace_entry.message_id
                for trace_entry in (
                    fixture.trading_controller.order_execution_trace
                )
            )
            self.assertEqual(expected_trace, actual_trace)

            # BUY를 실제로 낸 evaluation만 production observer의 exact 1L.1→1L.3 chain을 가져야 한다.
            buy_evaluation_id = (
                fixture.trading_controller.order_execution_trace[0]
                .command_event_id
            )
            public_boundary_trace = tuple(
                trace_entry
                for trace_entry in (
                    fixture.trading_controller.public_market_boundary_trace
                )
                if trace_entry.evaluation_id == buy_evaluation_id
            )
            self.assertEqual(
                ("1L.1", "1L.2", "1L.3"),
                tuple(trace_entry.message_id for trace_entry in public_boundary_trace),
            )
            self.assertEqual(
                ("KLINE_OBSERVED", "MARKET_EVALUATED", "ACTION_EMITTED"),
                tuple(trace_entry.event_type for trace_entry in public_boundary_trace),
            )
            self.assertEqual(
                sorted(
                    trace_entry.context_version
                    for trace_entry in public_boundary_trace
                ),
                [
                    trace_entry.context_version
                    for trace_entry in public_boundary_trace
                ],
            )
            self.assertEqual(
                fixture.trading_controller.last_risk_decision.budget.context_version,
                public_boundary_trace[-1].context_version,
            )

    def test_public_case_c_tp_fallback_persists_sell_and_performance(
        self,
    ) -> None:
        """
        함수 이름: test_public_case_c_tp_fallback_persists_sell_and_performance()
        기능: Public market tick이 Case C TP trailing·fallback SELL을 실행하고
            원가·실현손익·Position·durable 성과를 완결하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_public_case2_fixture(temporary_directory)

            # 기존 public Kline 세 개로 Case C BUY를 체결하고
            # SELL 전 전량 원가와 수량을 authoritative Position에서 고정한다.
            buy_results = _trigger_public_case_c_buy(fixture)
            self.assertGreaterEqual(len(buy_results), 1)
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))
            buy_order = fixture.rest_client.submitted_orders[0]
            opened_quantity = fixture.position.quantity
            opened_cost_basis = fixture.position.cost_basis
            self.assertGreater(opened_quantity, Decimal("0"))
            self.assertGreater(opened_cost_basis, Decimal("0"))

            # 101 public tick은 %B 0.10 이상으로 올라
            # PC-10 TP trailing을 만들지만 아직 drain하지 않는다.
            fixture.clock.advance(timedelta(seconds=5))
            fixture.market_controller.observe_kline(
                _create_live_thirty_minute_kline(
                    close_price=Decimal("101"),
                    candle_low=Decimal("84"),
                    event_time=fixture.clock(),
                )
            )

            # 다음 100 public tick도 처리 전에 enqueue해
            # immutable evaluation 순서를 검증한다.
            # 한 번의 drain에서 PC-16 TP_FALLBACK 전량 SELL까지 실행한다.
            fixture.clock.advance(timedelta(seconds=5))
            fixture.market_controller.observe_kline(
                _create_live_thirty_minute_kline(
                    close_price=Decimal("100"),
                    candle_low=Decimal("84"),
                    event_time=fixture.clock(),
                )
            )
            fallback_results = asyncio.run(
                fixture.trading_controller.drain_events()
            )
            ordered_transition_ids = tuple(
                transition_id
                for result in fallback_results
                for transition_id in result.transition_ids
            )
            ordered_transition_positions = {
                transition_id: ordered_transition_ids.index(transition_id)
                for transition_id in (
                    "PC-10",
                    "PC-11",
                    "PC-14",
                    "PC-16",
                    "PC-23F",
                    "PC-24",
                    "PC-25",
                )
            }
            self.assertEqual(
                tuple(sorted(ordered_transition_positions.values())),
                tuple(ordered_transition_positions.values()),
            )
            self.assertTrue(
                {"PC-26", "PC-27", "PC-28"}.isdisjoint(
                    ordered_transition_ids
                )
            )
            self.assertLess(
                fixture.trading_controller.context.market.realtime_pct_b,
                Decimal("0.10"),
            )
            self.assertIsNotNone(
                fixture.trading_controller.context.runtime.tp_price
            )

            # BUY·SELL은 서로 다른 exchange ID를 사용하고
            # SELL은 정확히 열린 Position 전량을 TP_FALLBACK으로 닫는다.
            self.assertEqual(2, len(fixture.rest_client.submitted_orders))
            sell_order = fixture.rest_client.submitted_orders[1]
            self.assertIs(buy_order.side, OrderSide.BUY)
            self.assertIs(sell_order.side, OrderSide.SELL)
            self.assertIs(sell_order.strategy, StrategyType.CASE_C)
            self.assertIs(sell_order.exit_reason, ExitReason.TP_FALLBACK)
            self.assertEqual(Decimal("100"), sell_order.market_price_at_decision)
            self.assertEqual(opened_quantity, sell_order.filled_quantity)
            self.assertNotEqual(
                buy_order.exchange_order_id,
                sell_order.exchange_order_id,
            )
            self.assertEqual(Decimal("0"), fixture.position.quantity)
            self.assertEqual(Decimal("0"), fixture.position.cost_basis)
            self.assertIsNone(fixture.position.owner)

            # PC-23F는 체결 사유와 %B provenance를 확인한 뒤 pending을 해제하며,
            # 낮은 %B의 PC-25 대기 상태를 CASE_C_CLOSED·IDLE로 유지한다.
            exit_context = fixture.trading_controller.context
            active_stm = fixture.trading_controller._active_stm
            self.assertIsNotNone(active_stm)
            self.assertIs(
                active_stm.current_state.case_c_position_state,
                CaseCPositionState.CASE_C_CLOSED,
            )
            self.assertIs(
                exit_context.runtime.case_c_exit_reason,
                ExitReason.TP_FALLBACK,
            )
            self.assertEqual(
                exit_context.market.realtime_pct_b,
                exit_context.runtime.case_c_exit_pct_b,
            )
            self.assertIsNone(exit_context.runtime.pending_exit_reason)
            self.assertIsNone(exit_context.runtime.pending_return_state)
            self.assertIs(
                exit_context.runtime.trading_phase,
                TradingPhase.IDLE,
            )

            # Message 11이 SELL 전 원가를 고정하고
            # message 13.1이 fee 포함 실현손익을 생성한 후에만 JSONL을 공개한다.
            trades = fixture.history_controller.trade_history.trades
            self.assertEqual(2, len(trades))
            buy_trade, sell_trade = trades
            self.assertIs(buy_trade.side, OrderSide.BUY)
            self.assertIs(sell_trade.side, OrderSide.SELL)
            self.assertEqual(opened_cost_basis, sell_trade.allocated_cost_basis)
            with localcontext() as decimal_context:
                decimal_context.prec = 34
                decimal_context.rounding = ROUND_HALF_EVEN
                expected_realized_pnl = (
                    sell_trade.executed_amount
                    - sell_trade.fee_quote_amount
                    - opened_cost_basis
                )
            self.assertEqual(expected_realized_pnl, sell_trade.realized_pnl)
            self.assertIs(sell_trade.exit_reason, ExitReason.TP_FALLBACK)
            self.assertEqual(trades, fixture.repository.get_trade_history())
            self.assertEqual(2, len(fixture.trade_publications))
            self.assertEqual(sell_trade, fixture.trade_publications[-1][0])
            self.assertEqual(
                2,
                len(
                    fixture.history_path.read_text(
                        encoding="utf-8"
                    ).splitlines()
                ),
            )

            # Durable SELL 하나의 realized result가 전체·당일 Performance와
            # publication snapshot에 동일하게 반영된다.
            performance = fixture.history_controller.performance
            self.assertEqual(sell_trade.realized_pnl, performance.realized_pnl)
            self.assertEqual(sell_trade.realized_pnl, performance.total_profit)
            self.assertEqual(1, performance.completed_sell_count)
            self.assertEqual(1, performance.winning_sell_count)
            self.assertEqual(0, performance.losing_sell_count)
            self.assertEqual(
                sell_trade.realized_return_rate,
                performance.cumulative_return_rate,
            )
            self.assertEqual(
                sell_trade.realized_return_rate,
                performance.daily_return_rate,
            )
            self.assertEqual(performance, fixture.trade_publications[-1][1])

            # SELL 주문의 Case 2 trace는 원가 11과 realized 13.1을
            # 각각 한 번만 포함하고 terminal STM feedback 14로 끝난다.
            sell_trace_ids = tuple(
                trace_entry.message_id
                for trace_entry in fixture.trading_controller.order_execution_trace
                if trace_entry.client_order_id == sell_order.client_order_id
            )
            self.assertEqual(
                (
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
                ),
                sell_trace_ids,
            )

    def test_case_c_terminal_sell_keeps_intent_pct_b_for_active_handoff(
        self,
    ) -> None:
        """
        함수 이름: test_case_c_terminal_sell_keeps_intent_pct_b_for_active_handoff()
        기능: Partial SELL 뒤 시장 %B가 바뀌어도 최초 매도 %B와 Case B active 인계를 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_public_case2_fixture(
                temporary_directory,
                PublicCase2OrderScenario.SELL_PARTIAL_THEN_FILLED,
            )
            _trigger_public_case_c_buy(fixture)

            # 낮은 %B의 TP trailing에 진입한 뒤 확정 1분 slope 하락으로
            # active 인계 대상인 TP_TRAIL SELL 의도를 만든다.
            fixture.clock.advance(timedelta(seconds=5))
            fixture.market_controller.observe_kline(
                _create_live_thirty_minute_kline(
                    close_price=Decimal("101"),
                    candle_low=Decimal("84"),
                    event_time=fixture.clock(),
                )
            )
            asyncio.run(fixture.trading_controller.drain_events())
            fixture.clock.advance(timedelta(seconds=35))
            fixture.market_controller.observe_kline(
                _create_closed_one_minute_kline(
                    close_price=Decimal("90"),
                    event_time=fixture.clock(),
                )
            )
            asyncio.run(fixture.trading_controller.drain_events())
            sell_order = fixture.rest_client.submitted_orders[1]
            original_exit_pct_b = sell_order.exit_pct_b_at_intent
            self.assertIs(sell_order.status, OrderStatus.PARTIALLY_FILLED)
            self.assertIs(sell_order.exit_reason, ExitReason.TP_TRAIL)
            self.assertIsNotNone(original_exit_pct_b)
            self.assertLess(original_exit_pct_b, Decimal("0.40"))
            self.assertEqual(
                original_exit_pct_b,
                fixture.trading_controller.context.runtime.pending_exit_pct_b,
            )

            # Terminal query 전에 현재 %B를 active 인계 기준 위로 변경한다.
            fixture.clock.advance(timedelta(seconds=5))
            fixture.market_controller.observe_kline(
                _create_live_thirty_minute_kline(
                    close_price=Decimal("115"),
                    candle_low=Decimal("84"),
                    event_time=fixture.clock(),
                )
            )
            asyncio.run(fixture.trading_controller.drain_events())
            self.assertGreaterEqual(
                fixture.trading_controller.context.market.realtime_pct_b,
                Decimal("0.40"),
            )

            # Same-ID terminal fill은 변경된 시장값이 아닌 최초 intent %B를 게시한다.
            terminal_events = (
                fixture.trading_controller.trigger_order_reconciliation(
                    occurred_at=fixture.clock.advance(timedelta(seconds=1)),
                )
            )
            self.assertEqual(1, len(terminal_events))
            self.assertIs(
                terminal_events[0].event_type,
                TradingEventType.CASE_C_SELL_FILLED,
            )
            terminal_results = asyncio.run(
                fixture.trading_controller.drain_events()
            )
            terminal_transition_ids = tuple(
                transition_id
                for result in terminal_results
                for transition_id in result.transition_ids
            )
            exit_context = fixture.trading_controller.context
            self.assertEqual(
                original_exit_pct_b,
                exit_context.runtime.case_c_exit_pct_b,
            )
            self.assertIn("PC-27", terminal_transition_ids)
            self.assertNotIn("PC-28", terminal_transition_ids)
            self.assertFalse(exit_context.runtime.case_b_entry_paused)
            self.assertTrue(
                exit_context.runtime.case_b_only_until_next_lower_touch
            )

    def test_case_c_sell_provenance_mismatch_fails_before_journal_and_rest(
        self,
    ) -> None:
        """
        함수 이름: test_case_c_sell_provenance_mismatch_fails_before_journal_and_rest()
        기능: 일반 Case C SELL의 사유·%B drift를 durable journal과 REST 전에 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_public_case2_fixture(temporary_directory)
            _trigger_public_case_c_buy(fixture)
            exit_event = TradingEvent(
                TradingEventType.CASE_C_EMA_DECREASEMENT,
                fixture.clock(),
                sequence_number=91_001,
            )
            exit_patch, submit_action = create_exit_order_actions(
                StrategyType.CASE_C,
                ExitReason.TP_TRAIL,
                PositionReturnState.CASE_C_TP_TRAILING,
                exit_event,
                fixture.trading_controller.context,
                attempt_kind=OrderAttemptKind.INITIAL,
            )
            fixture.trading_controller._execute_action(exit_patch)
            self.assertIsInstance(submit_action, SubmitOrder)
            self.assertIsNotNone(submit_action.exit_pct_b_at_intent)
            invalid_actions = (
                (
                    "missing-pct-b",
                    replace(submit_action, exit_pct_b_at_intent=None),
                ),
                (
                    "drifted-pct-b",
                    replace(
                        submit_action,
                        exit_pct_b_at_intent=(
                            submit_action.exit_pct_b_at_intent
                            + Decimal("0.001")
                        ),
                    ),
                ),
                (
                    "drifted-reason",
                    replace(submit_action, exit_reason=ExitReason.STOP),
                ),
            )
            submitted_count_before = len(fixture.rest_client.submitted_orders)
            trace_count_before = len(
                fixture.trading_controller.order_execution_trace
            )
            position_quantity_before = fixture.position.quantity

            # Recovery journal flag를 열어도 provenance 검증이 save·REST·Position effect보다 먼저 실패해야 한다.
            fixture.trading_controller._pending_order_recovery_enabled = True
            with mock_patch.object(
                TradeHistoryController,
                "save_pending_order",
            ) as journal_save:
                for case_name, invalid_action in invalid_actions:
                    with self.subTest(case_name=case_name):
                        with self.assertRaisesRegex(
                            ValueError,
                            "exact exit intent provenance",
                        ):
                            fixture.trading_controller._submit_order_action(
                                invalid_action
                            )
            journal_save.assert_not_called()
            self.assertEqual(
                submitted_count_before,
                len(fixture.rest_client.submitted_orders),
            )
            self.assertEqual(
                trace_count_before,
                len(fixture.trading_controller.order_execution_trace),
            )
            self.assertEqual(position_quantity_before, fixture.position.quantity)

    def test_public_stop_force_sells_case_c_and_terminates_durably(self) -> None:
        """
        함수 이름: test_public_stop_force_sells_case_c_and_terminates_durably()
        기능: Public STOP command가 열린 Case C Position을 전량 SELL하고
            두 Trade와 realized Performance를 남긴 뒤 session을 종료하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_public_case2_fixture(temporary_directory)
            _trigger_public_case_c_buy(fixture)
            opened_quantity = fixture.position.quantity
            opened_cost_basis = fixture.position.cost_basis
            self.assertGreater(opened_quantity, Decimal("0"))

            # Public command version으로 STOP을 요청해
            # canonical G-06 ForceSellAll을 실행한다.
            stop_result = fixture.trading_controller.stop_trading(
                command_id="stop-public-case2-position",
                expected_version=fixture.trading_controller.context.version,
            )
            self.assertIn(
                stop_result.status,
                (
                    TradingSessionStatus.STOPPING,
                    TradingSessionStatus.TERMINATED,
                ),
            )
            self.assertEqual(("G-06",), stop_result.transition_ids)
            stop_completion_results = asyncio.run(
                fixture.trading_controller.drain_events()
            )
            self.assertTrue(
                any(
                    "G-06F" in result.transition_ids
                    for result in stop_completion_results
                )
            )

            # STOP SELL은 원 Case C owner와 전량을 보존하고
            # Position 0 확정 후 session을 TERMINATED로 정리한다.
            self.assertEqual(2, len(fixture.rest_client.submitted_orders))
            buy_order, sell_order = fixture.rest_client.submitted_orders
            self.assertIs(buy_order.side, OrderSide.BUY)
            self.assertIs(sell_order.side, OrderSide.SELL)
            self.assertIs(sell_order.strategy, StrategyType.CASE_C)
            self.assertIs(sell_order.exit_reason, ExitReason.STOP)
            self.assertEqual(opened_quantity, sell_order.filled_quantity)
            self.assertNotEqual(
                buy_order.exchange_order_id,
                sell_order.exchange_order_id,
            )
            self.assertEqual(Decimal("0"), fixture.position.quantity)
            self.assertEqual(Decimal("0"), fixture.position.cost_basis)
            self.assertIsNone(fixture.position.owner)
            self.assertIs(
                fixture.trading_controller.status,
                TradingSessionStatus.TERMINATED,
            )

            # STOP SELL도 message 11·13.1 회계 경계를 거쳐
            # BUY·SELL 두 행과 Performance를 durable JSONL에 남긴다.
            trades = fixture.history_controller.trade_history.trades
            self.assertEqual(2, len(trades))
            sell_trade = trades[1]
            self.assertIs(sell_trade.side, OrderSide.SELL)
            self.assertIs(sell_trade.exit_reason, ExitReason.STOP)
            self.assertEqual(opened_cost_basis, sell_trade.allocated_cost_basis)
            self.assertEqual(trades, fixture.repository.get_trade_history())
            self.assertEqual(2, len(fixture.trade_publications))
            performance = fixture.history_controller.performance
            self.assertEqual(sell_trade.realized_pnl, performance.realized_pnl)
            self.assertEqual(1, performance.completed_sell_count)
            self.assertEqual(
                2,
                len(
                    fixture.history_path.read_text(
                        encoding="utf-8"
                    ).splitlines()
                ),
            )
            sell_trace = tuple(
                trace_entry
                for trace_entry in (
                    fixture.trading_controller.order_execution_trace
                )
                if trace_entry.client_order_id == sell_order.client_order_id
            )
            sell_trace_ids = tuple(
                trace_entry.message_id for trace_entry in sell_trace
            )
            self.assertEqual(
                ("1", "2", "3", "4", "5", "6", "6.1", "7"),
                sell_trace_ids[:8],
            )
            # STOP의 예약 message도 일반 BUY처럼 mutation 전 동일 version과 정확한 +1을 증명한다.
            self.assertEqual(
                sell_trace[0].context_version_before,
                sell_trace[0].context_version_after,
            )
            self.assertEqual(
                sell_trace[0].context_version_before,
                sell_trace[1].context_version_before,
            )
            self.assertEqual(
                sell_trace[1].context_version_before + 1,
                sell_trace[1].context_version_after,
            )
            expected_stop_event_id = (
                f"stop-{fixture.trading_controller.session_id}-"
                "stop-public-case2-position"
            )
            self.assertEqual(
                expected_stop_event_id,
                sell_trace[0].command_event_id,
            )
            self.assertEqual(
                (
                    f"order-outcome-{sell_order.client_order_id}-"
                    "FORCE_SELL_FINISHED"
                ),
                sell_trace[-1].command_event_id,
            )  # 최초 STOP과 terminal outcome은 서로 다른 production event identity를 보존한다.
            self.assertEqual(1, sell_trace_ids.count("11"))
            self.assertEqual(1, sell_trace_ids.count("13.1"))
            self.assertEqual("14", sell_trace_ids[-1])

    def test_public_case_c_partials_reconcile_to_one_filled_trade(self) -> None:
        """
        함수 이름: test_public_case_c_partials_reconcile_to_one_filled_trade()
        기능: Public Case C BUY의 누적 partial을 delta 적용해
            FILLED Trade 하나로 끝내는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_public_case2_fixture(
                temporary_directory,
                PublicCase2OrderScenario.PARTIALS_THEN_FILLED,
            )

            # Public Kline 세 개가 private executor 없이
            # 최초 partial 주문을 만든다.
            processed_results = _trigger_public_case_c_buy(fixture)
            self.assertGreaterEqual(len(processed_results), 1)
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))
            order = fixture.rest_client.submitted_orders[0]
            self.assertIs(order.strategy, StrategyType.CASE_C)
            self.assertEqual(
                Decimal("88.6"),
                order.market_price_at_decision,
            )
            self.assertIs(order.status, OrderStatus.PARTIALLY_FILLED)
            self.assertEqual(1, len(order.fills))
            initial_partial_quantity = fixture.position.quantity
            self.assertGreater(initial_partial_quantity, Decimal("0"))
            self.assertLess(
                initial_partial_quantity,
                order.submitted_quantity,
            )
            self.assertEqual((), fixture.history_controller.trade_history.trades)

            # 1초 query는 누적 응답의 새 두 번째 fill만
            # Position에 추가한다.
            first_due_at = fixture.clock.advance(timedelta(seconds=1))
            first_outcomes = (
                fixture.trading_controller.trigger_order_reconciliation(
                    occurred_at=first_due_at,
                )
            )
            self.assertEqual((), first_outcomes)
            self.assertEqual(2, len(order.fills))
            self.assertGreater(
                fixture.position.quantity,
                initial_partial_quantity,
            )
            self.assertLess(
                fixture.position.quantity,
                order.submitted_quantity,
            )
            self.assertEqual((), fixture.history_controller.trade_history.trades)

            # 다음 2초 query는 final fill만 적용한다.
            # Case C 성공 outcome도 같은 경계에서 enqueue한다.
            second_due_at = fixture.clock.advance(timedelta(seconds=2))
            terminal_outcomes = (
                fixture.trading_controller.trigger_order_reconciliation(
                    occurred_at=second_due_at,
                )
            )
            self.assertEqual(1, len(terminal_outcomes))
            self.assertIs(
                terminal_outcomes[0].event_type,
                TradingEventType.CASE_C_POSITION_OPENED,
            )
            asyncio.run(fixture.trading_controller.drain_events())

            # 같은 Order의 세 fill과 단일 terminal summary만
            # Position과 history에 남는다.
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))
            self.assertEqual(2, len(fixture.rest_client.queried_orders))
            self.assertTrue(
                all(
                    queried_order is order
                    for queried_order in fixture.rest_client.queried_orders
                )
            )
            self.assertIs(order.status, OrderStatus.FILLED)
            self.assertEqual(3, len(order.fills))
            self.assertEqual(order.submitted_quantity, order.filled_quantity)
            self.assertEqual(order.submitted_quantity, fixture.position.quantity)
            self.assertIs(fixture.position.owner, StrategyType.CASE_C)
            trades = fixture.history_controller.trade_history.trades
            self.assertEqual(1, len(trades))
            self.assertEqual(order.submitted_quantity, trades[0].executed_quantity)
            self.assertEqual(trades, fixture.repository.get_trade_history())
            self.assertEqual(1, len(fixture.trade_publications))
            self.assertIs(
                fixture.trading_controller.status,
                TradingSessionStatus.RUNNING,
            )

            # Reconciliation은 submit message를 반복하지 않는다.
            # Query 경계만 두 번 남긴다.
            trace_ids = tuple(
                trace_entry.message_id
                for trace_entry in (
                    fixture.trading_controller.order_execution_trace
                )
            )
            self.assertEqual(1, trace_ids.count("6"))
            self.assertEqual(2, trace_ids.count("8"))
            self.assertEqual(2, trace_ids.count("8.1"))
            self.assertEqual(2, trace_ids.count("8.2"))
            self.assertEqual(2, trace_ids.count("9"))

    def test_public_case_c_unknown_exhaustion_fails_closed(self) -> None:
        """
        함수 이름: test_public_case_c_unknown_exhaustion_fails_closed()
        기능: Public Case C UNKNOWN이 same-order 조회 한도 뒤
            reconciliation으로 잠기는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_public_case2_fixture(
                temporary_directory,
                PublicCase2OrderScenario.UNKNOWN_REMAINS_UNRESOLVED,
            )

            # Public BUY submit의 UNKNOWN은 fill이나 새 client ID 없이
            # 조회 하나만 예약한다.
            _trigger_public_case_c_buy(fixture)
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))
            submitted_order = fixture.rest_client.submitted_orders[0]
            self.assertIs(submitted_order.strategy, StrategyType.CASE_C)
            self.assertEqual(
                Decimal("88.6"),
                submitted_order.market_price_at_decision,
            )
            self.assertIs(submitted_order.status, OrderStatus.UNKNOWN)
            self.assertEqual(
                1,
                fixture.trading_controller.pending_order_query_count,
            )
            self.assertEqual(Decimal("0"), fixture.position.quantity)
            self.assertEqual((), fixture.history_controller.trade_history.trades)

            # 1·2·4·8초 backoff마다 같은 Order를 조회한다.
            # 모든 UNKNOWN은 최종적으로 fail closed한다.
            for query_delay in (
                timedelta(seconds=1),
                timedelta(seconds=2),
                timedelta(seconds=4),
                timedelta(seconds=8),
            ):
                due_at = fixture.clock.advance(query_delay)
                self.assertEqual(
                    (),
                    fixture.trading_controller.trigger_order_reconciliation(
                        occurred_at=due_at,
                    ),
                )

            # 조회 예산 소진은 미체결로 추측하지 않는다.
            # Pending identity와 운영 lock을 그대로 남긴다.
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))
            self.assertEqual(4, len(fixture.rest_client.queried_orders))
            self.assertTrue(
                all(
                    queried_order is submitted_order
                    for queried_order in fixture.rest_client.queried_orders
                )
            )
            self.assertEqual(
                0,
                fixture.trading_controller.pending_order_query_count,
            )
            self.assertIs(
                fixture.trading_controller.status,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            )
            self.assertIs(
                fixture.trading_controller.context.runtime.trading_phase,
                TradingPhase.RECONCILIATION_REQUIRED,
            )
            self.assertIsNotNone(
                fixture.trading_controller.context.runtime.pending_order_id
            )
            self.assertEqual(Decimal("0"), fixture.position.quantity)
            self.assertEqual((), fixture.repository.get_trade_history())
            self.assertEqual([], fixture.trade_publications)

            # 충분히 뒤의 public trigger도
            # 다섯 번째 query나 중복 submit을 만들지 않는다.
            late_time = fixture.clock.advance(timedelta(seconds=60))
            self.assertEqual(
                (),
                fixture.trading_controller.trigger_order_reconciliation(
                    occurred_at=late_time,
                ),
            )
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))
            self.assertEqual(4, len(fixture.rest_client.queried_orders))

    def test_public_case_c_submission_rejection_requires_four_absences(
        self,
    ) -> None:
        """
        함수 이름: test_public_case_c_submission_rejection_requires_four_absences()
        기능: Public Case C의 typed 거부를 네 번의 주문 부재 뒤
            zero-fill 실패로 확정하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_public_case2_fixture(
                temporary_directory,
                PublicCase2OrderScenario.SUBMISSION_REJECTION_CONFIRMED,
            )

            # Typed submit rejection도 즉시 실패로 단정하지 않는다.
            # 같은 주문의 조회를 먼저 예약한다.
            _trigger_public_case_c_buy(fixture)
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))
            submitted_order = fixture.rest_client.submitted_orders[0]
            self.assertIs(submitted_order.strategy, StrategyType.CASE_C)
            self.assertEqual(
                Decimal("88.6"),
                submitted_order.market_price_at_decision,
            )
            self.assertIs(submitted_order.status, OrderStatus.REJECTED)
            self.assertEqual(
                1,
                fixture.trading_controller.pending_order_query_count,
            )
            self.assertEqual(Decimal("0"), fixture.position.quantity)
            self.assertEqual((), fixture.history_controller.trade_history.trades)

            # 첫 세 부재 관찰은 아직
            # terminal zero-fill outcome을 만들지 않는다.
            for query_delay in (
                timedelta(seconds=1),
                timedelta(seconds=2),
                timedelta(seconds=4),
            ):
                due_at = fixture.clock.advance(query_delay)
                self.assertEqual(
                    (),
                    fixture.trading_controller.trigger_order_reconciliation(
                        occurred_at=due_at,
                    ),
                )

            # 네 번째 연속 부재만 Case C BUY_FAILED를 생성한다.
            # 생성된 결과는 public queue로 처리한다.
            final_due_at = fixture.clock.advance(timedelta(seconds=8))
            confirmed_outcomes = (
                fixture.trading_controller.trigger_order_reconciliation(
                    occurred_at=final_due_at,
                )
            )
            self.assertEqual(1, len(confirmed_outcomes))
            self.assertIs(
                confirmed_outcomes[0].event_type,
                TradingEventType.CASE_C_BUY_FAILED,
            )
            asyncio.run(fixture.trading_controller.drain_events())

            # 확정 거부는 Position·Trade를 만들지 않는다.
            # 원 주문 identity만 네 번 조회한다.
            self.assertEqual(1, len(fixture.rest_client.submitted_orders))
            self.assertEqual(4, len(fixture.rest_client.queried_orders))
            self.assertTrue(
                all(
                    queried_order is submitted_order
                    for queried_order in fixture.rest_client.queried_orders
                )
            )
            self.assertEqual(
                0,
                fixture.trading_controller.pending_order_query_count,
            )
            self.assertIsNone(
                fixture.trading_controller.context.runtime.pending_order_id
            )
            self.assertEqual(Decimal("0"), fixture.position.quantity)
            self.assertEqual((), fixture.repository.get_trade_history())
            self.assertEqual([], fixture.trade_publications)
            self.assertIs(
                fixture.trading_controller.status,
                TradingSessionStatus.RUNNING,
            )


if __name__ == "__main__":
    unittest.main()
