"""시장 snapshot 초기화와 동일 구독의 연속 Kline 반영을 조정한다."""

from binance_auto_trader.domain.market.stream_ordering import classify_kline

from collections.abc import Callable
from datetime import datetime, timedelta
from threading import RLock
from typing import Protocol

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    Subscription,
    WebSocketGateway,
)
from binance_auto_trader.domain.common import Interval, SUPPORTED_INTERVALS
from binance_auto_trader.domain.market import Kline, MarketSnapshot
from binance_auto_trader.domain.regime import RegimeEvaluationTrigger

from .regime_controller import RegimeController
from .runtime_diagnostics import RuntimeDiagnostics


MARKET_INTERVALS = SUPPORTED_INTERVALS
DEFAULT_KLINE_LIMIT = 500
MINIMUM_KLINE_LIMIT = 1
MAXIMUM_KLINE_LIMIT = 1000
# 연속 봉의 정확한 다음 open time을 검증할 canonical interval 길이를 공유한다.
_INTERVAL_DURATION_BY_INTERVAL = {
    Interval.ONE_MINUTE: timedelta(minutes=1),
    Interval.THIRTY_MINUTES: timedelta(minutes=30),
    Interval.FOUR_HOURS: timedelta(hours=4),
    Interval.ONE_DAY: timedelta(days=1),
}


