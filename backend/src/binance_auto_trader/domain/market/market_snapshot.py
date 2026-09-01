"""네 Kline 시간 주기를 원자적으로 보존하는 MarketSnapshot을 정의한다."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType

from ..common import Interval, SUPPORTED_INTERVALS
from .kline import (
    Kline,
    _normalize_utc_datetime,
    _validate_normalized_symbol,
)


_INTERVAL_DURATION_BY_INTERVAL: Mapping[Interval, timedelta] = MappingProxyType(
    {
        Interval.ONE_MINUTE: timedelta(minutes=1),
        Interval.THIRTY_MINUTES: timedelta(minutes=30),
        Interval.FOUR_HOURS: timedelta(hours=4),
        Interval.ONE_DAY: timedelta(days=1),
    }
)
_SUPPORTED_MARKET_SYMBOL = "ETHUSDT"


def _utc_now() -> datetime:
    """
    함수 이름: _utc_now()
    기능: MarketSnapshot 기본 clock에 사용할 현재 UTC 시각을 반환한다.
    인자: 없음
    반환값: 현재 UTC datetime
    작성 날짜: 2026/08/20
    """
    return datetime.now(timezone.utc)


def _validate_continuity(
    interval: Interval,
    sorted_klines: tuple[Kline, ...],
) -> None:
    """
    함수 이름: _validate_continuity()
    기능: 정렬된 Kline의 open_time이 주기 간격과 정확히 일치하는지 검증한다.
    인자: interval -> 검증할 canonical 시간 주기
        sorted_klines -> open_time 순으로 정렬된 Kline tuple
    반환값: 없음
    작성 날짜: 2026/08/20
    """
    expected_duration = _INTERVAL_DURATION_BY_INTERVAL[interval]

    for previous_kline, current_kline in zip(
        sorted_klines,
        sorted_klines[1:],
    ):
        actual_duration = current_kline.open_time - previous_kline.open_time
        if actual_duration != expected_duration:
            raise ValueError(
                f"{interval.value} klines must be exactly continuous"
            )


def _validate_four_hour_current_candle(
    sorted_klines: tuple[Kline, ...],
    updated_at: datetime,
) -> None:
    """
    함수 이름: _validate_four_hour_current_candle()
    기능: 최신 4시간봉 하나만 진행 중이어서 현재가를 나타내는지 검증한다.
    인자: sorted_klines -> 시간순으로 정렬된 4시간봉 tuple
        updated_at -> snapshot이 갱신되는 UTC 시각
    반환값: 없음
    작성 날짜: 2026/08/20
    """
    if any(not kline.closed for kline in sorted_klines[:-1]):
        raise ValueError("only the latest four-hour Kline may be open")
    if sorted_klines[-1].closed:
        raise ValueError("the latest four-hour Kline must be open")

    latest_open_time = sorted_klines[-1].open_time
    latest_close_boundary = latest_open_time + timedelta(hours=4)
    if not latest_open_time <= updated_at < latest_close_boundary:
        raise ValueError(
            "the open four-hour Kline must contain the snapshot time"
        )


def _validate_klines_at_snapshot_time(
    interval: Interval,
    sorted_klines: tuple[Kline, ...],
    updated_at: datetime,
) -> None:
    """
    함수 이름: _validate_klines_at_snapshot_time()
    기능: 봉의 open/closed 상태가 snapshot 시각보다 미래이거나 stale open이 아닌지 검증한다.
    인자: interval -> 검증할 canonical 시간 주기
        sorted_klines -> 시간순으로 정렬된 Kline tuple
        updated_at -> snapshot이 갱신되는 UTC 시각
    반환값: 없음
    작성 날짜: 2026/08/20
    """
    interval_duration = _INTERVAL_DURATION_BY_INTERVAL[interval]

    for kline in sorted_klines:
        close_boundary = kline.open_time + interval_duration
        if kline.open_time > updated_at:
            raise ValueError("Kline open time must not be in the future")
        if kline.closed and close_boundary > updated_at:
            raise ValueError("closed Kline must end by the snapshot time")
        if not kline.closed and not (
            kline.open_time <= updated_at < close_boundary
        ):
            raise ValueError("open Kline must contain the snapshot time")


@dataclass(frozen=True, slots=True)
class MarketStateSnapshot:
    """
    클래스 이름: MarketStateSnapshot
    기능: 한 version의 MarketSnapshot 공개 상태와 source Kline을 단일 불변 값으로 묶는다.
    작성 날짜: 2026/09/01
    """

    klines_by_interval: Mapping[Interval, tuple[Kline, ...]]
    current_eth_price: Decimal | None
    version: int
    updated_at: datetime | None
    ready: bool
    update_source_klines: tuple[Kline, ...]


class MarketSnapshot:
    """
    클래스 이름: MarketSnapshot
    기능: 한 symbol의 네 시간 주기 Kline을 검증한 뒤 원자적으로 갱신한다.
    작성 날짜: 2026/08/20
    """

    __slots__ = (
        "_clock",
        "_state",
        "_symbol",
    )

    def __init__(
        self,
        symbol: str = "ETHUSDT",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 비어 있고 준비되지 않은 version 0 시장 snapshot을 생성한다.
        인자: symbol -> snapshot이 보존할 정규화된 거래 symbol
            clock -> 성공한 update 시각을 제공할 UTC clock
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        _validate_normalized_symbol(symbol)
        if symbol != _SUPPORTED_MARKET_SYMBOL:
            raise ValueError("MarketSnapshot supports only ETHUSDT")

        selected_clock = _utc_now if clock is None else clock
        if not callable(selected_clock):
            raise TypeError("clock must be callable")

        empty_klines = {
            interval: ()
            for interval in SUPPORTED_INTERVALS
        }
        self._symbol = symbol
        self._clock = selected_clock
        self._state = MarketStateSnapshot(
            klines_by_interval=MappingProxyType(empty_klines),
            current_eth_price=None,
            version=0,
            updated_at=None,
            ready=False,
            update_source_klines=(),
        )

    @property
    def symbol(self) -> str:
        """
        함수 이름: symbol()
        기능: snapshot이 보존하는 정규화된 거래 symbol을 반환한다.
        인자: 없음
        반환값: 거래 symbol
        작성 날짜: 2026/08/20
        """
        return self._symbol

    @property
    def klines_by_interval(self) -> Mapping[Interval, tuple[Kline, ...]]:
        """
        함수 이름: klines_by_interval()
        기능: 외부에서 변경할 수 없는 주기별 Kline tuple mapping을 반환한다.
        인자: 없음
        반환값: 읽기 전용 주기별 Kline mapping
        작성 날짜: 2026/08/20
        """
        return self._state.klines_by_interval

    @property
    def current_eth_price(self) -> Decimal | None:
        """
        함수 이름: current_eth_price()
        기능: 준비된 snapshot의 최신 4시간봉 종가 또는 초기 None을 반환한다.
        인자: 없음
        반환값: 최신 ETH 가격 또는 None
        작성 날짜: 2026/08/20
        """
        return self._state.current_eth_price

    @property
    def version(self) -> int:
        """
        함수 이름: version()
        기능: 성공한 전체 snapshot update 횟수를 반환한다.
        인자: 없음
        반환값: 0부터 단조 증가하는 snapshot version
        작성 날짜: 2026/08/20
        """
        return self._state.version

    @property
    def updated_at(self) -> datetime | None:
        """
        함수 이름: updated_at()
        기능: 마지막 성공 update의 UTC 시각 또는 초기 None을 반환한다.
        인자: 없음
        반환값: 마지막 갱신 UTC datetime 또는 None
        작성 날짜: 2026/08/20
        """
        return self._state.updated_at

    @property
    def ready(self) -> bool:
        """
        함수 이름: ready()
        기능: 네 주기의 일관된 Kline이 모두 준비됐는지 반환한다.
        인자: 없음
        반환값: 전체 snapshot 준비 여부
        작성 날짜: 2026/08/20
        """
        return self._state.ready

    @property
    def update_source_klines(self) -> tuple[Kline, ...]:
        """
        함수 이름: update_source_klines()
        기능: 현재 version을 만든 WebSocket Kline 원본 또는 full update의 빈 tuple을 반환한다.
        인자: 없음
        반환값: 이번 version에 함께 반영된 불변 Kline tuple
        작성 날짜: 2026/08/29
        """
        return self._state.update_source_klines

    def get_snapshot(self) -> MarketStateSnapshot:
        """
        함수 이름: get_snapshot()
        기능: 한 mutation version의 전체 시장 상태와 source를 불변 snapshot으로 반환한다.
        인자: 없음
        반환값: 후속 mutation으로 변경되지 않는 MarketStateSnapshot
        작성 날짜: 2026/09/01
        """
        return self._state  # 단일 불변 pointer로 version과 원인 Kline을 같이 고정한다.

    def update(
        self,
        klines_by_interval: Mapping[Interval, Iterable[Kline]],
        *,
        source_klines: Iterable[Kline] | None = None,
    ) -> None:
        """
        함수 이름: update()
        기능: 전체 Kline 입력을 검증하고 dedup·정렬한 뒤 snapshot을 원자적으로 교체한다.
        인자: klines_by_interval -> canonical 주기별 전체 Kline 입력
            source_klines -> 이번 version을 발생시킨 WebSocket Kline 또는 full update의 None
        반환값: 없음
        작성 날짜: 2026/08/20
        """
        if not isinstance(klines_by_interval, Mapping):
            raise TypeError("klines_by_interval must be a mapping")

        provided_intervals = tuple(klines_by_interval.keys())
        if any(
            not isinstance(interval, Interval)
            for interval in provided_intervals
        ):
            raise TypeError("all mapping keys must be canonical Intervals")

        if set(provided_intervals) != set(SUPPORTED_INTERVALS):
            raise ValueError(
                "klines_by_interval must contain exactly all supported intervals"
            )

        next_klines_by_interval: dict[
            Interval,
            tuple[Kline, ...],
        ] = {}

        for interval in SUPPORTED_INTERVALS:
            try:
                incoming_klines = tuple(klines_by_interval[interval])
            except TypeError as error:
                raise TypeError(
                    f"{interval.value} klines must be iterable"
                ) from error

            if not incoming_klines:
                raise ValueError(
                    f"{interval.value} klines must not be empty"
                )

            deduplicated_klines: dict[
                tuple[str, Interval, datetime],
                Kline,
            ] = {}
            for kline in incoming_klines:
                if not isinstance(kline, Kline):
                    raise TypeError("all interval values must contain Kline")

                if kline.symbol != self._symbol:
                    raise ValueError("kline symbol must match snapshot symbol")

                if kline.interval is not interval:
                    raise ValueError(
                        "kline interval must match its mapping interval"
                    )

                deduplication_key = (
                    kline.symbol,
                    kline.interval,
                    kline.open_time,
                )
                deduplicated_klines[deduplication_key] = kline

            sorted_klines = tuple(
                sorted(
                    deduplicated_klines.values(),
                    key=lambda kline: kline.open_time,
                )
            )
            _validate_continuity(interval, sorted_klines)
            next_klines_by_interval[interval] = sorted_klines

        next_updated_at = _normalize_utc_datetime(
            self._clock(),
            "updated_at",
        )
        for interval in SUPPORTED_INTERVALS:
            _validate_klines_at_snapshot_time(
                interval,
                next_klines_by_interval[interval],
                next_updated_at,
            )
        _validate_four_hour_current_candle(
            next_klines_by_interval[Interval.FOUR_HOURS],
            next_updated_at,
        )

        # Live provenance는 최종 candidate history에 exact value로 남은 typed Kline만 허용한다.
        if source_klines is None:
            normalized_source_klines: tuple[Kline, ...] = ()
        else:
            try:
                normalized_source_klines = tuple(source_klines)
            except TypeError as error:
                raise TypeError("source_klines must be iterable") from error
            if not normalized_source_klines:
                raise ValueError("source_klines must not be empty when provided")
            for source_kline in normalized_source_klines:
                if not isinstance(source_kline, Kline):
                    raise TypeError("source_klines must contain only Kline")
                if source_kline not in next_klines_by_interval[
                    source_kline.interval
                ]:
                    raise ValueError(
                        "every source Kline must be present in the next snapshot"
                    )

        next_current_eth_price = next_klines_by_interval[
            Interval.FOUR_HOURS
        ][-1].close
        next_read_only_klines = MappingProxyType(next_klines_by_interval)
        next_state = MarketStateSnapshot(
            klines_by_interval=next_read_only_klines,
            current_eth_price=next_current_eth_price,
            version=self._state.version + 1,
            updated_at=next_updated_at,
            ready=True,
            update_source_klines=normalized_source_klines,
        )

        self._state = next_state

    def get_current_eth_price(self) -> Decimal:
        """
        함수 이름: get_current_eth_price()
        기능: 준비된 snapshot의 최신 4시간봉 종가를 반환한다.
        인자: 없음
        반환값: 최신 ETH 가격 Decimal
        작성 날짜: 2026/08/20
        """
        current_state = self._state
        if not current_state.ready or current_state.current_eth_price is None:
            raise RuntimeError("MarketSnapshot is not ready")

        return current_state.current_eth_price
