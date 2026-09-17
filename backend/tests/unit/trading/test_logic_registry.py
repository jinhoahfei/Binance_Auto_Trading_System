"""REGIME별 거래 로직 registry와 fallback 차단 계약을 검증한다."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.trading.context import (
    MarketEvaluationSnapshot,
    TradingContextView,
    TradingRuntimeSnapshot,
)
from binance_auto_trader.domain.trading.events import (
    EventPriority,
    TradingEvent,
    TradingEventType,
)
from binance_auto_trader.domain.trading.logic_registry import (
    TradingLogicStartGuard,
    TradingLogicSupportStatus,
    TradingRegistrySource,
    UnsupportedTradingLogicError,
    UpperBandPolicy,
    get_trading_logic_configuration,
    list_trading_logic_configurations,
)
from binance_auto_trader.domain.trading.states import TradingStateConfiguration
from binance_auto_trader.domain.trading.stm import TradingSTM
from binance_auto_trader.domain.trading.transitions.catalog import TRANSITION_IDS


TEST_EVALUATION_TIME = datetime(2026, 8, 21, 8, 0, tzinfo=UTC)


class TradingLogicRegistryTests(unittest.TestCase):
    """
    클래스 이름: TradingLogicRegistryTests
    기능: 다섯 REGIME mapping과 TYPE_0 lower-BB transition 범위를 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_registry_covers_five_regimes_in_canonical_order(self) -> None:
        """
        함수 이름: test_registry_covers_five_regimes_in_canonical_order()
        기능: registry가 중복·누락 없이 다섯 REGIME을 canonical 순서로 포함하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        configurations = list_trading_logic_configurations()

        self.assertEqual(5, len(configurations))
        self.assertEqual(
            tuple(RegimeType),
            tuple(configuration.regime_type for configuration in configurations),
        )
        self.assertEqual(
            5,
            len({id(configuration) for configuration in configurations}),
        )

    def test_type_zero_uses_exact_lower_bb_registry_and_resume_lower_watch(self) -> None:
        """
        함수 이름: test_type_zero_uses_exact_lower_bb_registry_and_resume_lower_watch()
        기능: TYPE_0이 109개 lower-BB ID와 상단 접촉 후 하단 감시 복귀 정책을 사용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        configuration = get_trading_logic_configuration(RegimeType.TYPE_0)

        self.assertIs(
            TradingLogicSupportStatus.SUPPORTED,
            configuration.support_status,
        )
        self.assertIs(TradingRegistrySource.LOWER_BB, configuration.transition_source)
        self.assertIsInstance(configuration.transition_ids, tuple)
        self.assertEqual(109, len(configuration.transition_ids))
        self.assertEqual(TRANSITION_IDS, configuration.transition_ids)
        self.assertIs(TradingLogicStartGuard.READY, configuration.start_guard)
        self.assertIs(
            UpperBandPolicy.RESUME_LOWER_WATCH,
            configuration.upper_band_policy,
        )

    def test_unsupported_regimes_expose_no_registry_or_fallback(self) -> None:
        """
        함수 이름: test_unsupported_regimes_expose_no_registry_or_fallback()
        기능: TYPE_1~4가 transition 없이 동일한 typed 시작 차단 코드를 제공하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        for regime_type in tuple(RegimeType)[1:]:
            with self.subTest(regime_type=regime_type):
                configuration = get_trading_logic_configuration(regime_type)

                self.assertIs(
                    TradingLogicSupportStatus.UNSUPPORTED,
                    configuration.support_status,
                )
                self.assertIsNone(configuration.transition_source)
                self.assertEqual((), configuration.transition_ids)
                self.assertIs(
                    TradingLogicStartGuard.UNSUPPORTED_TRADING_LOGIC,
                    configuration.start_guard,
                )
                self.assertIsNone(configuration.upper_band_policy)

                with self.assertRaises(UnsupportedTradingLogicError) as raised:
                    TradingSTM.get_stm_instance(regime_type)
                self.assertEqual("UNSUPPORTED_TRADING_LOGIC", raised.exception.code)
                self.assertIs(regime_type, raised.exception.regime_type)

    def test_trading_stm_requires_explicit_regime_and_keeps_frozen_config(self) -> None:
        """
        함수 이름: test_trading_stm_requires_explicit_regime_and_keeps_frozen_config()
        기능: REGIME 생략 fallback이 없고 세션별 STM이 같은 불변 구성을 선택하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        with self.assertRaises(TypeError):
            TradingSTM()  # type: ignore[call-arg]

        first = TradingSTM.get_stm_instance(RegimeType.TYPE_0)
        second = TradingSTM.get_stm_instance(RegimeType.TYPE_0)

        self.assertIsNot(first, second)
        self.assertIs(first.configuration, second.configuration)
        with self.assertRaises(FrozenInstanceError):
            first.configuration.start_guard = (  # type: ignore[misc]
                TradingLogicStartGuard.UNSUPPORTED_TRADING_LOGIC
            )

    def test_same_snapshot_replays_to_same_safe_termination_result(self) -> None:
        """
        함수 이름: test_same_snapshot_replays_to_same_safe_termination_result()
        기능: 동일한 TYPE_0 상태·이벤트·Context가 결정론적으로 같은 G-07 결과를 내는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        event = TradingEvent(
            event_type=TradingEventType.UPPER_BAND_TOUCHED,
            occurred_at=TEST_EVALUATION_TIME,
            sequence_number=7,
            priority=EventPriority.MARKET,
            event_id="deterministic-g07",
        )
        context = TradingContextView(
            version=9,
            evaluated_at=TEST_EVALUATION_TIME,
            market=MarketEvaluationSnapshot(
                realtime_price=Decimal("120"),
                upper_band=Decimal("120"),
            ),
            runtime=TradingRuntimeSnapshot(),
        )
        first = TradingSTM(RegimeType.TYPE_0)
        second = TradingSTM(RegimeType.TYPE_0)
        first._state = TradingStateConfiguration.create_trade_management_initial_state()
        second._state = (
            TradingStateConfiguration.create_trade_management_initial_state()
        )

        first_result = first.handle(event, context)
        second_result = second.handle(event, context)

        self.assertEqual(first_result, second_result)
        self.assertEqual(("G-07",), first_result.transition_ids)


if __name__ == "__main__":
    unittest.main()
