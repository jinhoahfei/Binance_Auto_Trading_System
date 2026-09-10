"""빈 분봉이 반복돼도 실행 중 전략의 지표가 계속 갱신되는지 검증한다."""
import asyncio
from datetime import timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from binance_auto_trader.application.trading_controller import TradingSessionStatus
from binance_auto_trader.domain.common import Interval
from binance_auto_trader.transport.contracts import map_trading_snapshot
from tests.integration.test_public_market_case2_flow import _create_public_case2_fixture, INITIAL_TIME
from tests.integration.test_market_initialization_flow import _create_web_socket_kline_payload


class EmptyKlineIndicatorFlowTests(unittest.TestCase):
    """
    클래스 이름: EmptyKlineIndicatorFlowTests
    기능: 시세 파싱부터 실행 전략의 화면용 snapshot까지 분봉·30분봉 연속성을 검증한다.
    작성 날짜: 2026/09/10
    """
    def test_three_hours_of_empty_minute_opens_keep_strategy_indicators_updating(self):
        """
        함수 이름: test_three_hours_of_empty_minute_opens_keep_strategy_indicators_updating()
        기능: 3시간을 가속 재생하며 180번 빈 봉 시작 및 6번 30분봉 마감 후 지표 갱신을 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        with TemporaryDirectory() as directory:
            fixture = _create_public_case2_fixture(directory)
            trading = fixture.trading_controller
            self.addCleanup(trading.close_session_resources)
            market = fixture.market_controller
            gateway = market._web_socket_gateway
            client = gateway._web_socket_client
            handle = client.latest_handle
            previous_version = 0
            for minute in range(1, 181):
                boundary = INITIAL_TIME.replace(second=0, microsecond=0) + timedelta(minutes=minute)
                fixture.clock.advance(boundary + timedelta(seconds=2) - fixture.clock())
                price = format(Decimal('120') + Decimal(minute) / 100, 'f')
                event_ms = int(boundary.timestamp() * 1000)
                latest = {i: market._market_snapshot.klines_by_interval[i][-1] for i in Interval}
                closing = [Interval.ONE_MINUTE]
                if boundary.minute % 30 == 0:
                    closing.append(Interval.THIRTY_MINUTES)
                # 경계의 확정 봉을 먼저 전달한 뒤 새 봉과 각 주기의 live tick을 갱신한다.
                updates = [(i, True, False) for i in closing]
                updates += [(i, False, True) for i in closing]
                updates += [(i, False, False) for i in (Interval.FOUR_HOURS, Interval.ONE_DAY)]
                if Interval.THIRTY_MINUTES not in closing:
                    updates.append((Interval.THIRTY_MINUTES, False, False))
                for offset, (interval, closed, new_open) in enumerate(updates):
                    opened = boundary if new_open else latest[interval].open_time
                    payload = _create_web_socket_kline_payload(
                        interval, price, open_time_milliseconds=int(opened.timestamp() * 1000),
                        event_time_milliseconds=event_ms + 10 + offset * 100, closed=closed,
                    )
                    if new_open:
                        payload['k'].update(f=-1, L=-1, n=0, v='0', q='0', V='0', Q='0', h=price, l=price)
                    client.emit(handle, payload)
                    asyncio.run(trading.drain_events())
                wire = map_trading_snapshot(trading, 'fake')
                self.assertIs(trading.status, TradingSessionStatus.RUNNING, minute)
                self.assertEqual(wire['active_logic']['root_state'], 'LOWER_TOUCH_WATCH', minute)
                row = wire['active_logic']['indicators']['conditions'][0]
                self.assertEqual(Decimal(row['value']), Decimal(price), minute)
                self.assertGreater(row['market_version'], previous_version, minute)
                previous_version = row['market_version']
                self.assertTrue(gateway.kline_live_ready, minute)
                self.assertEqual(fixture.position.quantity, 0)
            self.assertEqual(fixture.trade_publications, [])
