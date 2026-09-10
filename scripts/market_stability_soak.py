"""Real public-market soak: no account access, credentials, or order-capable controller."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import resource
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))
from binance_auto_trader.adapters.binance.live_clients import BinanceLiveRESTClient, BinanceLiveWebSocketClient
from binance_auto_trader.adapters.binance import APIGateway, WebSocketGateway
from binance_auto_trader.adapters.filesystem.diagnostic_log_writer import DiagnosticLogWriter
from binance_auto_trader.application import MarketDataController, RegimeController
from binance_auto_trader.application.market_evaluation_builder import ThirtyMinuteMarketEvaluationBuilder
from binance_auto_trader.application.runtime_diagnostics import RuntimeDiagnostics
from binance_auto_trader.domain.market import MarketSnapshot
from binance_auto_trader.domain.regime import RegimeSTM


class PublicREST:
    """Expose only the public candle operation; never read local credentials."""
    def __init__(self):
        self.client = BinanceLiveRESTClient('public-soak-placeholder', 'unused-placeholder')

    def get_klines(self, **kwargs):
        return self.client.get_klines(**kwargs)


class PublicWebSocket:
    """Only public subscriptions are available to this harness."""
    def __init__(self):
        self.client = BinanceLiveWebSocketClient('public-soak-placeholder', 'unused-placeholder')

    def subscribe_all_kline_streams(self, **kwargs):
        return self.client.subscribe_all_kline_streams(**kwargs)


class EvaluationObserver:
    def __init__(self):
        self.evaluations = 0
        self.last_evaluation = time.monotonic()
        self.last_input = None

    def note_market_input(self):
        self.last_input = time.monotonic()

    def observe_market_evaluation(self, market, **source):
        self.evaluations += 1
        self.last_evaluation = time.monotonic()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hours', type=float, default=48)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 0 < args.hours <= 168:
        parser.error('hours must be in (0, 168]')
    args.output.mkdir(parents=True, exist_ok=False)
    writer = DiagnosticLogWriter(args.output, 'disabled')
    counters = Counter()
    counter_lock = threading.Lock()

    def record(item):
        with counter_lock:
            counters[item['event']] += 1
        writer(item)

    diagnostics = RuntimeDiagnostics(record)
    observer = EvaluationObserver()
    snapshot = MarketSnapshot(symbol='ETHUSDT')
    regime = RegimeController(RegimeSTM(), snapshot)
    gateway = WebSocketGateway(PublicWebSocket(), market_diagnostic_callback=diagnostics.record)
    recover = threading.Event()
    controller = MarketDataController(APIGateway(PublicREST()), gateway, snapshot, regime,
        market_evaluation_builder=ThirtyMinuteMarketEvaluationBuilder(),
        trading_market_observer=observer, market_stream_recovery_requester=recover.set,
        diagnostics=diagnostics)
    start = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    deadline = start + args.hours * 3600
    next_retry = start
    retry_index = 0
    next_sample = start
    last_success = None
    failures = 0
    last_failure_type = None
    interrupted = False
    recover.set()
    diagnostics.record('soak_started', hours=args.hours, orders_enabled=False, pid=os.getpid())

    def report(status):
        now = time.monotonic()
        with counter_lock:
            event_counts = dict(counters)
        result = dict(status=status, pid=os.getpid(), started_at=started_at,
            updated_at=datetime.now(timezone.utc).isoformat(), target_hours=args.hours,
            observed_seconds=round(now-start, 3), orders_enabled=False,
            evaluations=observer.evaluations, market_version=snapshot.version,
            seconds_since_evaluation=round(now-observer.last_evaluation, 3),
            failures=failures, last_failure_type=last_failure_type,
            recovery_pending=recover.is_set(), event_counts=event_counts,
            thread_count=threading.active_count(), max_rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            gateway_fingerprint_count=gateway.kline_replay_buffer_size,
            last_success_seconds=None if last_success is None else round(last_success-start, 3),
            validation_scope='public market and production indicators only; no account/order validation')
        with (args.output / 'metrics.jsonl').open('a') as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + '\n')
        temporary = args.output / 'status.tmp'
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        temporary.replace(args.output / 'status.json')

    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now - max(observer.last_evaluation, last_success or start) >= 60:
                recover.set()
            if recover.is_set() and now >= next_retry:
                recover.clear()
                try:
                    controller.reconcile_market_stream()
                    last_success = time.monotonic()
                    retry_index = 0
                    last_failure_type = None
                    diagnostics.record('soak_market_recovered', market_version=snapshot.version)
                except Exception as error:
                    failures += 1
                    last_failure_type = type(error).__name__
                    diagnostics.record_exception('soak_recovery', error)
                    recover.set()
                    next_retry = time.monotonic() + (1, 2, 4, 8, 16, 30)[min(retry_index, 5)]
                    retry_index += 1
            if now >= next_sample:
                report('running')
                next_sample = now + 10
            time.sleep(0.25)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        controller.close_market_stream()
        report('interrupted' if interrupted else 'completed_needs_review')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
