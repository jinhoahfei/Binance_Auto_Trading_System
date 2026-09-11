"""01시 실제 장애에서 발견한 독립 일봉 혼입과 canonical 평가 순서를 검증한다."""

from itertools import permutations
import unittest

from binance_auto_trader.application.market_evaluation_builder import ThirtyMinuteMarketEvaluationBuilder
from binance_auto_trader.application.runtime_diagnostics import RuntimeDiagnostics
from binance_auto_trader.domain.common import Interval
from tests.integration.test_market_all_interval_boundary_flow import (
    FOUR_HOUR_BOUNDARY, _create_all_interval_boundary_flow,
    _create_boundary_source_specs, _datetime_to_milliseconds, _emit_boundary_source,
)


class MarketDeferredBoundaryRegressionTests(unittest.TestCase):
    """
    클래스 이름: MarketDeferredBoundaryRegressionTests
    기능: 독립 일봉의 모든 도착 순서에서 경계 source와 평가 version의 일치를 검증한다.
    작성 날짜: 2026/09/11
    """

    def test_day_tick_never_contaminates_canonical_strategy_sources(self):
        """
        함수 이름: test_day_tick_never_contaminates_canonical_strategy_sources()
        기능: 4H 경계 네 봉과 일봉의 120가지 도착 순서가 strict builder 검증을 통과하는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/11
        """
        specs = _create_boundary_source_specs(FOUR_HOUR_BOUNDARY, include_one_day=False)
        specs["day_progress"] = (
            Interval.ONE_DAY, "141",
            _datetime_to_milliseconds(FOUR_HOUR_BOUNDARY.replace(hour=0)),
            _datetime_to_milliseconds(FOUR_HOUR_BOUNDARY) + 250, False,
        )
        for order in permutations(specs):
            with self.subTest(order=order):
                controller, snapshot, clock, rest, socket, builder, observer, regime = (
                    _create_all_interval_boundary_flow(FOUR_HOUR_BOUNDARY)
                )
                verified_sources = []
                diagnostics = []
                controller._diagnostics = RuntimeDiagnostics(diagnostics.append)

                def verify_and_build(current_snapshot, observed):
                    """
                    함수 이름: verify_and_build()
                    기능: 가짜 수치 builder 앞에서 production source 검증과 immutable 기록을 수행한다.
                    인자: current_snapshot -> commit 결과, observed -> 평가의 primary 봉
                    반환값: fixture 평가
                    작성 날짜: 2026/09/11
                    """
                    sources = current_snapshot.update_source_klines
                    if any(source.interval in (Interval.ONE_MINUTE, Interval.THIRTY_MINUTES) for source in sources):
                        ThirtyMinuteMarketEvaluationBuilder._resolve_boundary_one_minute_close_source(current_snapshot, observed)
                    verified_sources.append(sources)
                    return builder(current_snapshot, observed)

                controller._market_evaluation_builder = verify_and_build
                for name in order:
                    _emit_boundary_source(socket, specs[name])
                self.assertEqual(len(verified_sources), 2)
                self.assertEqual(tuple(source.interval for source in verified_sources[0]), (
                    Interval.ONE_MINUTE, Interval.THIRTY_MINUTES, Interval.FOUR_HOURS, Interval.FOUR_HOURS,
                ))
                self.assertEqual(tuple(source.interval for source in verified_sources[1]), (Interval.ONE_DAY,))
                self.assertEqual(str(snapshot.klines_by_interval[Interval.ONE_DAY][-1].close), "141")
                self.assertEqual(len(regime.calls), 2)
                self.assertEqual(observer.market_stream_failures, ["market_stream_initializing"])
                inputs = [record for record in diagnostics if record["event"] == "market_input_observed"]
                self.assertEqual(inputs[0]["details"]["klines"], inputs[0]["details"]["snapshot_source_klines"])
