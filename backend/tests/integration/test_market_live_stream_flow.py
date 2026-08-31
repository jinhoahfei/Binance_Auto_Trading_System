"""Communication 1L~1L.3의 연속 시장 event 적용과 공개 관찰 경로를 검증한다."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    WebSocketGateway,
)
from binance_auto_trader.application.market_data_controller import (
    MarketDataController,
    MarketDataStreamStateError,
)
from binance_auto_trader.domain.common import Interval, RegimeType
from binance_auto_trader.domain.market import Kline, MarketSnapshot
from binance_auto_trader.domain.regime import RegimeEvaluationTrigger
from binance_auto_trader.domain.trading import MarketEvaluationSnapshot

from tests.integration.test_market_initialization_flow import (
    FIXED_GATEWAY_TIME,
    FIXED_SNAPSHOT_TIME,
    OPEN_TIME_MILLISECONDS,
    SYMBOL,
    ControlledClock,
    FakeRestClient,
    FakeWebSocketClient,
    _create_rest_kline_payload,
    _create_rest_responses,
    _create_web_socket_kline_payload,
)


# Cross-stream 회귀는 UTC 02:00 경계 직전의 진행 1분·30분봉에서 시작한다.
THIRTY_MINUTE_BOUNDARY_MILLISECONDS = OPEN_TIME_MILLISECONDS + 7_200_000
BOUNDARY_SNAPSHOT_TIME = datetime(
    2026,
    8,
    20,
    1,
    59,
    59,
    500_000,
    tzinfo=timezone.utc,
)


class RecordingRegimeController:
    """
    클래스 이름: RecordingRegimeController
    기능: REGIME 재평가 trigger를 기록하면서 사용자 선택은 변경 불가능하게 보존한다.
    작성 날짜: 2026/08/25
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 미평가 상태와 고정된 사용자 선택 REGIME 및 빈 호출 기록을 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 추천 평가 이력과 사용자 선택을 별도 필드로 두어 자동 선택 혼입을 드러낸다.
        self._last_regime_result: object | None = None
        self._selected_regime = RegimeType.TYPE_2
        self.fail_next_reconciliation = False
        self.calls: list[
            tuple[RegimeEvaluationTrigger, MarketSnapshot]
        ] = []

    @property
    def last_regime_result(self) -> object | None:
        """
        함수 이름: last_regime_result()
        기능: 초기 평가 여부를 Controller에 제공한다.
        인자: 없음
        반환값: 마지막 평가 표식 또는 초기 None
        작성 날짜: 2026/08/25
        """
        return self._last_regime_result

    @property
    def selected_regime(self) -> RegimeType:
        """
        함수 이름: selected_regime()
        기능: 재평가와 분리된 고정 사용자 선택 REGIME을 반환한다.
        인자: 없음
        반환값: 테스트가 보존하는 TYPE_2 사용자 선택
        작성 날짜: 2026/08/25
        """
        return self._selected_regime

    def evaluate_regime(
        self,
        trigger: RegimeEvaluationTrigger,
        market_snapshot: MarketSnapshot,
    ) -> object:
        """
        함수 이름: evaluate_regime()
        기능: 추천 재평가 호출을 기록하되 사용자 선택에는 손대지 않는다.
        인자: trigger -> 초기 또는 4H 확정봉 평가 trigger
            market_snapshot -> 평가 source인 authoritative snapshot
        반환값: source market version을 가진 최소 성공 결과
        작성 날짜: 2026/08/25
        """
        # 추천 결과 표식만 전진시키고 _selected_regime은 의도적으로 변경하지 않는다.
        self.calls.append((trigger, market_snapshot))
        self._last_regime_result = _RecordingRegimeResult(
            market_snapshot.version
        )
        return self._last_regime_result

    def reconcile_regime(
        self,
        market_snapshot: MarketSnapshot,
    ) -> object | None:
        """
        함수 이름: reconcile_regime()
        기능: production full-resync Operation과 같은 version 성공 또는 명시 실패를 기록한다.
        인자: market_snapshot -> 새 세대 REST 병합을 마친 snapshot
        반환값: source version 결과 또는 실패를 주입하면 None
        작성 날짜: 2026/08/29
        """
        # 호출 자체는 4H 재조정 기록으로 남기고 test가 요청한 한 번의 실패만 소비한다.
        self.calls.append(
            (RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE, market_snapshot)
        )
        if self.fail_next_reconciliation:
            self.fail_next_reconciliation = False
            return None
        self._last_regime_result = _RecordingRegimeResult(
            market_snapshot.version
        )
        return self._last_regime_result


