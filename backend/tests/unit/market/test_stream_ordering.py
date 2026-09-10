"""Synthetic wire cases; these are not claims about unrecorded exchange payloads."""
import unittest
from binance_auto_trader.adapters.binance.websocket_gateway import WebSocketGateway
from binance_auto_trader.domain.market import Interval
from tests.unit.market.test_kline_live_promotion import FakeWebSocketClient, _create_kline_event

class StreamOrderingTests(unittest.TestCase):
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
