"""Binance Spot 자산 잔액과 ETH 평가금액을 보존하는 Account를 정의한다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock
from types import MappingProxyType
from typing import Mapping


ZERO_DECIMAL = Decimal("0")
SUPPORTED_VALUATION_ASSET = "ETH"


def _validate_asset_name(asset: object, field_name: str) -> None:
    """
    함수 이름: _validate_asset_name()
    기능: Binance가 전달한 UTF-8 asset 이름이 공백 없는 문자열인지 검증한다.
    인자: asset -> 검증할 asset 이름
        field_name -> 오류 메시지에 사용할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    if not isinstance(asset, str):
        raise TypeError(f"{field_name} must be a string")
    if not asset or asset != asset.strip():
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    if any(character.isspace() or ord(character) < 32 for character in asset):
        raise ValueError(f"{field_name} must not contain whitespace or controls")


def _validate_non_negative_decimal(value: object, field_name: str) -> None:
    """
    함수 이름: _validate_non_negative_decimal()
    기능: 잔액과 평가 수치가 유한한 0 이상 Decimal인지 검증한다.
    인자: value -> 검증할 금융 수치
        field_name -> 오류 메시지에 사용할 필드 이름
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    if value < ZERO_DECIMAL:
        raise ValueError(f"{field_name} must not be negative")


def _normalize_utc_datetime(value: object, field_name: str) -> datetime:
    """
    함수 이름: _normalize_utc_datetime()
    기능: 계좌 갱신 시각이 timezone-aware UTC인지 검증하고 정규화한다.
    인자: value -> 검증할 datetime 값
        field_name -> 오류 메시지에 사용할 필드 이름
    반환값: timezone.utc로 정규화한 datetime
    작성 날짜: 2026/08/21
    """
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")

    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class AssetBalance:
    """
    클래스 이름: AssetBalance
    기능: 한 Binance Spot asset의 free와 locked 잔액을 불변으로 보존한다.
    작성 날짜: 2026/08/21
    """

    asset: str
    free: Decimal
    locked: Decimal

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: asset 이름과 두 잔액의 타입·범위를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        _validate_asset_name(self.asset, "asset")
        _validate_non_negative_decimal(self.free, "free")
        _validate_non_negative_decimal(self.locked, "locked")

    @property
    def total(self) -> Decimal:
        """
        함수 이름: total()
        기능: 매도 가능 free와 주문 잠금 locked를 합친 총 보유량을 반환한다.
        인자: 없음
        반환값: asset의 총 보유량
        작성 날짜: 2026/08/21
        """
        return self.free + self.locked


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """
    클래스 이름: AccountSnapshot
    기능: REST 전체 잔액 또는 account stream 부분 잔액 patch를 불변으로 보존한다.
    작성 날짜: 2026/08/21
    """

    balances: tuple[AssetBalance, ...]
    updated_at: datetime
    is_full_snapshot: bool

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 잔액 tuple의 타입·중복과 UTC 갱신 시각을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not isinstance(self.balances, tuple):
            raise TypeError("balances must be a tuple")
        if any(
            not isinstance(balance, AssetBalance)
            for balance in self.balances
        ):
            raise TypeError("balances must contain AssetBalance values")

        asset_names = tuple(balance.asset for balance in self.balances)
        if len(set(asset_names)) != len(asset_names):
            raise ValueError("balances must not contain duplicate assets")

        normalized_updated_at = _normalize_utc_datetime(
            self.updated_at,
            "updated_at",
        )
        object.__setattr__(self, "updated_at", normalized_updated_at)

        if not isinstance(self.is_full_snapshot, bool):
            raise TypeError("is_full_snapshot must be a bool")

    @property
    def balances_by_asset(self) -> Mapping[str, AssetBalance]:
        """
        함수 이름: balances_by_asset()
        기능: snapshot 잔액을 asset 이름으로 조회할 수 있는 읽기 전용 mapping으로 반환한다.
        인자: 없음
        반환값: asset별 AssetBalance 읽기 전용 mapping
        작성 날짜: 2026/08/21
        """
        return MappingProxyType(
            {
                balance.asset: balance
                for balance in self.balances
            }
        )


@dataclass(frozen=True, slots=True)
class _AccountState:
    """
    클래스 이름: _AccountState
    기능: Account의 한 version에 속한 잔액·가격·평가 상태를 원자적으로 묶는다.
    작성 날짜: 2026/08/21
    """

    balances: Mapping[str, AssetBalance]
    current_price: Decimal | None
    valuation: Decimal | None
    updated_at: datetime | None
    version: int
    ready: bool


class Account:
    """
    클래스 이름: Account
    기능: 자산별 Spot 잔액과 MarketSnapshot 가격을 결합한 ETH 평가 상태를 보존한다.
    작성 날짜: 2026/08/21
    """

    __slots__ = (
        "_lock",
        "_state",
        "_valuation_asset",
    )

    def __init__(self, valuation_asset: str = SUPPORTED_VALUATION_ASSET) -> None:
        """
        함수 이름: __init__()
        기능: 비어 있고 준비되지 않은 ETH Spot 계좌 state를 생성한다.
        인자: valuation_asset -> MarketSnapshot 가격으로 평가할 기준 asset
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        _validate_asset_name(valuation_asset, "valuation_asset")
        if valuation_asset != SUPPORTED_VALUATION_ASSET:
            raise ValueError("Account valuation supports only ETH")

        self._valuation_asset = valuation_asset
        self._lock = RLock()
        self._state = _AccountState(
            balances=MappingProxyType({}),
            current_price=None,
            valuation=None,
            updated_at=None,
            version=0,
            ready=False,
        )

    @property
    def valuation_asset(self) -> str:
        """
        함수 이름: valuation_asset()
        기능: current_price로 평가하는 기준 asset 이름을 반환한다.
        인자: 없음
        반환값: 기준 asset 이름
        작성 날짜: 2026/08/21
        """
        return self._valuation_asset

    @property
    def balances(self) -> Mapping[str, AssetBalance]:
        """
        함수 이름: balances()
        기능: 외부에서 변경할 수 없는 최신 asset별 잔액 mapping을 반환한다.
        인자: 없음
        반환값: 최신 읽기 전용 잔액 mapping
        작성 날짜: 2026/08/21
        """
        return self._state.balances

    @property
    def current_price(self) -> Decimal | None:
        """
        함수 이름: current_price()
        기능: Account valuation에 사용한 최신 ETHUSDT 가격을 반환한다.
        인자: 없음
        반환값: 최신 ETH 가격
        작성 날짜: 2026/08/21
        """
        return self._state.current_price

    @property
    def valuation(self) -> Decimal | None:
        """
        함수 이름: valuation()
        기능: free와 locked ETH 총량에 current_price를 곱한 평가금액을 반환한다.
        인자: 없음
        반환값: ETH 보유량의 USDT 평가금액
        작성 날짜: 2026/08/21
        """
        return self._state.valuation

    @property
    def updated_at(self) -> datetime | None:
        """
        함수 이름: updated_at()
        기능: 마지막으로 적용한 Binance account 갱신 UTC 시각을 반환한다.
        인자: 없음
        반환값: 마지막 계좌 갱신 시각 또는 초기 None
        작성 날짜: 2026/08/21
        """
        return self._state.updated_at

    @property
    def version(self) -> int:
        """
        함수 이름: version()
        기능: 성공한 전체 snapshot 또는 stream patch 적용 횟수를 반환한다.
        인자: 없음
        반환값: 0부터 단조 증가하는 Account version
        작성 날짜: 2026/08/21
        """
        return self._state.version

    @property
    def ready(self) -> bool:
        """
        함수 이름: ready()
        기능: REST 전체 snapshot과 양수 ETH 가격이 적용됐는지 반환한다.
        인자: 없음
        반환값: Account 준비 여부
        작성 날짜: 2026/08/21
        """
        return self._state.ready

    def get_holdings(self, asset: str = SUPPORTED_VALUATION_ASSET) -> Decimal:
        """
        함수 이름: get_holdings()
        기능: 지정 asset의 free와 locked를 합친 authoritative 총 보유량을 반환한다.
        인자: asset -> 조회할 Binance asset 이름
        반환값: asset의 총 보유 수량 또는 없는 asset이면 Decimal 0
        작성 날짜: 2026/08/21
        """
        _validate_asset_name(asset, "asset")
        balance = self._state.balances.get(asset)
        if balance is None:
            return ZERO_DECIMAL

        return balance.total

    def apply_initial_snapshot(
        self,
        snapshot: AccountSnapshot,
        current_price: Decimal,
    ) -> None:
        """
        함수 이름: apply_initial_snapshot()
        기능: REST 전체 잔액과 같은 startup cycle의 ETH 가격을 원자적으로 적용한다.
        인자: snapshot -> APIGateway가 정규화한 REST 전체 계좌 snapshot
            current_price -> 먼저 준비된 MarketSnapshot의 ETHUSDT 가격
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self._apply_full_snapshot(
            snapshot,
            current_price,
            replace_stream_state=False,
        )  # 일반 startup 재호출은 이미 관찰한 stream source time을 되돌리지 않는다.

    def apply_reconciliation_snapshot(
        self,
        snapshot: AccountSnapshot,
        current_price: Decimal,
    ) -> None:
        """
        함수 이름: apply_reconciliation_snapshot()
        기능: stream 단절 뒤 REST 전체 잔액을 source watermark와 함께 authoritative하게 교체한다.
        인자: snapshot -> 재연결 전에 조회한 REST 전체 계좌 snapshot
            current_price -> 먼저 준비된 MarketSnapshot의 ETHUSDT 가격
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self._apply_full_snapshot(
            snapshot,
            current_price,
            replace_stream_state=True,
        )  # 끊긴 stream의 더 큰 event time보다 현재 REST 전체 사실을 우선한다.

    def apply_startup_reconciliation_snapshot(
        self,
        snapshot: AccountSnapshot,
        current_price: Decimal,
    ) -> bool:
        """
        함수 이름: apply_startup_reconciliation_snapshot()
        기능: startup stream ACK 뒤의 두 번째 REST 전체 계좌를 최신 stream patch와 안전하게 병합한다.
        인자: snapshot -> stream 시작 뒤 다시 조회한 REST 전체 계좌 snapshot
            current_price -> 먼저 준비된 MarketSnapshot의 ETHUSDT 가격
        반환값: 두 번째 전체 snapshot을 적용했으면 True, 더 최신 stream 상태면 False
        작성 날짜: 2026/08/22
        """
        return self._apply_full_snapshot(
            snapshot,
            current_price,
            replace_stream_state=True,
            ignore_stale=True,
        )  # 같거나 더 최신인 두 번째 REST 전체 사실만 startup 기준으로 교체한다.

    def _apply_full_snapshot(
        self,
        snapshot: AccountSnapshot,
        current_price: Decimal,
        *,
        replace_stream_state: bool,
        ignore_stale: bool = False,
    ) -> bool:
        """
        함수 이름: _apply_full_snapshot()
        기능: 전체 account snapshot을 검증해 startup 또는 full reconciliation 정책으로 원자 적용한다.
        인자: snapshot -> 적용할 REST 전체 계좌 snapshot
            current_price -> 계좌 평가에 사용할 양수 ETHUSDT 가격
            replace_stream_state -> 끊긴 stream source watermark를 교체할지 여부
            ignore_stale -> 더 최신 stream 상태가 있으면 예외 대신 무시할지 여부
        반환값: 전체 snapshot을 적용했으면 True, 동일·무시한 snapshot이면 False
        작성 날짜: 2026/08/22
        """
        if not isinstance(snapshot, AccountSnapshot):
            raise TypeError("snapshot must be an AccountSnapshot")
        if not snapshot.is_full_snapshot:
            raise ValueError("initial snapshot must be a full snapshot")
        if type(replace_stream_state) is not bool:
            raise TypeError("replace_stream_state must be a bool")
        if type(ignore_stale) is not bool:
            raise TypeError("ignore_stale must be a bool")
        _validate_non_negative_decimal(current_price, "current_price")
        if current_price == ZERO_DECIMAL:
            raise ValueError("current_price must be greater than zero")

        next_balances = dict(snapshot.balances_by_asset)
        next_valuation = self._calculate_valuation(
            next_balances,
            current_price,
        )

        with self._lock:
            current_state = self._state
            current_updated_at = current_state.updated_at
            if (
                current_updated_at is not None
                and snapshot.updated_at < current_updated_at
            ):
                if ignore_stale:
                    return False  # 첫 REST와 최신 stream patch의 합성 상태를 그대로 보존한다.
                if not replace_stream_state:
                    raise ValueError("initial account snapshot is stale")
            if (
                current_state.ready
                and current_state.updated_at == snapshot.updated_at
                and current_state.balances == snapshot.balances_by_asset
                and current_state.current_price == current_price
            ):
                return False  # 동일 full 사실은 observer가 읽는 version을 불필요하게 올리지 않는다.

            self._state = _AccountState(
                balances=MappingProxyType(next_balances),
                current_price=current_price,
                valuation=next_valuation,
                updated_at=snapshot.updated_at,
                version=self._state.version + 1,
                ready=True,
            )

        return True

    def apply_stream_snapshot(self, snapshot: AccountSnapshot) -> bool:
        """
        함수 이름: apply_stream_snapshot()
        기능: account stream의 변경 asset만 patch하고 stale 또는 상태 중복은 적용하지 않는다.
        인자: snapshot -> WebSocketGateway가 정규화한 부분 계좌 snapshot
        반환값: 계좌 state가 실제 변경됐으면 True, stale·중복이면 False
        작성 날짜: 2026/08/21
        """
        if not isinstance(snapshot, AccountSnapshot):
            raise TypeError("snapshot must be an AccountSnapshot")
        if snapshot.is_full_snapshot:
            raise ValueError("stream snapshot must be a partial snapshot")

        with self._lock:
            current_state = self._state
            if not current_state.ready or current_state.updated_at is None:
                raise RuntimeError("initial account snapshot is not ready")
            if current_state.current_price is None:
                raise RuntimeError("initial account price is not ready")
            if snapshot.updated_at < current_state.updated_at:
                return False

            next_balances = dict(current_state.balances)
            has_balance_change = False
            for asset, balance in snapshot.balances_by_asset.items():
                if next_balances.get(asset) != balance:
                    has_balance_change = True
                next_balances[asset] = balance

            if (
                not has_balance_change
                and snapshot.updated_at == current_state.updated_at
            ):
                return False

            self._state = _AccountState(
                balances=MappingProxyType(next_balances),
                current_price=current_state.current_price,
                valuation=self._calculate_valuation(
                    next_balances,
                    current_state.current_price,
                ),
                updated_at=snapshot.updated_at,
                version=current_state.version + 1,
                ready=True,
            )

        return True

    def _calculate_valuation(
        self,
        balances: Mapping[str, AssetBalance],
        current_price: Decimal,
    ) -> Decimal:
        """
        함수 이름: _calculate_valuation()
        기능: 기준 asset 총 보유량과 현재가로 단일 asset 평가금액을 계산한다.
        인자: balances -> 계산에 사용할 asset별 잔액 mapping
            current_price -> ETHUSDT 현재가
        반환값: ETH 보유량의 USDT 평가금액
        작성 날짜: 2026/08/21
        """
        asset_balance = balances.get(self._valuation_asset)
        if asset_balance is None:
            return ZERO_DECIMAL

        return asset_balance.total * current_price
