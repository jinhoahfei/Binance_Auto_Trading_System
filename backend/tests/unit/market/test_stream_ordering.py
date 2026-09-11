"""Synthetic wire cases; these are not claims about unrecorded exchange payloads."""
import unittest
from binance_auto_trader.adapters.binance.websocket_gateway import WebSocketGateway
from binance_auto_trader.domain.market import Interval
from tests.unit.market.test_kline_live_promotion import FakeWebSocketClient, _create_kline_event

class StreamOrderingTests(unittest.TestCase):
    def test_same_time_closed_candle_and_exact_next_open_remain_live(self):
        """
        함수 이름: test_same_time_closed_candle_and_exact_next_open_remain_live()
        기능: 실제 거래소에서 관찰된 동일 E의 확정→다음 진행 전환을 Gateway 전체 경로로 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/11
        """
        client = FakeWebSocketClient()
        gateway = WebSocketGateway(client)
        seen = []
        sub = gateway.start_all_kline_buffering('ETHUSDT', (Interval.ONE_MINUTE,))
        gateway.promote_kline_buffer_to_live(seen.append, sub)
        final = _create_kline_event(close='100', event_offset_milliseconds=60000)
        final['k']['x'] = True
        client.emit(0, final)
        next_open = _create_kline_event(close='101', event_offset_milliseconds=60000)
        next_open['k']['t'] += 60000
        next_open['k']['T'] += 60000
        client.emit(0, next_open)
        self.assertEqual(len(seen), 2)
        self.assertTrue(gateway.kline_live_ready)

    def test_same_time_skipped_candle_still_requires_resync(self):
        """
        함수 이름: test_same_time_skipped_candle_still_requires_resync()
        기능: 동일 E 허용이 중간 봉 누락을 숨기지 않는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/11
        """
        client = FakeWebSocketClient()
        gateway = WebSocketGateway(client)
        sub = gateway.start_all_kline_buffering('ETHUSDT', (Interval.ONE_MINUTE,))
        gateway.promote_kline_buffer_to_live(lambda item: None, sub)
        final = _create_kline_event(close='100', event_offset_milliseconds=120000)
        final['k']['x'] = True
        client.emit(0, final)
        gap = _create_kline_event(close='101', event_offset_milliseconds=120000)
        gap['k']['t'] += 120000
        gap['k']['T'] += 120000
        with self.assertRaises(ValueError):
            client.emit(0, gap)
        self.assertFalse(gateway.kline_live_ready)

    def test_duplicate_stale_and_same_time_finalization(self):
        client = FakeWebSocketClient()
        gateway = WebSocketGateway(client)
        seen = []
        sub = gateway.start_all_kline_buffering('ETHUSDT', (Interval.ONE_MINUTE,))
        gateway.promote_kline_buffer_to_live(seen.append, sub)
        original = _create_kline_event(close='100', event_offset_milliseconds=60000)
        client.emit(0, original)
        client.emit(0, original)
        client.emit(0, _create_kline_event(close='99', event_offset_milliseconds=59000))
        final = _create_kline_event(close='100', event_offset_milliseconds=60000)
        final['k']['x'] = True
        client.emit(0, final)
        client.emit(0, _create_kline_event(close='100', event_offset_milliseconds=61000))
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[-1].closed)
        self.assertTrue(gateway.kline_live_ready)

    def test_same_time_ambiguous_change_requires_resync_and_records_reason(self):
        client = FakeWebSocketClient()
        records = []
        gateway = WebSocketGateway(client, market_diagnostic_callback=lambda event, **details: records.append(details))
        sub = gateway.start_all_kline_buffering('ETHUSDT', (Interval.ONE_MINUTE,))
        gateway.promote_kline_buffer_to_live(lambda item: None, sub)
        client.emit(0, _create_kline_event(close='100', event_offset_milliseconds=1000))
        with self.assertRaises(ValueError):
            client.emit(0, _create_kline_event(close='101', event_offset_milliseconds=1000))
        self.assertFalse(gateway.kline_live_ready)
        self.assertEqual(records[-1]['reason'], 'conflict')
