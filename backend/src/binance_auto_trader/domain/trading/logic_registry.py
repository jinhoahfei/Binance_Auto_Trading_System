"""REGIME별 TradingSTM 구현 범위를 표현하는 불변 레지스트리를 정의한다."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final, Mapping

from ..common import RegimeType
from .transitions.catalog import TRANSITION_IDS


class TradingLogicSupportStatus(str, Enum):
    """
    클래스 이름: TradingLogicSupportStatus
    기능: 각 REGIME에 대응하는 거래 로직의 구현 완료 여부를 정의한다.
    작성 날짜: 2026/08/21
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class TradingLogicStartGuard(str, Enum):
    """
    클래스 이름: TradingLogicStartGuard
    기능: 선택한 REGIME 거래 로직의 시작 허용 여부와 차단 코드를 정의한다.
    작성 날짜: 2026/08/21
    """

    READY = "READY"
    UNSUPPORTED_TRADING_LOGIC = "UNSUPPORTED_TRADING_LOGIC"


class TradingRegistrySource(str, Enum):
    """
    클래스 이름: TradingRegistrySource
    기능: 구현된 거래 로직이 사용하는 transition registry 출처를 정의한다.
    작성 날짜: 2026/08/21
    """

    LOWER_BB = "LOWER_BB"


class UpperBandPolicy(str, Enum):
    """
    클래스 이름: UpperBandPolicy
    기능: 상단 BB 접촉 시 구현된 포지션 처리 정책을 정의한다.
    작성 날짜: 2026/08/21
    """

    RESUME_LOWER_WATCH = "RESUME_LOWER_WATCH"


@dataclass(frozen=True, slots=True)
class TradingLogicConfiguration:
    """
    클래스 이름: TradingLogicConfiguration
    기능: 하나의 REGIME에 대한 지원 상태와 transition 출처를 불변으로 보존한다.
    작성 날짜: 2026/08/21
    """

    regime_type: RegimeType
    support_status: TradingLogicSupportStatus
    transition_source: TradingRegistrySource | None
    transition_ids: tuple[str, ...]
    start_guard: TradingLogicStartGuard
    upper_band_policy: UpperBandPolicy | None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: 지원 상태와 registry 및 시작 Guard의 조합이 모순되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # 문자열 우회 입력이 enum identity 기반 선택을 통과하지 못하도록 차단한다.
        if not isinstance(self.regime_type, RegimeType):
            raise TypeError("regime_type must be a RegimeType")
        if not isinstance(self.support_status, TradingLogicSupportStatus):
            raise TypeError("support_status must be a TradingLogicSupportStatus")
        if not isinstance(self.start_guard, TradingLogicStartGuard):
            raise TypeError("start_guard must be a TradingLogicStartGuard")
        if self.transition_source is not None and not isinstance(
            self.transition_source,
            TradingRegistrySource,
        ):
            raise TypeError("transition_source must be a TradingRegistrySource or None")
        if self.upper_band_policy is not None and not isinstance(
            self.upper_band_policy,
            UpperBandPolicy,
        ):
            raise TypeError("upper_band_policy must be an UpperBandPolicy or None")

        # frozen configuration 내부에도 mutable list가 들어가지 않도록 tuple만 허용한다.
        if not isinstance(self.transition_ids, tuple) or any(
            not isinstance(transition_id, str)
            for transition_id in self.transition_ids
        ):
            raise TypeError("transition_ids must be a tuple of strings")
        if len(self.transition_ids) != len(set(self.transition_ids)):
            raise ValueError("transition_ids cannot contain duplicates")

        # 지원 로직은 실행 가능한 registry와 READY Guard를 함께 가져야 한다.
        if self.support_status is TradingLogicSupportStatus.SUPPORTED:
            if self.transition_source is None or not self.transition_ids:
                raise ValueError(
                    "Supported trading logic requires a transition registry"
                )
            if self.start_guard is not TradingLogicStartGuard.READY:
                raise ValueError(
                    "Supported trading logic requires the READY start guard"
                )
            return

        # 미지원 로직에 임의의 transition 또는 성공 Guard가 연결되는 fallback을 막는다.
        if self.transition_source is not None or self.transition_ids:
            raise ValueError("Unsupported trading logic cannot expose transitions")
        if self.start_guard is not TradingLogicStartGuard.UNSUPPORTED_TRADING_LOGIC:
            raise ValueError(
                "Unsupported trading logic requires the UNSUPPORTED_TRADING_LOGIC guard"
            )
        if self.upper_band_policy is not None:
            raise ValueError(
                "Unsupported trading logic cannot expose an upper-band policy"
            )