class MarketEvaluationBuilder(Protocol):
    """
    클래스 이름: MarketEvaluationBuilder
    기능: 명세가 확정된 외부 지표 구현으로 same-version 거래 평가를 만드는 계약을 정의한다.
    작성 날짜: 2026/08/25
    """

    def __call__(
        self,
        market_snapshot: MarketSnapshot,
        observed_kline: Kline,
    ) -> object | None:
        """
        함수 이름: __call__()
        기능: 갱신된 시장 snapshot과 원본 Kline으로 불변 거래 평가를 생성한다.
        인자: market_snapshot -> observed_kline까지 반영한 authoritative snapshot
            observed_kline -> 이번 version을 발생시킨 WebSocket Kline
        반환값: 공개 TradingController seam에 전달할 시장 평가 또는 정상 rollover 보류의 None
        작성 날짜: 2026/08/25
        """
        ...

    def rebase(self, market_snapshot: MarketSnapshot) -> None:
        """
        함수 이름: rebase()
        기능: 초기화·full-resync snapshot에서 live 계산 baseline과 입력 충분성을 검증한다.
        인자: market_snapshot -> 새 Kline generation의 authoritative snapshot
        반환값: 계산 baseline이 유효하면 없음
        작성 날짜: 2026/08/29
        """
        ...

    def reset(self) -> None:
        """
        함수 이름: reset()
        기능: stream generation이 바뀔 때 후보 EMA와 monotonic 유지 상태를 폐기한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        ...


class TradingMarketObserver(Protocol):
    """
    클래스 이름: TradingMarketObserver
    기능: TradingController의 공개 시장 평가 관찰 seam을 구조적으로 정의한다.
    작성 날짜: 2026/08/25
    """

    def observe_market_evaluation(
        self,
        market: object,
        *,
        source_event_id: str,
        market_version: int,
    ) -> object:
        """
        함수 이름: observe_market_evaluation()
        기능: 같은 시장 version의 평가와 비밀 없는 source provenance를 관찰한다.
        인자: market -> 계산이 끝난 불변 시장 평가
            source_event_id -> 원본 Kline event의 안정적 식별자
            market_version -> 평가가 사용한 MarketSnapshot version
        반환값: observer가 공개하는 선택 결과
        작성 날짜: 2026/08/25
        """
        ...


class MarketStreamStateObserver(Protocol):
    """
    클래스 이름: MarketStreamStateObserver
    기능: 시장 stream 장애와 same-version full-resync 완료를 거래 effect gate에 전달한다.
    작성 날짜: 2026/08/25
    """

    def mark_market_stream_reconciliation_required(
        self,
        reason: str,
    ) -> None:
        """
        함수 이름: mark_market_stream_reconciliation_required()
        기능: 현재 시장 세대를 사용할 수 없음을 즉시 fail-closed gate에 기록한다.
        인자: reason -> credential 없는 안정적 장애 분류
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        ...

    def complete_market_stream_reconciliation(
        self,
        market_version: int,
    ) -> None:
        """
        함수 이름: complete_market_stream_reconciliation()
        기능: 새 세대 REST 병합과 평가가 완료된 정확한 시장 version을 gate에 전달한다.
        인자: market_version -> full-resync와 평가가 공유한 MarketSnapshot version
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        ...


class _PendingCandleBoundary(Exception):
    """
    클래스 이름: _PendingCandleBoundary
    기능: A bounded partial update is waiting for its matching close/open source.
    인자: 선언된 입력으로 현재 처리 상태를 확인한다.
    반환값: 처리 결과 또는 없음
    작성 날짜: 2026/09/10
    """


class MarketDataStreamStateError(RuntimeError):
    """
    클래스 이름: MarketDataStreamStateError
    기능: 중복 충돌·역행·gap·불완전 multi-interval rollover의 fail-closed 오류를 나타낸다.
    작성 날짜: 2026/08/25
    """


class MarketDataController:
    """
    클래스 이름: MarketDataController
    기능: REST/WS 시장 snapshot을 초기화하고 같은 구독의 연속 Kline 평가를 직렬화한다.
    작성 날짜: 2026/08/20
    """

    def __init__(
        self,
        api_gateway: APIGateway,
        web_socket_gateway: WebSocketGateway,
        market_snapshot: MarketSnapshot,
        regime_controller: RegimeController,
        kline_limit: int = DEFAULT_KLINE_LIMIT,
        market_evaluation_builder: MarketEvaluationBuilder | None = None,
        trading_market_observer: TradingMarketObserver | None = None,
        market_stream_state_observer: MarketStreamStateObserver | None = None,
        market_stream_recovery_requester: Callable[[], object] | None = None,
        diagnostics: RuntimeDiagnostics | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 주입된 Gateway와 동일 수명의 MarketSnapshot을 보존한다.
        인자: api_gateway -> 과거 Kline을 조회할 REST Gateway
            web_socket_gateway -> 초기 Kline buffer를 관리할 WebSocket Gateway
            market_snapshot -> 성공한 전체 초기화 결과를 반영할 시장 snapshot
            regime_controller -> 같은 snapshot으로 4H 지표와 추천을 계산할 controller
            kline_limit -> 각 주기에서 조회할 Kline 개수
            market_evaluation_builder -> 확정된 지표식으로 거래 평가를 만들 선택 callback
            trading_market_observer -> 공개 시장 평가를 받을 TradingController 호환 객체
            market_stream_state_observer -> 장애와 복구 version을 받을 거래 gate observer
            market_stream_recovery_requester -> non-blocking full-resync 요청 callback
            diagnostics -> 시장 입력·지표 계산 진단 경계 또는 None
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        # Binance 조회 상한과 bool 혼입을 외부 Gateway 호출보다 먼저 차단한다.
        if isinstance(kline_limit, bool) or not isinstance(kline_limit, int):
            raise TypeError("kline_limit must be an integer")
        if not MINIMUM_KLINE_LIMIT <= kline_limit <= MAXIMUM_KLINE_LIMIT:
            raise ValueError("kline_limit must be between 1 and 1000")

        # 미확정 수치식을 빈 평가로 가장하지 않도록 builder와 공개 observer를 한 쌍으로 받는다.
        if market_evaluation_builder is not None:
            provides_builder_contract = (
                callable(market_evaluation_builder)
                and callable(
                    getattr(market_evaluation_builder, "rebase", None)
                )
                and callable(
                    getattr(market_evaluation_builder, "reset", None)
                )
            )
            if not provides_builder_contract:
                raise TypeError(
                    "market_evaluation_builder must provide call, rebase, and reset"
                )
        provides_market_observer = callable(
            getattr(
                trading_market_observer,
                "observe_market_evaluation",
                None,
            )
        )
        if trading_market_observer is not None and not provides_market_observer:
            raise TypeError(
                "trading_market_observer must provide observe_market_evaluation"
            )
        if (market_evaluation_builder is None) != (
            trading_market_observer is None
        ):
            raise ValueError(
                "market evaluation builder and observer must be configured together"
            )
        provides_market_stream_state_observer = all(
            callable(getattr(market_stream_state_observer, method_name, None))
            for method_name in (
                "mark_market_stream_reconciliation_required",
                "complete_market_stream_reconciliation",
            )
        )
        if (
            market_stream_state_observer is not None
            and not provides_market_stream_state_observer
        ):
            raise TypeError(
                "market_stream_state_observer must provide market stream state methods"
            )
        if (
            market_stream_recovery_requester is not None
            and not callable(market_stream_recovery_requester)
        ):
            raise TypeError(
                "market_stream_recovery_requester must be callable or None"
            )

        # 초기화 직렬화와 live stream 직렬화를 분리해 Gateway callback과 lock 역전을 막는다.
        self._api_gateway = api_gateway
        self._web_socket_gateway = web_socket_gateway
        self._market_snapshot = market_snapshot
        self._regime_controller = regime_controller
        self._kline_limit = kline_limit
        self._market_evaluation_builder = market_evaluation_builder
        self._deferred_live_klines: dict[tuple[Interval, datetime], Kline] = {}
        self._trading_market_observer = trading_market_observer
        self._market_stream_state_observer = market_stream_state_observer
        self._market_stream_recovery_requester = (
            market_stream_recovery_requester
        )
        self._initialization_lock = RLock()
        self._stream_lock = RLock()
        self._collecting_initial_buffer = False
        self._initial_buffer: list[Kline] = []
        self._initialized = False
        self._market_available = False
        self._diagnostics = diagnostics or RuntimeDiagnostics()  # 수치 계산·파일 기록의 책임은 서로 분리한다.
        self._live_subscription: Subscription | None = None
        self._stream_cursor_by_interval: dict[Interval, Kline] = {}
        self._pending_boundary_one_minute_close: Kline | None = None
        self._pending_boundary_one_minute_open: Kline | None = None
        self._pending_boundary_thirty_minute_close: Kline | None = None
        self._pending_boundary_thirty_minute_open: Kline | None = None
        self._pending_boundary_four_hour_close: Kline | None = None
        self._pending_boundary_four_hour_open: Kline | None = None
        self._pending_boundary_one_day_close: Kline | None = None
        self._pending_boundary_one_day_open: Kline | None = None
        self._pending_boundary_time: datetime | None = None
        self._pending_post_boundary_one_minute: Kline | None = None
        self._awaiting_post_boundary_thirty_minute_open = False

    def initialize_market_data(
        self,
        symbol: str = "ETHUSDT",
    ) -> MarketSnapshot:
        """
        함수 이름: initialize_market_data()
        기능: WebSocket, REST, 병합, snapshot 갱신과 최초 REGIME 평가 순으로 초기화한다.
        인자: symbol -> 초기화할 Binance Spot 거래 symbol
        반환값: 주입 시 받은 것과 동일한 최신 MarketSnapshot
        작성 날짜: 2026/08/20
        """
        # symbol 불일치는 새 구독 generation을 만들기 전에 거부한다.
        normalized_symbol = self._normalize_symbol(symbol)
        if normalized_symbol != self._market_snapshot.symbol:
            raise ValueError("symbol must match the MarketSnapshot symbol")

        with self._initialization_lock:
            return self._initialize_market_data(normalized_symbol)

    def _initialize_market_data(
        self,
        normalized_symbol: str,
    ) -> MarketSnapshot:
        """
        함수 이름: _initialize_market_data()
        기능: 한 초기화 시도의 snapshot commit 뒤 같은 구독을 live observer로 승격한다.
        인자: normalized_symbol -> 검증과 정규화를 마친 Binance symbol
        반환값: 주입 시 받은 것과 동일한 최신 MarketSnapshot
        작성 날짜: 2026/08/20
        """
        # Gateway 세대를 바꾸기 전에 거래 gate를 닫아 이전 snapshot 기반 신규 effect를 차단한다.
        self._set_market_stream_unavailable(
            "market_stream_initializing",
            request_recovery=False,
        )

        # Buffer callback이 즉시 들어와도 REST 기준선 전에는 snapshot을 바꾸지 않도록 먼저 수집 상태를 연다.
        with self._stream_lock:
            self._collecting_initial_buffer = True
            self._initial_buffer = []
            self._initialized = False
            self._market_available = False
            self._live_subscription = None
            self._deferred_live_klines.clear()
            self._stream_cursor_by_interval = {}
            self._pending_boundary_one_minute_close = None
            self._pending_boundary_one_minute_open = None
            self._pending_boundary_thirty_minute_close = None
            self._pending_boundary_thirty_minute_open = None
            self._pending_boundary_four_hour_close = None
            self._pending_boundary_four_hour_open = None
            self._pending_boundary_one_day_close = None
            self._pending_boundary_one_day_open = None
            self._pending_boundary_time = None
            self._pending_post_boundary_one_minute = None
            self._awaiting_post_boundary_thirty_minute_open = False
        subscription: Subscription | None = None
        try:
            # 새 generation의 모든 disconnect·payload 오류는 이 Controller의 full-resync 경계로 돌아온다.
            subscription = self._web_socket_gateway.start_all_kline_buffering(
                symbol=normalized_symbol,
                intervals=MARKET_INTERVALS,
                reconciliation_required_callback=(
                    self.mark_market_stream_reconciliation_required
                ),
            )

            # REST 기준선을 읽은 뒤 재구독 없이 현재 buffer generation을 observer로 승격한다.
            rest_klines = self._api_gateway.load_all_klines(
                symbol=normalized_symbol,
                limit=self._kline_limit,
            )
            promoted_subscription = (
                self._web_socket_gateway.promote_kline_buffer_to_live(
                    self.observe_kline,
                    subscription,
                )
            )
            with self._stream_lock:
                # promotion이 순서대로 전달한 Kline을 interval별 초기 병합 입력으로 분리한다.
                buffered_klines = {
                    interval: tuple(
                        kline
                        for kline in self._initial_buffer
                        if kline.interval is interval
                    )
                    for interval in MARKET_INTERVALS
                }
                merged_klines = {}
                for interval in MARKET_INTERVALS:
                    candidates = {item.open_time: item for item in rest_klines[interval]}
                    for item in buffered_klines[interval]:
                        previous = candidates.get(item.open_time)
                        if previous is not None and previous.closed:
                            continue
                        candidates[item.open_time] = item
                    merged_klines[interval] = tuple(sorted(candidates.values(), key=lambda item: item.open_time))[-self._kline_limit:]


                # WebSocket 봉을 REST 봉 뒤에 배치해 같은 key에서 실시간 값을 우선한다.
                self._market_snapshot.update(merged_klines)

                # 최초 평가는 STM을 실행하고 full-resync는 같은 candle의 중복 전이 없이 새 version을 검증한다.
                if self._regime_controller.last_regime_result is None:
                    regime_result = self._regime_controller.evaluate_regime(
                        RegimeEvaluationTrigger.INITIAL,
                        self._market_snapshot,
                    )
                else:
                    regime_result = self._regime_controller.reconcile_regime(
                        self._market_snapshot,
                    )
                evaluated_market_version = self._market_snapshot.version
                if (
                    regime_result is None
                    or regime_result.source_market_version
                    != evaluated_market_version
                ):
                    raise MarketDataStreamStateError(
                        "REGIME reconciliation did not bind the market version"
                    )

                # REGIME과 별도로 30분 builder 입력·baseline을 검증한 뒤에만 새 market gate를 연다.
                evaluation_builder = self._market_evaluation_builder
                if evaluation_builder is not None:
                    evaluation_builder.rebase(self._market_snapshot)

                # REST에는 E가 없으므로 실제 WebSocket source가 남은 최신 봉만 cursor로 채택한다.
                self._stream_cursor_by_interval = {}
                for interval in MARKET_INTERVALS:
                    latest_kline = (
                        self._market_snapshot.klines_by_interval[interval][
                            -1
                        ]
                    )
                    if latest_kline.event_time is not None:
                        self._stream_cursor_by_interval[
                            interval
                        ] = latest_kline
                self._collecting_initial_buffer = False
                self._initial_buffer = []
                self._initialized = True
                self._live_subscription = promoted_subscription

                # 새 handle과 same-version 평가가 모두 준비된 시점에만 거래 effect gate를 다시 연다.
                if not self._web_socket_gateway.kline_live_ready:
                    raise MarketDataStreamStateError(
                        "Kline stream disconnected during market evaluation"
                    )
                state_observer = self._market_stream_state_observer
                if state_observer is not None:
                    state_observer.complete_market_stream_reconciliation(
                        evaluated_market_version
                    )
                self._market_available = True
        except Exception as initialization_error:
            # promotion 이후 실패도 새 구독을 닫고 부분 collector·cursor를 다음 재시도에 넘기지 않는다.
            self._set_market_stream_unavailable(
                "market_stream_initialization_failed",
                request_recovery=False,
            )
            if subscription is not None:
                try:
                    subscription.close()
                except Exception as close_error:
                    initialization_error.add_note(
                        f"subscription cleanup failed: {close_error!r}"
                    )
            raise

        return self._market_snapshot

    @property
    def market_available(self) -> bool:
        """
        함수 이름: market_available()
        기능: 현재 snapshot이 연결된 새 Kline 세대와 same-version 평가까지 완료했는지 반환한다.
        인자: 없음
        반환값: 신규 시장 effect가 현재 snapshot을 사용할 수 있으면 True
        작성 날짜: 2026/08/25
        """
        with self._stream_lock:
            return self._market_available

    def reconcile_market_stream(self) -> MarketSnapshot:
        """
        함수 이름: reconcile_market_stream()
        기능: 장애 뒤 새 WebSocket 세대와 REST 전체 Kline을 사용해 시장을 재초기화한다.
        인자: 없음
        반환값: full-resync와 평가를 완료한 동일 MarketSnapshot
        작성 날짜: 2026/08/25
        """
        # 자동 worker도 최초 startup과 동일한 검증·병합 Operation만 재사용한다.
        return self.initialize_market_data(self._market_snapshot.symbol)

    def mark_market_stream_reconciliation_required(
        self,
        reason: str,
    ) -> None:
        """
        함수 이름: mark_market_stream_reconciliation_required()
        기능: 현재 Kline 세대를 폐기하고 snapshot 보존 상태에서 full-resync를 요청한다.
        인자: reason -> Gateway가 만든 credential 없는 장애 분류
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        self._set_market_stream_unavailable(
            reason,
            request_recovery=True,
        )

    def close_market_stream(self) -> None:
        """
        함수 이름: close_market_stream()
        기능: 신규 복구를 예약하지 않고 현재 Kline handle과 live 관찰 상태를 멱등 종료한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        with self._initialization_lock:
            # Handle identity를 상태 폐기 전에 캡처해 소유자 close callback을 정확히 한 번 실행한다.
            with self._stream_lock:
                subscription = self._live_subscription
            self._set_market_stream_unavailable(
                "kline_stream_closed",
                request_recovery=False,
            )
            if subscription is not None:
                subscription.close()

    def _set_market_stream_unavailable(
        self,
        reason: str,
        *,
        request_recovery: bool,
    ) -> None:
        """
        함수 이름: _set_market_stream_unavailable()
        기능: MarketSnapshot은 보존하고 현재 stream 파생 상태와 거래 effect gate만 닫는다.
        인자: reason -> credential 없는 안정적 장애 또는 종료 분류
            request_recovery -> non-blocking full-resync worker를 깨울지 여부
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        if not isinstance(reason, str):
            raise TypeError("reason must be a string")
        if not reason or reason != reason.strip():
            raise ValueError(
                "reason must be non-empty without outer whitespace"
            )
        if type(request_recovery) is not bool:
            raise TypeError("request_recovery must be a bool")

        # Snapshot 자체는 UI와 다음 REST 병합 기준으로 보존하고 현재 세대 cursor만 모두 폐기한다.
        with self._stream_lock:
            self._collecting_initial_buffer = False
            self._initial_buffer = []
            self._initialized = False
            self._market_available = False
            self._live_subscription = None
            self._stream_cursor_by_interval = {}
            self._pending_boundary_one_minute_close = None
            self._pending_boundary_one_minute_open = None
            self._pending_boundary_thirty_minute_close = None
            self._pending_boundary_thirty_minute_open = None
            self._pending_boundary_four_hour_close = None
            self._pending_boundary_four_hour_open = None
            self._pending_boundary_one_day_close = None
            self._pending_boundary_one_day_open = None
            self._pending_boundary_time = None
            self._pending_post_boundary_one_minute = None
            self._awaiting_post_boundary_thirty_minute_open = False

        # 이전 Kline 세대의 후보 slope와 유지 시간은 새 REST 기준선으로 절대 승계하지 않는다.
        evaluation_builder = self._market_evaluation_builder
        reset_builder = getattr(evaluation_builder, "reset", None)
        if callable(reset_builder):
            reset_builder()

        # Application callback은 stream lock 밖에서 실행해 Gateway→market→application lock 역전을 막는다.
        state_observer = self._market_stream_state_observer
        try:
            if state_observer is not None:
                state_observer.mark_market_stream_reconciliation_required(
                    reason
                )
        finally:
            recovery_requester = self._market_stream_recovery_requester
            if request_recovery and recovery_requester is not None:
                recovery_requester()

    def observe_kline(self, kline: Kline) -> None:
        """
        함수 이름: observe_kline()
        기능: 현재 generation의 WebSocket Kline을 단조 검증해 snapshot·REGIME·거래 관찰에 반영한다.
        인자: kline -> WebSocketGateway가 정규화한 event 시각 포함 Kline
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # Gateway 밖에서 직접 잘못된 값이 들어와도 snapshot mutation 전에 타입과 source를 닫는다.
        if not isinstance(kline, Kline):
            raise TypeError("kline must be a Kline")
        if kline.event_time is None:
            raise ValueError("observed Kline must contain an event time")
        if kline.symbol != self._market_snapshot.symbol:
            raise ValueError("Kline symbol must match the MarketSnapshot")

        observer = getattr(self._trading_market_observer, "note_market_input", None)
        if callable(observer):
            observer()
        try:
            self._observe_validated_kline(kline)
        except _PendingCandleBoundary:
            return
        except Exception as error:
            self._diagnostics.record_exception("market_kline_processing", error, kline=kline, market_version=self._market_snapshot.version)
            raise  # Production Gateway의 동일 generation disconnect·full-resync 계약은 유지한다.

    def _observe_validated_kline(self, kline: Kline) -> None:
        """
        함수 이름: _observe_validated_kline()
        기능: 형식 검증을 마친 한 Kline을 stream lock 아래 snapshot과 거래 평가에 반영한다.
        인자: kline -> symbol과 event time 검증을 통과한 WebSocket Kline
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with self._stream_lock:
            # 초기 promotion callback은 REST 병합 전까지 live snapshot update 대신 collector에 보존한다.
            if self._collecting_initial_buffer:
                self._initial_buffer.append(kline)
                return
            if not self._initialized:
                raise MarketDataStreamStateError(
                    "market data must be initialized before live observation"
                )

            latest_committed = self._market_snapshot.klines_by_interval[kline.interval][-1]
            if kline.open_time < latest_committed.open_time:
                return
            if (kline.open_time == latest_committed.open_time and latest_committed.closed
                    and not kline.closed):
                return
            # interval별 wire cursor가 있으면 exact duplicate를 버리고 역행은 즉시 차단한다.
            previous_cursor = self._stream_cursor_by_interval.get(
                kline.interval
            )
            if previous_cursor is not None:
                duplicate = self._validate_stream_cursor(
                    previous_cursor,
                    kline,
                )
                if duplicate:
                    return

            # 모든 WebSocket event는 봉 open 이후이고 x=true면 실제 close boundary 이후여야 한다.
            self._validate_kline_event_time(kline)

            # 동시에 닫히는 interval은 stale-open 중간 snapshot 없이 bounded canonical batch로 조정한다.
            if self._coordinate_atomic_boundary(kline):
                if previous_cursor is None or kline.open_time >= previous_cursor.open_time:
                    self._stream_cursor_by_interval[kline.interval] = kline
                return

            # snapshot 기준 gap 검증을 통과한 event만 한 version update에 반영한다.
            self._validate_snapshot_sequence(kline)
            if kline.interval is Interval.FOUR_HOURS:
                committed = self._observe_four_hour_kline(kline)
                if not committed:
                    self._stream_cursor_by_interval[kline.interval] = kline
                    return
            else:
                self._merge_klines_into_snapshot((kline,))

            # commit 뒤 cursor와 공개 거래 평가가 정확히 같은 MarketSnapshot version을 공유한다.
            self._stream_cursor_by_interval[kline.interval] = kline
            self._publish_market_evaluation(kline)

    def _coordinate_atomic_boundary(self, kline: Kline) -> bool:
        """
        함수 이름: _coordinate_atomic_boundary()
        기능: 30분·4시간·일 경계 source와 각 bounded successor를 canonical batch로 조정한다.
        인자: kline -> interval cursor와 event time 검증을 통과한 WebSocket Kline
        반환값: 경계 조정기가 event를 보류하거나 원자 commit했으면 True
        작성 날짜: 2026/08/29
        """
        if kline.interval in (
            Interval.ONE_MINUTE,
            Interval.THIRTY_MINUTES,
        ):
            # 먼저 도착한 strategy close 뒤 동일 interval next-open 하나만 최신 값으로 보류한다.
            if self._stage_same_interval_boundary_successor(kline):
                return True
            if self._coordinate_post_boundary_rollover(kline):
                return True

        # 일반 live tick은 즉시 반영하고 경계 close 또는 필수 upper next-open만 조정기에 맡긴다.
        boundary_time = self._resolve_atomic_boundary_time(kline)
        if boundary_time is None:
            return False
        self._stage_atomic_boundary_source(kline, boundary_time)
        return True

    def _resolve_atomic_boundary_time(
        self,
        kline: Kline,
    ) -> datetime | None:
        """
        함수 이름: _resolve_atomic_boundary_time()
        기능: Kline이 현재 atomic boundary의 close 또는 upper next-open source인지 판정한다.
        인자: kline -> 단조성과 event-time 검증을 통과한 Kline
        반환값: source가 닫거나 여는 UTC boundary, 일반 live tick이면 None
        작성 날짜: 2026/08/29
        """
        if kline.interval in (
            Interval.ONE_MINUTE,
            Interval.THIRTY_MINUTES,
        ):
            if not self._is_thirty_minute_boundary_close(kline):
                return None
            return (
                kline.open_time
                + _INTERVAL_DURATION_BY_INTERVAL[kline.interval]
            )

        if kline.interval not in (
            Interval.FOUR_HOURS,
            Interval.ONE_DAY,
        ):
            return None
        interval_duration = _INTERVAL_DURATION_BY_INTERVAL[kline.interval]
        pending_close = (
            self._pending_boundary_four_hour_close
            if kline.interval is Interval.FOUR_HOURS
            else self._pending_boundary_one_day_close
        )
        if kline.closed:
            return kline.open_time + interval_duration
        if pending_close is not None:
            expected_open_time = pending_close.open_time + interval_duration
            if kline.open_time != expected_open_time:
                raise MarketDataStreamStateError(
                    "upper boundary exceeded its bounded next-open slot"
                )
            return expected_open_time

        latest = self._market_snapshot.klines_by_interval[kline.interval][-1]
        if kline.open_time == latest.open_time + interval_duration:
            return kline.open_time

        # 활성 경계가 이 upper interval을 요구하면 close보다 먼저 온 OPEN을 추측하지 않는다.
        pending_boundary_time = self._pending_boundary_time
        if (
            pending_boundary_time is not None
            and self._boundary_requires_interval(
                pending_boundary_time,
                kline.interval,
            )
        ):
            raise MarketDataStreamStateError(
                "upper boundary open arrived before its closed Kline"
            )
        return None

    def _stage_atomic_boundary_source(
        self,
        kline: Kline,
        boundary_time: datetime,
    ) -> None:
        """
        함수 이름: _stage_atomic_boundary_source()
        기능: 한 UTC boundary의 canonical source를 interval별 bounded slot에 저장하고 commit을 시도한다.
        인자: kline -> 저장할 close 또는 upper next-open Kline
            boundary_time -> 모든 pending source가 공유해야 하는 UTC close boundary
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        pending_boundary_time = self._pending_boundary_time
        if (
            pending_boundary_time is not None
            and pending_boundary_time != boundary_time
        ):
            raise MarketDataStreamStateError(
                "atomic Kline sources must share one boundary"
            )

        # Strategy source는 현재 authoritative open을 닫는 exact final Kline이어야 한다.
        if kline.interval is Interval.ONE_MINUTE:
            self._validate_snapshot_sequence(kline)
            self._pending_boundary_one_minute_close = kline
        elif kline.interval is Interval.THIRTY_MINUTES:
            self._validate_snapshot_sequence(kline)
            self._pending_boundary_thirty_minute_close = kline
        elif kline.interval is Interval.FOUR_HOURS:
            self._stage_upper_boundary_source(
                kline,
                boundary_time,
                Interval.FOUR_HOURS,
            )
        elif kline.interval is Interval.ONE_DAY:
            self._stage_upper_boundary_source(
                kline,
                boundary_time,
                Interval.ONE_DAY,
            )
        else:
            raise ValueError("unsupported atomic boundary interval")

        # 첫 source 검증·저장이 성공한 뒤에만 active boundary identity를 commit한다.
        if pending_boundary_time is None:
            self._pending_boundary_time = boundary_time
        self._try_commit_atomic_boundary()

    def _stage_upper_boundary_source(
        self,
        kline: Kline,
        boundary_time: datetime,
        interval: Interval,
    ) -> None:
        """
        함수 이름: _stage_upper_boundary_source()
        기능: 4시간 또는 1일 close와 exact next-open을 각 한 slot에 검증·저장한다.
        인자: kline -> 저장할 upper interval Kline
            boundary_time -> strategy pair와 공유할 UTC boundary
            interval -> FOUR_HOURS 또는 ONE_DAY
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        if not self._boundary_requires_interval(boundary_time, interval):
            raise MarketDataStreamStateError(
                "upper Kline does not belong to the active UTC boundary"
            )
        if interval is Interval.FOUR_HOURS:
            pending_close = self._pending_boundary_four_hour_close
        elif interval is Interval.ONE_DAY:
            pending_close = self._pending_boundary_one_day_close
        else:
            raise ValueError("upper boundary interval must be 4h or 1d")

        if kline.closed:
            # Close source는 snapshot의 최신 진행봉과 같은 open time에서만 교체할 수 있다.
            self._validate_snapshot_sequence(kline)
            if interval is Interval.FOUR_HOURS:
                self._pending_boundary_four_hour_close = kline
            else:
                self._pending_boundary_one_day_close = kline
            return
        if pending_close is None:
            latest = self._market_snapshot.klines_by_interval[interval][-1]
            if kline.open_time != latest.open_time + _INTERVAL_DURATION_BY_INTERVAL[interval]:
                raise MarketDataStreamStateError("upper boundary requires the exact next open Kline")
            if interval is Interval.FOUR_HOURS:
                self._pending_boundary_four_hour_open = kline
            else:
                self._pending_boundary_one_day_open = kline
            return

        # Upper OPEN은 close 직후 정확한 한 봉만 허용하며 여러 tick은 최신 값으로 교체한다.
        expected_open_time = (
            pending_close.open_time
            + _INTERVAL_DURATION_BY_INTERVAL[interval]
        )
        if (
            kline.open_time != expected_open_time
            or kline.open_time != boundary_time
        ):
            raise MarketDataStreamStateError(
                "upper boundary requires the exact next open Kline"
            )
        if interval is Interval.FOUR_HOURS:
            self._pending_boundary_four_hour_open = kline
        else:
            self._pending_boundary_one_day_open = kline

    def _try_commit_atomic_boundary(self) -> None:
        """
        함수 이름: _try_commit_atomic_boundary()
        기능: 현재 UTC boundary의 모든 필수 source가 모이면 한 snapshot version으로 commit한다.
        인자: 없음
        반환값: 필수 source가 부족하면 없음, commit 후에도 없음
        작성 날짜: 2026/08/29
        """
        boundary_time = self._pending_boundary_time
        if boundary_time is not None:
            required_sources = {
                "1m_close": self._pending_boundary_one_minute_close,
                "30m_close": self._pending_boundary_thirty_minute_close,
            }
            if self._is_four_hour_boundary(boundary_time):
                required_sources.update({
                    "4h_close": self._pending_boundary_four_hour_close,
                    "4h_open": self._pending_boundary_four_hour_open,
                })
            if self._is_one_day_boundary(boundary_time):
                required_sources.update({
                    "1d_close": self._pending_boundary_one_day_close,
                    "1d_open": self._pending_boundary_one_day_open,
                })
            missing_sources = tuple(name for name, source in required_sources.items() if source is None)
            if missing_sources:
                self._diagnostics.record(
                    "market_boundary_waiting", reason="MISSING_BOUNDARY_SOURCES",
                    boundary_time=boundary_time, market_version=self._market_snapshot.version,
                    missing_sources=missing_sources,
                    received_sources={name: source for name, source in required_sources.items() if source is not None},
                    pending_count=len(required_sources) - len(missing_sources),
                )
        one_minute_close = self._pending_boundary_one_minute_close
        thirty_minute_close = self._pending_boundary_thirty_minute_close
        if (
            boundary_time is None
            or one_minute_close is None
            or thirty_minute_close is None
        ):
            return
        self._validate_thirty_minute_boundary_pair(
            one_minute_close,
            thirty_minute_close,
        )

        # UTC 4시간 경계는 closed/open 4H가 모두 있어야 current-price invariant를 보존한다.
        boundary_sources: tuple[Kline, ...] = (
            one_minute_close,
            thirty_minute_close,
        )
        if self._is_four_hour_boundary(boundary_time):
            four_hour_close = self._pending_boundary_four_hour_close
            four_hour_open = self._pending_boundary_four_hour_open
            if four_hour_close is None or four_hour_open is None:
                return
            self._validate_upper_boundary_pair(
                four_hour_close,
                four_hour_open,
                boundary_time,
                Interval.FOUR_HOURS,
            )
            boundary_sources = (
                *boundary_sources,
                four_hour_close,
                four_hour_open,
            )

        # UTC 자정에는 같은 version에 closed/open 1D까지 포함해 stale day open을 숨긴다.
        if self._is_one_day_boundary(boundary_time):
            one_day_close = self._pending_boundary_one_day_close
            one_day_open = self._pending_boundary_one_day_open
            if one_day_close is None or one_day_open is None:
                return
            self._validate_upper_boundary_pair(
                one_day_close,
                one_day_open,
                boundary_time,
                Interval.ONE_DAY,
            )
            boundary_sources = (
                *boundary_sources,
                one_day_close,
                one_day_open,
            )

        self._commit_atomic_boundary(
            boundary_time,
            boundary_sources,
            thirty_minute_close,
        )

    def _coordinate_post_boundary_rollover(self, kline: Kline) -> bool:
        """
        함수 이름: _coordinate_post_boundary_rollover()
        기능: close pair 뒤 30분 OPEN보다 먼저 온 첫 1분봉을 한 slot에 보류한다.
        인자: kline -> 경계 close pair 이후의 1분 또는 30분 Kline
        반환값: event를 보류하거나 30분 OPEN 뒤 순서대로 commit했으면 True
        작성 날짜: 2026/08/29
        """
        pending_one_minute = self._pending_post_boundary_one_minute
        if not self._awaiting_post_boundary_thirty_minute_open:
            if pending_one_minute is not None:
                raise MarketDataStreamStateError(
                    "pending one-minute rollover outlived the closed boundary"
                )
            return False

        latest_thirty_minute = self._market_snapshot.klines_by_interval[
            Interval.THIRTY_MINUTES
        ][-1]
        if not latest_thirty_minute.closed:
            raise MarketDataStreamStateError(
                "post-boundary rollover requires a closed thirty-minute Kline"
            )

        # 닫힌 30분봉 다음에는 exact 30분 OPEN이 오기 전 첫 1분봉 하나만 보존한다.
        next_thirty_minute_open = latest_thirty_minute.open_time + timedelta(
            minutes=30
        )
        if kline.interval is Interval.ONE_MINUTE:
            if kline.open_time != next_thirty_minute_open:
                raise MarketDataStreamStateError(
                    "one-minute rollover exceeded its bounded boundary slot"
                )
            self._validate_snapshot_sequence(kline)
            if (
                pending_one_minute is not None
                and pending_one_minute.closed
                and not kline.closed
            ):
                raise MarketDataStreamStateError(
                    "a pending one-minute rollover cannot reopen"
                )
            self._pending_post_boundary_one_minute = kline
            return True

        if kline.interval is not Interval.THIRTY_MINUTES:
            return False
        if kline.open_time != next_thirty_minute_open or kline.closed:
            raise MarketDataStreamStateError(
                "closed boundary requires the exact next thirty-minute open"
            )
        # 늦은 30분 OPEN을 먼저 공개한 뒤 보류 1분봉이 있으면 별도 version으로 재생한다.
        self._validate_snapshot_sequence(kline)
        self._merge_klines_into_snapshot((kline,))
        self._publish_market_evaluation(kline)
        self._awaiting_post_boundary_thirty_minute_open = False
        if pending_one_minute is None:
            return True
        self._pending_post_boundary_one_minute = None
        self._validate_snapshot_sequence(pending_one_minute)
        self._merge_klines_into_snapshot((pending_one_minute,))
        self._publish_market_evaluation(pending_one_minute)
        return True

    def _stage_same_interval_boundary_successor(
        self,
        kline: Kline,
    ) -> bool:
        """
        함수 이름: _stage_same_interval_boundary_successor()
        기능: counterpart를 기다리는 close와 같은 interval의 next-open 하나만 bounded 보류한다.
        인자: kline -> 새 1분 또는 30분 Kline
        반환값: pending close 뒤 successor로 처리했으면 True, pending close가 없으면 False
        작성 날짜: 2026/08/29
        """
        if kline.interval is Interval.ONE_MINUTE:
            pending_close = self._pending_boundary_one_minute_close
            interval_duration = timedelta(minutes=1)
        elif kline.interval is Interval.THIRTY_MINUTES:
            pending_close = self._pending_boundary_thirty_minute_close
            interval_duration = timedelta(minutes=30)
        else:
            return False
        if pending_close is None:
            latest = self._market_snapshot.klines_by_interval[kline.interval][-1]
            boundary = latest.open_time + interval_duration
            if (not kline.closed and not latest.closed and kline.open_time == boundary
                    and boundary.minute in (0, 30)):
                if kline.interval is Interval.ONE_MINUTE:
                    self._pending_boundary_one_minute_open = kline
                else:
                    self._pending_boundary_thirty_minute_open = kline
                return True
            return False

        # 더 늦은 E를 가진 동일 close는 final payload를 교체하되 open으로 되돌아가지는 못한다.
        if kline.open_time == pending_close.open_time:
            if not kline.closed:
                raise MarketDataStreamStateError(
                    "a pending boundary close cannot become open again"
                )
            if kline.interval is Interval.ONE_MINUTE:
                self._pending_boundary_one_minute_close = kline
            else:
                self._pending_boundary_thirty_minute_close = kline
            return True

        expected_next_open = pending_close.open_time + interval_duration
        if kline.open_time != expected_next_open or kline.closed:
            raise MarketDataStreamStateError(
                "thirty-minute boundary exceeded its bounded next-open buffer"
            )

        # 같은 next-open의 연속 tick은 하나의 slot을 최신 exact Kline으로 교체한다.
        if kline.interval is Interval.ONE_MINUTE:
            self._pending_boundary_one_minute_open = kline
        else:
            self._pending_boundary_thirty_minute_open = kline
        return True

    @staticmethod
    def _is_thirty_minute_boundary_close(kline: Kline) -> bool:
        """
        함수 이름: _is_thirty_minute_boundary_close()
        기능: Kline이 UTC 30분 경계를 닫는 1분 또는 30분 확정봉인지 판정한다.
        인자: kline -> 판정할 canonical Kline
        반환값: 30분 경계 close이면 True
        작성 날짜: 2026/08/29
        """
        if not kline.closed:
            return False
        if kline.interval is Interval.THIRTY_MINUTES:
            return True
        if kline.interval is not Interval.ONE_MINUTE:
            return False

        # Binance UTC Kline 경계는 끝 시각의 분이 00 또는 30이고 초·microsecond가 모두 0이다.
        close_boundary = kline.open_time + timedelta(minutes=1)
        return (
            close_boundary.minute in (0, 30)
            and close_boundary.second == 0
            and close_boundary.microsecond == 0
        )

    @staticmethod
    def _validate_kline_event_time(kline: Kline) -> None:
        """
        함수 이름: _validate_kline_event_time()
        기능: 모든 WebSocket Kline event의 open 인과성과 close 최종성 시각을 검증한다.
        인자: kline -> Gateway가 정규화한 Kline
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        if kline.event_time is None or kline.event_time < kline.open_time:
            raise MarketDataStreamStateError(
                "Kline event time must not precede its open time"
            )
        if not kline.closed:
            return

        # x=true source는 interval 전체가 끝나기 전에 생성된 최종 event일 수 없다.
        close_boundary = (
            kline.open_time
            + _INTERVAL_DURATION_BY_INTERVAL[kline.interval]
        )
        if kline.event_time < close_boundary:
            raise MarketDataStreamStateError(
                "closed Kline event time must reach its close boundary"
            )

    @staticmethod
    def _is_four_hour_boundary(boundary_time: datetime) -> bool:
        """
        함수 이름: _is_four_hour_boundary()
        기능: UTC 시각이 canonical 4시간 Kline 경계인지 판정한다.
        인자: boundary_time -> 판정할 UTC datetime
        반환값: 00·04·08·12·16·20시 정각이면 True
        작성 날짜: 2026/08/29
        """
        return (
            boundary_time.hour % 4 == 0
            and boundary_time.minute == 0
            and boundary_time.second == 0
            and boundary_time.microsecond == 0
        )

    @staticmethod
    def _is_one_day_boundary(boundary_time: datetime) -> bool:
        """
        함수 이름: _is_one_day_boundary()
        기능: UTC 시각이 canonical 일 Kline의 자정 경계인지 판정한다.
        인자: boundary_time -> 판정할 UTC datetime
        반환값: UTC 00:00:00 정각이면 True
        작성 날짜: 2026/08/29
        """
        return (
            boundary_time.hour == 0
            and boundary_time.minute == 0
            and boundary_time.second == 0
            and boundary_time.microsecond == 0
        )

    @staticmethod
    def _boundary_requires_interval(
        boundary_time: datetime,
        interval: Interval,
    ) -> bool:
        """
        함수 이름: _boundary_requires_interval()
        기능: UTC boundary가 4시간 또는 1일 closed/open source를 요구하는지 판정한다.
        인자: boundary_time -> active atomic boundary UTC 시각
            interval -> FOUR_HOURS 또는 ONE_DAY
        반환값: 해당 upper interval rollover가 필수이면 True
        작성 날짜: 2026/08/29
        """
        if interval is Interval.FOUR_HOURS:
            return MarketDataController._is_four_hour_boundary(boundary_time)
        if interval is Interval.ONE_DAY:
            return MarketDataController._is_one_day_boundary(boundary_time)
        raise ValueError("boundary interval must be 4h or 1d")

    @staticmethod
    def _validate_thirty_minute_boundary_pair(
        one_minute_close: Kline,
        thirty_minute_close: Kline,
    ) -> None:
        """
        함수 이름: _validate_thirty_minute_boundary_pair()
        기능: 두 확정봉이 symbol과 정확한 동일 30분 UTC close boundary를 공유하는지 검증한다.
        인자: one_minute_close -> 마지막 확정 1분봉
            thirty_minute_close -> 같은 경계의 확정 30분봉
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        if (
            one_minute_close.interval is not Interval.ONE_MINUTE
            or thirty_minute_close.interval is not Interval.THIRTY_MINUTES
            or not one_minute_close.closed
            or not thirty_minute_close.closed
        ):
            raise MarketDataStreamStateError(
                "boundary pair requires closed one- and thirty-minute Klines"
            )
        if one_minute_close.symbol != thirty_minute_close.symbol:
            raise MarketDataStreamStateError(
                "boundary pair Kline symbols must match"
            )

        one_minute_boundary = one_minute_close.open_time + timedelta(
            minutes=1
        )
        thirty_minute_boundary = thirty_minute_close.open_time + timedelta(
            minutes=30
        )
        if one_minute_boundary != thirty_minute_boundary:
            raise MarketDataStreamStateError(
                "one- and thirty-minute Klines must close at the same boundary"
            )

        # 두 x=true source 모두 공통 boundary 이후 event여야 확정 provenance로 사용할 수 있다.
        for source_kline in (one_minute_close, thirty_minute_close):
            if (
                source_kline.event_time is None
                or source_kline.event_time < one_minute_boundary
            ):
                raise MarketDataStreamStateError(
                    "boundary close event time must reach the shared boundary"
                )

    @staticmethod
    def _validate_upper_boundary_pair(
        closed_kline: Kline,
        open_kline: Kline,
        boundary_time: datetime,
        interval: Interval,
    ) -> None:
        """
        함수 이름: _validate_upper_boundary_pair()
        기능: upper interval의 closed Kline과 exact next-open이 같은 UTC boundary를 잇는지 검증한다.
        인자: closed_kline -> boundary에서 끝나는 확정 4시간 또는 1일 Kline
            open_kline -> 같은 boundary에서 시작하는 진행 Kline
            boundary_time -> strategy pair와 공유하는 UTC boundary
            interval -> FOUR_HOURS 또는 ONE_DAY
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        if interval not in (Interval.FOUR_HOURS, Interval.ONE_DAY):
            raise ValueError("upper boundary interval must be 4h or 1d")
        if (
            closed_kline.interval is not interval
            or open_kline.interval is not interval
            or not closed_kline.closed
            or open_kline.closed
        ):
            raise MarketDataStreamStateError(
                "upper boundary requires one closed and one open Kline"
            )
        if closed_kline.symbol != open_kline.symbol:
            raise MarketDataStreamStateError(
                "upper boundary Kline symbols must match"
            )

        # Close의 정확한 다음 open만 허용하고 두 event 모두 boundary 이후 provenance를 요구한다.
        expected_open_time = (
            closed_kline.open_time
            + _INTERVAL_DURATION_BY_INTERVAL[interval]
        )
        if (
            expected_open_time != boundary_time
            or open_kline.open_time != boundary_time
        ):
            raise MarketDataStreamStateError(
                "upper boundary Klines must share the active boundary"
            )
        for source_kline in (closed_kline, open_kline):
            if (
                source_kline.event_time is None
                or source_kline.event_time < boundary_time
            ):
                raise MarketDataStreamStateError(
                    "upper boundary event time must reach the boundary"
                )

    def _commit_atomic_boundary(
        self,
        boundary_time: datetime,
        boundary_sources: tuple[Kline, ...],
        thirty_minute_close: Kline,
    ) -> None:
        """
        함수 이름: _commit_atomic_boundary()
        기능: canonical boundary sources를 한 version으로 commit하고 REGIME·시장 평가를 한 번 게시한다.
        인자: boundary_time -> 모든 source가 공유하는 UTC close boundary
            boundary_sources -> canonical 1m·30m 및 선택적 4H·1D closed/open tuple
            thirty_minute_close -> 같은 경계의 확정 30분 source
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        pending_one_minute_open = self._pending_boundary_one_minute_open
        pending_thirty_minute_open = (
            self._pending_boundary_thirty_minute_open
        )

        # 모든 required interval을 한 update에 넣어 어느 arrival permutation에도 stale open을 노출하지 않는다.
        self._merge_klines_into_snapshot(boundary_sources, include_deferred=False)
        self._awaiting_post_boundary_thirty_minute_open = True
        self._clear_pending_atomic_boundary()

        # 4H close가 포함된 canonical batch만 같은 committed version에서 REGIME을 정확히 한 번 평가한다.
        if self._is_four_hour_boundary(boundary_time):
            regime_result = self._regime_controller.evaluate_regime(
                RegimeEvaluationTrigger.FOUR_HOUR_CANDLE_CLOSE,
                self._market_snapshot,
            )
            if (
                regime_result is None
                or regime_result.source_market_version
                != self._market_snapshot.version
            ):
                raise MarketDataStreamStateError(
                    "four-hour REGIME evaluation did not bind the market version"
                )
        self._publish_market_evaluation(
            thirty_minute_close,
            source_klines=boundary_sources,
        )

        # 경계와 독립적인 upper 진행 tick은 별도 version으로 보존한다.
        # source tuple 검증을 완화하거나 일봉을 strategy source로 위장하지 않는다.
        deferred_sources = tuple(self._deferred_live_klines.values())
        for deferred_source in deferred_sources:
            if deferred_source.interval not in (Interval.FOUR_HOURS, Interval.ONE_DAY):
                raise MarketDataStreamStateError("unexpected deferred strategy source at atomic boundary")
            self._merge_klines_into_snapshot((deferred_source,), include_deferred=False)
            self._publish_market_evaluation(deferred_source)

        # 30분 OPEN을 먼저 commit해야 뒤따르는 1분 source가 새 전략 candle에 정확히 결합된다.
        if pending_thirty_minute_open is not None:
            self._validate_snapshot_sequence(pending_thirty_minute_open)
            self._merge_klines_into_snapshot((pending_thirty_minute_open,))
            self._publish_market_evaluation(pending_thirty_minute_open)
            self._awaiting_post_boundary_thirty_minute_open = False
        if pending_one_minute_open is None:
            return
        if pending_thirty_minute_open is None:
            self._pending_post_boundary_one_minute = pending_one_minute_open
            return
        self._validate_snapshot_sequence(pending_one_minute_open)
        self._merge_klines_into_snapshot((pending_one_minute_open,))
        self._publish_market_evaluation(pending_one_minute_open)

    def _clear_pending_atomic_boundary(self) -> None:
        """
        함수 이름: _clear_pending_atomic_boundary()
        기능: 성공 commit 또는 generation 폐기 후 active boundary의 모든 bounded slot을 비운다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # Post-boundary 1m/30m replay 상태는 별도 lifecycle이므로 canonical source slot만 초기화한다.
        self._pending_boundary_one_minute_close = None
        self._pending_boundary_one_minute_open = None
        self._pending_boundary_thirty_minute_close = None
        self._pending_boundary_thirty_minute_open = None
        self._pending_boundary_four_hour_close = None
        self._pending_boundary_four_hour_open = None
        self._pending_boundary_one_day_close = None
        self._pending_boundary_one_day_open = None
        self._pending_boundary_time = None

    @staticmethod
    def _validate_stream_cursor(
        previous: Kline,
        current: Kline,
    ) -> bool:
        """
        함수 이름: _validate_stream_cursor()
        기능: interval 안의 event/open 시각 단조성과 exact duplicate 여부를 검증한다.
        인자: previous -> 직전에 성공 처리한 같은 interval Kline
            current -> 새로 관찰한 같은 interval Kline
        반환값: 완전히 같은 event라서 무시해야 하면 True
        작성 날짜: 2026/08/25
        """
        # REST Kline을 실시간 cursor로 잘못 사용하지 않도록 양쪽 event time을 요구한다.
        if previous.event_time is None or current.event_time is None:
            raise MarketDataStreamStateError(
                "stream cursors require WebSocket event times"
            )

        disposition = classify_kline(previous, current)
        if disposition == "conflict":
            raise MarketDataStreamStateError("conflicting Kline update requires resync")
        return disposition in ("duplicate", "stale")

    def _validate_snapshot_sequence(self, kline: Kline) -> None:
        """
        함수 이름: _validate_snapshot_sequence()
        기능: snapshot 최신 봉과 새 봉 사이의 gap·재개방·건너뛴 rollover를 차단한다.
        인자: kline -> snapshot에 반영하기 전 새 WebSocket Kline
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 최신 authoritative 봉에서 한 interval보다 멀리 뛴 event는 REST full-resync가 필요하다.
        latest_kline = self._market_snapshot.klines_by_interval[
            kline.interval
        ][-1]
        expected_next_open = (
            latest_kline.open_time
            + _INTERVAL_DURATION_BY_INTERVAL[kline.interval]
        )
        if kline.open_time > expected_next_open:
            raise MarketDataStreamStateError(
                "Kline gap requires a full market resynchronization"
            )
        if kline.open_time < latest_kline.open_time:
            raise MarketDataStreamStateError(
                "Kline is older than the authoritative snapshot"
            )

        # 4H rollover는 atomic coordinator가 소유하고 나머지는 close 이후 재개방을 금지한다.
        if (
            kline.interval is not Interval.FOUR_HOURS
            and kline.open_time == latest_kline.open_time
            and latest_kline.closed
            and not kline.closed
        ):
            raise MarketDataStreamStateError(
                "a closed Kline cannot become open again"
            )

        # 다음 봉으로 넘어가려면 직전 비-4H 봉의 확정 event가 먼저 snapshot에 있어야 한다.
        if (
            kline.interval is not Interval.FOUR_HOURS
            and kline.open_time == expected_next_open
            and not latest_kline.closed
        ):
            raise MarketDataStreamStateError(
                "new Kline arrived before the previous close"
            )

    def _observe_four_hour_kline(self, kline: Kline) -> bool:
        """
        함수 이름: _observe_four_hour_kline()
        기능: atomic boundary가 아닌 현재 진행 4H tick만 authoritative snapshot에 반영한다.
        인자: kline -> 단조성과 gap 검증을 마친 4H WebSocket Kline
        반환값: snapshot version을 실제 commit했으면 True
        작성 날짜: 2026/08/29
        """
        latest_kline = self._market_snapshot.klines_by_interval[
            Interval.FOUR_HOURS
        ][-1]
        # 4H close와 next-open은 1m·30m pair까지 포함한 atomic coordinator만 소유한다.
        if kline.closed or kline.open_time != latest_kline.open_time:
            raise MarketDataStreamStateError(
                "four-hour rollover requires an atomic boundary batch"
            )

        # 같은 4H open time의 최신 current-price tick만 단일-source version으로 공개한다.
        self._merge_klines_into_snapshot((kline,))
        return True

    def _merge_klines_into_snapshot(
        self,
        observed_klines: tuple[Kline, ...],
        *,
        include_deferred: bool = True,
    ) -> None:
        """
        함수 이름: _merge_klines_into_snapshot()
        기능: 새 봉을 open time으로 덮어쓴 전체 history를 구성해 snapshot을 원자 갱신한다.
        인자: observed_klines -> 한 version에 함께 반영할 정규화 WebSocket Kline tuple
            include_deferred -> 일반 경계의 대기 봉을 함께 반영할지 여부
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        for incoming in observed_klines:
            key = (incoming.interval, incoming.open_time)
            previous = self._deferred_live_klines.get(key)
            if previous is not None and previous.closed and not incoming.closed:
                continue
            self._deferred_live_klines[key] = incoming
        for interval in MARKET_INTERVALS:
            if sum(key[0] is interval for key in self._deferred_live_klines) > 2:
                raise MarketDataStreamStateError("pending boundary exceeded two candles per interval")
        if include_deferred:
            observed_klines = tuple(self._deferred_live_klines.values())
        # 기존 history를 복사한 뒤 같은 open time의 새 사실만 candidate mapping에서 교체한다.
        next_klines_by_interval = {
            interval: {
                existing_kline.open_time: existing_kline
                for existing_kline in self._market_snapshot.klines_by_interval[
                    interval
                ]
            }
            for interval in MARKET_INTERVALS
        }
        for observed_kline in observed_klines:
            next_klines_by_interval[observed_kline.interval][
                observed_kline.open_time
            ] = observed_kline

        # dedup 이후 주기별 limit을 적용하고 전체 네 interval을 단 한 번 update한다.
        bounded_klines_by_interval = {
            interval: tuple(
                sorted(
                    klines_by_open_time.values(),
                    key=lambda existing_kline: existing_kline.open_time,
                )[-self._kline_limit :]
            )
            for interval, klines_by_open_time in (
                next_klines_by_interval.items()
            )
        }
        try:
            self._market_snapshot.update(
                bounded_klines_by_interval,
                source_klines=observed_klines,
            )
        except ValueError as error:
            if str(error) in {
                "open Kline must contain the snapshot time",
                "closed Kline must end by the snapshot time",
                "Kline open time must not be in the future",
                "the open four-hour Kline must contain the snapshot time",
                "the latest four-hour Kline must be open",
                "only the latest four-hour Kline may be open",
            }:
                self._diagnostics.record(
                    "market_boundary_waiting", reason="SNAPSHOT_TIME_VALIDATION",
                    # 위 여섯 고정 검증 문구만 허용하며 임의 예외 원문은 기록하지 않는다.
                    validation_rule=str(error), pending_count=len(observed_klines),
                    boundary_time=self._pending_boundary_time,
                    market_version=self._market_snapshot.version,
                    last_snapshot_at=self._market_snapshot.updated_at,
                    pending_klines=observed_klines,
                    candidate_latest_klines={interval.value: klines[-1] for interval, klines in bounded_klines_by_interval.items()},
                )
                raise _PendingCandleBoundary() from error
            raise
        for committed_kline in observed_klines:
            self._deferred_live_klines.pop((committed_kline.interval, committed_kline.open_time), None)

    def _publish_market_evaluation(
        self,
        observed_kline: Kline,
        *,
        source_klines: tuple[Kline, ...] | None = None,
    ) -> None:
        """
        함수 이름: _publish_market_evaluation()
        기능: 주입된 확정 지표 builder 결과를 TradingController 공개 seam에 same-version으로 전달한다.
        인자: observed_kline -> 이번 snapshot version을 만든 원본 WebSocket Kline
            source_klines -> 복합 경계 version을 만든 canonical Kline tuple 또는 None
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        evaluation_builder = self._market_evaluation_builder
        market_observer = self._trading_market_observer
        if evaluation_builder is None or market_observer is None:
            return  # 미확정 30분 EMA·slope 식을 기본값으로 추측해 거래 event를 만들지 않는다.

        # Source identity를 한 번만 만들고 optional production observer에 builder 전 Kline 경계를 먼저 남긴다.
        source_event_id = self._create_source_event_id(
            observed_kline if source_klines is None else source_klines
        )
        self._diagnostics.record(
            "market_input_observed", source_event_id=source_event_id, market_version=self._market_snapshot.version,
            klines=(observed_kline,) if source_klines is None else source_klines,
            snapshot_source_klines=self._market_snapshot.update_source_klines,
        )  # 입력 봉과 계산 시점을 연결해 지표 계산 전에 멈춘 경우도 원본을 확인한다.
        boundary_observer = getattr(
            market_observer,
            "observe_public_market_boundary",
            None,
        )
        if callable(boundary_observer):
            boundary_observer(
                message_id="1L.1",
                event_type="KLINE_OBSERVED",
                source_event_id=source_event_id,
                market_version=self._market_snapshot.version,
            )

        # Builder가 읽은 현재 snapshot identity와 version을 바꾸지 않고 공개 seam에 전달한다.
        market_evaluation = evaluation_builder(
            self._market_snapshot,
            observed_kline,
        )
        if market_evaluation is None:
            self._diagnostics.record("market_evaluation_deferred", source_event_id=source_event_id, reason="BUILDER_RETURNED_NO_EVALUATION")
            return  # 30분 close 뒤 다음 OPEN을 기다리는 정상 cross-interval event는 게시하지 않는다.

        # 계산 완료 사실은 TradingController enqueue 전에 기록해 실제 Kline→평가 순서를 immutable하게 보존한다.
        if callable(boundary_observer):
            boundary_observer(
                message_id="1L.2",
                event_type="MARKET_EVALUATED",
                source_event_id=source_event_id,
                market_version=self._market_snapshot.version,
            )
        # 구체 MarketEvaluationSnapshot 검증은 계층 경계를 소유한 공개 TradingController가 수행한다.
        market_observer.observe_market_evaluation(
            market_evaluation,
            source_event_id=source_event_id,
            market_version=self._market_snapshot.version,
        )

    @staticmethod
    def _create_source_event_id(
        source: Kline | tuple[Kline, ...],
    ) -> str:
        """
        함수 이름: _create_source_event_id()
        기능: 비밀 없이 모든 source의 interval·open/event 시각·확정 상태를 결정적으로 직렬화한다.
        인자: source -> 단일 Kline 또는 canonical 순서의 복합 source tuple
        반환값: 재실행에도 같은 canonical source event ID
        작성 날짜: 2026/08/25
        """
        source_klines = source if isinstance(source, tuple) else (source,)
        if not source_klines:
            raise ValueError("source Kline tuple must not be empty")

        # API key나 payload 원문 없이 각 Kline의 canonical market key와 시각만 포함한다.
        source_event_ids: list[str] = []
        for kline in source_klines:
            if not isinstance(kline, Kline):
                raise TypeError("source must contain only Kline")
            if kline.event_time is None:
                raise ValueError("source Kline must contain an event time")
            open_time = MarketDataController._format_utc_datetime(
                kline.open_time
            )
            event_time = MarketDataController._format_utc_datetime(
                kline.event_time
            )
            close_state = "closed" if kline.closed else "open"
            source_event_ids.append(
                f"kline:{kline.symbol}:{kline.interval.value}:"
                f"{open_time}:{event_time}:{close_state}"
            )

        if len(source_event_ids) == 1:
            return source_event_ids[0]
        return "kline-batch|" + "|".join(source_event_ids)

    @staticmethod
    def _format_utc_datetime(value: datetime) -> str:
        """
        함수 이름: _format_utc_datetime()
        기능: UTC datetime을 공백 없는 Z suffix canonical 문자열로 직렬화한다.
        인자: value -> Kline에서 이미 UTC 검증된 datetime
        반환값: microsecond를 보존한 ISO 8601 UTC 문자열
        작성 날짜: 2026/08/25
        """
        return value.isoformat().replace("+00:00", "Z")  # 검증된 UTC offset만 canonical Z로 바꾼다.

    def _normalize_symbol(self, symbol: str) -> str:
        """
        함수 이름: _normalize_symbol()
        기능: 외부 입력 symbol을 Binance에서 사용하는 대문자 형식으로 검증한다.
        인자: symbol -> 호출자가 전달한 거래 symbol
        반환값: 공백을 제거한 대문자 거래 symbol
        작성 날짜: 2026/08/20
        """
        if not isinstance(symbol, str):
            raise TypeError("symbol must be a string")

        normalized_symbol = symbol.strip().upper()
        if (
            not normalized_symbol
            or not normalized_symbol.isascii()
            or not normalized_symbol.isalnum()
        ):
            raise ValueError("symbol must be a non-empty alphanumeric string")

        return normalized_symbol
