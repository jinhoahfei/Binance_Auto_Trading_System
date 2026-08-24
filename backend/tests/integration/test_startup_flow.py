"""Communication 1~3 application startup 순서, 실패 격리와 lifecycle을 검증한다."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from binance_auto_trader.application import MarketDataController
from binance_auto_trader.bootstrap import (
    ApplicationRuntime,
    ApplicationStartupError,
    ApplicationStatus,
    StartupFailureCode,
    StartupStage,
    StartupTraceResult,
    close_application,
    create_application_runtime,
    start_application,
)
from binance_auto_trader.bootstrap.application import _FAKE_ORDER_CAPABILITY
from binance_auto_trader.domain.history import Trade
from binance_auto_trader.domain.regime import RegimeEvaluationTrigger
from binance_auto_trader.domain.trading import Account
from tests.unit.market.test_indicator_snapshot import (
    SNAPSHOT_UPDATED_AT,
    load_golden_vector,
    make_market_snapshot,
)


REST_UPDATED_AT_MILLISECONDS = 1_787_270_400_000


def _account_rest_payload(
    *,
    eth_free: str = "1.00000000",
    eth_locked: str = "0.20000000",
    update_time_milliseconds: int = REST_UPDATED_AT_MILLISECONDS,
) -> dict[str, object]:
    """
    함수 이름: _account_rest_payload()
    기능: startup Account 전체 적용에 사용할 공식 Spot REST payload를 만든다.
    인자: eth_free -> ETH free 잔액 문자열
        eth_locked -> ETH locked 잔액 문자열
        update_time_milliseconds -> REST account source timestamp
    반환값: updateTime과 ETH·USDT 잔액이 있는 account payload
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
        "updateTime": update_time_milliseconds,
        "accountType": "SPOT",
        "balances": [
            {
                "asset": "ETH",
                "free": eth_free,
                "locked": eth_locked,
            },
            {
                "asset": "USDT",
                "free": "100.00",
                "locked": "10.00",
            },
        ],
        "permissions": ["SPOT"],
    }


def _account_stream_payload(
    free: str,
    locked: str,
    update_time_milliseconds: int = REST_UPDATED_AT_MILLISECONDS,
) -> dict[str, object]:
    """
    함수 이름: _account_stream_payload()
    기능: observer 변경 여부를 검증할 공식 outboundAccountPosition envelope를 만든다.
    인자: free -> ETH free 잔액 문자열
        locked -> ETH locked 잔액 문자열
        update_time_milliseconds -> account patch source timestamp
    반환값: WebSocketGateway가 정규화할 공식 event envelope
    작성 날짜: 2026/08/21
    """
    return {
        "subscriptionId": 3,
        "event": {
            "e": "outboundAccountPosition",
            "E": update_time_milliseconds + 100,
            "u": update_time_milliseconds,
            "B": [
                {
                    "a": "ETH",
                    "f": free,
                    "l": locked,
                }
            ],
        },
    }


class _TrackingSubscription:
    """
    클래스 이름: _TrackingSubscription
    기능: raw WebSocket subscription의 실제 close 호출 횟수를 기록한다.
    작성 날짜: 2026/08/21
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 종료 호출이 없는 초기 subscription을 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.close_count = 0

    def close(self) -> None:
        """
        함수 이름: close()
        기능: transport close 요청 횟수를 한 번 증가시킨다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.close_count += 1


