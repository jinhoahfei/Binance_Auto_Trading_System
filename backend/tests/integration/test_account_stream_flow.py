"""Communication 메시지 2~2.2.1과 6.1.1.1의 Controller 흐름을 검증한다."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    Subscription,
    WebSocketGateway,
)
from binance_auto_trader.application.trading_controller import (
    TradingController,
)
from binance_auto_trader.domain.common import (
    Interval,
    RegimeType,
    SUPPORTED_INTERVALS,
)
from binance_auto_trader.domain.market import Kline, MarketSnapshot
from binance_auto_trader.domain.trading.account import Account, AccountSnapshot
from binance_auto_trader.domain.trading.logic_registry import (
    TradingLogicStartGuard,
    UnsupportedTradingLogicError,
)

from tests.integration.trace_helpers import (
    TraceEntry,
    assert_trace_contract,
    finish_trace_entry,
    start_trace_entry,
)


REST_UPDATED_AT_MILLISECONDS = 1_787_270_400_000
STREAM_UPDATED_AT_MILLISECONDS = 1_787_270_460_000
MARKET_UPDATED_AT = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)
CURRENT_ETH_PRICE = Decimal("2500.50")
ACCOUNT_LOAD_COMMAND_ID = "account-load-20260821-001"


def _account_rest_payload() -> dict[str, object]:
    """
    함수 이름: _account_rest_payload()
    기능: initial account load에 사용할 공식 REST 응답 fixture를 만든다.
    인자: 없음
    반환값: updateTime과 전체 balances를 갖는 Binance account payload
    작성 날짜: 2026/08/21
    """
    return {
        "makerCommission": 15,
        "takerCommission": 15,
        "buyerCommission": 0,
        "sellerCommission": 0,
        "canTrade": True,
        "canWithdraw": True,
        "canDeposit": True,
        "updateTime": REST_UPDATED_AT_MILLISECONDS,
        "accountType": "SPOT",
        "balances": [
            {"asset": "ETH", "free": "1.00000000", "locked": "0.20000000"},
            {"asset": "USDT", "free": "100.00", "locked": "10.00"},
        ],
        "permissions": ["SPOT"],
    }


def _account_stream_payload() -> dict[str, object]:
    """
    함수 이름: _account_stream_payload()
    기능: subscribe 중 동기 발생할 partial account position event를 만든다.
    인자: 없음
    반환값: 현재 WebSocket API envelope의 outboundAccountPosition payload
    작성 날짜: 2026/08/21
    """
    return {
        "subscriptionId": 3,
        "event": {
            "e": "outboundAccountPosition",
            "E": STREAM_UPDATED_AT_MILLISECONDS + 100,
            "u": STREAM_UPDATED_AT_MILLISECONDS,
            "B": [
                {"a": "ETH", "f": "0.80000000", "l": "0.10000000"},
            ],
        },
    }


def _market_kline(interval: Interval, open_time: datetime) -> Kline:
    """
    함수 이름: _market_kline()
    기능: MarketSnapshot에 현재 ETH 가격을 공급할 open Kline을 만든다.
    인자: interval -> canonical Kline 주기
        open_time -> snapshot 시각을 포함하는 봉 시작 UTC
    반환값: 현재가를 close로 갖는 불변 Kline
    작성 날짜: 2026/08/21
    """
    return Kline(
        symbol="ETHUSDT",
        interval=interval,
        open_time=open_time,
        open=CURRENT_ETH_PRICE,
        high=CURRENT_ETH_PRICE + Decimal("1"),
        low=CURRENT_ETH_PRICE - Decimal("1"),
        close=CURRENT_ETH_PRICE,
        volume=Decimal("1"),
        closed=False,
    )


def _ready_market_snapshot() -> MarketSnapshot:
    """
    함수 이름: _ready_market_snapshot()
    기능: 네 주기와 current ETH price가 준비된 MarketSnapshot을 만든다.
    인자: 없음
    반환값: version 1의 ready MarketSnapshot
    작성 날짜: 2026/08/21
    """
    open_times = {
        Interval.ONE_MINUTE: MARKET_UPDATED_AT,
        Interval.THIRTY_MINUTES: MARKET_UPDATED_AT,
        Interval.FOUR_HOURS: datetime(
            2026,
            8,
            21,
            0,
            0,
            tzinfo=timezone.utc,
        ),
        Interval.ONE_DAY: datetime(
            2026,
            8,
            21,
            0,
            0,
            tzinfo=timezone.utc,
        ),
    }
    snapshot = MarketSnapshot(clock=lambda: MARKET_UPDATED_AT)
    snapshot.update(
        {
            interval: (_market_kline(interval, open_times[interval]),)
            for interval in SUPPORTED_INTERVALS
        }
    )

    return snapshot


class FakeSubscription:
    """
    클래스 이름: FakeSubscription
    기능: TradingController 통합 시험의 account stream handle을 표현한다.
    작성 날짜: 2026/08/21
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 종료되지 않은 구독 상태를 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.closed = False

    def close(self) -> None:
        """
        함수 이름: close()
        기능: fake account stream을 종료된 상태로 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.closed = True


class FakeAccountRESTClient:
    """
    클래스 이름: FakeAccountRESTClient
    기능: 계좌 REST 호출 순서와 성공·실패 응답을 제어한다.
    작성 날짜: 2026/08/21
    """

    def __init__(
        self,
        external_trace: list[str],
        integration_trace: list[TraceEntry] | None = None,
        account_version: Callable[[], int] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 외부 호출 trace와 정상 account payload를 초기화한다.
        인자: external_trace -> REST, Account, WS 순서를 기록할 목록
            integration_trace -> 구조화된 Communication trace 또는 None
            account_version -> Account state version을 읽는 callable 또는 None
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.external_trace = external_trace
        self.integration_trace = integration_trace
        self.account_version = account_version
        self.account_payload: object = _account_rest_payload()

    def get_account(self) -> object:
        """
        함수 이름: get_account()
        기능: REST 호출을 trace하고 설정된 payload 또는 장애를 반환한다.
        인자: 없음
        반환값: Binance account REST payload
        작성 날짜: 2026/08/21
        """
        trace_entry: TraceEntry | None = None
        if (
            self.integration_trace is not None
            and self.account_version is not None
        ):
            trace_entry = start_trace_entry(
                self.integration_trace,
                message_id="2.1.1",
                caller="APIGateway",
                receiver="BinanceRESTAPI",
                command_event_id=ACCOUNT_LOAD_COMMAND_ID,
                state_version_before=self.account_version(),
                related_id="ETH",
            )

        try:
            self.external_trace.append("rest:fetch")
            if isinstance(self.account_payload, BaseException):
                raise self.account_payload
        except Exception as error:
            if trace_entry is not None and self.account_version is not None:
                finish_trace_entry(
                    trace_entry,
                    state_version_after=self.account_version(),
                    error=error,
                )
            raise

        if trace_entry is not None and self.account_version is not None:
            finish_trace_entry(
                trace_entry,
                state_version_after=self.account_version(),
            )

        return self.account_payload

    def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> object:
        """
        함수 이름: get_klines()
        기능: account load slice가 market REST를 재초기화하는 오류를 차단한다.
        인자: symbol -> 예상하지 않은 market symbol
            interval -> 예상하지 않은 Kline interval
            limit -> 예상하지 않은 조회 개수
        반환값: 정상 경로에서 반환하지 않음
        작성 날짜: 2026/08/21
        """
        raise AssertionError(
            f"unexpected Kline request: {symbol} {interval} {limit}"
        )


class SynchronousAccountWebSocketClient:
    """
    클래스 이름: SynchronousAccountWebSocketClient
    기능: subscribe 호출 중 account event를 동기 발생시켜 순서 계약을 검증한다.
    작성 날짜: 2026/08/21
    """

    def __init__(
        self,
        external_trace: list[str],
        integration_trace: list[TraceEntry] | None = None,
        account_version: Callable[[], int] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 외부 호출 trace와 동기 event, optional 시작 장애를 초기화한다.
        인자: external_trace -> REST, Account, WS 순서를 기록할 목록
            integration_trace -> 구조화된 Communication trace 또는 None
            account_version -> Account state version을 읽는 callable 또는 None
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.external_trace = external_trace
        self.integration_trace = integration_trace
        self.account_version = account_version
        self.start_error: Exception | None = None
        self.synchronous_payload: object | None = _account_stream_payload()
        self.on_disconnect: Callable[[], None] | None = None

    def subscribe_account_info(
        self,
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> FakeSubscription:
        """
        함수 이름: subscribe_account_info()
        기능: WS start를 trace하고 반환 전 account event callback을 동기 실행한다.
        인자: on_message -> account event callback
            on_disconnect -> account stream 종료 callback
        반환값: fake account stream 구독 handle
        작성 날짜: 2026/08/21
        """
        trace_entry: TraceEntry | None = None
        if (
            self.integration_trace is not None
            and self.account_version is not None
        ):
            trace_entry = start_trace_entry(
                self.integration_trace,
                message_id="2.2.1",
                caller="WebSocketGateway",
                receiver="BinanceWebSocket",
                command_event_id=ACCOUNT_LOAD_COMMAND_ID,
                state_version_before=self.account_version(),
                related_id="ETH",
            )

        try:
            self.external_trace.append("ws:start")
            if self.start_error is not None:
                raise self.start_error

            self.on_disconnect = on_disconnect
            if self.synchronous_payload is not None:
                on_message(self.synchronous_payload)
        except Exception as error:
            if trace_entry is not None and self.account_version is not None:
                finish_trace_entry(
                    trace_entry,
                    state_version_after=self.account_version(),
                    error=error,
                )
            raise

        if trace_entry is not None and self.account_version is not None:
            finish_trace_entry(
                trace_entry,
                state_version_after=self.account_version(),
            )

        return FakeSubscription()

    def subscribe_all_kline_streams(
        self,
        *,
        symbol: str,
        intervals: tuple[str, ...],
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> FakeSubscription:
        """
        함수 이름: subscribe_all_kline_streams()
        기능: account load slice에서 Kline stream 재구독을 금지한다.
        인자: symbol -> 예상하지 않은 market symbol
            intervals -> 예상하지 않은 Kline interval들
            on_message -> 예상하지 않은 message callback
            on_disconnect -> 예상하지 않은 disconnect callback
        반환값: 정상 경로에서 반환하지 않음
        작성 날짜: 2026/08/21
        """
        raise AssertionError(
            f"unexpected Kline subscription: {symbol} {intervals}"
        )


class TracingAccount(Account):
    """
    클래스 이름: TracingAccount
    기능: 실제 Account 동작을 유지하며 snapshot 적용 순서를 기록한다.
    작성 날짜: 2026/08/21
    """

    def __init__(self, external_trace: list[str]) -> None:
        """
        함수 이름: __init__()
        기능: ETH 평가 Account와 외부 호출 trace를 초기화한다.
        인자: external_trace -> snapshot 적용 순서를 기록할 목록
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        super().__init__(valuation_asset="ETH")
        self.external_trace = external_trace

    def apply_initial_snapshot(
        self,
        snapshot: AccountSnapshot,
        current_price: Decimal,
    ) -> None:
        """
        함수 이름: apply_initial_snapshot()
        기능: initial commit 순서를 기록하고 실제 Account 규칙으로 적용한다.
        인자: snapshot -> REST에서 정규화한 full account snapshot
            current_price -> MarketSnapshot의 ETH 현재가
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.external_trace.append("account:initial")
        super().apply_initial_snapshot(snapshot, current_price)

    def apply_stream_snapshot(self, snapshot: AccountSnapshot) -> bool:
        """
        함수 이름: apply_stream_snapshot()
        기능: stream patch 순서를 기록하고 실제 Account 규칙으로 적용한다.
        인자: snapshot -> WebSocket에서 정규화한 partial account snapshot
        반환값: snapshot이 새 상태로 적용됐는지 여부
        작성 날짜: 2026/08/21
        """
        self.external_trace.append("account:stream")
        return super().apply_stream_snapshot(snapshot)


class AccountStreamFlowTests(unittest.TestCase):
    """
    클래스 이름: AccountStreamFlowTests
    기능: Account startup과 selected REGIME TradingSTM 선택의 Communication 흐름을 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_message_6_1_1_1_selects_exact_trading_logic_without_fallback(
        self,
    ) -> None:
        """
        함수 이름: test_message_6_1_1_1_selects_exact_trading_logic_without_fallback()
        기능: 메시지 6.1.1.1이 TYPE_0 새 STM만 반환하고 TYPE_1~4를 typed 오류로 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        account = Account()
        web_socket_client = SynchronousAccountWebSocketClient([])
        controller = TradingController(
            APIGateway(FakeAccountRESTClient([])),
            WebSocketGateway(
                web_socket_client,
                account_snapshot_callback=account.apply_stream_snapshot,
            ),
            account,
            _ready_market_snapshot(),
        )

        # 선택 요청마다 session 전용 STM을 만들되 같은 immutable registry를 공유한다.
        first = controller.fetch_selected_trading_logic(RegimeType.TYPE_0)
        second = controller.fetch_selected_trading_logic(RegimeType.TYPE_0)

        self.assertIsNot(first, second)
        self.assertIs(first.configuration, second.configuration)
        self.assertEqual(109, len(first.configuration.transition_ids))
        self.assertIs(TradingLogicStartGuard.READY, first.configuration.start_guard)

        for regime_type in tuple(RegimeType)[1:]:
            with self.subTest(regime_type=regime_type):
                with self.assertRaises(UnsupportedTradingLogicError) as raised:
                    controller.fetch_selected_trading_logic(regime_type)
                self.assertEqual(
                    "UNSUPPORTED_TRADING_LOGIC",
                    raised.exception.code,
                )

        with self.assertRaises(TypeError):
            controller.fetch_selected_trading_logic("type0")  # type: ignore[arg-type]

    def test_account_startup_and_stream_trace_applies_rest_before_delta(self) -> None:
        """
        함수 이름: test_account_startup_and_stream_trace_applies_rest_before_delta()
        기능: 메시지 2~2.2.1 trace와 fetch→apply→WS의 동기 delta 순서를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        external_trace: list[str] = []
        integration_trace: list[TraceEntry] = []
        account = TracingAccount(external_trace)

        def account_version() -> int:
            """
            함수 이름: account_version()
            기능: trace 경계에서 실제 Account version을 읽는다.
            인자: 없음
            반환값: 현재 Account version
            작성 날짜: 2026/08/21
            """
            return account.version

        rest_client = FakeAccountRESTClient(
            external_trace,
            integration_trace,
            account_version,
        )
        web_socket_client = SynchronousAccountWebSocketClient(
            external_trace,
            integration_trace,
            account_version,
        )
        market_snapshot = _ready_market_snapshot()
        api_gateway = APIGateway(rest_client)
        web_socket_gateway = WebSocketGateway(
            web_socket_client,
            account_snapshot_callback=account.apply_stream_snapshot,
        )
        controller = TradingController(
            api_gateway,
            web_socket_gateway,
            account,
            market_snapshot,
        )

        original_fetch_account_snapshot = (
            api_gateway.fetch_account_snapshot
        )
        original_start_account_info_stream = (
            web_socket_gateway.start_account_info_stream
        )

        def traced_fetch_account_snapshot(asset: str) -> AccountSnapshot:
            """
            함수 이름: traced_fetch_account_snapshot()
            기능: 실제 APIGateway 호출의 메시지 2.1 경계와 Account version을 기록한다.
            인자: asset -> 조회할 기준 asset
            반환값: 실제 Gateway가 정규화한 AccountSnapshot
            작성 날짜: 2026/08/21
            """
            trace_entry = start_trace_entry(
                integration_trace,
                message_id="2.1",
                caller="TradingController",
                receiver="APIGateway",
                command_event_id=ACCOUNT_LOAD_COMMAND_ID,
                state_version_before=account.version,
                related_id=asset,
            )
            try:
                snapshot = original_fetch_account_snapshot(asset)
            except Exception as error:
                finish_trace_entry(
                    trace_entry,
                    state_version_after=account.version,
                    error=error,
                )
                raise

            finish_trace_entry(
                trace_entry,
                state_version_after=account.version,
            )
            return snapshot

        def traced_start_account_info_stream() -> Subscription:
            """
            함수 이름: traced_start_account_info_stream()
            기능: 실제 WebSocketGateway 호출의 메시지 2.2 경계와 Account version을 기록한다.
            인자: 없음
            반환값: 실제 Gateway가 만든 account stream 구독 handle
            작성 날짜: 2026/08/21
            """
            trace_entry = start_trace_entry(
                integration_trace,
                message_id="2.2",
                caller="TradingController",
                receiver="WebSocketGateway",
                command_event_id=ACCOUNT_LOAD_COMMAND_ID,
                state_version_before=account.version,
                related_id="ETH",
            )
            try:
                subscription = original_start_account_info_stream()
            except Exception as error:
                finish_trace_entry(
                    trace_entry,
                    state_version_after=account.version,
                    error=error,
                )
                raise

            finish_trace_entry(
                trace_entry,
                state_version_after=account.version,
            )
            return subscription

        load_trace_entry = start_trace_entry(
            integration_trace,
            message_id="2",
            caller="UIStateController",
            receiver="TradingController",
            command_event_id=ACCOUNT_LOAD_COMMAND_ID,
            state_version_before=account.version,
            related_id="ETH",
        )
        try:
            with patch.object(
                api_gateway,
                "fetch_account_snapshot",
                side_effect=traced_fetch_account_snapshot,
            ), patch.object(
                web_socket_gateway,
                "start_account_info_stream",
                side_effect=traced_start_account_info_stream,
            ):
                controller.load_account("ETH")
        except Exception as error:
            finish_trace_entry(
                load_trace_entry,
                state_version_after=account.version,
                error=error,
            )
            raise

        finish_trace_entry(
            load_trace_entry,
            state_version_after=account.version,
        )

        assert_trace_contract(
            self,
            integration_trace,
            ("2", "2.1", "2.1.1", "2.2", "2.2.1"),
        )
        self.assertEqual(
            tuple(
                (
                    entry["state_version_before"],
                    entry["state_version_after"],
                )
                for entry in integration_trace
            ),
            ((0, 2), (0, 0), (0, 0), (1, 2), (1, 2)),
        )
        self.assertTrue(
            all(
                entry["command_event_id"] == ACCOUNT_LOAD_COMMAND_ID
                and entry["related_id"] == "ETH"
                for entry in integration_trace
            )
        )

        self.assertEqual(
            external_trace,
            ["rest:fetch", "account:initial", "ws:start", "account:stream"],
        )
        self.assertTrue(account.ready)
        self.assertEqual(account.version, 2)
        self.assertEqual(account.get_holdings("ETH"), Decimal("0.90000000"))
        self.assertEqual(account.current_price, CURRENT_ETH_PRICE)
        self.assertEqual(account.valuation, Decimal("2250.4500000000"))
        self.assertEqual(
            account.balances["USDT"].free,
            Decimal("100.00"),
        )
        self.assertEqual(
            account.updated_at,
            datetime(2026, 8, 21, 0, 1, tzinfo=timezone.utc),
        )

    def test_load_account_propagates_stream_start_failure_after_rest_commit(self) -> None:
        """
        함수 이름: test_load_account_propagates_stream_start_failure_after_rest_commit()
        기능: REST commit 후 account stream 시작 장애를 ready 성공으로 숨기지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        external_trace: list[str] = []
        rest_client = FakeAccountRESTClient(external_trace)
        web_socket_client = SynchronousAccountWebSocketClient(external_trace)
        web_socket_client.start_error = RuntimeError(
            "account stream startup failed"
        )
        account = TracingAccount(external_trace)
        web_socket_gateway = WebSocketGateway(
            web_socket_client,
            account_snapshot_callback=account.apply_stream_snapshot,
        )
        controller = TradingController(
            APIGateway(rest_client),
            web_socket_gateway,
            account,
            _ready_market_snapshot(),
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "account stream startup failed",
        ):
            controller.load_account("ETH")

        self.assertEqual(
            external_trace,
            ["rest:fetch", "account:initial", "ws:start"],
        )
        self.assertTrue(account.ready)
        self.assertEqual(account.version, 1)
        self.assertEqual(account.get_holdings("ETH"), Decimal("1.20000000"))
        self.assertIsNone(controller.account_subscription)

    def test_load_account_does_not_start_stream_when_rest_fails(self) -> None:
        """
        함수 이름: test_load_account_does_not_start_stream_when_rest_fails()
        기능: REST full snapshot 실패 시 Account commit과 WebSocket start를 수행하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        external_trace: list[str] = []
        rest_client = FakeAccountRESTClient(external_trace)
        rest_client.account_payload = RuntimeError("account REST failed")
        web_socket_client = SynchronousAccountWebSocketClient(external_trace)
        account = TracingAccount(external_trace)
        controller = TradingController(
            APIGateway(rest_client),
            WebSocketGateway(
                web_socket_client,
                account_snapshot_callback=account.apply_stream_snapshot,
            ),
            account,
            _ready_market_snapshot(),
        )

        with self.assertRaisesRegex(RuntimeError, "account REST failed"):
            controller.load_account("ETH")

        self.assertEqual(external_trace, ["rest:fetch"])
        self.assertFalse(account.ready)
        self.assertEqual(account.version, 0)


if __name__ == "__main__":
    unittest.main()
