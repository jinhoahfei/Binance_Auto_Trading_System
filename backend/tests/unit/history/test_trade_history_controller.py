"""Phase 8 durable 기록과 Phase 10 상세 조회·실시간 observer 계약을 검증한다."""

from collections.abc import Callable, Iterable, Iterator
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Event, Thread
import unittest
from unittest.mock import patch

import binance_auto_trader.application.trade_history_controller as controller_module
from binance_auto_trader.application.trade_history_controller import (
    CSVExportUnavailableError,
    TradeDetailsResult,
    TradeHistoryController,
    TradeHistoryPersistencePendingError,
)
from binance_auto_trader.domain.history import (
    CSVExportOptions,
    CSVExportResult,
    CSVPeriod,
    HistoryPeriod,
    OrderHistoryConflictError,
    Performance,
    Trade,
    TradeHistory,
    TradeHistoryQuery,
    TradeSide,
)
from binance_auto_trader.domain.trading.account import (
    Account,
    AccountSnapshot,
    AssetBalance,
)
from binance_auto_trader.domain.trading.states import OrderSide
from binance_auto_trader.transport.contracts import map_trade_details

from tests.unit.history.factories import make_order_execution, make_trade


CURRENT_TIME = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)
KST_MIDNIGHT_TIME = datetime(2026, 8, 21, 15, 0, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    """
    함수 이름: fixed_clock()
    기능: controller Performance의 현재 KST account day를 고정한다.
    인자: 없음
    반환값: 고정 UTC datetime
    작성 날짜: 2026/08/22
    """
    return CURRENT_TIME


def kst_midnight_clock() -> datetime:
    """
    함수 이름: kst_midnight_clock()
    기능: UTC 15시와 Asia/Seoul 자정이 일치하는 Phase 10 경계 시각을 반환한다.
    인자: 없음
    반환값: KST 2026-08-22 00:00:00에 해당하는 UTC datetime
    작성 날짜: 2026/08/23
    """
    return KST_MIDNIGHT_TIME  # query preset의 오늘 날짜를 KST 자정에 고정한다.


def naive_clock() -> datetime:
    """
    함수 이름: naive_clock()
    기능: timezone이 없는 잘못된 controller clock 결과를 제공한다.
    인자: 없음
    반환값: naive datetime
    작성 날짜: 2026/08/23
    """
    return datetime(2026, 8, 22, 3, 0)  # UTC 여부를 판별할 수 없는 입력을 의도적으로 만든다.


def make_ready_account() -> Account:
    """
    함수 이름: make_ready_account()
    기능: Phase 10 상세 조회에서 authoritative ETH free+locked 보유량을 제공한다.
    인자: 없음
    반환값: version 1의 준비된 Account
    작성 날짜: 2026/08/23
    """
    account = Account()
    account.apply_initial_snapshot(
        AccountSnapshot(
            balances=(
                AssetBalance(
                    asset="ETH",
                    free=Decimal("2.50"),
                    locked=Decimal("0.25"),
                ),
            ),
            updated_at=CURRENT_TIME,
            is_full_snapshot=True,
        ),
        Decimal("3000"),
    )  # 보유량 2.75와 Account version 1을 한 번의 full snapshot으로 만든다.
    return account


def make_period_trade(
    order_id: str,
    days_ago: int,
    side: OrderSide,
) -> Trade:
    """
    함수 이름: make_period_trade()
    기능: 고정 KST 정오를 기준으로 지정 일수 전의 BUY 또는 SELL Trade를 만든다.
    인자: order_id -> 유일한 거래소 주문 ID
        days_ago -> CURRENT_TIME에서 뺄 calendar day 수
        side -> 생성할 거래 방향
    반환값: 기간 조합 검증용 Trade
    작성 날짜: 2026/08/23
    """
    return make_trade(
        trade_id=f"period-{order_id}",
        order_id=order_id,
        executed_at=CURRENT_TIME - timedelta(days=days_ago),
        side=side,
    )  # UTC 정오 간격은 KST calendar day 간격도 그대로 유지한다.


class RecordingRepository:
    """
    클래스 이름: RecordingRepository
    기능: in-memory durable index와 저장 실패 및 호출 순서를 제어하는 repository fake이다.
    작성 날짜: 2026/08/22
    """

    def __init__(
        self,
        *,
        trace: list[str] | None = None,
        save_failures: int = 0,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 비어 있는 durable index와 선택적 trace 및 남은 저장 실패 횟수를 준비한다.
        인자: trace -> save 단계 이름을 기록할 optional list
            save_failures -> 성공 전에 발생시킬 저장 오류 횟수
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.trace = [] if trace is None else trace
        self.save_failures = save_failures
        self.save_call_count = 0
        self.trades_by_order_id: dict[str, Trade] = {}
        self.after_durable_write: Callable[[], None] | None = None
        self.stream_queries: list[TradeHistoryQuery] = []

    def get_trade_history(self) -> tuple[Trade, ...]:
        """
        함수 이름: get_trade_history()
        기능: in-memory durable order 순서의 Trade tuple을 반환한다.
        인자: 없음
        반환값: durable Trade tuple
        작성 날짜: 2026/08/22
        """
        return tuple(self.trades_by_order_id.values())

    def save_this_trade_by_order_id(
        self,
        order_id: int | str,
        trade: Trade,
    ) -> None:
        """
        함수 이름: save_this_trade_by_order_id()
        기능: 제어된 실패 뒤 order ID idempotency로 Trade를 in-memory durable 저장한다.
        인자: order_id -> 저장 key
            trade -> 저장할 terminal Trade
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        normalized_order_id = str(order_id)
        self.trace.append("13.5")
        self.save_call_count += 1
        if self.save_failures > 0:
            self.save_failures -= 1
            raise OSError("controlled persistence failure")

        # 실제 repository와 같이 동일 내용은 no-op, 다른 내용은 typed conflict로 처리한다.
        existing_trade = self.trades_by_order_id.get(normalized_order_id)
        if existing_trade is not None and existing_trade != trade:
            raise OrderHistoryConflictError("controlled order conflict")
        self.trades_by_order_id[normalized_order_id] = trade
        if self.after_durable_write is not None:
            self.after_durable_write()  # repository 반환 전 publication 상태를 관찰한다.

    def stream_trades(
        self,
        query: TradeHistoryQuery,
    ) -> Iterator[Trade]:
        """
        함수 이름: stream_trades()
        기능: test durable index를 query로 필터해 한 번 소비할 iterator를 반환한다.
        인자: query -> Controller가 만든 KST 날짜와 side 조건
        반환값: 조건에 맞는 Trade iterator
        작성 날짜: 2026/08/23
        """
        # 전달 query를 기록하고 production과 같은 canonical TradeHistory 조건을 재사용한다.
        self.stream_queries.append(query)
        matching_trades = TradeHistory(
            tuple(self.trades_by_order_id.values())
        ).find(query)
        return iter(matching_trades)  # Writer가 materialize 전 iterator를 받는 경계를 보존한다.


class RecordingCSVExportWriter:
    """
    클래스 이름: RecordingCSVExportWriter
    기능: Controller가 전달한 option과 Trade iterator를 기록하는 CSV writer fake이다.
    작성 날짜: 2026/08/23
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 비어 있는 호출 기록과 결정적 성공 결과를 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        self.received_options: list[CSVExportOptions] = []
        self.received_trades: list[tuple[Trade, ...]] = []
        self.result = CSVExportResult(
            file_path="/tmp/trade-history.csv",
            exported_row_count=1,
        )

    def write_csv(
        self,
        trades: Iterable[Trade],
        options: CSVExportOptions,
    ) -> CSVExportResult:
        """
        함수 이름: write_csv()
        기능: 전달받은 iterator를 한 번 소비하고 설정된 성공 결과를 반환한다.
        인자: trades -> Controller가 materialize하지 않은 Trade iterable
            options -> 원래 검증된 CSV option
        반환값: test가 설정한 CSVExportResult
        작성 날짜: 2026/08/23
        """
        # Test double에서만 소비해 Controller가 중간 tuple을 만들지 않았는지 호출 타입과 함께 본다.
        self.received_options.append(options)
        self.received_trades.append(tuple(trades))
        return self.result  # 실제 writer의 원자 파일 작업 대신 결정적 receipt를 사용한다.


class BlockingCSVExportWriter(RecordingCSVExportWriter):
    """
    클래스 이름: BlockingCSVExportWriter
    기능: CSV write 구간을 멈춰 Controller operation lock의 범위를 관찰한다.
    작성 날짜: 2026/08/24
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: write 진입·해제 event와 기본 recording 결과를 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        super().__init__()
        self.write_started = Event()
        self.allow_write_to_finish = Event()

    def write_csv(
        self,
        trades: Iterable[Trade],
        options: CSVExportOptions,
    ) -> CSVExportResult:
        """
        함수 이름: write_csv()
        기능: writer 진입을 알린 뒤 test가 허용할 때 iterator를 소비하고 receipt를 반환한다.
        인자: trades -> Controller가 snapshot으로 획득한 Trade iterable
            options -> 검증 완료 CSV option
        반환값: RecordingCSVExportWriter의 결정적 성공 결과
        작성 날짜: 2026/08/24
        """
        self.write_started.set()
        if not self.allow_write_to_finish.wait(timeout=2.0):
            raise TimeoutError("CSV writer release was not signalled")

        return super().write_csv(trades, options)


class TradeHistoryControllerPhase8Tests(unittest.TestCase):
    """
    클래스 이름: TradeHistoryControllerPhase8Tests
    기능: 메시지 13.1~13.5 조정과 저장 전후 atomic state 경계를 테스트한다.
    작성 날짜: 2026/08/22
    """

    def test_sell_records_exact_submessage_order_and_publishes_after_save(
        self,
    ) -> None:
        """
        함수 이름: test_sell_records_exact_submessage_order_and_publishes_after_save()
        기능: SELL의 13.1~13.5 순서와 durable write 반환 뒤 state publication을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        trace: list[str] = []
        repository = RecordingRepository(trace=trace)
        controller = TradeHistoryController(repository, clock=fixed_clock)
        order, summary = make_order_execution(
            exchange_order_id="401",
            side=OrderSide.SELL,
        )
        observed_during_save: list[tuple[int, Decimal]] = []
        repository.after_durable_write = lambda: observed_during_save.append(
            (
                len(controller.trade_history.trades),
                controller.performance.realized_pnl,
            )
        )

        # 실제 domain operation을 감싸 메시지 하위 호출 순서만 관찰한다.
        original_calculate = Performance.calculate_realized_result
        original_factory = Trade.from_order_execution
        original_add_trade = TradeHistory.add_trade
        original_apply_trade = Performance.apply_new_trade

        def calculate_realized_result(
            performance: Performance,
            execution_summary: object,
            allocated_cost_basis: Decimal,
        ) -> object:
            """
            함수 이름: calculate_realized_result()
            기능: 메시지 13.1을 기록하고 실제 realized 계산을 호출한다.
            인자: performance -> 계산을 수행할 Performance
                execution_summary -> 실제 ExecutionSummary
                allocated_cost_basis -> 사전 고정 원가
            반환값: 실제 RealizedResult
            작성 날짜: 2026/08/22
            """
            trace.append("13.1")
            return original_calculate(
                performance,
                execution_summary,
                allocated_cost_basis,
            )

        def build_trade(*factory_arguments: object) -> Trade:
            """
            함수 이름: build_trade()
            기능: 메시지 13.2를 기록하고 실제 Trade factory를 호출한다.
            인자: factory_arguments -> factory에 전달된 order, summary, realized result
            반환값: 실제 Trade
            작성 날짜: 2026/08/22
            """
            trace.append("13.2")
            return original_factory(*factory_arguments)

        def add_trade(history: TradeHistory, trade: Trade) -> None:
            """
            함수 이름: add_trade()
            기능: 메시지 13.3을 기록하고 candidate TradeHistory에 실제 Trade를 추가한다.
            인자: history -> 공개 전 candidate TradeHistory
                trade -> 추가할 실제 Trade
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            trace.append("13.3")
            original_add_trade(history, trade)

        def apply_new_trade(performance: Performance, trade: Trade) -> None:
            """
            함수 이름: apply_new_trade()
            기능: 메시지 13.4를 기록하고 candidate Performance에 실제 Trade를 반영한다.
            인자: performance -> 공개 전 candidate Performance
                trade -> 반영할 실제 Trade
            반환값: 없음
            작성 날짜: 2026/08/22
            """
            trace.append("13.4")
            original_apply_trade(performance, trade)

        with patch.object(
            Performance,
            "calculate_realized_result",
            autospec=True,
            side_effect=calculate_realized_result,
        ), patch.object(
            Trade,
            "from_order_execution",
            side_effect=build_trade,
        ), patch.object(
            TradeHistory,
            "add_trade",
            autospec=True,
            side_effect=add_trade,
        ), patch.object(
            Performance,
            "apply_new_trade",
            autospec=True,
            side_effect=apply_new_trade,
        ):
            trade = controller.record_order_execution(
                order,
                summary,
                Decimal("100.10"),
            )

        self.assertEqual(trace, ["13.1", "13.2", "13.3", "13.4", "13.5"])
        self.assertEqual(observed_during_save, [(0, Decimal("0"))])
        self.assertEqual(controller.trade_history.trades, (trade,))
        self.assertEqual(controller.performance.realized_pnl, Decimal("9.79"))
        self.assertEqual(repository.trades_by_order_id, {"401": trade})

    def test_storage_failure_keeps_published_state_clean_until_same_order_retry(
        self,
    ) -> None:
        """
        함수 이름: test_storage_failure_keeps_published_state_clean_until_same_order_retry()
        기능: 저장 실패가 공개 snapshot을 오염시키지 않고 save-only retry 뒤 게시하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        repository = RecordingRepository(save_failures=1)
        controller = TradeHistoryController(repository, clock=fixed_clock)
        order, summary = make_order_execution(exchange_order_id="501")
        previous_history = controller.trade_history
        previous_performance = controller.performance

        # 첫 호출은 candidate만 dirty로 보존하고 authoritative state identity를 유지한다.
        with self.assertRaisesRegex(OSError, "controlled persistence failure"):
            controller.record_order_execution(order, summary)

        self.assertIs(controller.trade_history, previous_history)
        self.assertIs(controller.performance, previous_performance)
        self.assertEqual(controller.trade_history.trades, ())
        self.assertEqual(controller.performance.total_fee, Decimal("0"))
        self.assertEqual(controller.dirty_order_ids, frozenset({"501"}))

        with self.assertRaises(TradeHistoryPersistencePendingError):
            next_order, next_summary = make_order_execution(
                exchange_order_id="502"
            )
            controller.record_order_execution(next_order, next_summary)

        retried_trade = controller.retry_pending_persistence("501")

        self.assertEqual(repository.save_call_count, 2)
        self.assertEqual(controller.trade_history.trades, (retried_trade,))
        self.assertEqual(controller.performance.total_fee, Decimal("0.10"))
        self.assertEqual(controller.dirty_order_ids, frozenset())

    def test_entry_prices_survive_sell_only_and_date_filters(self) -> None:
        """
        함수 이름: test_entry_prices_survive_sell_only_and_date_filters()
        기능: 기간 밖 매수의 평균 체결가를 매도에 연결하고 이후 새 매수의 가격과 섞지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        buy = make_trade(order_id="901", executed_at=CURRENT_TIME - timedelta(days=40))
        sell = make_trade(order_id="902", side=OrderSide.SELL, executed_at=CURRENT_TIME - timedelta(minutes=2))
        next_buy = make_trade(order_id="903", executed_at=CURRENT_TIME, average_fill_price=Decimal("120"), executed_amount=Decimal("240"), fee_quote_amount=Decimal("0.24"))
        repository = RecordingRepository()
        repository.trades_by_order_id.update({trade.order_id: trade for trade in (buy, sell, next_buy)})
        controller = TradeHistoryController(repository, clock=fixed_clock, account=make_ready_account())
        controller.load_trade_history()

        only_sell = map_trade_details(controller.get_trade_details(HistoryPeriod.TODAY, TradeSide.SELL), HistoryPeriod.TODAY)
        self.assertEqual(len(only_sell["rows"]), 1)
        self.assertEqual(only_sell["rows"][0]["entry_price"], "100")
        self.assertEqual(only_sell["rows"][0]["average_fill_price"], "110")
        today = map_trade_details(controller.get_trade_details(HistoryPeriod.TODAY, TradeSide.ALL), HistoryPeriod.TODAY)
        self.assertEqual([row["entry_price"] for row in today["rows"]], ["100", "120"])
        self.assertEqual(controller.trade_history.trades, (buy, sell, next_buy))
        self.assertEqual(repository.save_call_count, 0)

    def test_buy_rejects_allocated_cost_and_skips_realized_fields(self) -> None:
        """
        함수 이름: test_buy_rejects_allocated_cost_and_skips_realized_fields()
        기능: BUY는 원가 인자를 거부하고 정상 기록에서는 realized 필드를 null로 유지한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        repository = RecordingRepository()
        controller = TradeHistoryController(repository, clock=fixed_clock)
        order, summary = make_order_execution(exchange_order_id="601")

        with self.assertRaisesRegex(ValueError, "must be None for BUY"):
            controller.record_order_execution(
                order,
                summary,
                Decimal("100"),
            )

        trade = controller.record_order_execution(order, summary)

        self.assertIsNone(trade.allocated_cost_basis)
        self.assertIsNone(trade.realized_pnl)
        self.assertIsNone(trade.realized_return_rate)
        self.assertEqual(controller.performance.total_fee, Decimal("0.10"))


class TradeHistoryControllerPhase10Tests(unittest.TestCase):
    """
    클래스 이름: TradeHistoryControllerPhase10Tests
    기능: 상세 조회 기간·side 결합, D-12 요약과 durable update observer를 테스트한다.
    작성 날짜: 2026/08/23
    """

    def test_get_trade_details_supports_all_twelve_filter_combinations(
        self,
    ) -> None:
        """
        함수 이름: test_get_trade_details_supports_all_twelve_filter_combinations()
        기능: 오늘·7일·30일·전체와 ALL·BUY·SELL의 12개 결합 결과를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        period_trades = (
            make_period_trade("701", 0, OrderSide.BUY),
            make_period_trade("702", 0, OrderSide.SELL),
            make_period_trade("703", 6, OrderSide.BUY),
            make_period_trade("704", 6, OrderSide.SELL),
            make_period_trade("705", 7, OrderSide.BUY),
            make_period_trade("706", 29, OrderSide.SELL),
            make_period_trade("707", 30, OrderSide.BUY),
            make_period_trade("708", 365, OrderSide.SELL),
        )
        repository = RecordingRepository()
        repository.trades_by_order_id.update(
            {trade.order_id: trade for trade in period_trades}
        )
        account = make_ready_account()
        controller = TradeHistoryController(
            repository,
            clock=fixed_clock,
            account=account,
        )
        controller.load_trade_history()

        # 각 expected tuple을 명시해 기간 경계와 side 결합 중 하나라도 누락되면 드러내게 한다.
        expected_order_ids = {
            (HistoryPeriod.TODAY, TradeSide.ALL): ("701", "702"),
            (HistoryPeriod.TODAY, TradeSide.BUY): ("701",),
            (HistoryPeriod.TODAY, TradeSide.SELL): ("702",),
            (HistoryPeriod.LAST_7_DAYS, TradeSide.ALL): (
                "701",
                "702",
                "703",
                "704",
            ),
            (HistoryPeriod.LAST_7_DAYS, TradeSide.BUY): ("701", "703"),
            (HistoryPeriod.LAST_7_DAYS, TradeSide.SELL): ("702", "704"),
            (HistoryPeriod.LAST_30_DAYS, TradeSide.ALL): (
                "701",
                "702",
                "703",
                "704",
                "705",
                "706",
            ),
            (HistoryPeriod.LAST_30_DAYS, TradeSide.BUY): (
                "701",
                "703",
                "705",
            ),
            (HistoryPeriod.LAST_30_DAYS, TradeSide.SELL): (
                "702",
                "704",
                "706",
            ),
            (HistoryPeriod.ALL, TradeSide.ALL): (
                "701",
                "702",
                "703",
                "704",
                "705",
                "706",
                "707",
                "708",
            ),
            (HistoryPeriod.ALL, TradeSide.BUY): (
                "701",
                "703",
                "705",
                "707",
            ),
            (HistoryPeriod.ALL, TradeSide.SELL): (
                "702",
                "704",
                "706",
                "708",
            ),
        }
        expected_start_dates = {
            HistoryPeriod.TODAY: date(2026, 8, 22),
            HistoryPeriod.LAST_7_DAYS: date(2026, 8, 16),
            HistoryPeriod.LAST_30_DAYS: date(2026, 7, 24),
            HistoryPeriod.ALL: date.min,
        }

        self.assertEqual(len(expected_order_ids), 12)
        for (period, side), expected_ids in expected_order_ids.items():
            with self.subTest(period=period, side=side):
                details = controller.get_trade_details(period, side)

                self.assertIsInstance(details, TradeDetailsResult)
                self.assertEqual(
                    tuple(row.order_id for row in details.rows),
                    expected_ids,
                )
                self.assertIs(details.trades, details.rows)
                self.assertEqual(details.query.start_date, expected_start_dates[period])
                self.assertEqual(details.query.end_date, date(2026, 8, 22))
                self.assertIs(details.query.side, side)
                self.assertEqual(details.holdings_asset, "ETH")
                self.assertEqual(details.holdings, Decimal("2.75"))
                self.assertEqual(details.account_version, 1)
                self.assertIs(details.performance, controller.performance)

        self.assertIs(controller.account, account)

    def test_trade_details_result_is_frozen(self) -> None:
        """
        함수 이름: test_trade_details_result_is_frozen()
        기능: 조회 결과 field와 Trade tuple을 caller가 교체할 수 없는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        controller = TradeHistoryController(
            RecordingRepository(),
            clock=fixed_clock,
            account=make_ready_account(),
        )
        details = controller.get_trade_details()

        # frozen dataclass의 공개 field 대입과 tuple 행 변경을 모두 허용하지 않는다.
        with self.assertRaises(FrozenInstanceError):
            details.holdings = Decimal("0")
        with self.assertRaises(TypeError):
            details.rows[0] = make_period_trade("799", 0, OrderSide.BUY)

    def test_today_query_uses_exact_kst_midnight_boundary(self) -> None:
        """
        함수 이름: test_today_query_uses_exact_kst_midnight_boundary()
        기능: UTC 15시 직전과 정확한 시각을 서로 다른 KST 날짜로 조회하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        before_midnight = make_trade(
            trade_id="before-kst-midnight",
            order_id="801",
            executed_at=KST_MIDNIGHT_TIME - timedelta(seconds=1),
        )
        at_midnight = make_trade(
            trade_id="at-kst-midnight",
            order_id="802",
            executed_at=KST_MIDNIGHT_TIME,
            side=OrderSide.SELL,
        )
        after_today = make_trade(
            trade_id="after-kst-today",
            order_id="803",
            executed_at=KST_MIDNIGHT_TIME + timedelta(days=1),
        )
        repository = RecordingRepository()
        repository.trades_by_order_id.update(
            {
                trade.order_id: trade
                for trade in (before_midnight, at_midnight, after_today)
            }
        )
        controller = TradeHistoryController(
            repository,
            clock=kst_midnight_clock,
        )
        controller.load_trade_history()

        # TODAY의 양끝 포함 query는 현재 KST 날짜의 정확한 자정 거래만 선택한다.
        details = controller.get_trade_details(
            HistoryPeriod.TODAY,
            TradeSide.ALL,
        )

        self.assertEqual(details.query.start_date, date(2026, 8, 22))
        self.assertEqual(details.query.end_date, date(2026, 8, 22))
        self.assertEqual(details.rows, (at_midnight,))

    def test_initial_details_retries_when_clock_crosses_kst_midnight(self) -> None:
        """
        함수 이름: test_initial_details_retries_when_clock_crosses_kst_midnight()
        기능: 최초 상세 결합 중 KST 자정이 지나면 query와 당일 Performance를 새 날짜로 다시 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        before_midnight = KST_MIDNIGHT_TIME - timedelta(microseconds=1)
        at_midnight = make_trade(
            trade_id="crossed-kst-midnight",
            order_id="804",
            executed_at=KST_MIDNIGHT_TIME,
        )
        repository = RecordingRepository()
        repository.trades_by_order_id[at_midnight.order_id] = at_midnight
        clock_results = iter(
            (
                before_midnight,
                before_midnight,
                before_midnight,
                KST_MIDNIGHT_TIME,
                KST_MIDNIGHT_TIME,
                KST_MIDNIGHT_TIME,
                KST_MIDNIGHT_TIME,
                KST_MIDNIGHT_TIME,
                KST_MIDNIGHT_TIME,
            )
        )

        def crossing_clock() -> datetime:
            """
            함수 이름: crossing_clock()
            기능: 초기화와 첫 query 뒤 Performance 조회부터 KST 새 날짜를 반환한다.
            인자: 없음
            반환값: 호출 순서에 따른 timezone-aware UTC datetime
            작성 날짜: 2026/08/23
            """
            return next(clock_results)  # controller가 자정 전후 snapshot을 섞는 경쟁을 재현한다.

        controller = TradeHistoryController(repository, clock=crossing_clock)
        controller.load_trade_history()

        # 첫 시도는 이전 날짜 query이지만 반환 결과는 두 번째 새 날짜 결합만 게시해야 한다.
        details = controller.get_trade_details()

        self.assertEqual(details.query.start_date, date(2026, 8, 22))
        self.assertEqual(details.query.end_date, date(2026, 8, 22))
        self.assertEqual(details.rows, (at_midnight,))
        self.assertEqual(
            details.performance.daily_fee,
            at_midnight.fee_quote_amount,
        )

    def test_cached_details_retries_when_find_crosses_kst_midnight(self) -> None:
        """
        함수 이름: test_cached_details_retries_when_find_crosses_kst_midnight()
        기능: summary cache hit의 history find 중 KST 자정이 지나면 새 날짜로 재조회한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        current_clock_time = [KST_MIDNIGHT_TIME - timedelta(microseconds=1)]

        def crossing_clock() -> datetime:
            """
            함수 이름: crossing_clock()
            기능: history find test가 제어하는 timezone-aware UTC 시각을 반환한다.
            인자: 없음
            반환값: current_clock_time의 단일 UTC datetime
            작성 날짜: 2026/08/23
            """
            return current_clock_time[0]  # 첫 find가 종료된 직후에만 KST 날짜를 넘긴다.

        at_midnight = make_trade(
            trade_id="cached-crossed-kst-midnight",
            order_id="806",
            executed_at=KST_MIDNIGHT_TIME,
        )
        repository = RecordingRepository()
        repository.trades_by_order_id[at_midnight.order_id] = at_midnight
        controller = TradeHistoryController(repository, clock=crossing_clock)
        controller.load_trade_history()
        warmed_details = controller.get_trade_details()
        original_find = TradeHistory.find
        find_call_count = [0]

        def crossing_find(
            history: TradeHistory,
            query: TradeHistoryQuery,
        ) -> tuple[Trade, ...]:
            """
            함수 이름: crossing_find()
            기능: cache hit 조회의 첫 find 직후 clock을 다음 KST 날짜로 전진시킨다.
            인자: history -> 조회할 TradeHistory
                query -> controller가 생성한 TradeHistoryQuery
            반환값: 전달된 query에 맞는 Trade tuple
            작성 날짜: 2026/08/23
            """
            rows = original_find(history, query)
            find_call_count[0] += 1
            if find_call_count[0] == 1:
                current_clock_time[0] = KST_MIDNIGHT_TIME
            return rows  # 첫 결과는 전날 query이므로 controller가 폐기해야 한다.

        # Warm cache의 요약을 재사용하려던 첫 시도는 자정 경계 검증에서 폐기한다.
        with patch.object(
            TradeHistory,
            "find",
            autospec=True,
            side_effect=crossing_find,
        ):
            refreshed_details = controller.get_trade_details()

        self.assertEqual(warmed_details.rows, ())
        self.assertEqual(find_call_count[0], 2)
        self.assertEqual(refreshed_details.query.start_date, date(2026, 8, 22))
        self.assertEqual(refreshed_details.query.end_date, date(2026, 8, 22))
        self.assertEqual(refreshed_details.rows, (at_midnight,))

    def test_cached_details_refresh_account_holdings_after_version_change(self) -> None:
        """
        함수 이름: test_cached_details_refresh_account_holdings_after_version_change()
        기능: 필터 독립 summary cache가 Account version 변경 뒤 이전 ETH 보유량을 반환하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        account = make_ready_account()
        controller = TradeHistoryController(
            RecordingRepository(),
            clock=fixed_clock,
            account=account,
        )
        initial_details = controller.get_trade_details()
        account.apply_stream_snapshot(
            AccountSnapshot(
                balances=(
                    AssetBalance(
                        asset="ETH",
                        free=Decimal("4.00"),
                        locked=Decimal("0.50"),
                    ),
                ),
                updated_at=CURRENT_TIME + timedelta(seconds=1),
                is_full_snapshot=False,
            )
        )  # 실제 Account stream처럼 보유량과 version을 함께 한 단계 전진시킨다.

        # 같은 날짜의 후속 filter도 Account provenance가 바뀌었으면 summary를 새로 결합해야 한다.
        refreshed_details = controller.get_trade_details(
            HistoryPeriod.LAST_7_DAYS,
            TradeSide.BUY,
        )

        self.assertEqual(initial_details.holdings, Decimal("2.75"))
        self.assertEqual(initial_details.account_version, 1)
        self.assertEqual(refreshed_details.holdings, Decimal("4.50"))
        self.assertEqual(refreshed_details.account_version, 2)

    def test_cached_details_refresh_daily_performance_after_kst_rollover(
        self,
    ) -> None:
        """
        함수 이름: test_cached_details_refresh_daily_performance_after_kst_rollover()
        기능: KST 날짜가 바뀐 후 TODAY 행과 daily Performance가 모두 새 account day를 사용한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        current_clock_time = [CURRENT_TIME]

        def advancing_clock() -> datetime:
            """
            함수 이름: advancing_clock()
            기능: test가 제어하는 현재 timezone-aware UTC 시각을 반환한다.
            인자: 없음
            반환값: current_clock_time의 단일 UTC datetime
            작성 날짜: 2026/08/23
            """
            return current_clock_time[0]  # 상세 cache 전후의 KST 날짜를 test가 명시적으로 바꾼다.

        trade = make_trade(
            trade_id="previous-kst-day",
            order_id="805",
            executed_at=CURRENT_TIME,
        )
        repository = RecordingRepository()
        repository.trades_by_order_id[trade.order_id] = trade
        controller = TradeHistoryController(repository, clock=advancing_clock)
        controller.load_trade_history()
        previous_day_details = controller.get_trade_details()
        previous_day_fee = previous_day_details.performance.daily_fee

        # Cache를 만든 뒤 clock만 다음 KST 날짜로 이동해 자정 event가 없는 경로를 재현한다.
        current_clock_time[0] = CURRENT_TIME + timedelta(days=1)
        next_day_details = controller.get_trade_details()

        self.assertEqual(previous_day_details.rows, (trade,))
        self.assertEqual(
            previous_day_fee,
            trade.fee_quote_amount,
        )
        self.assertEqual(next_day_details.query.start_date, date(2026, 8, 23))
        self.assertEqual(next_day_details.query.end_date, date(2026, 8, 23))
        self.assertEqual(next_day_details.rows, ())
        self.assertEqual(next_day_details.performance.daily_fee, Decimal("0"))

    def test_empty_default_query_preserves_account_and_zero_summary(self) -> None:
        """
        함수 이름: test_empty_default_query_preserves_account_and_zero_summary()
        기능: TODAY+ALL 기본 조회의 empty 행과 authoritative Account 및 0 성과를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        controller = TradeHistoryController(
            RecordingRepository(),
            clock=fixed_clock,
            account=make_ready_account(),
        )

        details = controller.get_trade_details()

        self.assertEqual(details.rows, ())
        self.assertEqual(details.query.start_date, date(2026, 8, 22))
        self.assertEqual(details.query.end_date, date(2026, 8, 22))
        self.assertIs(details.query.side, TradeSide.ALL)
        self.assertEqual(details.holdings, Decimal("2.75"))
        self.assertEqual(details.account_version, 1)
        self.assertEqual(details.performance.completed_sell_count, 0)
        self.assertEqual(details.performance.realized_pnl, Decimal("0"))
        self.assertEqual(details.performance.total_fee, Decimal("0"))

    def test_filtered_rows_do_not_filter_d12_performance_summary(self) -> None:
        """
        함수 이름: test_filtered_rows_do_not_filter_d12_performance_summary()
        기능: TODAY+BUY 행에서 제외된 과거 SELL도 전체 Performance 집계에는 남는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        today_buy = make_period_trade("901", 0, OrderSide.BUY)
        previous_sell = make_period_trade("902", 1, OrderSide.SELL)
        repository = RecordingRepository()
        repository.trades_by_order_id.update(
            {
                today_buy.order_id: today_buy,
                previous_sell.order_id: previous_sell,
            }
        )
        controller = TradeHistoryController(
            repository,
            clock=fixed_clock,
            account=make_ready_account(),
        )
        controller.load_trade_history()

        # 행은 결합 filter로 제한하지만 summary는 published history 전체의 D-12 범위를 사용한다.
        details = controller.get_trade_details(
            HistoryPeriod.TODAY,
            TradeSide.BUY,
        )

        self.assertEqual(details.rows, (today_buy,))
        self.assertEqual(details.performance.completed_sell_count, 1)
        self.assertEqual(details.performance.realized_pnl, Decimal("9.79"))
        self.assertEqual(details.performance.total_fee, Decimal("0.31"))
        self.assertEqual(details.performance.daily_fee, Decimal("0.20"))

    def test_details_reads_find_then_account_then_performance(self) -> None:
        """
        함수 이름: test_details_reads_find_then_account_then_performance()
        기능: Communication 1.1.2.2~1.1.2.4의 domain 호출 순서를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        repository = RecordingRepository()
        controller = TradeHistoryController(
            repository,
            clock=fixed_clock,
            account=make_ready_account(),
        )
        trace: list[str] = []
        original_find = TradeHistory.find
        original_get_holdings = Account.get_holdings
        original_get_performance = Performance.get_performance

        def find(history: TradeHistory, query: object) -> tuple[Trade, ...]:
            """
            함수 이름: find()
            기능: history 조회 호출을 기록하고 실제 filter 결과를 반환한다.
            인자: history -> 조회할 TradeHistory
                query -> controller가 만든 실제 TradeHistoryQuery
            반환값: 실제 필터 Trade tuple
            작성 날짜: 2026/08/23
            """
            trace.append("1.1.2.2")
            return original_find(history, query)

        def get_holdings(account: Account, asset: str = "ETH") -> Decimal:
            """
            함수 이름: get_holdings()
            기능: Account 보유량 호출을 기록하고 실제 ETH 수량을 반환한다.
            인자: account -> authoritative Account
                asset -> 조회 자산 이름
            반환값: 실제 free+locked 보유량
            작성 날짜: 2026/08/23
            """
            trace.append("1.1.2.3")
            return original_get_holdings(account, asset)

        def get_performance(performance: Performance) -> Performance:
            """
            함수 이름: get_performance()
            기능: 전체 성과 호출을 기록하고 실제 Performance를 반환한다.
            인자: performance -> published Performance
            반환값: account day가 갱신된 실제 Performance
            작성 날짜: 2026/08/23
            """
            trace.append("1.1.2.4")
            return original_get_performance(performance)

        # 세 canonical operation을 감싸 상세 조회의 실제 호출 순서를 관찰한다.
        with patch.object(
            TradeHistory,
            "find",
            autospec=True,
            side_effect=find,
        ), patch.object(
            Account,
            "get_holdings",
            autospec=True,
            side_effect=get_holdings,
        ), patch.object(
            Performance,
            "get_performance",
            autospec=True,
            side_effect=get_performance,
        ):
            controller.get_trade_details()

        self.assertEqual(trace, ["1.1.2.2", "1.1.2.3", "1.1.2.4"])

    def test_successful_record_notifies_after_publication_and_isolates_failure(
        self,
    ) -> None:
        """
        함수 이름: test_successful_record_notifies_after_publication_and_isolates_failure()
        기능: record 성공 observer가 게시 후 한 번 호출되고 예외가 durable 결과를 바꾸지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        repository = RecordingRepository()
        observations: list[
            tuple[Trade, Performance, tuple[Trade, ...], frozenset[str]]
        ] = []

        def failing_observer(trade: Trade, performance: Performance) -> None:
            """
            함수 이름: failing_observer()
            기능: publication 상태를 기록한 뒤 transport event 실패를 모사한다.
            인자: trade -> observer에 전달된 Trade
                performance -> 같은 candidate의 Performance
            반환값: 없음
            작성 날짜: 2026/08/23
            """
            observations.append(
                (
                    trade,
                    performance,
                    controller.trade_history.trades,
                    controller.dirty_order_ids,
                )
            )
            raise RuntimeError("controlled observer failure")

        controller = TradeHistoryController(
            repository,
            clock=fixed_clock,
            trade_update_observer=failing_observer,
        )
        order, summary = make_order_execution(exchange_order_id="1001")

        # observer 예외가 밖으로 새면 caller가 durable 주문을 재처리할 수 있으므로 성공으로 끝나야 한다.
        recorded_trade = controller.record_order_execution(order, summary)

        self.assertEqual(len(observations), 1)
        observed_trade, observed_performance, observed_rows, observed_dirty = (
            observations[0]
        )
        self.assertIs(observed_trade, recorded_trade)
        self.assertIs(observed_performance, controller.performance)
        self.assertEqual(observed_rows, (recorded_trade,))
        self.assertEqual(observed_dirty, frozenset())
        self.assertEqual(repository.trades_by_order_id, {"1001": recorded_trade})
        self.assertEqual(controller.dirty_order_ids, frozenset())
        with self.assertRaises(KeyError):
            controller.retry_pending_persistence("1001")

    def test_successful_record_refreshes_performance_when_save_crosses_midnight(
        self,
    ) -> None:
        """
        함수 이름: test_successful_record_refreshes_performance_when_save_crosses_midnight()
        기능: durable save 중 KST 자정이 지나면 observer가 새 날짜의 daily Performance만 받는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        current_clock_time = [KST_MIDNIGHT_TIME - timedelta(seconds=1)]

        def publication_clock() -> datetime:
            """
            함수 이름: publication_clock()
            기능: repository durable write 전후로 test가 제어하는 UTC 시각을 반환한다.
            인자: 없음
            반환값: 현재 publication UTC datetime
            작성 날짜: 2026/08/23
            """
            return current_clock_time[0]  # Candidate와 observer publication의 날짜를 분리한다.

        repository = RecordingRepository()
        repository.after_durable_write = lambda: current_clock_time.__setitem__(
            0,
            KST_MIDNIGHT_TIME,
        )
        observed_daily_fees: list[Decimal] = []
        controller = TradeHistoryController(
            repository,
            clock=publication_clock,
            trade_update_observer=lambda _trade, performance: (
                observed_daily_fees.append(performance.daily_fee)
            ),
        )
        order, summary = make_order_execution(exchange_order_id="1004")

        controller.record_order_execution(order, summary)

        self.assertEqual(observed_daily_fees, [Decimal("0")])
        self.assertEqual(controller.performance.daily_fee, Decimal("0"))

    def test_retry_notifies_once_only_after_dirty_publication_succeeds(
        self,
    ) -> None:
        """
        함수 이름: test_retry_notifies_once_only_after_dirty_publication_succeeds()
        기능: 저장 실패는 알리지 않고 save-only retry 성공과 dirty 해제 뒤 정확히 한 번 알리는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        repository = RecordingRepository(save_failures=1)
        observations: list[
            tuple[Trade, Performance, tuple[Trade, ...], frozenset[str]]
        ] = []

        def observer(trade: Trade, performance: Performance) -> None:
            """
            함수 이름: observer()
            기능: retry observer가 보는 published history와 dirty 상태를 기록한다.
            인자: trade -> retry에서 게시한 기존 Trade
                performance -> 기존 candidate의 Performance
            반환값: 없음
            작성 날짜: 2026/08/23
            """
            observations.append(
                (
                    trade,
                    performance,
                    controller.trade_history.trades,
                    controller.dirty_order_ids,
                )
            )

        controller = TradeHistoryController(
            repository,
            clock=fixed_clock,
            trade_update_observer=observer,
        )
        order, summary = make_order_execution(exchange_order_id="1002")

        # 최초 save 오류는 unpublished candidate를 알리지 않고 dirty retry 근거만 남긴다.
        with self.assertRaises(OSError):
            controller.record_order_execution(order, summary)
        self.assertEqual(observations, [])
        self.assertEqual(controller.dirty_order_ids, frozenset({"1002"}))

        retried_trade = controller.retry_pending_persistence("1002")

        self.assertEqual(len(observations), 1)
        observed_trade, observed_performance, observed_rows, observed_dirty = (
            observations[0]
        )
        self.assertIs(observed_trade, retried_trade)
        self.assertIs(observed_performance, controller.performance)
        self.assertEqual(observed_rows, (retried_trade,))
        self.assertEqual(observed_dirty, frozenset())
        self.assertEqual(controller.dirty_order_ids, frozenset())
        with self.assertRaises(KeyError):
            controller.retry_pending_persistence("1002")
        self.assertEqual(len(observations), 1)  # 중복 retry 거부는 observer도 다시 호출하지 않는다.

    def test_retry_refreshes_performance_when_persistence_crosses_midnight(
        self,
    ) -> None:
        """
        함수 이름: test_retry_refreshes_performance_when_persistence_crosses_midnight()
        기능: 지연된 save-only retry가 새 KST 날짜의 daily Performance를 observer에 게시한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        current_clock_time = [KST_MIDNIGHT_TIME - timedelta(seconds=1)]

        def retry_clock() -> datetime:
            """
            함수 이름: retry_clock()
            기능: 최초 candidate와 retry publication 사이의 UTC 시각을 test가 제어하게 한다.
            인자: 없음
            반환값: 현재 retry UTC datetime
            작성 날짜: 2026/08/23
            """
            return current_clock_time[0]  # 실패 candidate를 다음 KST account day까지 보류한다.

        repository = RecordingRepository(save_failures=1)
        repository.after_durable_write = lambda: current_clock_time.__setitem__(
            0,
            KST_MIDNIGHT_TIME,
        )
        observed_daily_fees: list[Decimal] = []
        controller = TradeHistoryController(
            repository,
            clock=retry_clock,
            trade_update_observer=lambda _trade, performance: (
                observed_daily_fees.append(performance.daily_fee)
            ),
        )
        order, summary = make_order_execution(exchange_order_id="1005")

        with self.assertRaises(OSError):
            controller.record_order_execution(order, summary)
        controller.retry_pending_persistence("1005")

        self.assertEqual(observed_daily_fees, [Decimal("0")])
        self.assertEqual(controller.performance.daily_fee, Decimal("0"))

    def test_idempotent_same_order_does_not_publish_duplicate_event(
        self,
    ) -> None:
        """
        함수 이름: test_idempotent_same_order_does_not_publish_duplicate_event()
        기능: 같은 terminal order 재처리가 저장·state·observer를 한 번만 게시하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        repository = RecordingRepository()
        observations: list[tuple[Trade, Performance]] = []

        def observer(trade: Trade, performance: Performance) -> None:
            """
            함수 이름: observer()
            기능: durable publication마다 전달된 Trade와 Performance를 기록한다.
            인자: trade -> 방금 게시된 canonical Trade
                performance -> 같은 publication의 전체 Performance
            반환값: 없음
            작성 날짜: 2026/08/23
            """
            observations.append((trade, performance))

        controller = TradeHistoryController(
            repository,
            clock=fixed_clock,
            trade_update_observer=observer,
        )
        order, summary = make_order_execution(exchange_order_id="1003")

        # 같은 immutable execution을 두 번 전달해 두 번째 호출의 전체 no-op 경계를 검증한다.
        first_trade = controller.record_order_execution(order, summary)
        second_trade = controller.record_order_execution(order, summary)

        self.assertIs(second_trade, first_trade)
        self.assertEqual(controller.trade_history.trades, (first_trade,))
        self.assertEqual(repository.save_call_count, 1)
        self.assertEqual(observations, [(first_trade, controller.performance)])

    def test_details_reject_noncanonical_filters_and_clock(self) -> None:
        """
        함수 이름: test_details_reject_noncanonical_filters_and_clock()
        기능: 문자열 filter와 naive clock 결과를 application query 경계에서 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        controller = TradeHistoryController(
            RecordingRepository(),
            clock=fixed_clock,
        )

        # Transport parsing을 우회한 문자열과 timezone 없는 시각은 묵시적으로 보정하지 않는다.
        with self.assertRaises(TypeError):
            controller.get_trade_details("TODAY", TradeSide.ALL)
        with self.assertRaises(TypeError):
            controller.get_trade_details(HistoryPeriod.TODAY, "ALL")

        with self.assertRaisesRegex(ValueError, "timezone-aware UTC"):
            TradeHistoryController(
                RecordingRepository(),
                clock=naive_clock,
            )


class TradeHistoryControllerPhase11Tests(unittest.TestCase):
    """
    클래스 이름: TradeHistoryControllerPhase11Tests
    기능: KST CSV 기간 확정, ALL-side streaming과 writer 조정 경계를 검증한다.
    작성 날짜: 2026/08/23
    """

    def test_export_csv_resolves_weekly_kst_range_and_streams_all_sides(
        self,
    ) -> None:
        """
        함수 이름: test_export_csv_resolves_weekly_kst_range_and_streams_all_sides()
        기능: 최근 7일 preset이 backend KST 기준일과 ALL side query로 writer에 전달되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        repository = RecordingRepository()
        included_buy = make_period_trade("1101", 0, OrderSide.BUY)
        included_sell = make_period_trade("1102", 6, OrderSide.SELL)
        excluded_buy = make_period_trade("1103", 7, OrderSide.BUY)
        repository.trades_by_order_id.update(
            {
                trade.order_id: trade
                for trade in (included_buy, included_sell, excluded_buy)
            }
        )
        writer = RecordingCSVExportWriter()
        writer.result = CSVExportResult(
            file_path="/tmp/weekly.csv",
            exported_row_count=2,
        )
        controller = TradeHistoryController(
            repository,
            clock=fixed_clock,
            csv_export_writer=writer,
        )
        options = CSVExportOptions(
            save_location="/tmp",
            period=CSVPeriod.WEEKLY,
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 2),
            file_name="weekly",
        )

        # UI가 보낸 오래된 preset draft 날짜는 무시하고 backend 현재 KST 날짜로 다시 계산한다.
        result = controller.export_csv(options)

        self.assertIs(result, writer.result)
        self.assertEqual(
            repository.stream_queries,
            [
                TradeHistoryQuery(
                    start_date=date(2026, 8, 16),
                    end_date=date(2026, 8, 22),
                    side=TradeSide.ALL,
                )
            ],
        )
        self.assertEqual(writer.received_options, [options])
        self.assertEqual(
            writer.received_trades,
            [(included_buy, included_sell)],
        )  # 기간 안의 BUY와 SELL을 입력 순서대로 한 stream에 포함한다.

    def test_export_csv_preserves_custom_dates(self) -> None:
        """
        함수 이름: test_export_csv_preserves_custom_dates()
        기능: CUSTOM 기간은 backend clock과 무관하게 검증된 양끝 날짜를 그대로 사용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        repository = RecordingRepository()
        custom_trade = make_trade(
            trade_id="custom-1104",
            order_id="1104",
            executed_at=datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc),
        )
        repository.trades_by_order_id[custom_trade.order_id] = custom_trade
        writer = RecordingCSVExportWriter()
        controller = TradeHistoryController(
            repository,
            clock=fixed_clock,
            csv_export_writer=writer,
        )
        options = CSVExportOptions(
            save_location="/tmp",
            period=CSVPeriod.CUSTOM,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            file_name="custom.csv",
        )

        controller.export_csv(options)

        self.assertEqual(
            repository.stream_queries[0],
            TradeHistoryQuery(
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 1),
                side=TradeSide.ALL,
            ),
        )  # CUSTOM은 현재 2026-08-22로 끝 날짜를 확장하지 않는다.

    def test_export_csv_fails_closed_without_stream_or_writer(self) -> None:
        """
        함수 이름: test_export_csv_fails_closed_without_stream_or_writer()
        기능: optional Phase 11 capability가 빠진 기존 fake 구성에서 파일 생성을 시도하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        options = CSVExportOptions(
            save_location="/tmp",
            period=CSVPeriod.TODAY,
            start_date=date(2026, 8, 22),
            end_date=date(2026, 8, 22),
            file_name="today",
        )
        repository = RecordingRepository()

        with self.assertRaises(CSVExportUnavailableError):
            TradeHistoryController(
                repository,
                clock=fixed_clock,
            ).export_csv(options)

        class HistoryOnlyRepository:
            """
            클래스 이름: HistoryOnlyRepository
            기능: CSV stream capability가 없는 이전 단계 repository fake를 제공한다.
            작성 날짜: 2026/08/23
            """

            def get_trade_history(self) -> tuple[Trade, ...]:
                """
                함수 이름: get_trade_history()
                기능: 호환 생성자 검증에 필요한 빈 history를 반환한다.
                인자: 없음
                반환값: 빈 Trade tuple
                작성 날짜: 2026/08/23
                """
                return ()  # CSV stream operation은 의도적으로 제공하지 않는다.

        with self.assertRaises(CSVExportUnavailableError):
            TradeHistoryController(
                HistoryOnlyRepository(),
                clock=fixed_clock,
                csv_export_writer=RecordingCSVExportWriter(),
            ).export_csv(options)

    def test_export_csv_blocks_ambiguous_pending_persistence(self) -> None:
        """
        함수 이름: test_export_csv_blocks_ambiguous_pending_persistence()
        기능: 불확실한 durable save가 남아 있으면 CSV snapshot을 추측하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        repository = RecordingRepository(save_failures=1)
        controller = TradeHistoryController(
            repository,
            clock=fixed_clock,
            csv_export_writer=RecordingCSVExportWriter(),
        )
        order, summary = make_order_execution(exchange_order_id="1105")
        options = CSVExportOptions(
            save_location="/tmp",
            period=CSVPeriod.TODAY,
            start_date=date(2026, 8, 22),
            end_date=date(2026, 8, 22),
            file_name="pending",
        )

        # 첫 save 실패가 dirty publication을 만든 뒤 export가 repository를 읽기 전에 거부된다.
        with self.assertRaises(OSError):
            controller.record_order_execution(order, summary)
        with self.assertRaises(TradeHistoryPersistencePendingError):
            controller.export_csv(options)

        self.assertEqual(repository.stream_queries, [])

    def test_export_csv_releases_operation_lock_after_snapshot_capture(
        self,
    ) -> None:
        """
        함수 이름: test_export_csv_releases_operation_lock_after_snapshot_capture()
        기능: 느린 CSV write 중에도 새 terminal Trade가 durable publication을 완료하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        repository = RecordingRepository()
        snapshot_trade = make_period_trade("1106", 0, OrderSide.BUY)
        repository.trades_by_order_id[snapshot_trade.order_id] = snapshot_trade
        writer = BlockingCSVExportWriter()
        controller = TradeHistoryController(
            repository,
            clock=fixed_clock,
            csv_export_writer=writer,
        )
        options = CSVExportOptions(
            save_location="/tmp",
            period=CSVPeriod.TODAY,
            start_date=date(2026, 8, 22),
            end_date=date(2026, 8, 22),
            file_name="concurrent-publication",
        )
        export_failures: list[BaseException] = []

        def export_csv() -> None:
            """
            함수 이름: export_csv()
            기능: background export의 예상 밖 실패만 thread-safe append로 기록한다.
            인자: 없음
            반환값: 없음
            작성 날짜: 2026/08/24
            """
            try:
                controller.export_csv(options)
            except BaseException as error:
                export_failures.append(error)

        export_thread = Thread(target=export_csv)
        export_thread.start()
        self.assertTrue(writer.write_started.wait(timeout=1.0))

        # Writer가 멈춘 동안 같은 Controller의 terminal execution이 lock 대기 없이 끝나야 한다.
        order, summary = make_order_execution(exchange_order_id="1107")
        publication_thread = Thread(
            target=controller.record_order_execution,
            args=(order, summary),
        )
        publication_thread.start()
        publication_thread.join(timeout=1.0)
        self.assertFalse(publication_thread.is_alive())
        self.assertIn("1107", repository.trades_by_order_id)

        writer.allow_write_to_finish.set()
        export_thread.join(timeout=2.0)
        self.assertFalse(export_thread.is_alive())
        self.assertEqual(export_failures, [])
        self.assertEqual(
            writer.received_trades,
            [(snapshot_trade,)],
        )  # Snapshot 이후 append된 1107은 진행 중인 CSV에 섞이지 않는다.


if __name__ == "__main__":
    unittest.main()
