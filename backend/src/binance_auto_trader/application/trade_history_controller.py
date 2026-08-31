"""Trade history 복원·상세 조회·durable execution publication을 조정한다."""

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock
from typing import Protocol
from zoneinfo import ZoneInfo

from binance_auto_trader.domain.history import (
    CSVExportOptions,
    CSVExportResult,
    HistoryPeriod,
    Performance,
    Trade,
    TradeHistory,
    TradeHistoryQuery,
    TradeSide,
)
from binance_auto_trader.domain.trading.account import Account
from binance_auto_trader.domain.trading.order import (
    ExecutionSummary,
    Order,
    PendingOrderRecoveryLifecycle,
    PendingOrderRecoveryRecord,
)
from binance_auto_trader.domain.trading.risk import ManualKillControlState
from binance_auto_trader.domain.trading.states import OrderSide


_HOLDINGS_ASSET = "ETH"
_KOREA_TIME_ZONE = ZoneInfo("Asia/Seoul")


def _utc_now() -> datetime:
    """
    함수 이름: _utc_now()
    기능: 거래 상세 기간과 Performance account day를 결정할 현재 UTC 시각을 반환한다.
    인자: 없음
    반환값: timezone-aware 현재 UTC datetime
    작성 날짜: 2026/08/23
    """
    return datetime.now(timezone.utc)  # 기간 preset의 기준 시각은 항상 UTC clock에서 시작한다.


class TradeHistoryPersistencePendingError(RuntimeError):
    """
    클래스 이름: TradeHistoryPersistencePendingError
    기능: 미완료 durable save가 있는 동안 새 history operation을 차단한다.
    작성 날짜: 2026/08/22
    """

    code = "TRADE_HISTORY_PERSISTENCE_PENDING"


class CSVExportUnavailableError(RuntimeError):
    """
    클래스 이름: CSVExportUnavailableError
    기능: CSV export writer가 조립되지 않은 application 구성을 typed 오류로 알린다.
    작성 날짜: 2026/08/23
    """

    code = "CSV_EXPORT_UNAVAILABLE"


