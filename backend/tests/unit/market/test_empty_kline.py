"""체결 전 빈 봉의 sentinel과 이후 실제 체결 갱신을 검증한다."""
from copy import deepcopy
from decimal import Decimal
import sys
import json
from pathlib import Path
import unittest
from binance_auto_trader.adapters.binance import WebSocketGateway
from binance_auto_trader.adapters.binance.websocket_gateway import _parse_websocket_kline
from binance_auto_trader.domain.common import Interval
from tests.unit.market.test_kline_live_promotion import FakeWebSocketClient, _create_kline_event


def empty_kline():
    """
    함수 이름: empty_kline()
    기능: 공개 시세에서 관측한 체결 없는 봉의 trade ID·수량 형식을 재현한다.
    인자: 없음
    반환값: 1분봉 raw Kline
    작성 날짜: 2026/09/10
    """
    payload = _create_kline_event(close="2492.09", event_offset_milliseconds=2000)
    payload['k'].update(f=-1, L=-1, n=0, v='0.00000000', q='0.00000000', V='0.00000000', Q='0.00000000', h='2492.09', l='2492.09')
    return payload


class EmptyKlineTests(unittest.TestCase):
    """
    클래스 이름: EmptyKlineTests
    기능: 빈 봉 수락과 유효하지 않은 음수 ID의 fail-closed 계약을 검증한다.
    작성 날짜: 2026/09/10
    """
    def test_captured_ethusdt_minute_payload_is_accepted(self):
        """
        함수 이름: test_captured_ethusdt_minute_payload_is_accepted()
        기능: 03:13:02 공개 ETHUSDT 1분봉 원문이 정상 Kline으로 변환되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        path = Path(__file__).parents[2] / 'fixtures/market_snapshots/empty_ethusdt_1m.json'
        kline = _parse_websocket_kline(json.loads(path.read_text()))
        self.assertEqual(kline.close, Decimal('2490.27000000'))
        self.assertEqual(kline.volume, 0)
        self.assertIs(kline.interval, Interval.ONE_MINUTE)
        self.assertFalse(kline.closed)

    def test_empty_then_trade_updates_keep_live_subscription(self):
        """
        함수 이름: test_empty_then_trade_updates_keep_live_subscription()
        기능: 빈 봉, 중복 빈 봉, 첫 거래, 마감까지 같은 구독에서 전달되는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        client = FakeWebSocketClient()
        gateway = WebSocketGateway(client)
        errors, observed = [], []
        handle = gateway.start_all_kline_buffering('ETHUSDT', (Interval.ONE_MINUTE,), reconciliation_required_callback=errors.append)
        gateway.promote_kline_buffer_to_live(observed.append, handle)
        empty = empty_kline()
        client.emit(0, empty)
        client.emit(0, deepcopy(empty))
        tick = _create_kline_event(close='2492.10', event_offset_milliseconds=4000)
        client.emit(0, tick)
        closed = deepcopy(tick)
        closed['E'] += 56000
        closed['k']['x'] = True
        client.emit(0, closed)
        self.assertEqual([k.close for k in observed], [Decimal('2492.09'), Decimal('2492.10'), Decimal('2492.10')])
        self.assertEqual(observed[0].volume, 0)
        self.assertTrue(observed[-1].closed)
        self.assertTrue(gateway.kline_live_ready)
        self.assertEqual(errors, [])

    def test_malformed_empty_metadata_still_fails_closed(self):
        """
        함수 이름: test_malformed_empty_metadata_still_fails_closed()
        기능: sentinel과 모순되는 수량·체결 수 및 잘못된 ID 형식을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        for change in ({'n': 1}, {'n': -1}, {'n': False}, {'f': -2}, {'L': 0}, {'f': '-1'}, {'f': -1.0}, {'f': True}, *({field: '1'} for field in ('v', 'q', 'V', 'Q'))):
            with self.subTest(change=change):
                payload = empty_kline()
                payload['k'].update(change)
                with self.assertRaises((ValueError, TypeError)):
                    _parse_websocket_kline(payload)

    def test_failure_callback_preserves_exception_for_diagnostics(self):
        """
        함수 이름: test_failure_callback_preserves_exception_for_diagnostics()
        기능: 구독 검증 실패의 원인이 진단 callback에서 사라지지 않음을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/10
        """
        errors = []
        client = FakeWebSocketClient()
        gateway = WebSocketGateway(client)
        gateway.start_all_kline_buffering('ETHUSDT', (Interval.ONE_MINUTE,), reconciliation_required_callback=lambda reason: errors.append((reason, sys.exception())))
        invalid = empty_kline()
        invalid['k']['f'] = -2
        with self.assertRaises(ValueError) as raised:
            client.emit(0, invalid)
        self.assertEqual(errors, [('kline_stream_invalid', raised.exception)])
        self.assertFalse(gateway.kline_live_ready)