class UnsupportedTradingLogicError(RuntimeError):
    """
    클래스 이름: UnsupportedTradingLogicError
    기능: 미지원 REGIME의 TradingSTM 생성을 typed 오류 코드와 함께 거부한다.
    작성 날짜: 2026/08/21
    """

    code: Final[str] = TradingLogicStartGuard.UNSUPPORTED_TRADING_LOGIC.value

    def __init__(self, regime_type: RegimeType) -> None:
        """
        함수 이름: __init__()
        기능: 거부된 canonical REGIME과 고정 오류 코드를 오류 객체에 보존한다.
        인자: regime_type -> 구현되지 않은 거래 로직의 REGIME
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        # application과 UI가 같은 REGIME 실패 원인을 추적할 수 있도록 값을 보존한다.
        self.regime_type = regime_type
        super().__init__(f"{self.code}: {regime_type.value}")


# Phase 6 기준 구현 범위를 canonical REGIME 순서로 고정해 누락과 암묵 fallback을 막는다.
TRADING_LOGIC_CONFIGURATIONS: Final[tuple[TradingLogicConfiguration, ...]] = (
    TradingLogicConfiguration(
        regime_type=RegimeType.TYPE_0,
        support_status=TradingLogicSupportStatus.SUPPORTED,
        transition_source=TradingRegistrySource.LOWER_BB,
        transition_ids=TRANSITION_IDS,
        start_guard=TradingLogicStartGuard.READY,
        upper_band_policy=UpperBandPolicy.RESUME_LOWER_WATCH,
    ),
    *(
        TradingLogicConfiguration(
            regime_type=regime_type,
            support_status=TradingLogicSupportStatus.UNSUPPORTED,
            transition_source=None,
            transition_ids=(),
            start_guard=TradingLogicStartGuard.UNSUPPORTED_TRADING_LOGIC,
            upper_band_policy=None,
        )
        for regime_type in (
            RegimeType.TYPE_1,
            RegimeType.TYPE_2,
            RegimeType.TYPE_3,
            RegimeType.TYPE_4,
        )
    ),
)

# 조회 경로도 읽기 전용 mapping으로 고정해 session 사이에 registry가 변하지 않게 한다.
_CONFIGURATION_BY_REGIME: Final[Mapping[RegimeType, TradingLogicConfiguration]] = (
    MappingProxyType(
        {
            configuration.regime_type: configuration
            for configuration in TRADING_LOGIC_CONFIGURATIONS
        }
    )
)

# 다섯 canonical REGIME의 중복 또는 누락은 모듈 import 시점에 즉시 실패시킨다.
_REGISTERED_REGIME_ORDER = tuple(
    configuration.regime_type
    for configuration in TRADING_LOGIC_CONFIGURATIONS
)
if _REGISTERED_REGIME_ORDER != tuple(RegimeType):
    raise RuntimeError(
        "Trading logic registry must cover all REGIME types in canonical order"
    )


def list_trading_logic_configurations() -> tuple[TradingLogicConfiguration, ...]:
    """
    함수 이름: list_trading_logic_configurations()
    기능: canonical 순서로 고정된 전체 REGIME 거래 로직 구성을 반환한다.
    인자: 없음
    반환값: 다섯 TradingLogicConfiguration의 불변 tuple
    작성 날짜: 2026/08/21
    """
    return TRADING_LOGIC_CONFIGURATIONS  # 매 호출에서 같은 불변 tuple을 공유한다.


def get_trading_logic_configuration(
    regime_type: RegimeType,
) -> TradingLogicConfiguration:
    """
    함수 이름: get_trading_logic_configuration()
    기능: fallback 없이 지정한 canonical REGIME의 거래 로직 구성을 조회한다.
    인자: regime_type -> 조회할 canonical REGIME
    반환값: REGIME에 정확히 대응하는 TradingLogicConfiguration
    작성 날짜: 2026/08/21
    """
    if not isinstance(regime_type, RegimeType):
        raise TypeError("regime_type must be a RegimeType")

    return _CONFIGURATION_BY_REGIME[regime_type]  # 다른 REGIME key로 대체하지 않는다.


def require_supported_trading_logic_configuration(
    regime_type: RegimeType,
) -> TradingLogicConfiguration:
    """
    함수 이름: require_supported_trading_logic_configuration()
    기능: 지원된 REGIME 구성을 반환하고 미지원 로직은 typed 오류로 거부한다.
    인자: regime_type -> 생성하려는 TradingSTM의 canonical REGIME
    반환값: 실행 가능한 TradingLogicConfiguration
    작성 날짜: 2026/08/21
    """
    configuration = get_trading_logic_configuration(regime_type)

    # 미지원 행을 TYPE_0 구현으로 대체하지 않고 고정 오류 코드로 즉시 차단한다.
    if configuration.support_status is TradingLogicSupportStatus.UNSUPPORTED:
        raise UnsupportedTradingLogicError(regime_type)

    return configuration  # 검증을 통과한 동일 frozen configuration을 반환한다.