class _StartupRestClient:
    """
    클래스 이름: _StartupRestClient
    기능: account REST 단계의 순서, readiness 관측과 선택 실패를 제어한다.
    작성 날짜: 2026/08/21
    """

    def __init__(
        self,
        operation_trace: list[str],
        readiness_observations: list[bool] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 호출 trace, optional ready 관측과 정상 account payload를 보존한다.
        인자: operation_trace -> startup 외부 호출 순서를 기록할 목록
            readiness_observations -> 단계 중 runtime.ready 관측 목록 또는 None
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.operation_trace = operation_trace
        self.readiness_observations = readiness_observations
        self.ready_probe: Callable[[], bool] | None = None
        self.account_error: BaseException | None = None

    def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> object:
        """
        함수 이름: get_klines()
        기능: lifecycle test가 market Controller patch를 우회하지 못하게 즉시 실패한다.
        인자: symbol -> 예상하지 않은 symbol
            interval -> 예상하지 않은 interval
            limit -> 예상하지 않은 limit
        반환값: 정상 반환 없이 AssertionError 발생
        작성 날짜: 2026/08/21
        """
        raise AssertionError(
            f"unexpected Kline request: {symbol} {interval} {limit}"
        )

    def get_account(self) -> object:
        """
        함수 이름: get_account()
        기능: account startup 순서와 ready 비공개를 기록한 뒤 payload 또는 장애를 반환한다.
        인자: 없음
        반환값: 정상 Spot account REST payload
        작성 날짜: 2026/08/21
        """
        self.operation_trace.append("account")
        self._observe_ready()
        if self.account_error is not None:
            raise self.account_error

        return _account_rest_payload()

    def list_open_order_results(
        self,
        *,
        symbol: str,
    ) -> tuple[object, ...]:
        """
        함수 이름: list_open_order_results()
        기능: startup fake 계좌에 앱 소유 미결 주문이 없음을 반환한다.
        인자: symbol -> 조회할 Spot 상품
        반환값: 비어 있는 normalized 주문 결과 tuple
        작성 날짜: 2026/08/23
        """
        if symbol != "ETHUSDT":
            raise AssertionError("unexpected open-order symbol")

        return ()  # Fake startup fixture에는 복구할 외부 주문이 없다.

    def list_recent_order_results(
        self,
        *,
        symbol: str,
        limit: int,
    ) -> tuple[object, ...]:
        """
        함수 이름: list_recent_order_results()
        기능: startup fake 계좌에 앱 소유 최근 체결이 없음을 반환한다.
        인자: symbol -> 조회할 Spot 상품
            limit -> 조회할 최대 주문 수
        반환값: 비어 있는 normalized 주문 결과 tuple
        작성 날짜: 2026/08/23
        """
        if symbol != "ETHUSDT" or limit != 100:
            raise AssertionError("unexpected recent-order request")

        return ()  # Reconnect full reconciliation의 빈 exchange 사실을 명시한다.

    def _observe_ready(self) -> None:
        """
        함수 이름: _observe_ready()
        기능: probe가 구성된 경우 현재 application ready 값을 test 목록에 추가한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if (
            self.readiness_observations is not None
            and self.ready_probe is not None
        ):
            self.readiness_observations.append(self.ready_probe())


class _StartupWebSocketClient:
    """
    클래스 이름: _StartupWebSocketClient
    기능: account stream 시작 순서, callback과 raw subscription을 보존한다.
    작성 날짜: 2026/08/21
    """

    def __init__(
        self,
        operation_trace: list[str],
        readiness_observations: list[bool] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: trace, optional ready 관측과 빈 callback·subscription 목록을 만든다.
        인자: operation_trace -> startup 외부 호출 순서를 기록할 목록
            readiness_observations -> 단계 중 runtime.ready 관측 목록 또는 None
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.operation_trace = operation_trace
        self.readiness_observations = readiness_observations
        self.ready_probe: Callable[[], bool] | None = None
        self.account_message_callback: Callable[[object], None] | None = None
        self.account_disconnect_callback: Callable[[], None] | None = None
        self.raw_subscriptions: list[_TrackingSubscription] = []

    def subscribe_all_kline_streams(
        self,
        *,
        symbol: str,
        intervals: tuple[str, ...],
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> _TrackingSubscription:
        """
        함수 이름: subscribe_all_kline_streams()
        기능: lifecycle test가 patched market 경계를 우회하면 즉시 실패한다.
        인자: symbol -> 예상하지 않은 symbol
            intervals -> 예상하지 않은 interval tuple
            on_message -> 예상하지 않은 callback
            on_disconnect -> 예상하지 않은 disconnect callback
        반환값: 정상 반환 없이 AssertionError 발생
        작성 날짜: 2026/08/21
        """
        raise AssertionError(
            f"unexpected Kline subscription: {symbol} {intervals}"
        )

    def subscribe_account_info(
        self,
        *,
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> _TrackingSubscription:
        """
        함수 이름: subscribe_account_info()
        기능: account stream 순서와 callback을 저장하고 새 raw subscription을 반환한다.
        인자: on_message -> account event callback
            on_disconnect -> account disconnect callback
        반환값: close 횟수를 추적하는 raw subscription
        작성 날짜: 2026/08/21
        """
        self.operation_trace.append("account_stream")
        self._observe_ready()
        self.account_message_callback = on_message
        self.account_disconnect_callback = on_disconnect
        raw_subscription = _TrackingSubscription()
        self.raw_subscriptions.append(raw_subscription)

        return raw_subscription

    def emit_account_event(self, payload: object) -> None:
        """
        함수 이름: emit_account_event()
        기능: 저장한 현재 account callback에 원본 WebSocket event를 전달한다.
        인자: payload -> WebSocketGateway가 정규화할 event payload
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if self.account_message_callback is None:
            raise RuntimeError("account stream callback is not configured")

        self.account_message_callback(payload)

    def _observe_ready(self) -> None:
        """
        함수 이름: _observe_ready()
        기능: probe가 구성된 경우 현재 application ready 값을 test 목록에 추가한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if (
            self.readiness_observations is not None
            and self.ready_probe is not None
        ):
            self.readiness_observations.append(self.ready_probe())


class _StartupHistoryRepository:
    """
    클래스 이름: _StartupHistoryRepository
    기능: history startup 순서, readiness 관측과 선택 실패를 제어한다.
    작성 날짜: 2026/08/21
    """

    def __init__(
        self,
        operation_trace: list[str],
        readiness_observations: list[bool] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: trace, optional ready 관측과 빈 정상 history를 보존한다.
        인자: operation_trace -> startup 외부 호출 순서를 기록할 목록
            readiness_observations -> 단계 중 runtime.ready 관측 목록 또는 None
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.operation_trace = operation_trace
        self.readiness_observations = readiness_observations
        self.ready_probe: Callable[[], bool] | None = None
        self.history_error: BaseException | None = None

    def get_trade_history(self) -> tuple[Trade, ...]:
        """
        함수 이름: get_trade_history()
        기능: history startup 순서와 ready 비공개를 기록한 뒤 거래 또는 장애를 반환한다.
        인자: 없음
        반환값: 정상 startup의 빈 Trade tuple
        작성 날짜: 2026/08/21
        """
        self.operation_trace.append("history")
        self._observe_ready()
        if self.history_error is not None:
            raise self.history_error

        return ()

    def _observe_ready(self) -> None:
        """
        함수 이름: _observe_ready()
        기능: probe가 구성된 경우 현재 application ready 값을 test 목록에 추가한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if (
            self.readiness_observations is not None
            and self.ready_probe is not None
        ):
            self.readiness_observations.append(self.ready_probe())


class _MarketStartupBehavior:
    """
    클래스 이름: _MarketStartupBehavior
    기능: startup lifecycle test에서 market 준비, Regime 평가와 선택 실패를 제어한다.
    작성 날짜: 2026/08/21
    """

    def __init__(
        self,
        runtime: ApplicationRuntime,
        operation_trace: list[str],
        readiness_observations: list[bool] | None = None,
        *,
        prepare_market: bool = True,
        evaluate_regime: bool = True,
        market_error: BaseException | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: runtime, trace, 준비 단계 flag와 optional market 장애를 보존한다.
        인자: runtime -> 준비할 MarketSnapshot과 RegimeController runtime
            operation_trace -> startup 단계 순서를 기록할 목록
            readiness_observations -> 단계 중 runtime.ready 관측 목록 또는 None
            prepare_market -> MarketSnapshot을 ready로 갱신할지 여부
            evaluate_regime -> 최초 Regime 평가를 실행할지 여부
            market_error -> market 호출에서 발생시킬 예외 또는 None
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.runtime = runtime
        self.operation_trace = operation_trace
        self.readiness_observations = readiness_observations
        self.prepare_market = prepare_market
        self.evaluate_regime = evaluate_regime
        self.market_error = market_error

    def __call__(
        self,
        controller: MarketDataController,
        symbol: str = "ETHUSDT",
    ) -> object:
        """
        함수 이름: __call__()
        기능: market 단계 진입을 기록하고 golden snapshot과 optional Regime 결과를 만든다.
        인자: controller -> patched runtime MarketDataController
            symbol -> startup이 요청한 symbol
        반환값: runtime의 동일 MarketSnapshot
        작성 날짜: 2026/08/21
        """
        if controller is not self.runtime.market_data_controller:
            raise AssertionError("unexpected MarketDataController identity")
        if symbol != self.runtime.market_snapshot.symbol:
            raise AssertionError("unexpected market symbol")

        self.operation_trace.append("market")
        self._observe_ready()
        if self.market_error is not None:
            raise self.market_error
        if not self.prepare_market:
            return self.runtime.market_snapshot

        # Golden vector의 validated Kline을 runtime의 authoritative identity에 적용한다.
        golden_snapshot = make_market_snapshot(load_golden_vector())
        self.runtime.market_snapshot.update(
            golden_snapshot.klines_by_interval
        )
        if self.evaluate_regime:
            self.runtime.regime_controller.evaluate_regime(
                RegimeEvaluationTrigger.INITIAL,
                self.runtime.market_snapshot,
            )
        self._observe_ready()

        return self.runtime.market_snapshot

    def _observe_ready(self) -> None:
        """
        함수 이름: _observe_ready()
        기능: 관측 목록이 있으면 현재 runtime ready 값을 추가한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if self.readiness_observations is not None:
            self.readiness_observations.append(self.runtime.ready)


def _create_test_runtime(
    operation_trace: list[str],
    readiness_observations: list[bool] | None = None,
    account_update_observer: Callable[[Account], object] | None = None,
    trade_history_update_observer: Callable[[object, object], object] | None = None,
    trading_session_update_observer: Callable[[object, object], object] | None = None,
) -> tuple[
    ApplicationRuntime,
    _StartupRestClient,
    _StartupWebSocketClient,
    _StartupHistoryRepository,
]:
    """
    함수 이름: _create_test_runtime()
    기능: startup integration test가 공유하는 client, repository와 runtime을 조립한다.
    인자: operation_trace -> startup 단계 순서를 기록할 목록
        readiness_observations -> 단계 중 runtime.ready 관측 목록 또는 None
        account_update_observer -> 실제 Account 변경 관측 callback 또는 None
        trade_history_update_observer -> durable Trade와 Performance 관측 callback 또는 None
        trading_session_update_observer -> background session lifecycle 관측 callback 또는 None
    반환값: runtime과 제어 가능한 세 fake boundary tuple
    작성 날짜: 2026/08/21
    """
    rest_client = _StartupRestClient(
        operation_trace,
        readiness_observations,
    )
    web_socket_client = _StartupWebSocketClient(
        operation_trace,
        readiness_observations,
    )
    history_repository = _StartupHistoryRepository(
        operation_trace,
        readiness_observations,
    )
    runtime = create_application_runtime(
        rest_client,
        web_socket_client,
        history_repository=history_repository,
        execution_mode="fake",
        _fake_order_capability=_FAKE_ORDER_CAPABILITY,
        account_update_observer=account_update_observer,
        trade_history_update_observer=trade_history_update_observer,
        trading_session_update_observer=trading_session_update_observer,
        clock=lambda: SNAPSHOT_UPDATED_AT,
    )

    # Runtime 생성 후 준비된 probe는 각 단계가 ready를 조기 공개하는지 관측한다.
    rest_client.ready_probe = lambda: runtime.ready
    web_socket_client.ready_probe = lambda: runtime.ready
    history_repository.ready_probe = lambda: runtime.ready

    return runtime, rest_client, web_socket_client, history_repository


class ApplicationStartupFlowTests(unittest.TestCase):
    """
    클래스 이름: ApplicationStartupFlowTests
    기능: startup 정상 순서, typed failure, observer와 subscription lifecycle을 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_startup_runs_exact_order_and_publishes_ready_only_at_end(
        self,
    ) -> None:
        """
        함수 이름: test_startup_runs_exact_order_and_publishes_ready_only_at_end()
        기능: market→account stream→history→gap REST 순서와 최종 READY만 공개함을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        operation_trace: list[str] = []
        readiness_observations: list[bool] = []
        runtime, _, _, _ = _create_test_runtime(
            operation_trace,
            readiness_observations,
        )
        market_behavior = _MarketStartupBehavior(
            runtime,
            operation_trace,
            readiness_observations,
        )

        # 실제 lifecycle만 호출하고 market I/O slice는 validated golden state로 대체한다.
        with patch.object(
            MarketDataController,
            "initialize_market_data",
            autospec=True,
            side_effect=market_behavior,
        ):
            ready_state = start_application(runtime)

        self.assertEqual(
            operation_trace,
            [
                "market",
                "account",
                "account_stream",
                "history",
                "account",
            ],
        )
        self.assertTrue(readiness_observations)
        self.assertTrue(all(not value for value in readiness_observations))
        self.assertIs(ready_state.status, ApplicationStatus.READY)
        self.assertTrue(runtime.ready)
        self.assertTrue(runtime.market_snapshot.ready)
        self.assertTrue(runtime.account.ready)
        self.assertIsNotNone(runtime.regime_controller.last_regime_result)
        self.assertIsNotNone(runtime.regime_controller.recommended_regime)
        self.assertIsNotNone(runtime.regime_controller.indicator_snapshot)
        self.assertTrue(runtime.regime_controller.indicator_snapshot.ready)
        self.assertEqual(runtime.trade_history.trades, ())

        # Top-level Communication trace는 raw payload 없이 메시지 1, 2, 3만 보존한다.
        self.assertEqual(
            tuple(entry.message_id for entry in runtime.startup_trace),
            ("1", "2", "3"),
        )
        self.assertEqual(
            tuple(entry.receiver for entry in runtime.startup_trace),
            (
                "MarketDataController",
                "TradingController",
                "TradeHistoryController",
            ),
        )
        self.assertTrue(
            all(
                entry.caller == "UIStateController"
                and entry.result is StartupTraceResult.SUCCESS
                and entry.failure_code is None
                for entry in runtime.startup_trace
            )
        )
        self.assertEqual(
            tuple(
                (entry.version_before, entry.version_after)
                for entry in runtime.startup_trace
            ),
            ((0, 1), (0, 1), (3, 4)),
        )
        serialized_trace = repr(runtime.startup_trace).lower()
        for secret_marker in (
            "api_key",
            "credential",
            "secret",
            "signature",
            "balances",
        ):
            self.assertNotIn(secret_marker, serialized_trace)

        # 이미 READY인 runtime startup은 Controller와 repository를 다시 호출하지 않는다.
        second_state = start_application(runtime)
        self.assertIs(second_state, ready_state)
        self.assertEqual(
            operation_trace,
            [
                "market",
                "account",
                "account_stream",
                "history",
                "account",
            ],
        )

    def test_startup_second_account_snapshot_closes_stream_start_gap(
        self,
    ) -> None:
        """
        함수 이름: test_startup_second_account_snapshot_closes_stream_start_gap()
        기능: stream ACK 전후 잔고 변경을 두 번째 REST와 이후 최신 patch 순서로 반영하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        operation_trace: list[str] = []
        runtime, rest_client, web_socket_client, _ = _create_test_runtime(
            operation_trace
        )
        market_behavior = _MarketStartupBehavior(
            runtime,
            operation_trace,
        )
        original_get_account = rest_client.get_account
        account_call_count = 0

        def get_account_with_gap_event() -> object:
            """
            함수 이름: get_account_with_gap_event()
            기능: 두 번째 REST 직전에 stream patch를 발생시키고 더 최신 full payload를 반환한다.
            인자: 없음
            반환값: 호출 순서에 따른 첫 또는 두 번째 account payload
            작성 날짜: 2026/08/22
            """
            nonlocal account_call_count
            account_call_count += 1
            original_payload = original_get_account()
            if account_call_count == 1:
                return original_payload

            # 구독 시작 뒤 REST #2 전에 도착한 patch보다 새 full snapshot의 source time이 더 최신이다.
            web_socket_client.emit_account_event(
                _account_stream_payload(
                    "0.80000000",
                    "0.10000000",
                    REST_UPDATED_AT_MILLISECONDS + 1_000,
                )
            )
            return _account_rest_payload(
                eth_free="0.70000000",
                eth_locked="0.10000000",
                update_time_milliseconds=(
                    REST_UPDATED_AT_MILLISECONDS + 2_000
                ),
            )

        rest_client.get_account = get_account_with_gap_event
        with patch.object(
            MarketDataController,
            "initialize_market_data",
            autospec=True,
            side_effect=market_behavior,
        ):
            start_application(runtime)

        self.assertEqual(account_call_count, 2)
        self.assertEqual(runtime.account.get_holdings("ETH"), Decimal("0.80000000"))

        # READY 뒤의 더 최신 stream patch는 REST #2보다 앞선 상태로 되돌아가지 않고 정상 적용된다.
        web_socket_client.emit_account_event(
            _account_stream_payload(
                "0.60000000",
                "0.10000000",
                REST_UPDATED_AT_MILLISECONDS + 3_000,
            )
        )
        self.assertEqual(runtime.account.get_holdings("ETH"), Decimal("0.70000000"))

    def test_market_exception_returns_typed_failure_without_ready(self) -> None:
        """
        함수 이름: test_market_exception_returns_typed_failure_without_ready()
        기능: market 단계 예외가 account/history를 실행하지 않고 typed failure가 되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        operation_trace: list[str] = []
        runtime, _, _, _ = _create_test_runtime(operation_trace)
        market_behavior = _MarketStartupBehavior(
            runtime,
            operation_trace,
            market_error=RuntimeError("controlled market failure"),
        )

        with patch.object(
            MarketDataController,
            "initialize_market_data",
            autospec=True,
            side_effect=market_behavior,
        ), self.assertRaises(ApplicationStartupError) as error_context:
            start_application(runtime)

        self.assertIs(
            error_context.exception.code,
            StartupFailureCode.MARKET_INITIALIZATION_FAILED,
        )
        self.assertIs(error_context.exception.stage, StartupStage.MARKET)
        self.assertEqual(operation_trace, ["market"])
        self.assertFalse(runtime.ready)
        self.assertIs(runtime.state.status, ApplicationStatus.FAILED)
        self.assertEqual(len(runtime.startup_trace), 1)
        self.assertIs(
            runtime.startup_trace[0].result,
            StartupTraceResult.FAILURE,
        )

    def test_market_and_regime_readiness_are_checked_explicitly(self) -> None:
        """
        함수 이름: test_market_and_regime_readiness_are_checked_explicitly()
        기능: ready 없는 market 반환과 None Regime 결과를 서로 다른 typed failure로 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        cases = (
            (
                False,
                False,
                StartupFailureCode.MARKET_NOT_READY,
                StartupStage.MARKET,
            ),
            (
                True,
                False,
                StartupFailureCode.REGIME_NOT_READY,
                StartupStage.REGIME,
            ),
        )

        # Controller가 예외를 삼키거나 불완전 state를 반환해도 startup은 fail closed한다.
        for (
            prepare_market,
            evaluate_regime,
            expected_code,
            expected_stage,
        ) in cases:
            with self.subTest(expected_code=expected_code):
                operation_trace: list[str] = []
                runtime, _, _, _ = _create_test_runtime(operation_trace)
                market_behavior = _MarketStartupBehavior(
                    runtime,
                    operation_trace,
                    prepare_market=prepare_market,
                    evaluate_regime=evaluate_regime,
                )

                with patch.object(
                    MarketDataController,
                    "initialize_market_data",
                    autospec=True,
                    side_effect=market_behavior,
                ), self.assertRaises(
                    ApplicationStartupError
                ) as error_context:
                    start_application(runtime)

                self.assertIs(error_context.exception.code, expected_code)
                self.assertIs(error_context.exception.stage, expected_stage)
                self.assertEqual(operation_trace, ["market"])
                self.assertFalse(runtime.ready)
                self.assertIs(runtime.failure.code, expected_code)

    def test_account_exception_stops_before_history_and_keeps_not_ready(
        self,
    ) -> None:
        """
        함수 이름: test_account_exception_stops_before_history_and_keeps_not_ready()
        기능: account REST 실패가 history를 실행하지 않고 메시지 2 failure로 남는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        operation_trace: list[str] = []
        runtime, rest_client, _, _ = _create_test_runtime(operation_trace)
        rest_client.account_error = RuntimeError("controlled account failure")
        market_behavior = _MarketStartupBehavior(runtime, operation_trace)

        with patch.object(
            MarketDataController,
            "initialize_market_data",
            autospec=True,
            side_effect=market_behavior,
        ), self.assertRaises(ApplicationStartupError) as error_context:
            start_application(runtime)

        self.assertIs(
            error_context.exception.code,
            StartupFailureCode.ACCOUNT_INITIALIZATION_FAILED,
        )
        self.assertEqual(operation_trace, ["market", "account"])
        self.assertFalse(runtime.ready)
        self.assertEqual(
            tuple(entry.message_id for entry in runtime.startup_trace),
            ("1", "2"),
        )
        self.assertIs(
            runtime.startup_trace[-1].result,
            StartupTraceResult.FAILURE,
        )

    def test_account_not_ready_return_is_rejected_before_history(self) -> None:
        """
        함수 이름: test_account_not_ready_return_is_rejected_before_history()
        기능: TradingController가 Account를 갱신하지 않고 반환하면 ACCOUNT_NOT_READY인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        operation_trace: list[str] = []
        runtime, _, _, _ = _create_test_runtime(operation_trace)
        market_behavior = _MarketStartupBehavior(runtime, operation_trace)

        # Instance method를 대체해 lifecycle의 명시적 Account.ready 검사를 직접 관측한다.
        with patch.object(
            MarketDataController,
            "initialize_market_data",
            autospec=True,
            side_effect=market_behavior,
        ), patch.object(
            runtime.trading_controller,
            "load_account",
            return_value=runtime.account,
        ), self.assertRaises(ApplicationStartupError) as error_context:
            start_application(runtime)

        self.assertIs(
            error_context.exception.code,
            StartupFailureCode.ACCOUNT_NOT_READY,
        )
        self.assertFalse(runtime.account.ready)
        self.assertFalse(runtime.ready)
        self.assertEqual(operation_trace, ["market"])

    def test_ready_account_without_subscription_is_rejected_before_history(
        self,
    ) -> None:
        """
        함수 이름: test_ready_account_without_subscription_is_rejected_before_history()
        기능: REST Account만 ready이고 stream handle이 없으면 메시지 2를 실패 처리하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        operation_trace: list[str] = []
        runtime, _, _, _ = _create_test_runtime(operation_trace)
        market_behavior = _MarketStartupBehavior(runtime, operation_trace)

        def load_ready_account_without_subscription() -> Account:
            """
            함수 이름: load_ready_account_without_subscription()
            기능: REST snapshot을 적용하되 account stream handle을 의도적으로 만들지 않는다.
            인자: 없음
            반환값: ready Account
            작성 날짜: 2026/08/21
            """
            account_snapshot = runtime.api_gateway.fetch_account_snapshot()
            current_price = runtime.market_snapshot.get_current_eth_price()
            runtime.account.apply_initial_snapshot(
                account_snapshot,
                current_price,
            )
            return runtime.account  # 회귀 상황처럼 Account 자체는 ready로 반환한다.

        # Communication 2는 REST 적용과 stream 구독이 모두 끝나야 성공 trace를 남긴다.
        with patch.object(
            MarketDataController,
            "initialize_market_data",
            autospec=True,
            side_effect=market_behavior,
        ), patch.object(
            runtime.trading_controller,
            "load_account",
            side_effect=load_ready_account_without_subscription,
        ), self.assertRaises(ApplicationStartupError) as error_context:
            start_application(runtime)

        self.assertIs(
            error_context.exception.code,
            StartupFailureCode.ACCOUNT_NOT_READY,
        )
        self.assertTrue(runtime.account.ready)
        self.assertIsNone(runtime.trading_controller.account_subscription)
        self.assertFalse(runtime.ready)
        self.assertIs(runtime.state.status, ApplicationStatus.FAILED)
        self.assertEqual(operation_trace, ["market", "account"])
        self.assertEqual(
            tuple(entry.message_id for entry in runtime.startup_trace),
            ("1", "2"),
        )
        self.assertIs(
            runtime.startup_trace[-1].result,
            StartupTraceResult.FAILURE,
        )

    def test_history_failure_closes_open_account_subscription(self) -> None:
        """
        함수 이름: test_history_failure_closes_open_account_subscription()
        기능: history 실패 뒤 먼저 열린 account subscription을 닫고 READY를 숨기는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        operation_trace: list[str] = []
        runtime, _, web_socket_client, history_repository = (
            _create_test_runtime(operation_trace)
        )
        history_repository.history_error = RuntimeError(
            "controlled history failure"
        )
        market_behavior = _MarketStartupBehavior(runtime, operation_trace)

        with patch.object(
            MarketDataController,
            "initialize_market_data",
            autospec=True,
            side_effect=market_behavior,
        ), self.assertRaises(ApplicationStartupError) as error_context:
            start_application(runtime)

        self.assertIs(
            error_context.exception.code,
            StartupFailureCode.HISTORY_INITIALIZATION_FAILED,
        )
        self.assertEqual(
            operation_trace,
            ["market", "account", "account_stream", "history"],
        )
        self.assertEqual(len(web_socket_client.raw_subscriptions), 1)
        self.assertEqual(
            web_socket_client.raw_subscriptions[0].close_count,
            1,
        )
        self.assertFalse(runtime.ready)
        self.assertIs(runtime.state.status, ApplicationStatus.FAILED)
        self.assertEqual(
            tuple(entry.message_id for entry in runtime.startup_trace),
            ("1", "2", "3"),
        )
        self.assertIs(
            runtime.startup_trace[-1].failure_code,
            StartupFailureCode.HISTORY_INITIALIZATION_FAILED,
        )

    def test_account_observer_runs_only_for_actual_patch_and_close_is_idempotent(
        self,
    ) -> None:
        """
        함수 이름: test_account_observer_runs_only_for_actual_patch_and_close_is_idempotent()
        기능: Account false patch는 알리지 않고 변경 patch만 알리며 close가 한 번만 수행되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        operation_trace: list[str] = []
        observed_accounts: list[Account] = []
        runtime, _, web_socket_client, _ = _create_test_runtime(
            operation_trace,
            account_update_observer=observed_accounts.append,
        )
        market_behavior = _MarketStartupBehavior(runtime, operation_trace)
        with patch.object(
            MarketDataController,
            "initialize_market_data",
            autospec=True,
            side_effect=market_behavior,
        ):
            start_application(runtime)

        # 같은 timestamp와 동일 잔액은 Account가 false를 반환하므로 observer를 호출하지 않는다.
        web_socket_client.emit_account_event(
            _account_stream_payload("1.00000000", "0.20000000")
        )
        self.assertEqual(observed_accounts, [])

        # 같은 timestamp의 실제 잔액 변경은 한 번 적용되고 exact duplicate는 dedup된다.
        changed_payload = _account_stream_payload(
            "0.80000000",
            "0.10000000",
        )
        web_socket_client.emit_account_event(changed_payload)
        web_socket_client.emit_account_event(changed_payload)
        self.assertEqual(observed_accounts, [runtime.account])
        self.assertEqual(runtime.account.version, 2)
        self.assertEqual(
            runtime.account.get_holdings(),
            Decimal("0.90000000"),
        )

        first_closed_state = close_application(runtime)
        second_closed_state = close_application(runtime)
        self.assertIs(first_closed_state.status, ApplicationStatus.CLOSED)
        self.assertIs(second_closed_state, first_closed_state)
        self.assertEqual(
            web_socket_client.raw_subscriptions[0].close_count,
            1,
        )
        self.assertFalse(runtime.ready)

        # 닫힌 runtime은 같은 lifecycle instance에서 다시 시작할 수 없다.
        with self.assertRaises(ApplicationStartupError) as error_context:
            start_application(runtime)
        self.assertIs(
            error_context.exception.code,
            StartupFailureCode.APPLICATION_CLOSED,
        )


if __name__ == "__main__":
    unittest.main()