class _RecordingRegimeResult:
    """
    클래스 이름: _RecordingRegimeResult
    기능: MarketDataController가 요구하는 source market version만 보존하는 test 결과다.
    작성 날짜: 2026/08/29
    """

    def __init__(self, source_market_version: int) -> None:
        """
        함수 이름: __init__()
        기능: 성공한 test REGIME 결과를 한 market version에 결합한다.
        인자: source_market_version -> 평가가 읽은 MarketSnapshot version
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        self.source_market_version = source_market_version


class RecordingEvaluationBuilder:
    """
    클래스 이름: RecordingEvaluationBuilder
    기능: 수치식 자체를 가장하지 않고 공개 seam 연결만 검증할 typed 평가를 만든다.
    작성 날짜: 2026/08/25
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 비어 있는 same-version builder 호출 기록을 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        self.calls: list[tuple[MarketSnapshot, Kline, int]] = []
        self.fail_next_evaluation = False
        self.reset_count = 0

    def __call__(
        self,
        market_snapshot: MarketSnapshot,
        observed_kline: Kline,
    ) -> MarketEvaluationSnapshot:
        """
        함수 이름: __call__()
        기능: 호출 version을 기록하고 원본 종가만 담은 test-only 평가를 반환한다.
        인자: market_snapshot -> 이미 원본 Kline을 commit한 snapshot
            observed_kline -> 이번 version의 WebSocket source Kline
        반환값: 공개 observer 타입 계약을 만족하는 최소 평가
        작성 날짜: 2026/08/25
        """
        # 실패 주입은 snapshot commit 뒤 production fail-close 경계가 실행되는지 검증할 때만 소비한다.
        if self.fail_next_evaluation:
            self.fail_next_evaluation = False
            raise RuntimeError("injected market evaluation failure")

        # 실제 30분 EMA·slope 수치 증거로 오인되지 않도록 source 종가 외에는 기본값을 쓴다.
        self.calls.append(
            (market_snapshot, observed_kline, market_snapshot.version)
        )
        source_klines = market_snapshot.update_source_klines
        return MarketEvaluationSnapshot(
            realtime_price=observed_kline.close,
            current_30m_candle_id=(
                f"{observed_kline.symbol}:{observed_kline.interval.value}:"
                f"{observed_kline.open_time.isoformat()}"
            ),
            confirmed_1m_close=any(
                source_kline.interval is Interval.ONE_MINUTE
                and source_kline.closed
                for source_kline in source_klines
            ),
            confirmed_30m_close=any(
                source_kline.interval is Interval.THIRTY_MINUTES
                and source_kline.closed
                for source_kline in source_klines
            ),
        )

    def rebase(self, market_snapshot: MarketSnapshot) -> None:
        """
        함수 이름: rebase()
        기능: production builder lifecycle signature를 유지하되 수치 계산 없이 ready snapshot만 확인한다.
        인자: market_snapshot -> 초기화 또는 full-resync를 마친 snapshot
        반환값: snapshot이 ready이면 없음
        작성 날짜: 2026/08/29
        """
        if not market_snapshot.ready:
            raise AssertionError("recording builder requires a ready snapshot")

    def reset(self) -> None:
        """
        함수 이름: reset()
        기능: production generation reset signature를 test double에서 부작용 없이 소비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        self.reset_count += 1  # 장애마다 이전 generation 파생 상태가 정확히 한 번 폐기됐음을 기록한다.


class RecordingTradingControllerPort:
    """
    클래스 이름: RecordingTradingControllerPort
    기능: TradingController.observe_market_evaluation 공개 signature의 호출만 기록한다.
    작성 날짜: 2026/08/25
    """

    def __init__(self) -> None:
        """
        함수 이름: __init__()
        기능: 비어 있는 public market observation 호출 기록을 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        self.calls: list[
            tuple[MarketEvaluationSnapshot, str, int]
        ] = []
        self.market_stream_failures: list[str] = []
        self.market_stream_completions: list[int] = []
        self.fail_next_observation = False  # Observer 장애를 public callback 경계에서 한 번만 주입한다.

    def observe_market_evaluation(
        self,
        market: MarketEvaluationSnapshot,
        *,
        source_event_id: str,
        market_version: int,
    ) -> None:
        """
        함수 이름: observe_market_evaluation()
        기능: production 공개 seam과 같은 keyword-only provenance 인자를 기록한다.
        인자: market -> 주입 builder가 만든 typed 평가
            source_event_id -> 원본 Kline의 canonical 비밀 없는 식별자
            market_version -> 평가가 읽은 authoritative snapshot version
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 실패 주입은 publication 기록 전에 발생시켜 부분 성공으로 오인하지 않게 한다.
        if self.fail_next_observation:
            self.fail_next_observation = False
            raise RuntimeError("injected market observer failure")

        # keyword-only production signature를 그대로 받아 private event trigger 우회를 막는다.
        self.calls.append((market, source_event_id, market_version))

    def mark_market_stream_reconciliation_required(
        self,
        reason: str,
    ) -> None:
        """
        함수 이름: mark_market_stream_reconciliation_required()
        기능: production 시장 fail-close observer와 같은 장애 분류를 기록한다.
        인자: reason -> credential 없는 시장 장애 분류
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        self.market_stream_failures.append(reason)

    def complete_market_stream_reconciliation(
        self,
        market_version: int,
    ) -> None:
        """
        함수 이름: complete_market_stream_reconciliation()
        기능: 새 세대 REST 병합과 평가가 완료된 시장 version을 기록한다.
        인자: market_version -> same-version full-resync 결과
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        self.market_stream_completions.append(market_version)


def _create_boundary_market_flow() -> tuple[
    MarketDataController,
    MarketSnapshot,
    ControlledClock,
    FakeRestClient,
    FakeWebSocketClient,
    RecordingEvaluationBuilder,
    RecordingTradingControllerPort,
]:
    """
    함수 이름: _create_boundary_market_flow()
    기능: 외부 network·주문 없이 UTC 30분 경계 직전의 live Controller fixture를 만든다.
    인자: 없음
    반환값: Controller, snapshot/clock, fake clients, builder와 observer tuple
    작성 날짜: 2026/08/29
    """
    # REST 기준선에는 01:59 진행 1분봉과 01:30 진행 30분봉을 정확히 배치한다.
    boundary_rest_responses = {
        Interval.ONE_MINUTE.value: [
            _create_rest_kline_payload(
                Interval.ONE_MINUTE.value,
                "119",
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS - 60_000,
            )
        ],
        Interval.THIRTY_MINUTES.value: [
            _create_rest_kline_payload(
                Interval.THIRTY_MINUTES.value,
                "120",
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS - 1_800_000,
            )
        ],
        Interval.FOUR_HOURS.value: [
            _create_rest_kline_payload(
                Interval.FOUR_HOURS.value,
                "103",
                OPEN_TIME_MILLISECONDS,
            )
        ],
        Interval.ONE_DAY.value: [
            _create_rest_kline_payload(
                Interval.ONE_DAY.value,
                "104",
                OPEN_TIME_MILLISECONDS,
            )
        ],
    }
    external_trace: list[str] = []
    gateway_clock = ControlledClock(BOUNDARY_SNAPSHOT_TIME)
    snapshot_clock = ControlledClock(BOUNDARY_SNAPSHOT_TIME)
    rest_client = FakeRestClient(
        boundary_rest_responses,
        external_trace,
    )
    web_socket_client = FakeWebSocketClient(external_trace)
    market_snapshot = MarketSnapshot(
        symbol=SYMBOL,
        clock=snapshot_clock,
    )
    evaluation_builder = RecordingEvaluationBuilder()
    trading_controller_port = RecordingTradingControllerPort()
    controller = MarketDataController(
        api_gateway=APIGateway(rest_client, clock=gateway_clock),
        web_socket_gateway=WebSocketGateway(web_socket_client),
        market_snapshot=market_snapshot,
        regime_controller=RecordingRegimeController(),
        market_evaluation_builder=evaluation_builder,
        trading_market_observer=trading_controller_port,
        market_stream_state_observer=trading_controller_port,
    )
    controller.initialize_market_data()

    # Live close pair의 commit 시각은 두 봉의 공통 close boundary 직후로 전진시킨다.
    snapshot_clock.current_time = datetime(
        2026,
        8,
        20,
        2,
        0,
        1,
        tzinfo=timezone.utc,
    )
    return (
        controller,
        market_snapshot,
        snapshot_clock,
        rest_client,
        web_socket_client,
        evaluation_builder,
        trading_controller_port,
    )


def _emit_boundary_kline(
    web_socket_client: FakeWebSocketClient,
    *,
    interval: Interval,
    close: str,
    open_time_milliseconds: int,
    event_time_milliseconds: int,
    closed: bool,
) -> None:
    """
    함수 이름: _emit_boundary_kline()
    기능: boundary fixture의 현재 live handle에 공식형 Kline payload를 전달한다.
    인자: web_socket_client -> 현재 구독을 소유한 in-memory fake client
        interval -> payload의 canonical Kline 주기
        close -> OHLC에 사용할 종가 문자열
        open_time_milliseconds -> 공식 t 봉 시작 millisecond
        event_time_milliseconds -> 공식 E event millisecond
        closed -> 공식 x 확정 상태
    반환값: 없음
    작성 날짜: 2026/08/29
    """
    current_subscription = web_socket_client.latest_handle
    if current_subscription is None:
        raise AssertionError("live Kline subscription is missing")

    # Raw fixture는 production WebSocketGateway 정규화와 Controller callback을 그대로 통과한다.
    web_socket_client.emit(
        current_subscription,
        _create_web_socket_kline_payload(
            interval,
            close,
            open_time_milliseconds=open_time_milliseconds,
            event_time_milliseconds=event_time_milliseconds,
            closed=closed,
        ),
    )


class MarketLiveStreamFlowTests(unittest.TestCase):
    """
    클래스 이름: MarketLiveStreamFlowTests
    기능: 연속 Kline의 gap 차단, 4H 원자 rollover와 공개 거래 관찰 provenance를 검증한다.
    작성 날짜: 2026/08/25
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 외부 network·주문 없이 production Gateway와 snapshot을 조립하고 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 결정적 clock과 메모리 fake client만 사용해 실제 Binance network를 열지 않는다.
        self.external_trace: list[str] = []
        self.gateway_clock = ControlledClock(FIXED_GATEWAY_TIME)
        self.snapshot_clock = ControlledClock(FIXED_SNAPSHOT_TIME)
        self.rest_client = FakeRestClient(
            _create_rest_responses(),
            self.external_trace,
        )
        self.web_socket_client = FakeWebSocketClient(self.external_trace)
        self.market_snapshot = MarketSnapshot(
            symbol=SYMBOL,
            clock=self.snapshot_clock,
        )
        self.regime_controller = RecordingRegimeController()
        self.evaluation_builder = RecordingEvaluationBuilder()
        self.trading_controller_port = RecordingTradingControllerPort()
        self.market_recovery_requests: list[str] = []
        self.controller = MarketDataController(
            api_gateway=APIGateway(
                self.rest_client,
                clock=self.gateway_clock,
            ),
            web_socket_gateway=WebSocketGateway(self.web_socket_client),
            market_snapshot=self.market_snapshot,
            regime_controller=self.regime_controller,
            market_evaluation_builder=self.evaluation_builder,
            trading_market_observer=self.trading_controller_port,
            market_stream_state_observer=self.trading_controller_port,
            market_stream_recovery_requester=(
                lambda: self.market_recovery_requests.append("requested")
            ),
        )
        self.controller.initialize_market_data()

    def _emit(
        self,
        *,
        interval: Interval,
        close: str,
        open_time_milliseconds: int,
        event_time_milliseconds: int,
        closed: bool,
    ) -> None:
        """
        함수 이름: _emit()
        기능: 현재 같은 구독의 production Gateway callback에 공식 Kline fixture를 전달한다.
        인자: interval -> event Kline의 canonical interval
            close -> OHLC에 사용할 종가
            open_time_milliseconds -> 봉 시작 Unix millisecond
            event_time_milliseconds -> event 발생 Unix millisecond
            closed -> 공식 확정봉 flag
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 초기화가 유지한 현재 handle을 사용해 재구독 없는 live callback 경로만 실행한다.
        current_subscription = self.web_socket_client.latest_handle
        if current_subscription is None:
            raise AssertionError("live Kline subscription is missing")

        self.web_socket_client.emit(
            current_subscription,
            _create_web_socket_kline_payload(
                interval,
                close,
                open_time_milliseconds=open_time_milliseconds,
                closed=closed,
                event_time_milliseconds=event_time_milliseconds,
            ),
        )

    def test_gap_event_preserves_snapshot_and_emits_no_market_evaluation(
        self,
    ) -> None:
        """
        함수 이름: test_gap_event_preserves_snapshot_and_emits_no_market_evaluation()
        기능: 1분봉 하나를 건너뛴 event가 snapshot과 공개 거래 관찰을 모두 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 실패 전 mapping identity와 version·시각·현재가를 함께 캡처한다.
        state_before = (
            self.market_snapshot.klines_by_interval,
            self.market_snapshot.version,
            self.market_snapshot.updated_at,
            self.market_snapshot.current_eth_price,
        )

        with self.assertRaises(MarketDataStreamStateError):
            self._emit(
                interval=Interval.ONE_MINUTE,
                close="111",
                open_time_milliseconds=(
                    OPEN_TIME_MILLISECONDS + 120_000
                ),
                event_time_milliseconds=(
                    OPEN_TIME_MILLISECONDS + 121_000
                ),
                closed=True,
            )

        # gap 오류 뒤에도 candidate snapshot과 거래 평가 callback은 하나도 publish되지 않는다.
        state_after = (
            self.market_snapshot.klines_by_interval,
            self.market_snapshot.version,
            self.market_snapshot.updated_at,
            self.market_snapshot.current_eth_price,
        )
        self.assertIs(state_after[0], state_before[0])
        self.assertEqual(state_after[1:], state_before[1:])
        self.assertEqual(self.evaluation_builder.calls, [])
        self.assertEqual(self.trading_controller_port.calls, [])

    def test_four_hour_sources_wait_for_strategy_boundary_pair(
        self,
    ) -> None:
        """
        함수 이름: test_four_hour_sources_wait_for_strategy_boundary_pair()
        기능: 4H close/open만으로 stale 1분·30분 snapshot을 commit하거나 REGIME 평가하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 현재 4H의 최종 close는 snapshot invariant 때문에 다음 OPEN 전까지 보류된다.
        version_before = self.market_snapshot.version
        selected_before = self.regime_controller.selected_regime
        initial_regime_calls = tuple(self.regime_controller.calls)
        next_open_time = OPEN_TIME_MILLISECONDS + 14_400_000
        self._emit(
            interval=Interval.FOUR_HOURS,
            close="113",
            open_time_milliseconds=OPEN_TIME_MILLISECONDS,
            event_time_milliseconds=next_open_time,
            closed=True,
        )

        self.assertEqual(self.market_snapshot.version, version_before)
        self.assertEqual(
            tuple(self.regime_controller.calls),
            initial_regime_calls,
        )

        # 다음 4H OPEN까지 와도 필수 1분·30분 close가 없으므로 snapshot publication을 보류한다.
        self.snapshot_clock.current_time = datetime.fromtimestamp(
            next_open_time / 1_000,
            tz=timezone.utc,
        ) + timedelta(minutes=1)
        self._emit(
            interval=Interval.FOUR_HOURS,
            close="114",
            open_time_milliseconds=next_open_time,
            event_time_milliseconds=next_open_time + 1_000,
            closed=False,
        )

        # Partial upper rollover는 authoritative source나 거래 평가로 노출되지 않는다.
        self.assertEqual(self.market_snapshot.version, version_before)
        self.assertEqual(
            tuple(self.regime_controller.calls),
            initial_regime_calls,
        )
        self.assertEqual(self.market_snapshot.update_source_klines, ())
        self.assertEqual(self.evaluation_builder.calls, [])
        self.assertEqual(self.trading_controller_port.calls, [])
        self.assertEqual(
            self.regime_controller.selected_regime,
            selected_before,
        )

    def test_public_market_observer_receives_same_version_source_provenance(
        self,
    ) -> None:
        """
        함수 이름: test_public_market_observer_receives_same_version_source_provenance()
        기능: 연속 event가 private trigger 없이 공개 observer에 same-version provenance로 전달되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 직전 확정 1분봉 바로 다음 봉을 전달해 gap 없는 공개 market path를 실행한다.
        next_open_time = OPEN_TIME_MILLISECONDS + 60_000
        event_time = next_open_time + 60_000
        self._emit(
            interval=Interval.ONE_MINUTE,
            close="121",
            open_time_milliseconds=next_open_time,
            event_time_milliseconds=event_time,
            closed=True,
        )

        # builder가 읽은 version과 observer keyword 인자가 authoritative snapshot과 일치한다.
        self.assertEqual(len(self.evaluation_builder.calls), 1)
        builder_snapshot, builder_kline, builder_version = (
            self.evaluation_builder.calls[0]
        )
        self.assertIs(builder_snapshot, self.market_snapshot)
        self.assertEqual(builder_version, self.market_snapshot.version)
        self.assertEqual(builder_kline.close, Decimal("121"))
        self.assertEqual(len(self.trading_controller_port.calls), 1)
        market, source_event_id, market_version = (
            self.trading_controller_port.calls[0]
        )
        expected_open_time = datetime.fromtimestamp(
            next_open_time / 1_000,
            tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        expected_event_time = datetime.fromtimestamp(
            event_time / 1_000,
            tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")

        # provenance는 market key와 UTC 시각만 포함하며 fake order/account API는 호출되지 않는다.
        self.assertEqual(market.realtime_price, Decimal("121"))
        self.assertEqual(market_version, self.market_snapshot.version)
        self.assertEqual(
            source_event_id,
            (
                f"kline:{SYMBOL}:1m:{expected_open_time}:"
                f"{expected_event_time}:closed"
            ),
        )
        self.assertEqual(self.rest_client.order_call_count, 0)
        self.assertEqual(
            self.web_socket_client.account_stream_call_count,
            0,
        )

    def test_boundary_close_arrival_orders_commit_same_composite_provenance(
        self,
    ) -> None:
        """
        함수 이름: test_boundary_close_arrival_orders_commit_same_composite_provenance()
        기능: final 1분·30분 close의 두 도착 순서가 같은 atomic source와 평가를 만드는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        source_event_ids: list[str] = []
        arrival_orders = (
            (Interval.ONE_MINUTE, Interval.THIRTY_MINUTES),
            (Interval.THIRTY_MINUTES, Interval.ONE_MINUTE),
        )
        for arrival_order in arrival_orders:
            with self.subTest(arrival_order=arrival_order):
                (
                    _controller,
                    market_snapshot,
                    _snapshot_clock,
                    rest_client,
                    web_socket_client,
                    evaluation_builder,
                    trading_controller_port,
                ) = _create_boundary_market_flow()
                version_before = market_snapshot.version

                # E의 전역 순서를 가정하지 않고 지정한 interval arrival order 그대로 close를 전달한다.
                for arrival_index, interval in enumerate(arrival_order):
                    if interval is Interval.ONE_MINUTE:
                        _emit_boundary_kline(
                            web_socket_client,
                            interval=interval,
                            close="121",
                            open_time_milliseconds=(
                                THIRTY_MINUTE_BOUNDARY_MILLISECONDS - 60_000
                            ),
                            event_time_milliseconds=(
                                THIRTY_MINUTE_BOUNDARY_MILLISECONDS + 100
                            ),
                            closed=True,
                        )
                    else:
                        _emit_boundary_kline(
                            web_socket_client,
                            interval=interval,
                            close="120",
                            open_time_milliseconds=(
                                THIRTY_MINUTE_BOUNDARY_MILLISECONDS
                                - 1_800_000
                            ),
                            event_time_milliseconds=(
                                THIRTY_MINUTE_BOUNDARY_MILLISECONDS + 200
                            ),
                            closed=True,
                        )

                    # 첫 close만 받은 동안에는 stale 중간 snapshot이나 observer event를 만들지 않는다.
                    if arrival_index == 0:
                        self.assertEqual(
                            market_snapshot.version,
                            version_before,
                        )
                        self.assertEqual(
                            market_snapshot.update_source_klines,
                            (),
                        )
                        self.assertEqual(evaluation_builder.calls, [])
                        self.assertEqual(trading_controller_port.calls, [])

                # Counterpart 도착 시 canonical 1m, 30m pair가 한 version과 한 평가를 소유한다.
                self.assertEqual(
                    market_snapshot.version,
                    version_before + 1,
                )
                boundary_sources = market_snapshot.update_source_klines
                self.assertEqual(
                    tuple(source.interval for source in boundary_sources),
                    (Interval.ONE_MINUTE, Interval.THIRTY_MINUTES),
                )
                self.assertTrue(all(source.closed for source in boundary_sources))
                self.assertEqual(len(evaluation_builder.calls), 1)
                self.assertIs(
                    evaluation_builder.calls[0][1].interval,
                    Interval.THIRTY_MINUTES,
                )
                self.assertEqual(
                    evaluation_builder.calls[0][2],
                    version_before + 1,
                )
                self.assertEqual(len(trading_controller_port.calls), 1)
                evaluation, source_event_id, market_version = (
                    trading_controller_port.calls[0]
                )
                self.assertTrue(evaluation.confirmed_1m_close)
                self.assertTrue(evaluation.confirmed_30m_close)
                self.assertEqual(market_version, version_before + 1)

                # 복합 ID는 arrival order와 무관하게 두 source의 t·E·x를 모두 포함한다.
                one_minute_open = datetime(
                    2026,
                    8,
                    20,
                    1,
                    59,
                    tzinfo=timezone.utc,
                ).isoformat().replace("+00:00", "Z")
                one_minute_event = datetime(
                    2026,
                    8,
                    20,
                    2,
                    0,
                    0,
                    100_000,
                    tzinfo=timezone.utc,
                ).isoformat().replace("+00:00", "Z")
                thirty_minute_open = datetime(
                    2026,
                    8,
                    20,
                    1,
                    30,
                    tzinfo=timezone.utc,
                ).isoformat().replace("+00:00", "Z")
                thirty_minute_event = datetime(
                    2026,
                    8,
                    20,
                    2,
                    0,
                    0,
                    200_000,
                    tzinfo=timezone.utc,
                ).isoformat().replace("+00:00", "Z")
                expected_source_event_id = (
                    f"kline-batch|kline:{SYMBOL}:1m:{one_minute_open}:"
                    f"{one_minute_event}:closed|kline:{SYMBOL}:30m:"
                    f"{thirty_minute_open}:{thirty_minute_event}:closed"
                )
                self.assertEqual(source_event_id, expected_source_event_id)
                source_event_ids.append(source_event_id)
                self.assertEqual(rest_client.order_call_count, 0)
                self.assertEqual(
                    web_socket_client.account_stream_call_count,
                    0,
                )

        self.assertEqual(source_event_ids[0], source_event_ids[1])

    def test_post_boundary_one_minute_close_replays_after_thirty_minute_open(
        self,
    ) -> None:
        """
        함수 이름: test_post_boundary_one_minute_close_replays_after_thirty_minute_open()
        기능: pair 뒤 새 1분 close가 30분 OPEN보다 먼저 와도 trailing provenance를 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        (
            _controller,
            market_snapshot,
            snapshot_clock,
            rest_client,
            web_socket_client,
            evaluation_builder,
            trading_controller_port,
        ) = _create_boundary_market_flow()
        version_before = market_snapshot.version

        # 공통 경계 close pair를 먼저 canonical composite version으로 commit한다.
        _emit_boundary_kline(
            web_socket_client,
            interval=Interval.ONE_MINUTE,
            close="121",
            open_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS - 60_000
            ),
            event_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS + 100
            ),
            closed=True,
        )
        _emit_boundary_kline(
            web_socket_client,
            interval=Interval.THIRTY_MINUTES,
            close="120",
            open_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS - 1_800_000
            ),
            event_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS + 200
            ),
            closed=True,
        )
        pair_version = version_before + 1
        self.assertEqual(market_snapshot.version, pair_version)
        self.assertEqual(len(trading_controller_port.calls), 1)

        # 새 30분 OPEN이 지연된 상태의 첫 1분 close는 snapshot과 observer를 바꾸지 않고 한 slot에 보류한다.
        snapshot_clock.current_time = datetime(
            2026,
            8,
            20,
            2,
            1,
            1,
            tzinfo=timezone.utc,
        )
        _emit_boundary_kline(
            web_socket_client,
            interval=Interval.ONE_MINUTE,
            close="123",
            open_time_milliseconds=THIRTY_MINUTE_BOUNDARY_MILLISECONDS,
            event_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS + 60_100
            ),
            closed=True,
        )
        self.assertEqual(market_snapshot.version, pair_version)
        self.assertEqual(len(evaluation_builder.calls), 1)
        self.assertEqual(len(trading_controller_port.calls), 1)

        # 늦은 30분 OPEN을 먼저 commit한 뒤 보류 1분 close를 별도 version으로 재생한다.
        _emit_boundary_kline(
            web_socket_client,
            interval=Interval.THIRTY_MINUTES,
            close="122",
            open_time_milliseconds=THIRTY_MINUTE_BOUNDARY_MILLISECONDS,
            event_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS + 60_200
            ),
            closed=False,
        )
        self.assertEqual(market_snapshot.version, pair_version + 2)
        self.assertEqual(
            [
                (call[1].interval, call[1].closed, call[2])
                for call in evaluation_builder.calls
            ],
            [
                (Interval.THIRTY_MINUTES, True, pair_version),
                (Interval.THIRTY_MINUTES, False, pair_version + 1),
                (Interval.ONE_MINUTE, True, pair_version + 2),
            ],
        )
        self.assertEqual(len(trading_controller_port.calls), 3)
        thirty_minute_open_evaluation = trading_controller_port.calls[1][0]
        replayed_one_minute_evaluation = trading_controller_port.calls[2][0]
        self.assertFalse(thirty_minute_open_evaluation.confirmed_1m_close)
        self.assertFalse(thirty_minute_open_evaluation.confirmed_30m_close)
        self.assertTrue(replayed_one_minute_evaluation.confirmed_1m_close)
        self.assertFalse(replayed_one_minute_evaluation.confirmed_30m_close)
        self.assertEqual(
            market_snapshot.klines_by_interval[Interval.ONE_MINUTE][-1].close,
            Decimal("123"),
        )
        self.assertEqual(
            market_snapshot.klines_by_interval[
                Interval.THIRTY_MINUTES
            ][-1].close,
            Decimal("122"),
        )
        self.assertEqual(rest_client.order_call_count, 0)
        self.assertEqual(
            web_socket_client.account_stream_call_count,
            0,
        )

    def test_post_boundary_one_minute_slot_overflow_fails_closed(
        self,
    ) -> None:
        """
        함수 이름: test_post_boundary_one_minute_slot_overflow_fails_closed()
        기능: 30분 OPEN 없이 두 번째 1분봉으로 넘어가면 복구 gate를 닫는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        (
            controller,
            market_snapshot,
            snapshot_clock,
            rest_client,
            web_socket_client,
            evaluation_builder,
            trading_controller_port,
        ) = _create_boundary_market_flow()
        version_before = market_snapshot.version

        # Close pair 반영 후 첫 1분 OPEN을 한 slot에 보류한다.
        _emit_boundary_kline(
            web_socket_client,
            interval=Interval.ONE_MINUTE,
            close="121",
            open_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS - 60_000
            ),
            event_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS + 100
            ),
            closed=True,
        )
        _emit_boundary_kline(
            web_socket_client,
            interval=Interval.THIRTY_MINUTES,
            close="120",
            open_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS - 1_800_000
            ),
            event_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS + 200
            ),
            closed=True,
        )
        pair_version = version_before + 1
        reset_count_before = evaluation_builder.reset_count
        _emit_boundary_kline(
            web_socket_client,
            interval=Interval.ONE_MINUTE,
            close="122",
            open_time_milliseconds=THIRTY_MINUTE_BOUNDARY_MILLISECONDS,
            event_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS + 300
            ),
            closed=False,
        )
        self.assertEqual(market_snapshot.version, pair_version)
        self.assertEqual(len(trading_controller_port.calls), 1)

        # 30분 OPEN 없이 다음 1분 open이 오면 slot 범위를 넘어 현재 generation을 폐기한다.
        snapshot_clock.current_time = datetime(
            2026,
            8,
            20,
            2,
            1,
            1,
            tzinfo=timezone.utc,
        )
        with self.assertRaises(MarketDataStreamStateError):
            _emit_boundary_kline(
                web_socket_client,
                interval=Interval.ONE_MINUTE,
                close="123",
                open_time_milliseconds=(
                    THIRTY_MINUTE_BOUNDARY_MILLISECONDS + 60_000
                ),
                event_time_milliseconds=(
                    THIRTY_MINUTE_BOUNDARY_MILLISECONDS + 60_100
                ),
                closed=False,
            )
        self.assertEqual(market_snapshot.version, pair_version)
        self.assertFalse(controller.market_available)
        self.assertEqual(len(trading_controller_port.calls), 1)
        self.assertEqual(
            evaluation_builder.reset_count,
            reset_count_before + 1,
        )
        self.assertEqual(rest_client.order_call_count, 0)

    def test_boundary_close_before_actual_event_time_fails_without_commit(
        self,
    ) -> None:
        """
        함수 이름: test_boundary_close_before_actual_event_time_fails_without_commit()
        기능: x=true E가 실제 close boundary보다 빠르면 pending조차 만들지 않고 gate를 닫는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        (
            controller,
            market_snapshot,
            _snapshot_clock,
            rest_client,
            web_socket_client,
            evaluation_builder,
            trading_controller_port,
        ) = _create_boundary_market_flow()
        version_before = market_snapshot.version
        reset_count_before = evaluation_builder.reset_count

        # E는 1분봉 open 이후지만 02:00 실제 close boundary보다 1ms 빠르다.
        with self.assertRaises(MarketDataStreamStateError):
            _emit_boundary_kline(
                web_socket_client,
                interval=Interval.ONE_MINUTE,
                close="121",
                open_time_milliseconds=(
                    THIRTY_MINUTE_BOUNDARY_MILLISECONDS - 60_000
                ),
                event_time_milliseconds=(
                    THIRTY_MINUTE_BOUNDARY_MILLISECONDS - 1
                ),
                closed=True,
            )
        self.assertEqual(market_snapshot.version, version_before)
        self.assertEqual(market_snapshot.update_source_klines, ())
        self.assertFalse(controller.market_available)
        self.assertEqual(evaluation_builder.calls, [])
        self.assertEqual(trading_controller_port.calls, [])
        self.assertEqual(
            evaluation_builder.reset_count,
            reset_count_before + 1,
        )
        self.assertEqual(rest_client.order_call_count, 0)

    def test_boundary_next_open_uses_one_latest_bounded_slot(self) -> None:
        """
        함수 이름: test_boundary_next_open_uses_one_latest_bounded_slot()
        기능: 먼저 닫힌 30분 stream의 next-open tick을 한 slot에 교체하고 pair 뒤에 공개하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        (
            _controller,
            market_snapshot,
            _snapshot_clock,
            rest_client,
            web_socket_client,
            evaluation_builder,
            trading_controller_port,
        ) = _create_boundary_market_flow()
        version_before = market_snapshot.version

        # 30분 close와 다음 OPEN 두 tick이 먼저 와도 snapshot과 observer는 counterpart까지 정지한다.
        _emit_boundary_kline(
            web_socket_client,
            interval=Interval.THIRTY_MINUTES,
            close="120",
            open_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS - 1_800_000
            ),
            event_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS + 200
            ),
            closed=True,
        )
        for close_price, event_offset in (("121", 300), ("122", 400)):
            _emit_boundary_kline(
                web_socket_client,
                interval=Interval.THIRTY_MINUTES,
                close=close_price,
                open_time_milliseconds=(
                    THIRTY_MINUTE_BOUNDARY_MILLISECONDS
                ),
                event_time_milliseconds=(
                    THIRTY_MINUTE_BOUNDARY_MILLISECONDS + event_offset
                ),
                closed=False,
            )
        self.assertEqual(market_snapshot.version, version_before)
        self.assertEqual(evaluation_builder.calls, [])
        self.assertEqual(trading_controller_port.calls, [])

        # 늦은 final 1분 close가 pair를 commit하면 최신 30분 OPEN만 다음 version으로 공개된다.
        _emit_boundary_kline(
            web_socket_client,
            interval=Interval.ONE_MINUTE,
            close="121",
            open_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS - 60_000
            ),
            event_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS + 100
            ),
            closed=True,
        )
        self.assertEqual(market_snapshot.version, version_before + 2)
        self.assertEqual(
            [call[2] for call in evaluation_builder.calls],
            [version_before + 1, version_before + 2],
        )
        self.assertEqual(
            [call[1].close for call in evaluation_builder.calls],
            [Decimal("120"), Decimal("122")],
        )
        self.assertEqual(len(trading_controller_port.calls), 2)
        first_evaluation = trading_controller_port.calls[0][0]
        self.assertTrue(first_evaluation.confirmed_1m_close)
        self.assertTrue(first_evaluation.confirmed_30m_close)
        latest_thirty_minute = market_snapshot.klines_by_interval[
            Interval.THIRTY_MINUTES
        ][-1]
        self.assertFalse(latest_thirty_minute.closed)
        self.assertEqual(latest_thirty_minute.close, Decimal("122"))
        self.assertEqual(rest_client.order_call_count, 0)

    def test_boundary_next_open_overflow_fails_closed_without_publication(
        self,
    ) -> None:
        """
        함수 이름: test_boundary_next_open_overflow_fails_closed_without_publication()
        기능: counterpart 없이 next 30분봉까지 닫히면 bounded 경계를 초과해 gate와 publication을 닫는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        (
            controller,
            market_snapshot,
            snapshot_clock,
            rest_client,
            web_socket_client,
            evaluation_builder,
            trading_controller_port,
        ) = _create_boundary_market_flow()
        version_before = market_snapshot.version
        reset_count_before = evaluation_builder.reset_count

        # 30분 close와 next-open 한 개는 bounded 보류되며 authoritative snapshot을 바꾸지 않는다.
        _emit_boundary_kline(
            web_socket_client,
            interval=Interval.THIRTY_MINUTES,
            close="120",
            open_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS - 1_800_000
            ),
            event_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS + 200
            ),
            closed=True,
        )
        _emit_boundary_kline(
            web_socket_client,
            interval=Interval.THIRTY_MINUTES,
            close="121",
            open_time_milliseconds=THIRTY_MINUTE_BOUNDARY_MILLISECONDS,
            event_time_milliseconds=(
                THIRTY_MINUTE_BOUNDARY_MILLISECONDS + 300
            ),
            closed=False,
        )
        self.assertEqual(market_snapshot.version, version_before)
        self.assertEqual(trading_controller_port.calls, [])

        # 한 slot을 넘어 next 30분 close까지 오면 추측하지 않고 current generation을 폐기한다.
        snapshot_clock.current_time = datetime(
            2026,
            8,
            20,
            2,
            30,
            1,
            tzinfo=timezone.utc,
        )
        with self.assertRaises(MarketDataStreamStateError):
            _emit_boundary_kline(
                web_socket_client,
                interval=Interval.THIRTY_MINUTES,
                close="123",
                open_time_milliseconds=(
                    THIRTY_MINUTE_BOUNDARY_MILLISECONDS
                ),
                event_time_milliseconds=(
                    THIRTY_MINUTE_BOUNDARY_MILLISECONDS
                    + 1_800_000
                    + 100
                ),
                closed=True,
            )

        self.assertEqual(market_snapshot.version, version_before)
        self.assertFalse(controller.market_available)
        self.assertEqual(evaluation_builder.calls, [])
        self.assertEqual(trading_controller_port.calls, [])
        self.assertEqual(
            evaluation_builder.reset_count,
            reset_count_before + 1,
        )
        self.assertEqual(
            trading_controller_port.market_stream_failures[-1],
            "kline_stream_invalid",
        )
        self.assertEqual(rest_client.order_call_count, 0)

    def test_builder_failure_closes_gate_and_requests_full_resync(self) -> None:
        """
        함수 이름: test_builder_failure_closes_gate_and_requests_full_resync()
        기능: production 지표 builder 실패가 publication 없이 시장 gate를 닫고 복구를 요청하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 정상 다음 1분봉을 snapshot에 commit한 직후 builder만 실패하도록 한 번 주입한다.
        reset_count_before = self.evaluation_builder.reset_count
        self.evaluation_builder.fail_next_evaluation = True
        with self.assertRaisesRegex(
            RuntimeError,
            "injected market evaluation failure",
        ):
            self._emit(
                interval=Interval.ONE_MINUTE,
                close="121",
                open_time_milliseconds=OPEN_TIME_MILLISECONDS + 60_000,
                event_time_milliseconds=OPEN_TIME_MILLISECONDS + 120_000,
                closed=True,
            )

        # 불완전 평가를 TradingController에 보내지 않고 generation·duration 상태를 폐기한다.
        self.assertFalse(self.controller.market_available)
        self.assertEqual(self.trading_controller_port.calls, [])
        self.assertEqual(
            self.evaluation_builder.reset_count,
            reset_count_before + 1,
        )
        self.assertEqual(
            self.trading_controller_port.market_stream_failures[-1],
            "kline_stream_invalid",
        )
        self.assertEqual(self.market_recovery_requests, ["requested"])

    def test_market_observer_failure_closes_gate_before_publication(
        self,
    ) -> None:
        """
        함수 이름: test_market_observer_failure_closes_gate_before_publication()
        기능: Trading market observer 실패가 publication을 남기지 않고
            현재 세대를 폐기해 full-resync를 요청하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # 정상 Kline commit 뒤 public observer에서만 실패하도록 현재 증거 수를 고정한다.
        snapshot_version_before = self.market_snapshot.version
        builder_call_count_before = len(self.evaluation_builder.calls)
        observer_call_count_before = len(self.trading_controller_port.calls)
        reset_count_before = self.evaluation_builder.reset_count
        self.trading_controller_port.fail_next_observation = True

        # Gateway callback은 observer 예외를 숨기지 않고 같은 generation을 즉시 종료한다.
        with self.assertRaisesRegex(
            RuntimeError,
            "injected market observer failure",
        ):
            self._emit(
                interval=Interval.ONE_MINUTE,
                close="121",
                open_time_milliseconds=OPEN_TIME_MILLISECONDS + 60_000,
                event_time_milliseconds=OPEN_TIME_MILLISECONDS + 120_000,
                closed=True,
            )

        # Snapshot과 builder 계산은 완료됐지만 observer publication은 원자적으로 남지 않는다.
        self.assertEqual(
            snapshot_version_before + 1,
            self.market_snapshot.version,
        )
        self.assertEqual(
            builder_call_count_before + 1,
            len(self.evaluation_builder.calls),
        )
        self.assertEqual(
            observer_call_count_before,
            len(self.trading_controller_port.calls),
        )

        # 현재 generation의 파생 상태를 폐기하고 새 REST/WS 전체 동기화 전까지 gate를 닫는다.
        self.assertFalse(self.controller.market_available)
        self.assertEqual(
            reset_count_before + 1,
            self.evaluation_builder.reset_count,
        )
        self.assertEqual(
            "kline_stream_invalid",
            self.trading_controller_port.market_stream_failures[-1],
        )
        self.assertEqual(["requested"], self.market_recovery_requests)

    def test_disconnect_preserves_snapshot_and_requires_new_full_resync(
        self,
    ) -> None:
        """
        함수 이름: test_disconnect_preserves_snapshot_and_requires_new_full_resync()
        기능: 단절은 snapshot을 보존하고 새 WS 세대·REST·평가 성공 뒤에만 availability를 복구한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 현재 snapshot identity와 version을 캡처한 뒤 실제 Gateway disconnect callback을 실행한다.
        first_subscription = self.web_socket_client.latest_handle
        if first_subscription is None:
            raise AssertionError("live Kline subscription is missing")
        snapshot_mapping = self.market_snapshot.klines_by_interval
        snapshot_version = self.market_snapshot.version
        self.web_socket_client.disconnect(first_subscription)

        # 단절은 authoritative snapshot을 지우지 않고 거래 gate와 복구 요청만 즉시 닫는다.
        self.assertIs(
            self.market_snapshot.klines_by_interval,
            snapshot_mapping,
        )
        self.assertEqual(self.market_snapshot.version, snapshot_version)
        self.assertFalse(self.controller.market_available)
        self.assertEqual(
            self.trading_controller_port.market_stream_failures[-1],
            "kline_stream_disconnected",
        )
        self.assertEqual(self.market_recovery_requests, ["requested"])

        # 같은 snapshot owner에서 새 generation과 REST 전체 병합·REGIME 평가를 다시 완료한다.
        recovered_snapshot = self.controller.reconcile_market_stream()
        self.assertIs(recovered_snapshot, self.market_snapshot)
        self.assertIsNot(
            self.web_socket_client.latest_handle,
            first_subscription,
        )
        self.assertTrue(self.controller.market_available)
        self.assertGreater(self.market_snapshot.version, snapshot_version)
        self.assertEqual(
            self.trading_controller_port.market_stream_completions[-1],
            self.market_snapshot.version,
        )

    def test_failed_regime_reconciliation_keeps_market_gate_closed(self) -> None:
        """
        함수 이름: test_failed_regime_reconciliation_keeps_market_gate_closed()
        기능: 새 REST snapshot의 REGIME 결합이 실패하면 completion을 게시하지 않고 구독을 닫는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        first_subscription = self.web_socket_client.latest_handle
        if first_subscription is None:
            raise AssertionError("live Kline subscription is missing")
        completion_count = len(
            self.trading_controller_port.market_stream_completions
        )
        self.web_socket_client.disconnect(first_subscription)
        self.regime_controller.fail_next_reconciliation = True

        # None 결과는 같은 market version 평가로 간주하지 않고 전체 초기화를 실패시킨다.
        with self.assertRaisesRegex(
            MarketDataStreamStateError,
            "did not bind",
        ):
            self.controller.reconcile_market_stream()

        failed_subscription = self.web_socket_client.latest_handle
        self.assertIsNotNone(failed_subscription)
        self.assertTrue(failed_subscription.closed)
        self.assertFalse(self.controller.market_available)
        self.assertEqual(
            len(self.trading_controller_port.market_stream_completions),
            completion_count,
        )


if __name__ == "__main__":
    unittest.main()