class TradeHistoryRepositoryPort(Protocol):
    """
    클래스 이름: TradeHistoryRepositoryPort
    기능: TradeHistoryController가 startup 복원·durable 저장·CSV stream에 요구하는 port를 정의한다.
    작성 날짜: 2026/08/23
    """

    def get_trade_history(self) -> tuple[Trade, ...]:
        """
        함수 이름: get_trade_history()
        기능: durable storage에서 복원한 canonical Trade tuple을 반환한다.
        인자: 없음
        반환값: startup Trade tuple
        작성 날짜: 2026/08/21
        """
        ...

    def save_this_trade_by_order_id(
        self,
        order_id: int | str,
        trade: Trade,
    ) -> None:
        """
        함수 이름: save_this_trade_by_order_id()
        기능: 한 terminal Trade를 order ID 기준으로 durable 저장한다.
        인자: order_id -> exchange order ID
            trade -> 저장할 canonical Trade
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        ...

    def stream_trades(self, query: TradeHistoryQuery) -> Iterator[Trade]:
        """
        함수 이름: stream_trades()
        기능: KST 날짜와 side 조건에 맞는 durable Trade를 iterator로 반환한다.
        인자: query -> 검증된 거래 이력 조회 조건
        반환값: 전체 이력을 복제하지 않는 Trade iterator
        작성 날짜: 2026/08/23
        """
        ...

    def flush_durable_state(self) -> None:
        """
        함수 이름: flush_durable_state()
        기능: 현재 history, pending-order와 manual-kill journal을 명시적인 fsync 경계까지 내린다.
        인자: 없음
        반환값: 세 저장소의 durability 확인이 끝나면 없음
        작성 날짜: 2026/08/24
        """
        ...


class CSVExportWriterPort(Protocol):
    """
    클래스 이름: CSVExportWriterPort
    기능: Controller가 filesystem 구현을 import하지 않고 CSV를 게시하는 port를 정의한다.
    작성 날짜: 2026/08/23
    """

    def write_csv(
        self,
        trades: Iterable[Trade],
        options: CSVExportOptions,
    ) -> CSVExportResult:
        """
        함수 이름: write_csv()
        기능: Trade stream을 검증된 위치에 CSV로 원자 게시한다.
        인자: trades -> 조회 조건에 맞는 Trade iterable
            options -> 저장 위치와 파일명을 포함한 CSV option
        반환값: 절대 파일 경로와 실제 data row 수
        작성 날짜: 2026/08/23
        """
        ...


@dataclass(frozen=True, slots=True)
class TradeDetailsResult:
    """
    클래스 이름: TradeDetailsResult
    기능: 적용 query, 필터 행, ETH 보유량 provenance와 전체 Performance를 불변으로 묶는다.
    작성 날짜: 2026/08/23
    """

    query: TradeHistoryQuery
    rows: tuple[Trade, ...]
    holdings_asset: str
    holdings: Decimal
    account_version: int
    performance: Performance

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 상세 결과가 canonical query·행·Account provenance와 Performance만 담는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # Transport가 duck typing 결과를 직렬화하지 않도록 domain 객체와 tuple을 고정한다.
        if not isinstance(self.query, TradeHistoryQuery):
            raise TypeError("query must be a TradeHistoryQuery")
        if not isinstance(self.rows, tuple):
            raise TypeError("rows must be a tuple")
        if any(not isinstance(row, Trade) for row in self.rows):
            raise TypeError("rows must contain only Trade values")
        if not isinstance(self.performance, Performance):
            raise TypeError("performance must be a Performance")

        # Account에서 읽은 ETH 보유량과 이를 식별할 단조 version의 값 범위를 검증한다.
        if self.holdings_asset != _HOLDINGS_ASSET:
            raise ValueError("holdings_asset must be ETH")
        if not isinstance(self.holdings, Decimal):
            raise TypeError("holdings must be a Decimal")
        if not self.holdings.is_finite():
            raise ValueError("holdings must be finite")
        if self.holdings < Decimal("0"):
            raise ValueError("holdings must not be negative")
        if type(self.account_version) is not int:
            raise TypeError("account_version must be an int")
        if self.account_version < 0:
            raise ValueError("account_version must not be negative")

    @property
    def trades(self) -> tuple[Trade, ...]:
        """
        함수 이름: trades()
        기능: Communication Diagram의 TradeDetailsResult.trades 이름으로 필터 행을 반환한다.
        인자: 없음
        반환값: rows와 동일한 immutable Trade tuple
        작성 날짜: 2026/08/23
        """
        return self.rows  # transport의 rows와 Diagram의 trades가 같은 결과를 공유한다.


@dataclass(frozen=True, slots=True)
class _TradeDetailsSummaryState:
    """
    클래스 이름: _TradeDetailsSummaryState
    기능: 최초 상세 조회와 같은 Account version·KST 날짜의 filter가 재사용할 summary를 보존한다.
    작성 날짜: 2026/08/23
    """

    holdings: Decimal
    account_version: int
    performance: Performance
    kst_date: date


@dataclass(frozen=True, slots=True)
class _TradeHistoryLoadState:
    """
    클래스 이름: _TradeHistoryLoadState
    기능: 같은 durable 거래 목록에서 만든 TradeHistory와 Performance를 원자적으로 묶는다.
    작성 날짜: 2026/08/21
    """

    trade_history: TradeHistory
    performance: Performance


@dataclass(frozen=True, slots=True)
class _PendingPublication:
    """
    클래스 이름: _PendingPublication
    기능: 저장 재시도 성공 뒤 함께 게시할 Trade와 history/performance 후보를 묶는다.
    작성 날짜: 2026/08/22
    """

    trade: Trade
    state: _TradeHistoryLoadState


class TradeHistoryController:
    """
    클래스 이름: TradeHistoryController
    기능: startup 복원, 상세·CSV 조회와 terminal execution의 durable 저장 및 publication을 조정한다.
    작성 날짜: 2026/08/23
    """

    __slots__ = (
        "_account",
        "_clock",
        "_csv_export_writer",
        "_details_summary_state",
        "_operation_lock",
        "_pending_publications",
        "_repository",
        "_state",
        "_state_lock",
        "_supports_manual_kill_control_recovery",
        "_supports_pending_order_recovery",
        "_trade_update_observer",
    )

    def __init__(
        self,
        repository: TradeHistoryRepositoryPort,
        clock: Callable[[], datetime] | None = None,
        *,
        account: Account | None = None,
        csv_export_writer: CSVExportWriterPort | None = None,
        trade_update_observer: Callable[[Trade, Performance], object]
        | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: repository, CSV writer, 공유 Account, observer와 빈 history/performance state를 준비한다.
        인자: repository -> startup Trade tuple을 제공할 persistence port
            clock -> Performance의 현재 KST 날짜를 결정할 optional UTC clock
            account -> authoritative ETH 보유량을 제공할 shared Account
            csv_export_writer -> Trade stream을 실제 CSV 파일로 게시할 optional port
            trade_update_observer -> durable Trade publication 직후 호출할 optional observer
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # runtime Protocol과 주입 dependency를 만족하지 않는 값을 state 생성 전에 거부한다.
        if repository is None or not callable(
            getattr(repository, "get_trade_history", None)
        ):
            raise TypeError("repository must provide get_trade_history")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        selected_account = Account() if account is None else account
        if not isinstance(selected_account, Account):
            raise TypeError("account must be an Account")
        if trade_update_observer is not None and not callable(
            trade_update_observer
        ):
            raise TypeError("trade_update_observer must be callable")
        if csv_export_writer is not None and not callable(
            getattr(csv_export_writer, "write_csv", None)
        ):
            raise TypeError("csv_export_writer must provide write_csv")

        # load와 record가 서로의 candidate를 덮지 않게 operation lock 하나로 직렬화한다.
        self._repository = repository
        self._account = selected_account
        self._clock = _utc_now if clock is None else clock
        self._csv_export_writer = csv_export_writer
        self._details_summary_state: _TradeDetailsSummaryState | None = None
        self._operation_lock = RLock()
        self._pending_publications: dict[str, _PendingPublication] = {}
        self._trade_update_observer = trade_update_observer
        pending_order_operation_names = (
            "delete_pending_order",
            "get_pending_order_recovery_records",
            "get_pending_order_submission_counts",
            "mark_pending_order_submission_rejected",
            "save_pending_order",
            "transition_pending_order_lifecycle",
        )
        self._supports_pending_order_recovery = all(
            callable(getattr(repository, operation_name, None))
            for operation_name in pending_order_operation_names
        )  # 기존 history-only fake는 optional capability 없이도 계속 조립할 수 있다.
        manual_kill_control_operation_names = (
            "get_manual_kill_control_replay",
            "get_manual_kill_control_state",
            "save_manual_kill_control_state",
        )
        self._supports_manual_kill_control_recovery = all(
            callable(getattr(repository, operation_name, None))
            for operation_name in manual_kill_control_operation_names
        )  # 기존 fake는 그대로 두고 concrete JSONL adapter만 restart capability를 알린다.
        self._state_lock = RLock()
        self._state = _TradeHistoryLoadState(
            trade_history=TradeHistory(),
            performance=Performance((), clock=self._clock),
        )

    @property
    def account(self) -> Account:
        """
        함수 이름: account()
        기능: 상세 조회에 사용하는 authoritative shared Account를 반환한다.
        인자: 없음
        반환값: 생성자에서 주입하거나 호환 경로로 생성한 Account
        작성 날짜: 2026/08/23
        """
        return self._account  # bootstrap은 identity로 shared Account 조립을 검증할 수 있다.

    @property
    def trade_history(self) -> TradeHistory:
        """
        함수 이름: trade_history()
        기능: 마지막 성공 load에서 publish한 TradeHistory를 반환한다.
        인자: 없음
        반환값: 현재 TradeHistory
        작성 날짜: 2026/08/22
        """
        with self._state_lock:
            return self._state.trade_history  # history와 performance가 공유하는 한 state에서 읽는다.

    @property
    def performance(self) -> Performance:
        """
        함수 이름: performance()
        기능: 마지막 성공 load에서 TradeHistory와 함께 publish한 Performance를 반환한다.
        인자: 없음
        반환값: 현재 Performance
        작성 날짜: 2026/08/21
        """
        with self._state_lock:
            return self._state.performance.get_performance()  # 같은 근거로 KST 날짜 경계만 현재 clock에 맞게 갱신한다.

    def get_trade_details(
        self,
        period: HistoryPeriod = HistoryPeriod.TODAY,
        side: TradeSide = TradeSide.ALL,
    ) -> TradeDetailsResult:
        """
        함수 이름: get_trade_details()
        기능: KST 기간·side query의 Trade와 authoritative ETH 보유량 및 전체 성과를 결합한다.
        인자: period -> 오늘·최근 7일·최근 30일·전체 기간 preset
            side -> 전체·매수·매도 거래 방향
        반환값: 적용 query와 필터 행 및 요약 provenance를 담은 TradeDetailsResult
        작성 날짜: 2026/08/23
        """
        # 최초 summary가 KST 자정을 가로지르면 새 account day에서 한 번 다시 결합한다.
        with self._operation_lock:
            for summary_attempt in range(2):
                query = self._build_trade_history_query(period, side)
                with self._state_lock:
                    current_state = self._state
                    rows = current_state.trade_history.find(query)

                # 최초 상세 조회에서만 1.1.2.3~.4를 읽고 filter 조회는 기존 summary를 유지한다.
                summary_state = self._details_summary_state
                summary_is_current = (
                    summary_state is not None
                    and summary_state.account_version == self._account.version
                    and summary_state.kst_date == query.end_date
                )
                if not summary_is_current:
                    holdings, account_version = self._read_eth_holdings_snapshot()
                    with self._state_lock:
                        performance = current_state.performance.get_performance()
                    if self._read_current_kst_date() != query.end_date:
                        if summary_attempt == 0:
                            continue  # 자정 뒤 query와 Performance를 같은 새 날짜로 다시 만든다.
                        raise RuntimeError(
                            "clock crossed the KST date boundary repeatedly"
                        )
                    summary_state = _TradeDetailsSummaryState(
                        holdings=holdings,
                        account_version=account_version,
                        performance=performance,
                        kst_date=query.end_date,
                    )
                    self._details_summary_state = summary_state

                # Warm cache의 find가 자정을 가로질러도 전날 query를 반환하지 않고 한 번 재시도한다.
                if self._read_current_kst_date() != query.end_date:
                    if summary_attempt == 0:
                        continue
                    raise RuntimeError(
                        "clock crossed the KST date boundary repeatedly"
                    )

                return TradeDetailsResult(
                    query=query,
                    rows=rows,
                    holdings_asset=_HOLDINGS_ASSET,
                    holdings=summary_state.holdings,
                    account_version=summary_state.account_version,
                    performance=summary_state.performance,
                )  # 필터 행과 기존 D-12 summary snapshot의 범위를 섞지 않는다.

        raise RuntimeError("trade details summary could not be stabilized")

    def export_csv(self, options: CSVExportOptions) -> CSVExportResult:
        """
        함수 이름: export_csv()
        기능: KST preset을 확정하고 ALL-side durable Trade stream을 CSV writer에 전달한다.
        인자: options -> backend에서 다시 검증한 CSV export option
        반환값: 생성된 절대 경로와 실제 data row 수를 담은 CSVExportResult
        작성 날짜: 2026/08/23
        """
        # Application 경계는 동형 DTO 대신 검증된 domain value만 받아 파일 작업으로 넘긴다.
        if not isinstance(options, CSVExportOptions):
            raise TypeError("options must be a CSVExportOptions")

        with self._operation_lock:
            # 불확실한 durable save가 있으면 export snapshot에 포함되는지 추측하지 않는다.
            if self._pending_publications:
                raise TradeHistoryPersistencePendingError(
                    "pending trade persistence blocks CSV export"
                )

            # Preset 날짜는 renderer가 보낸 draft가 아니라 backend KST clock으로 다시 확정한다.
            current_kst_date = self._read_current_kst_date()
            start_date, end_date = options.resolve_dates(current_kst_date)
            query = TradeHistoryQuery(
                start_date=start_date,
                end_date=end_date,
                side=TradeSide.ALL,
            )  # CSV 범위는 양끝 포함 KST LocalDate이며 side는 항상 ALL이다.

            # Repository와 writer의 optional capability를 호출 직전에 확인해 기존 test fake를 보존한다.
            stream_trades = getattr(self._repository, "stream_trades", None)
            if not callable(stream_trades):
                raise CSVExportUnavailableError(
                    "repository does not support CSV streaming"
                )
            csv_export_writer = self._csv_export_writer
            if csv_export_writer is None:
                raise CSVExportUnavailableError(
                    "CSV export writer is not configured"
                )

            # Repository가 현재 durable byte/index 경계를 lock 안에서 snapshot iterator로 고정한다.
            trade_iterator = stream_trades(query)

        # 장시간 파일 쓰기는 terminal Trade publication을 막지 않도록 operation lock 밖에서 수행한다.
        result = csv_export_writer.write_csv(
            trade_iterator,
            options,
        )
        if not isinstance(result, CSVExportResult):
            raise TypeError("CSV writer must return CSVExportResult")

        return result  # 성공 receipt는 writer가 실제 게시를 마친 뒤에만 반환된다.

    def _read_current_kst_date(self) -> date:
        """
        함수 이름: _read_current_kst_date()
        기능: 주입 UTC clock을 검증하고 현재 Asia/Seoul LocalDate로 변환한다.
        인자: 없음
        반환값: timezone-aware UTC clock이 가리키는 KST date
        작성 날짜: 2026/08/23
        """
        # 주입 clock도 Performance와 동일하게 timezone-aware UTC만 허용한다.
        current_time = self._clock()
        if not isinstance(current_time, datetime):
            raise TypeError("clock result must be a datetime")
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("clock result must be timezone-aware UTC")
        if current_time.utcoffset() != timedelta(0):
            raise ValueError("clock result must use UTC")

        return current_time.astimezone(_KOREA_TIME_ZONE).date()

    def _stabilize_performance_account_day(
        self,
        performance: Performance,
    ) -> Performance:
        """
        함수 이름: _stabilize_performance_account_day()
        기능: durable publication 직전 Performance를 경계 전후가 같은 KST account day로 갱신한다.
        인자: performance -> 아직 publish되지 않은 전체 Performance candidate
        반환값: 현재 KST 날짜의 daily aggregate를 가진 같은 Performance
        작성 날짜: 2026/08/23
        """
        if not isinstance(performance, Performance):
            raise TypeError("performance must be a Performance")

        # Clock이 자정을 가로지르면 첫 계산을 버리고 새 account day에서 한 번 다시 계산한다.
        for account_day_attempt in range(2):
            account_day_before = self._read_current_kst_date()
            current_performance = performance.get_performance()
            account_day_after = self._read_current_kst_date()
            if account_day_before == account_day_after:
                return current_performance  # 내부 clock 호출도 두 같은 날짜 사이에서 실행됐다.
            if account_day_attempt == 0:
                continue

        raise RuntimeError("performance account day could not be stabilized")

    def _build_trade_history_query(
        self,
        period: HistoryPeriod,
        side: TradeSide,
    ) -> TradeHistoryQuery:
        """
        함수 이름: _build_trade_history_query()
        기능: HistoryPeriod와 TradeSide를 현재 KST 양끝 포함 날짜 query로 변환한다.
        인자: period -> 변환할 canonical HistoryPeriod
            side -> 같은 query에 결합할 canonical TradeSide
        반환값: KST LocalDate 범위를 담은 TradeHistoryQuery
        작성 날짜: 2026/08/23
        """
        # Wire 문자열을 application 경계에서 묵시적으로 enum으로 바꾸지 않는다.
        if not isinstance(period, HistoryPeriod):
            raise TypeError("period must be the canonical HistoryPeriod")
        if not isinstance(side, TradeSide):
            raise TypeError("side must be the canonical TradeSide")

        current_kst_date = self._read_current_kst_date()

        # 최근 N일은 오늘을 1일로 세고 ALL도 미래 거래를 포함하지 않게 오늘에서 닫는다.
        if period is HistoryPeriod.ALL:
            start_date = date.min
        else:
            lookback_days = {
                HistoryPeriod.TODAY: 0,
                HistoryPeriod.LAST_7_DAYS: 6,
                HistoryPeriod.LAST_30_DAYS: 29,
            }[period]
            start_date = current_kst_date - timedelta(days=lookback_days)

        return TradeHistoryQuery(
            start_date=start_date,
            end_date=current_kst_date,
            side=side,
        )  # 조회 조건 하나가 기간과 방향을 동시에 보존한다.

    def _read_eth_holdings_snapshot(self) -> tuple[Decimal, int]:
        """
        함수 이름: _read_eth_holdings_snapshot()
        기능: Account update와 경합하지 않는 ETH 보유량 및 동일 version을 읽는다.
        인자: 없음
        반환값: authoritative ETH holdings와 이를 만든 Account version tuple
        작성 날짜: 2026/08/23
        """
        # Account의 immutable state 교체 사이에서 version이 같을 때만 두 값을 한 snapshot으로 채택한다.
        while True:
            account_version = self._account.version
            holdings = self._account.get_holdings(_HOLDINGS_ASSET)
            if self._account.version == account_version:
                return holdings, account_version  # 반환 version은 holdings의 source provenance다.

    def _notify_trade_update(
        self,
        trade: Trade,
        performance: Performance,
    ) -> None:
        """
        함수 이름: _notify_trade_update()
        기능: durable state publication 뒤 optional observer를 한 번 호출하고 실패를 격리한다.
        인자: trade -> 방금 durable 저장 및 게시한 Trade
            performance -> 같은 publication candidate의 전체 Performance
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        observer = self._trade_update_observer
        if observer is None:
            return

        # Event 전송 실패가 이미 저장·게시된 Trade의 save retry로 오인되지 않게 격리한다.
        try:
            observer(trade, performance)
        except Exception:
            return  # observer는 exactly-once 시도하며 durable operation 결과를 되돌리지 않는다.

    @property
    def dirty_order_ids(self) -> frozenset[str]:
        """
        함수 이름: dirty_order_ids()
        기능: durable save 재시도를 기다려 publication이 보류된 order ID를 반환한다.
        인자: 없음
        반환값: 불변 dirty order ID 집합
        작성 날짜: 2026/08/22
        """
        with self._operation_lock:
            return frozenset(
                self._pending_publications
            )  # caller가 retry 대기 index를 변경하지 못하게 복사한다.

    def flush_durable_state(self) -> None:
        """
        함수 이름: flush_durable_state()
        기능: 보류 publication이 없을 때 repository의 명시적 종료 fsync 장벽을 실행한다.
        인자: 없음
        반환값: 모든 local 거래 상태가 durable하면 없음
        작성 날짜: 2026/08/24
        """
        # Pending publication이 있으면 disk와 공개 상태가 일치한다고 추측하지 않고 종료를 막는다.
        with self._operation_lock:
            if self._pending_publications:
                raise TradeHistoryPersistencePendingError(
                    "pending trade persistence blocks durable shutdown"
                )

            flush_operation = getattr(
                self._repository,
                "flush_durable_state",
                None,
            )
            if not callable(flush_operation):
                raise NotImplementedError(
                    "repository does not provide a durable shutdown barrier"
                )

            flush_operation()  # operation lock은 terminal publication과 fsync 장벽을 직렬화한다.

    @property
    def supports_pending_order_recovery(self) -> bool:
        """
        함수 이름: supports_pending_order_recovery()
        기능: 조립된 repository가 세 pending-order recovery operation을 모두 제공하는지 알린다.
        인자: 없음
        반환값: durable pending-order 복구 지원 여부
        작성 날짜: 2026/08/22
        """
        return self._supports_pending_order_recovery  # startup caller가 fake와 concrete를 명시적으로 구분한다.

    @property
    def supports_manual_kill_control_recovery(self) -> bool:
        """
        함수 이름: supports_manual_kill_control_recovery()
        기능: 조립된 repository가 manual kill load/save operation을 모두 제공하는지 알린다.
        인자: 없음
        반환값: durable manual kill 복구 지원 여부
        작성 날짜: 2026/08/29
        """
        return self._supports_manual_kill_control_recovery  # Controller가 volatile fake와 concrete를 구분한다.

    def get_manual_kill_control_state(self) -> ManualKillControlState:
        """
        함수 이름: get_manual_kill_control_state()
        기능: repository의 durable manual kill state를 operation lock 안에서 읽는다.
        인자: 없음
        반환값: 재시작 뒤 복원할 ManualKillControlState
        작성 날짜: 2026/08/29
        """
        with self._operation_lock:
            get_control_state = getattr(
                self._repository,
                "get_manual_kill_control_state",
                None,
            )
            if not callable(get_control_state):
                raise NotImplementedError(
                    "repository does not support manual-kill control recovery"
                )
            state = get_control_state()
            if not isinstance(state, ManualKillControlState):
                raise TypeError(
                    "repository must return a ManualKillControlState"
                )

            return state  # frozen state는 TradingController 초기화까지 같은 값을 보존한다.

    def get_manual_kill_control_replay(
        self,
    ) -> tuple[ManualKillControlState, ...]:
        """
        함수 이름: get_manual_kill_control_replay()
        기능: repository가 검증한 bounded manual kill command receipt를 restart cache에 제공한다.
        인자: 없음
        반환값: 시간 순서의 ManualKillControlState tuple
        작성 날짜: 2026/08/29
        """
        with self._operation_lock:
            get_control_replay = getattr(
                self._repository,
                "get_manual_kill_control_replay",
                None,
            )
            if not callable(get_control_replay):
                raise NotImplementedError(
                    "repository does not support manual-kill control recovery"
                )
            replay = get_control_replay()
            if not isinstance(replay, tuple) or any(
                not isinstance(receipt, ManualKillControlState)
                for receipt in replay
            ):
                raise TypeError(
                    "repository must return manual-kill control state tuple"
                )

            return replay  # 불변 tuple만 Controller command cache 복원에 전달한다.

    def save_manual_kill_control_state(
        self,
        state: ManualKillControlState,
    ) -> None:
        """
        함수 이름: save_manual_kill_control_state()
        기능: 다음 manual kill 성공 command receipt를 repository의 fsync 경계까지 내린다.
        인자: state -> 현재 state에서 실행된 exact command 결과
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        # Mapping이나 tuple을 domain state로 암묵 변환하지 않고 exact value만 위임한다.
        if not isinstance(state, ManualKillControlState):
            raise TypeError("state must be a ManualKillControlState")
        with self._operation_lock:
            save_control_state = getattr(
                self._repository,
                "save_manual_kill_control_state",
                None,
            )
            if not callable(save_control_state):
                raise NotImplementedError(
                    "repository does not support manual-kill control recovery"
                )
            save_control_state(state)  # 성공 반환이 in-memory kill state 변경의 선행 조건이다.

    def save_pending_order(self, order: Order) -> None:
        """
        함수 이름: save_pending_order()
        기능: Binance 제출 전에 Order intent metadata의 durable UPSERT를 repository에 위임한다.
        인자: order -> 제출 직전 canonical Order
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 동형 임의 객체가 persistence 경계에 credential 필드를 실어 보내지 못하게 한다.
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        with self._operation_lock:
            save_pending_order = getattr(
                self._repository,
                "save_pending_order",
                None,
            )
            if not callable(save_pending_order):
                raise NotImplementedError(
                    "repository does not support pending-order recovery"
                )
            save_pending_order(order)  # repository fsync 반환이 외부 제출 허용의 durable 경계다.

    def delete_pending_order(self, client_order_id: str) -> None:
        """
        함수 이름: delete_pending_order()
        기능: terminal 처리 뒤 client order ID의 durable REMOVE를 repository에 위임한다.
        인자: client_order_id -> 제거할 active pending order 식별자
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 잘못된 ID는 capability 조회보다 먼저 거부해 fake와 concrete가 같은 입력 계약을 갖게 한다.
        if not isinstance(client_order_id, str):
            raise TypeError("client_order_id must be a string")
        if not client_order_id or client_order_id.strip() != client_order_id:
            raise ValueError(
                "client_order_id must be non-empty without outer whitespace"
            )
        with self._operation_lock:
            delete_pending_order = getattr(
                self._repository,
                "delete_pending_order",
                None,
            )
            if not callable(delete_pending_order):
                raise NotImplementedError(
                    "repository does not support pending-order recovery"
                )
            delete_pending_order(client_order_id)  # durable tombstone 뒤에만 local 복구 근거가 사라진다.

    def mark_pending_order_submission_rejected(
        self,
        client_order_id: str,
    ) -> None:
        """
        함수 이름: mark_pending_order_submission_rejected()
        기능: typed pre-matching 제출 거부 lifecycle의 durable transition을 repository에 위임한다.
        인자: client_order_id -> 제출 거부가 확인된 application order ID
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        # 외부 order ID는 공백 없는 canonical 문자열이어야 repository port에 도달할 수 있다.
        if not isinstance(client_order_id, str):
            raise TypeError("client_order_id must be a string")
        if not client_order_id or client_order_id.strip() != client_order_id:
            raise ValueError(
                "client_order_id must be non-empty without outer whitespace"
            )

        with self._operation_lock:
            transition_operation = getattr(
                self._repository,
                "mark_pending_order_submission_rejected",
                None,
            )
            if not callable(transition_operation):
                raise NotImplementedError(
                    "repository does not support pending-order lifecycle recovery"
                )
            transition_operation(client_order_id)  # fsync 반환 뒤에만 Controller가 거부 사실을 사용한다.

    def transition_pending_order_lifecycle(
        self,
        client_order_id: str,
        lifecycle: PendingOrderRecoveryLifecycle,
    ) -> None:
        """
        함수 이름: transition_pending_order_lifecycle()
        기능: 제출·UNKNOWN·partial·terminal·history lifecycle의 durable 전진을 repository에 위임한다.
        인자: client_order_id -> 전이할 application order ID
            lifecycle -> fsync할 canonical lifecycle
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # Order ID와 lifecycle을 repository 호출 전에 canonical application 값으로 검증한다.
        if not isinstance(client_order_id, str):
            raise TypeError("client_order_id must be a string")
        if not client_order_id or client_order_id.strip() != client_order_id:
            raise ValueError(
                "client_order_id must be non-empty without outer whitespace"
            )
        if not isinstance(lifecycle, PendingOrderRecoveryLifecycle):
            raise TypeError("lifecycle must be a PendingOrderRecoveryLifecycle")

        with self._operation_lock:
            # Generic lifecycle port가 없는 history-only fake는 durable 주문 mode로 오인하지 않는다.
            transition_operation = getattr(
                self._repository,
                "transition_pending_order_lifecycle",
                None,
            )
            if not callable(transition_operation):
                raise NotImplementedError(
                    "repository does not support pending-order lifecycle recovery"
                )
            transition_operation(
                client_order_id,
                lifecycle,
            )  # repository의 file·directory fsync 반환이 application 사실의 commit 경계다.

    def get_pending_orders(self) -> tuple[Order, ...]:
        """
        함수 이름: get_pending_orders()
        기능: startup reconciliation에 사용할 active Order tuple을 repository에서 복원한다.
        인자: 없음
        반환값: 검증된 canonical Order tuple
        작성 날짜: 2026/08/22
        """
        records = self.get_pending_order_recovery_records()

        return tuple(record.order for record in records)

    def get_pending_order_recovery_records(
        self,
    ) -> tuple[PendingOrderRecoveryRecord, ...]:
        """
        함수 이름: get_pending_order_recovery_records()
        기능: startup reconciliation에 사용할 Order와 durable lifecycle snapshot을 복원한다.
        인자: 없음
        반환값: 검증된 immutable PendingOrderRecoveryRecord tuple
        작성 날짜: 2026/08/23
        """
        with self._operation_lock:
            # history-only fake는 PREPARED로 추측하지 않고 명시적인 capability 오류를 낸다.
            get_recovery_records = getattr(
                self._repository,
                "get_pending_order_recovery_records",
                None,
            )
            if not callable(get_recovery_records):
                raise NotImplementedError(
                    "repository does not support pending-order lifecycle recovery"
                )
            recovery_records = get_recovery_records()
            if not isinstance(recovery_records, tuple):
                raise TypeError(
                    "repository pending-order records must be a tuple"
                )
            if any(
                not isinstance(record, PendingOrderRecoveryRecord)
                for record in recovery_records
            ):
                raise TypeError(
                    "repository pending-order records must use canonical values"
                )

            return recovery_records  # Order와 lifecycle이 같은 replay에서 나온 snapshot을 보존한다.

    def get_pending_order_submission_counts(
        self,
    ) -> tuple[tuple[str, int], ...]:
        """
        함수 이름: get_pending_order_submission_counts()
        기능: REMOVE 후에도 보존된 intent별 제출 예산 소비 snapshot을 복원한다.
        인자: 없음
        반환값: intent와 1 이상 submission count의 immutable tuple
        작성 날짜: 2026/08/25
        """
        with self._operation_lock:
            # Repository capability와 반환 shape를 검증해 Controller가 메모리 기본값으로 추측하지 않게 한다.
            get_submission_counts = getattr(
                self._repository,
                "get_pending_order_submission_counts",
                None,
            )
            if not callable(get_submission_counts):
                raise NotImplementedError(
                    "repository does not support pending-order submission budgets"
                )
            submission_counts = get_submission_counts()
            if not isinstance(submission_counts, tuple):
                raise TypeError(
                    "repository submission counts must be a tuple"
                )
            for record in submission_counts:
                if (
                    not isinstance(record, tuple)
                    or len(record) != 2
                    or not isinstance(record[0], str)
                    or not record[0]
                    or record[0].strip() != record[0]
                    or type(record[1]) is not int
                    or record[1] < 1
                ):
                    raise TypeError(
                        "repository submission counts must use canonical values"
                    )

            return submission_counts  # immutable replay snapshot을 변형 없이 Controller에 전달한다.

    def load_trade_history(self) -> TradeHistory:
        """
        함수 이름: load_trade_history()
        기능: Repository, TradeHistory, Performance 순서로 local 복원 후 결과를 원자 교체한다.
        인자: 없음
        반환값: 새로 publish한 TradeHistory
        작성 날짜: 2026/08/21
        """
        with self._operation_lock:
            # 보류 candidate가 있으면 disk와 published state 중 어느 쪽도 새 load로 덮지 않는다.
            if self._pending_publications:
                raise TradeHistoryPersistencePendingError(
                    "pending trade persistence must be reconciled before load"
                )

            # Repository 결과로 두 domain candidate를 완성한 뒤 한 번에 게시한다.
            restored_trades = self._repository.get_trade_history()
            next_trade_history = TradeHistory(restored_trades)
            next_performance = Performance(
                next_trade_history.trades,
                clock=self._clock,
            )
            next_state = _TradeHistoryLoadState(
                trade_history=next_trade_history,
                performance=next_performance,
            )

            # history와 performance candidate가 모두 완성된 뒤 공유 state 하나만 교체한다.
            with self._state_lock:
                self._state = next_state
            self._details_summary_state = None  # 다음 상세 진입은 복원된 전체 state를 다시 결합한다.

            return next_trade_history  # 반환값도 방금 게시한 state의 동일 객체다.

    def record_order_execution(
        self,
        order: Order,
        summary: ExecutionSummary,
        allocated_cost_basis: Decimal | None = None,
    ) -> Trade:
        """
        함수 이름: record_order_execution()
        기능: realized 계산부터 Trade 생성·후보 반영·durable 저장 후 원자 게시까지 조정한다.
        인자: order -> terminal 주문 의도와 식별자를 보존한 Order
            summary -> 같은 주문의 누적 fill ExecutionSummary
            allocated_cost_basis -> SELL Position 변경 전에 고정한 취득원가
        반환값: durable 저장과 publication을 마친 Trade
        작성 날짜: 2026/08/22
        """
        # aggregate와 summary는 구조가 비슷한 임의 객체가 아닌 canonical domain 타입만 받는다.
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        if not isinstance(summary, ExecutionSummary):
            raise TypeError("summary must be an ExecutionSummary")

        with self._operation_lock:
            # 이전 save의 내구성이 불명인 동안 다음 execution candidate 생성을 차단한다.
            if self._pending_publications:
                raise TradeHistoryPersistencePendingError(
                    "pending trade persistence blocks new execution records"
                )

            # 메시지 13.1은 SELL에만 적용하고 BUY의 원가는 반드시 null로 유지한다.
            with self._state_lock:
                current_state = self._state
            realized_result = None
            if order.side is OrderSide.SELL:
                if allocated_cost_basis is None:
                    raise ValueError(
                        "allocated_cost_basis is required for SELL"
                    )
                realized_result = current_state.performance.calculate_realized_result(
                    summary,
                    allocated_cost_basis,
                )
            elif allocated_cost_basis is not None:
                raise ValueError("allocated_cost_basis must be None for BUY")

            # 메시지 13.2~13.4를 공개되지 않은 local candidate에서 순서대로 실행한다.
            trade = Trade.from_order_execution(
                order,
                summary,
                realized_result,
            )
            published_trades = current_state.trade_history.trades
            next_trade_history = TradeHistory(published_trades)
            next_trade_history.add_trade(trade)

            # 같은 order·같은 Trade의 멱등 재처리는 durable state와 live event를 다시 만들지 않는다.
            if len(next_trade_history.trades) == len(published_trades):
                return next(
                    published_trade
                    for published_trade in published_trades
                    if published_trade.order_id == trade.order_id
                )  # caller에도 이미 durable publication된 canonical Trade를 반환한다.

            next_performance = Performance(
                published_trades,
                clock=self._clock,
            )
            next_performance.apply_new_trade(trade)
            next_state = _TradeHistoryLoadState(
                trade_history=next_trade_history,
                performance=next_performance,
            )
            pending_publication = _PendingPublication(
                trade=trade,
                state=next_state,
            )

            # 메시지 13.5와 account-day 안정화가 성공하기 전에는 두 candidate를 publish하지 않는다.
            save_trade = getattr(
                self._repository,
                "save_this_trade_by_order_id",
                None,
            )
            if not callable(save_trade):
                raise TypeError(
                    "repository must provide save_this_trade_by_order_id"
                )
            try:
                save_trade(trade.order_id, trade)
                published_performance = self._stabilize_performance_account_day(
                    next_performance
                )
            except Exception:
                self._pending_publications[trade.order_id] = pending_publication
                raise  # 공개 state는 유지하고 동일 Trade의 save-only retry 근거만 보존한다.

            published_state = _TradeHistoryLoadState(
                trade_history=next_trade_history,
                performance=published_performance,
            )  # Durable write 중 자정이 지나도 새 account day candidate만 공개한다.

            with self._state_lock:
                self._state = published_state  # 두 공개 snapshot을 같은 state 교체로 게시한다.
            self._details_summary_state = None  # 새 Trade 또는 KST rollover 성과는 다음 조회에서 원자 재결합한다.

            # durable state publication 경계가 끝난 뒤 같은 candidate를 event observer에 전달한다.
            self._notify_trade_update(trade, published_performance)

            return trade  # durable save와 두 domain publication이 모두 완료된 Trade다.

    def retry_pending_persistence(self, order_id: str) -> Trade:
        """
        함수 이름: retry_pending_persistence()
        기능: 실패한 동일 order의 저장만 재시도하고 성공 시 보류 state를 원자 게시한다.
        인자: order_id -> dirty_order_ids에 포함된 canonical exchange order ID
        반환값: durable 저장과 publication을 마친 기존 Trade
        작성 날짜: 2026/08/22
        """
        # retry key는 repository와 같은 canonical positive-integer 문자열만 허용한다.
        if not isinstance(order_id, str):
            raise TypeError("order_id must be a string")
        if not order_id or not order_id.isascii() or not order_id.isdigit():
            raise ValueError("order_id must be a positive integer string")

        with self._operation_lock:
            # 보류 index에서 정확히 같은 order candidate만 읽어 새 Trade 생성을 피한다.
            pending_publication = self._pending_publications.get(order_id)
            if pending_publication is None:
                raise KeyError(f"order_id {order_id} has no pending persistence")

            # 원 주문을 재제출하지 않고 같은 Trade의 repository save만 재시도한다.
            save_trade = getattr(
                self._repository,
                "save_this_trade_by_order_id",
                None,
            )
            if not callable(save_trade):
                raise TypeError(
                    "repository must provide save_this_trade_by_order_id"
                )
            save_trade(order_id, pending_publication.trade)
            published_performance = self._stabilize_performance_account_day(
                pending_publication.state.performance
            )
            published_state = _TradeHistoryLoadState(
                trade_history=pending_publication.state.trade_history,
                performance=published_performance,
            )  # Retry 지연 중 바뀐 KST account day도 publication 전에 다시 계산한다.

            # 재저장 성공 뒤에만 원래 candidate를 게시하고 마지막에 dirty key를 제거한다.
            with self._state_lock:
                self._state = published_state
            del self._pending_publications[order_id]  # 게시 완료 뒤 dirty lock을 해제한다.
            self._details_summary_state = None  # save-only retry의 새 성과도 다음 조회에서 원자 재결합한다.

            # dirty 해제까지 성공한 기존 candidate를 최초 성공 publication으로 한 번만 알린다.
            self._notify_trade_update(
                pending_publication.trade,
                published_performance,
            )

            return pending_publication.trade  # 최초 실패 때 만든 동일 immutable Trade다.
