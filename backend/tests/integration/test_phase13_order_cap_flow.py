"""Phase 13 주문 상한이 공개 시장 경로의 journal·REST 전에 닫히는지 검증한다."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, localcontext
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
    OrderExecutionFailureCode,
    OrderExecutionTraceResult,
    ReconciliationCauseCategory,
    ReconciliationCauseStatus,
    TradingController,
    TradingSessionStatus,
)
from binance_auto_trader.bootstrap.testnet import (
    _TestnetOrderPermissionRESTClient,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.market import MarketSnapshot
from binance_auto_trader.domain.regime import RegimeSTM
from binance_auto_trader.domain.trading import (
    Account,
    Order,
    OrderResult,
    Position,
    TradingContext,
)

from tests.integration.phase13_risk_fixture import create_test_risk_policy
from tests.integration.test_account_stream_flow import (
    SynchronousAccountWebSocketClient,
)
from tests.integration.test_buy_sell_flow import MutableUtcClock
from tests.integration.test_market_initialization_flow import (
    FakeWebSocketClient,
    NoOpRegimeController,
)
from tests.integration.test_public_market_case2_flow import (
    INITIAL_TIME,
    PublicCase2Fixture,
    PublicCase2OrderScenario,
    PublicCase2RESTClient,
    _trigger_public_case_c_buy,
)


ORDER_NOTIONAL_CAP = Decimal("100")
FILTER_RETURN_NOTIONAL = Decimal("100.01")


def _zero_monotonic_time() -> int:
    """
    함수 이름: _zero_monotonic_time()
    기능: 시장 평가 builder가 실제 시간을 읽지 않도록 고정 monotonic 값을 반환한다.
    인자: 없음
    반환값: 고정 monotonic 정수 0
    작성 날짜: 2026/08/31
    """
    return 0  # Public Kline 순서는 source UTC 시각만으로 결정한다.


def _zero_commission_payload(symbol: str) -> dict[str, object]:
    """
    함수 이름: _zero_commission_payload()
    기능: 주문 상한만 독립 검증하도록 지원 자산의 0 수수료 응답을 만든다.
    인자: symbol -> 응답에 결속할 canonical Spot symbol
    반환값: 공식 account commission 형태의 dictionary
    작성 날짜: 2026/08/31
    """
    # 할인 자산과 모든 수수료율을 0으로 두어 cap 이외의 preflight가 주문을 막지 않게 한다.
    return {
        "symbol": symbol,
        "standardCommission": {
            "maker": "0.00000000",
            "taker": "0.00000000",
            "buyer": "0.00000000",
            "seller": "0.00000000",
        },
        "specialCommission": {
            "maker": "0.00000000",
            "taker": "0.00000000",
            "buyer": "0.00000000",
            "seller": "0.00000000",
        },
        "taxCommission": {
            "maker": "0.00000000",
            "taker": "0.00000000",
            "buyer": "0.00000000",
            "seller": "0.00000000",
        },
        "discount": {
            "enabledForAccount": True,
            "enabledForSymbol": True,
            "discountAsset": None,
            "discount": "0.00000000",
        },
    }


class FilterRaisingPublicCase2RESTClient(PublicCase2RESTClient):
    """
    클래스 이름: FilterRaisingPublicCase2RESTClient
    기능: 공개 Kline fixture를 재사용하면서 filter 반환 수량만 cap 위로 높이는 local REST fake다.
    작성 날짜: 2026/08/31
    """

    def __init__(self, clock: MutableUtcClock) -> None:
        """
        함수 이름: __init__()
        기능: 즉시 체결 시나리오와 cap 경계의 관측 상태를 초기화한다.
        인자: clock -> 시장과 주문이 공유할 결정론적 UTC clock
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        super().__init__(
            clock,
            PublicCase2OrderScenario.IMMEDIATE_FILLED,
        )

        # Filter 전후 notional과 mutation 호출을 별도로 남겨 실패 cut-point를 증명한다.
        self.prepare_input_notional: Decimal | None = None
        self.prepare_output_notional: Decimal | None = None
        self.mutation_trace: list[str] = []

    def get_account_commission(self, *, symbol: str) -> object:
        """
        함수 이름: get_account_commission()
        기능: Testnet permission proxy의 주문 전 수수료 정책 조회에 0 수수료를 반환한다.
        인자: symbol -> 조회할 canonical Spot symbol
        반환값: 지원 자산만 허용하는 공식 commission payload
        작성 날짜: 2026/08/31
        """
        return _zero_commission_payload(symbol)  # 수수료 gate를 통과한 뒤 cap gate를 관찰한다.

    def list_open_order_results(
        self,
        *,
        symbol: str,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_open_order_results()
        기능: 빈 local 거래소의 open 주문 조회 결과를 반환한다.
        인자: symbol -> 조회할 canonical Spot symbol
        반환값: 빈 OrderResult tuple
        작성 날짜: 2026/08/31
        """
        if symbol != "ETHUSDT":
            raise AssertionError("unexpected startup symbol")

        return ()  # Startup reconciliation에는 외부 주문이 없다.

    def list_recent_order_results(
        self,
        *,
        symbol: str,
        limit: int = 100,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_recent_order_results()
        기능: 빈 local 거래소의 최근 주문 조회 결과를 반환한다.
        인자: symbol -> 조회할 canonical Spot symbol
            limit -> 조회할 최대 주문 개수
        반환값: 빈 OrderResult tuple
        작성 날짜: 2026/08/31
        """
        if symbol != "ETHUSDT":
            raise AssertionError("unexpected startup symbol")
        if limit != 100:
            raise AssertionError("unexpected startup order limit")

        return ()  # Durable history와 대조할 거래소 주문은 없다.

    def prepare_order(self, *, order: Order) -> Order:
        """
        함수 이름: prepare_order()
        기능: cap 이하 입력 수량을 filter 반환 단계에서 100.01 USDT 수량으로 높인다.
        인자: order -> 공개 Case C signal이 만든 filter 전 Order
        반환값: submitted_quantity만 cap 위로 변경한 동일 Order
        작성 날짜: 2026/08/31
        """
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")

        # 입력과 filter 반환 금액은 float 변환 없이 같은 immutable decision price로 계산한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            self.prepare_input_notional = (
                order.submitted_quantity * order.market_price_at_decision
            )
            raised_quantity = (
                FILTER_RETURN_NOTIONAL / order.market_price_at_decision
            )
        self.mutation_trace.append("prepare")

        # 결함 filter가 domain 생성 이후 수량을 올리는 상황을 재현해 bootstrap 방어층을 통과시킨다.
        order.submitted_quantity = raised_quantity
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            self.prepare_output_notional = (
                order.submitted_quantity * order.market_price_at_decision
            )
        self.mutation_trace.append("filter-return")

        return order  # Permission proxy가 journal 전에 최종 수량을 다시 검사해야 한다.

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: 호출 여부를 기록한 뒤 기존 local 체결 응답을 위임한다.
        인자: order -> 제출할 prepared Order
        반환값: 기존 public Case 2 fake의 normalized 체결 결과
        작성 날짜: 2026/08/31
        """
        self.mutation_trace.append("submit")  # 이 문장은 cap gate가 실패하면 실행되어서는 안 된다.
        return super().submit_order(order=order)


@dataclass(slots=True)
class Phase13OrderCapFixture:
    """
    클래스 이름: Phase13OrderCapFixture
    기능: 공개 Case 2 runtime과 filter 결함 delegate를 한 통합 fixture로 묶는다.
    작성 날짜: 2026/08/31
    """

    public_case2: PublicCase2Fixture
    filter_delegate: FilterRaisingPublicCase2RESTClient


def _create_phase13_order_cap_fixture(
    temporary_directory: str,
) -> Phase13OrderCapFixture:
    """
    함수 이름: _create_phase13_order_cap_fixture()
    기능: 실제 public market/controller와 pending journal을 100 USDT permission proxy에 연결한다.
    인자: temporary_directory -> durable history와 pending sidecar를 둘 임시 디렉터리
    반환값: 공개 Kline 입력을 받을 준비가 끝난 Phase13OrderCapFixture
    작성 날짜: 2026/08/31
    """
    # 시장, 계좌, 주문이 같은 시각을 읽고 network 대신 local filter delegate를 사용하게 한다.
    clock = MutableUtcClock(INITIAL_TIME)
    filter_delegate = FilterRaisingPublicCase2RESTClient(clock)
    permission_client = _TestnetOrderPermissionRESTClient(
        filter_delegate,
        allow_orders=True,
        maximum_order_notional=ORDER_NOTIONAL_CAP,
    )
    api_gateway = APIGateway(permission_client, clock=clock)
    account = Account()
    position = Position()
    market_snapshot = MarketSnapshot(symbol="ETHUSDT", clock=clock)
    context = TradingContext(clock=clock)

    # 실제 JSONL repository와 pending sidecar owner를 준비하되 첫 주문 전에는 빈 상태를 유지한다.
    history_path = Path(temporary_directory) / "phase13-cap-trades.jsonl"
    repository = TradeHistoryRepository(history_path, clock=clock)
    history_controller = TradeHistoryController(
        repository,
        clock=clock,
        account=account,
    )
    history_controller.load_trade_history()

    # Account stream과 TradingController는 production adapter를 사용하고 pending 복구를 생성 시점부터 켠다.
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
        pending_order_recovery_enabled=True,
        maximum_order_notional=ORDER_NOTIONAL_CAP,
        risk_policy_state=create_test_risk_policy(),
        clock=clock,
    )

    # 실제 market builder와 공개 WebSocket Gateway가 Kline을 production observer로 전달하게 한다.
    market_web_socket_client = FakeWebSocketClient([])
    market_controller = MarketDataController(
        api_gateway,
        WebSocketGateway(market_web_socket_client),
        market_snapshot,
        NoOpRegimeController(),
        kline_limit=21,
        market_evaluation_builder=ThirtyMinuteMarketEvaluationBuilder(
            monotonic_clock=_zero_monotonic_time,
        ),
        trading_market_observer=trading_controller,
    )
    market_controller.initialize_market_data()

    # Account gap과 빈 거래소 주문을 공개 startup reconciliation으로 닫은 뒤 TYPE_0 session을 시작한다.
    trading_controller.load_account()
    trading_controller.reconcile_startup_state()
    regime_controller = RegimeController(
        RegimeSTM(),
        market_snapshot,
        trading_controller,
    )
    selection = regime_controller.set_regime_type(
        RegimeType.TYPE_0,
        command_id="select-phase13-cap",
        expected_version=trading_controller.context.version,
    )
    split_result = trading_controller.update_split_ratios(
        command_id="split-phase13-cap",
        expected_version=selection.version,
        scale_in=Decimal("0.5"),
        scale_out=Decimal("1"),
    )
    trading_controller.start_trading(
        command_id="start-phase13-cap",
        expected_version=split_result.version,
    )

    # 기존 public trigger가 같은 production collaborator를 재사용하도록 표준 fixture shape로 묶는다.
    public_case2 = PublicCase2Fixture(
        clock=clock,
        rest_client=filter_delegate,
        market_controller=market_controller,
        trading_controller=trading_controller,
        position=position,
        history_controller=history_controller,
        repository=repository,
        history_path=history_path,
        trade_publications=[],
    )
    return Phase13OrderCapFixture(
        public_case2=public_case2,
        filter_delegate=filter_delegate,
    )


class Phase13OrderCapFlowTests(unittest.TestCase):
    """
    클래스 이름: Phase13OrderCapFlowTests
    기능: Filter 후 절대 cap gate와 durable journal·REST의 선후 관계를 통합 검증한다.
    작성 날짜: 2026/08/31
    """

    def test_filter_return_crossing_fails_before_pending_journal_and_rest(
        self,
    ) -> None:
        """
        함수 이름: test_filter_return_crossing_fails_before_pending_journal_and_rest()
        기능: cap 이하 public BUY 입력을 100.01로 높인 filter가 journal과 submit 전에 차단되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with TemporaryDirectory() as temporary_directory:
            fixture = _create_phase13_order_cap_fixture(temporary_directory)
            public_case2 = fixture.public_case2
            pending_path = public_case2.repository.pending_order_storage_path
            self.assertFalse(pending_path.exists())

            # 실제 history owner의 save 경계를 감시하면서 public Kline 세 개로 production Case C를 구동한다.
            with mock_patch.object(
                TradeHistoryController,
                "save_pending_order",
            ) as save_pending_order:
                processed_results = _trigger_public_case_c_buy(public_case2)
                asyncio.run(public_case2.trading_controller.drain_events())

            # Filter 전 금액은 cap 이하이고 동일 decision price의 최종 제출 금액만 cap을 넘는다.
            self.assertGreaterEqual(len(processed_results), 1)
            self.assertIsNotNone(fixture.filter_delegate.prepare_input_notional)
            self.assertIsNotNone(fixture.filter_delegate.prepare_output_notional)
            self.assertLessEqual(
                fixture.filter_delegate.prepare_input_notional,
                ORDER_NOTIONAL_CAP,
            )
            self.assertGreater(
                fixture.filter_delegate.prepare_output_notional,
                ORDER_NOTIONAL_CAP,
            )

            # Permission proxy의 최종 cap gate가 durable PREPARED와 실제 submit 양쪽보다 먼저 닫힌다.
            save_pending_order.assert_not_called()
            self.assertEqual(
                ["prepare", "filter-return"],
                fixture.filter_delegate.mutation_trace,
            )
            self.assertEqual([], fixture.filter_delegate.submitted_orders)
            self.assertFalse(pending_path.exists())
            self.assertEqual(
                (),
                public_case2.history_controller.get_pending_order_recovery_records(),
            )

            # 실패는 Position·Trade를 만들지 않고 typed filter reconciliation 상태만 남긴다.
            self.assertEqual(Decimal("0"), public_case2.position.quantity)
            self.assertEqual(
                (),
                public_case2.history_controller.trade_history.trades,
            )
            self.assertIs(
                public_case2.trading_controller.status,
                TradingSessionStatus.RECONCILIATION_REQUIRED,
            )
            terminal_trace = public_case2.trading_controller.order_execution_trace[-1]
            self.assertEqual("5", terminal_trace.message_id)
            self.assertIs(terminal_trace.result, OrderExecutionTraceResult.FAILURE)
            self.assertIs(
                terminal_trace.failure_code,
                OrderExecutionFailureCode.SYMBOL_FILTER_REJECTED,
            )

            # Filter 또는 cap 거부 origin은 raw 예외 없이 정확한 stable category 하나만 공개한다.
            cause_snapshot = (
                public_case2.trading_controller.reconciliation_cause_snapshot
            )
            self.assertTrue(cause_snapshot.reconciliation_required)
            self.assertIs(
                cause_snapshot.status,
                ReconciliationCauseStatus.EXACT,
            )
            self.assertIs(
                cause_snapshot.category,
                ReconciliationCauseCategory.PREPARE_FILTER_OR_CAP_REJECTED,
            )


if __name__ == "__main__":
    unittest.main()
