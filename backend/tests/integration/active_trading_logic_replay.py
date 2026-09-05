"""실제 STM의 시장·가짜 체결 전이를 UI가 재생할 수 있는 offline event로 출력한다."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
from unittest.mock import patch

from binance_auto_trader.domain.trading.context import MarketEvaluationSnapshot
from binance_auto_trader.domain.trading.results import TradingSTMResult
from binance_auto_trader.transport import (
    BackendEventStream,
    create_trading_session_update_observer,
)
from tests.integration.test_buy_sell_flow import (
    BuyFlowFixture,
    FakeOrderScenario,
    _create_buy_flow_fixture,
)


def replay_market_evaluation(
    fixture: BuyFlowFixture,
    market: MarketEvaluationSnapshot,
    source_event_id: str,
) -> tuple[TradingSTMResult, ...]:
    """
    함수 이름: replay_market_evaluation()
    기능: 새로운 시장 version과 평가 입력을 실제 Controller queue에서 처리한다.
    인자: fixture -> 가짜 REST와 임시 이력을 사용하는 실행 세션
        market -> 시나리오의 시장 평가값
        source_event_id -> 입력을 구분할 고정 식별자
    반환값: 실제 STM이 처리한 상태 전이 결과 목록
    작성 날짜: 2026/09/05
    """
    # 시세 수신과 지표 계산 경계의 입력만 재현하고 전략 선택·주문 판단은 production 코드를 실행한다.
    fixture.clock.advance(timedelta(seconds=1))
    controller = fixture.controller
    market_snapshot = controller._market_snapshot  # 테스트 fixture의 메모리 시장 aggregate만 조회한다.
    market_snapshot.update(market_snapshot.klines_by_interval)
    for message_id, event_type in (
        ("1L.1", "KLINE_OBSERVED"),
        ("1L.2", "MARKET_EVALUATED"),
    ):
        controller.observe_public_market_boundary(
            message_id=message_id,
            event_type=event_type,
            source_event_id=source_event_id,
            market_version=market_snapshot.version,
        )
    controller.observe_market_evaluation(
        market,
        source_event_id=source_event_id,
        market_version=market_snapshot.version,
    )
    return asyncio.run(controller.drain_events(max_microsteps=100))


def record_replay_step(
    fixture: BuyFlowFixture,
    event_stream: BackendEventStream,
    *,
    stage: str,
    expected_label: str,
    expected_strategies: tuple[str, ...],
    results: tuple[TradingSTMResult, ...] = (),
) -> dict[str, object]:
    """
    함수 이름: record_replay_step()
    기능: 실제 transport event의 전략을 독립 기대값과 비교한 뒤 UI 검증 단계로 기록한다.
    인자: fixture -> 실행 중인 가짜 거래 세션
        event_stream -> 순서가 증가하는 transport event stream
        stage -> 검증 단계 이름
        expected_label -> UI가 표시해야 하는 문구
        expected_strategies -> 이 상황에서 기대하는 backend Case 목록
        results -> 이 단계에서 실제 처리한 STM 결과
    반환값: 기대 문구, 실제 전이 ID와 production event DTO를 가진 단계
    작성 날짜: 2026/09/05
    """
    # UI 문구를 backend에 주입하지 않고 production observer가 만든 DTO를 그대로 보존한다.
    observer = create_trading_session_update_observer(event_stream)
    event = observer(fixture.controller, "fake")
    active_logic = event.payload["trading"]["active_logic"]
    actual_strategies = () if active_logic is None else tuple(active_logic["active_strategies"])
    if actual_strategies != expected_strategies:
        raise AssertionError(f"{stage}: expected {expected_strategies}, got {actual_strategies}; transitions={[result.transition_ids for result in results]}; phase={active_logic.get('indicators') if active_logic else None}")

    # 전이 ID도 출력해 단순히 표시용 필드를 바꾼 테스트와 실제 상태 전이를 구분할 수 있게 한다.
    return {
        "stage": stage,
        "expected_label": expected_label,
        "transition_ids": [
            transition_id
            for result in results
            for transition_id in result.transition_ids
        ],
        "event": event.to_dto(),
    }


def replay_entry_scenario(strategy: str) -> dict[str, object]:
    """
    함수 이름: replay_entry_scenario()
    기능: Case별 시장 조건으로 실제 매수 전이·NEW·FILLED·중지·종료를 재현한다.
    인자: strategy -> 검증할 CASE_B 또는 CASE_C
    반환값: 실제 제출 횟수와 순서대로 기록한 UI 재생 단계
    작성 날짜: 2026/09/05
    """
    # 모든 주문은 기존 FakeOrderRESTClient에서 처리하고 이력 파일은 임시 디렉터리에만 남긴다.
    with TemporaryDirectory() as temporary_directory:
        fixture = _create_buy_flow_fixture(temporary_directory, FakeOrderScenario.NEW_THEN_FILLED)
        controller = fixture.controller
        controller.update_split_ratios(command_id="replay-split", expected_version=controller.context.version,
            scale_in=Decimal("0.5"), scale_out=Decimal("1"))  # 회복 인계 검증은 전량 매도 시나리오다.
        event_stream = BackendEventStream(clock=fixture.clock)
        try:
            steps = [record_replay_step(
                fixture, event_stream,
                stage="시작 후 하단 접촉 대기", expected_label="하단 밴드 대기", expected_strategies=(),
            )]
            market = MarketEvaluationSnapshot(
                realtime_price=Decimal("4320"),
                lower_band=Decimal("4350"),
                upper_band=Decimal("4500"),
                realtime_pct_b=Decimal("-0.3"),
                current_30m_candle_id="ETHUSDT:30m:replay-1",
                current_30m_low=Decimal("4310"),
                current_30m_high=Decimal("4360"),
                touch_candle_bbw=Decimal("0.01"),
                cci_30m_realtime=Decimal("-150") if strategy == "CASE_C" else Decimal("0"),
            )
            lower_results = replay_market_evaluation(fixture, market, "lower-touch")
            steps.append(record_replay_step(
                fixture, event_stream,
                stage="하단 접촉 후 두 Case 감시", expected_label="Case_B / Case_C",
                expected_strategies=("CASE_B", "CASE_C"), results=lower_results,
            ))

            # B는 확정봉 signal 뒤 pullback, C는 flush 뒤 회복을 입력해 서로 다른 매수 전이를 유도한다.
            if strategy == "CASE_B":
                market = replace(
                    market,
                    realtime_price=Decimal("4400"),
                    realtime_pct_b=Decimal("0.5"),
                    confirmed_30m_close=True,
                    current_30m_candle_id="ETHUSDT:30m:replay-2",
                    current_30m_low=Decimal("4360"),
                    pct_b_close=Decimal("0.5"),
                    current_closed_candle_low=Decimal("4360"),
                    previous_3_closed_candle_lows=(Decimal("4310"), Decimal("4320"), Decimal("4330")),
                )
                signal_results = replay_market_evaluation(fixture, market, "signal-close")
                steps.append(record_replay_step(
                    fixture, event_stream,
                    stage="Case B 신호 확정 후 눌림 대기", expected_label="Case_B / Case_C",
                    expected_strategies=("CASE_B", "CASE_C"), results=signal_results,
                ))
            entry_market = replace(
                market,
                realtime_price=Decimal("4327"),
                realtime_pct_b=Decimal("0.2") if strategy == "CASE_B" else Decimal("-0.24"),
                confirmed_30m_close=False,
            )
            entry_results = replay_market_evaluation(fixture, entry_market, "entry-condition")
            entry_transition = "B-09" if strategy == "CASE_B" else "C-12"
            if not any(entry_transition in result.transition_ids for result in entry_results):
                raise AssertionError(f"The real STM did not take {entry_transition}")
            expected_label = "Case_B" if strategy == "CASE_B" else "Case_C"
            steps.append(record_replay_step(
                fixture, event_stream,
                stage=f"{strategy} 매수 요청·미체결", expected_label=expected_label,
                expected_strategies=(strategy,), results=entry_results,
            ))

            # 동일 가짜 주문의 조회 결과를 FILLED로 진행해 production Position owner 전이까지 처리한다.
            controller.trigger_order_reconciliation(occurred_at=fixture.clock.advance(timedelta(seconds=1)))
            fill_results = asyncio.run(controller.drain_events(max_microsteps=100))
            if fixture.position.owner is None or fixture.position.owner.value != strategy:
                raise AssertionError("The filled Position must belong to the requested Case")
            steps.append(record_replay_step(
                fixture, event_stream,
                stage=f"{strategy} 체결·포지션 관리", expected_label=expected_label,
                expected_strategies=(strategy,), results=fill_results,
            ))

            # 동일 ACTIVE STATE에서도 값·유지시간 판정이 바뀌는 실제 holding 입력을 재생한다.
            holding_market = replace(entry_market, realtime_price=Decimal("4400"),
                realtime_pct_b=Decimal("0.61") if strategy == "CASE_B" else Decimal("0.09"),
                realtime_ema_slope=Decimal("0.09") if strategy == "CASE_B" else Decimal("-0.55"))
            for held in (False, True) if strategy == "CASE_B" else (False,):
                holding_market = replace(holding_market, pct_b_at_least_060_for_5s=held)
                results = replay_market_evaluation(fixture, holding_market, f"holding-duration-{held}")
                step = record_replay_step(fixture, event_stream,
                    stage=f"{strategy} 같은 보유 단계 지표 갱신 {held}", expected_label=expected_label,
                    expected_strategies=(strategy,), results=results)
                rows = step["event"]["payload"]["trading"]["active_logic"]["indicators"]["conditions"]
                checked_id = "b_profit_zone" if strategy == "CASE_B" else "c_stop"
                if next(row for row in rows if row["condition_id"] == checked_id)["satisfied"] is not held:
                    raise AssertionError("Displayed duration must equal the strategy builder result")
                steps.append(step)

            # 같은 Case 안의 추세 유지·익절 trailing 전이도 실제 event queue로 처리한다.
            trend_market = replace(holding_market, realtime_slope_above_008_for_5s=True,
                realtime_pct_b=Decimal("0.61") if strategy == "CASE_B" else Decimal("0.10"),
                tp_reference_ema_slope=Decimal("0.15"))
            results = replay_market_evaluation(fixture, trend_market, "trend-or-trailing")
            steps.append(record_replay_step(fixture, event_stream,
                stage=f"{strategy} 추세·트레일링 단계 전환", expected_label=expected_label,
                expected_strategies=(strategy,), results=results))
            if strategy == "CASE_C":
                for confirmed, slope in ((True, "0.2"), (False, "0.99")):
                    trailing_market = replace(trend_market, realtime_pct_b=Decimal("0.20"),
                        confirmed_1m_close=confirmed, current_close_ema_slope=Decimal(slope))
                    results = replay_market_evaluation(fixture, trailing_market, f"trailing-{confirmed}")
                    step = record_replay_step(fixture, event_stream,
                        stage=f"C 이전 EMA 기준 보존 {confirmed}", expected_label="Case_C",
                        expected_strategies=("CASE_C",), results=results)
                    rows = step["event"]["payload"]["trading"]["active_logic"]["indicators"]["conditions"]
                    row = next(row for row in rows if row["condition_id"] == "c_trail_increase")
                    if (row["value"], row["threshold"], row["satisfied"]) != ("0.2", "0.15", True):
                        raise AssertionError("PC-15 must preserve the previous comparison reference")
                    steps.append(step)

                # 확정 1분봉 slope 비증가로 실제 청산한 뒤 회복 대기와 B 인계까지 진행한다.
                fixture.rest_client.scenario = FakeOrderScenario.IMMEDIATE_FILLED
                exit_market = replace(trend_market, realtime_pct_b=Decimal("0.20"),
                    confirmed_1m_close=True, current_close_ema_slope=Decimal("0.2"))
                results = replay_market_evaluation(fixture, exit_market, "trailing-exit")
                steps.append(record_replay_step(fixture, event_stream,
                    stage="C 청산 후 회복 대기", expected_label="Case_B",
                    expected_strategies=("CASE_B",), results=results))
                recovery_market = replace(exit_market, confirmed_1m_close=False, realtime_pct_b=Decimal("0.25"))
                results = replay_market_evaluation(fixture, recovery_market, "c-recovery-b-handoff")
                steps.append(record_replay_step(fixture, event_stream,
                    stage="C 회복 후 B 활성 인계", expected_label="Case_B",
                    expected_strategies=("CASE_B",), results=results))

            # 중지 청산만 즉시 체결하는 fake 응답으로 바꿔 G-06과 G-06F 사이의 표시도 관찰한다.
            fixture.rest_client.scenario = FakeOrderScenario.IMMEDIATE_FILLED
            controller.stop_trading(command_id="replay-stop", expected_version=controller.context.version)
            steps.append(record_replay_step(
                fixture, event_stream,
                stage="가짜 청산 완료·종료 전이 대기",
                expected_label="중지 처리 중" if strategy == "CASE_B" else "자동매매 종료", expected_strategies=(),
            ))
            stop_results = asyncio.run(controller.drain_events(max_microsteps=100))
            steps.append(record_replay_step(
                fixture, event_stream,
                stage="세션 종료", expected_label="자동매매 종료", expected_strategies=(), results=stop_results,
            ))
            if len(fixture.rest_client.submitted_orders) != 2 or fixture.position.quantity != Decimal("0"):
                raise AssertionError("Each scenario must finish one fake buy and one fake liquidation")
            return {"strategy": strategy, "fake_order_count": 2, "steps": steps}
        finally:
            controller.close_session_resources()  # 실행 중인 사용자 앱이나 그 session에는 접근하지 않는다.


def replay_recovery_timer_scenario() -> dict[str, object]:
    """
    함수 이름: replay_recovery_timer_scenario()
    기능: 실제 C setup의 대기·정확히 3분·초과·저점 갱신을 거래 없이 재생한다.
    인자: 없음
    반환값: 실제 STM timer event와 독립적인 시간·상태 기대값
    작성 날짜: 2026/09/05
    """
    with TemporaryDirectory() as temporary_directory:
        fixture = _create_buy_flow_fixture(temporary_directory, FakeOrderScenario.NEW_THEN_FILLED)
        event_stream = BackendEventStream(clock=fixture.clock)
        steps = []
        try:
            market = MarketEvaluationSnapshot(
                realtime_price=Decimal("4320"), lower_band=Decimal("4350"), upper_band=Decimal("4500"),
                realtime_pct_b=Decimal("-0.20"), current_30m_candle_id="ETHUSDT:30m:timer",
                current_30m_low=Decimal("4310"), current_30m_high=Decimal("4360"),
                touch_candle_bbw=Decimal("0.01"), cci_30m_realtime=Decimal("-150"),
            )

            def record_timer(stage: str, results: tuple[TradingSTMResult, ...], condition_id: str, remaining: str, state: str, reason: str | None = None) -> None:
                """
                함수 이름: record_timer()
                기능: 실제 production timer DTO를 독립적인 기대값과 검사한 뒤 UI 입력에 추가한다.
                인자: stage -> 단계명, results -> 실제 처리 결과, condition_id -> 검사 행
                    remaining -> 기대 잔여 초, state -> 기대 상태, reason -> 기대 리셋 사유
                반환값: 없음
                작성 날짜: 2026/09/05
                """
                step = record_replay_step(fixture, event_stream, stage=stage, expected_label="Case_B / Case_C",
                    expected_strategies=("CASE_B", "CASE_C"), results=results)
                rows = step["event"]["payload"]["trading"]["active_logic"]["indicators"]["conditions"]
                row = next(row for row in rows if row["condition_id"] == condition_id)
                actual = row["timer"]
                if (actual["remaining_seconds"], actual["state"], actual["reset_reason"]) != (remaining, state, reason):
                    raise AssertionError(f"{stage}: unexpected timer {actual}")
                step.update(timer_condition_id=condition_id, expected_remaining_seconds=remaining,
                    expected_timer_state=state, expected_reset_reason=reason)
                steps.append(step)  # 표시 기대값으로 production event를 덮어쓰지 않는다.

            # 아직 flush가 없으면 실제 C_SETUP 단계에서도 3분 타이머는 시작 대기다.
            results = replay_market_evaluation(fixture, market, "timer-waiting")
            record_timer("C 최초 저점 확인 대기", results, "c_flush", "180", "waiting")
            market = replace(market, realtime_price=Decimal("4305"), realtime_pct_b=Decimal("-0.30"))
            results = replay_market_evaluation(fixture, market, "timer-first-flush")
            record_timer("C 최초 회복 타이머 시작", results, "c_recovery_window", "180", "running")
            fixture.clock.advance(timedelta(seconds=178))
            results = replay_market_evaluation(fixture, market, "timer-before-boundary")
            record_timer("C 회복 만료 1초 전", results, "c_recovery_window", "1", "running")
            results = replay_market_evaluation(fixture, market, "timer-exact-boundary")
            record_timer("C 회복 정확히 180초", results, "c_recovery_window", "0", "running")
            if any("C-11" in result.transition_ids for result in results):
                raise AssertionError("Exactly 180 seconds must still permit recovery")
            results = replay_market_evaluation(fixture, market, "timer-expired")
            record_timer("C 시간 초과 후 즉시 재시작", results, "c_recovery_window", "180", "running", "timeout")
            if not any("C-11" in result.transition_ids for result in results):
                raise AssertionError("Recovery timeout must use the real C-11 transition")
            market = replace(market, realtime_price=Decimal("4300"), realtime_pct_b=Decimal("-0.333"))
            results = replay_market_evaluation(fixture, market, "timer-new-low")
            record_timer("C 새 저점에서 즉시 재시작", results, "c_recovery_window", "180", "running", "new_low")
            if not any("C-10" in result.transition_ids for result in results):
                raise AssertionError("A new low must use the real C-10 transition")
            results = replay_market_evaluation(fixture, market, "timer-new-generation-tick")
            record_timer("C 새 회차의 첫 유효 평가", results, "c_recovery_window", "179", "running", "new_low")
            fixture.controller.stop_trading(command_id="timer-stop", expected_version=fixture.controller.context.version)
            results = asyncio.run(fixture.controller.drain_events(max_microsteps=100))
            steps.append(record_replay_step(fixture, event_stream, stage="C 타이머 종료", expected_label="자동매매 종료",
                expected_strategies=(), results=results))
            if fixture.rest_client.submitted_orders:
                raise AssertionError("Recovery timer observation must not submit any order")
            return {"steps": steps, "fake_order_count": 0}
        finally:
            fixture.controller.close_session_resources()


def main() -> None:
    """
    함수 이름: main()
    기능: 외부 socket 연결을 차단한 별도 process에서 두 Case의 실제 전이 event를 JSON으로 출력한다.
    인자: 없음
    반환값: 없음
    작성 날짜: 2026/09/05
    """
    # 실수로 실제 REST 경로가 연결되어도 외부 통신 전에 즉시 실패하도록 검사 범위를 고정한다.
    with patch("socket.socket.connect", side_effect=AssertionError("Offline replay forbids network")), patch(
        "socket.create_connection", side_effect=AssertionError("Offline replay forbids network"),
    ):
        report = {
            "network_connections_allowed": False,
            "scenarios": [replay_entry_scenario(strategy) for strategy in ("CASE_B", "CASE_C")],
            "timer_scenario": replay_recovery_timer_scenario(),
        }
    print(json.dumps(report, ensure_ascii=False))  # UI test가 실제 wire DTO를 동일한 bytes로 재사용한다.


if __name__ == "__main__":
    main()
