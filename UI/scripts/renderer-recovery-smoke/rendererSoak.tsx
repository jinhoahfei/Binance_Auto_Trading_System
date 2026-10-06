// 실제 차트 컴포넌트에 합성 봉만 공급하는 격리 검증 진입점이며 거래소를 호출하지 않는다.
import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { invoke } from '@tauri-apps/api/core';
import { PriceChartPanel } from '../../src/features/price-chart/components/PriceChartPanel';
import { create_realtime_chart_view_model } from '../../src/features/price-chart/presenters/createRealtimeChartViewModel';
import type { NormalizedKline } from '../../src/features/price-chart/data/types';
import { start_react_performance_retention_cleanup } from '../../src/shared/api/reactPerformanceRetention';
import { observe_renderer_connection } from '../../src/shared/api/rendererLiveness';
import '../../src/shared/styles/global.css';
import './main';

declare const __SOAK_DEVELOPMENT_BUILD__: boolean;
declare const __SOAK_CLEANUP_ENABLED__: boolean;

const CANDLE_COUNT = 1000;
const TICK_INTERVAL_MS = 250;
const started_at = performance.now();
const counters = { ticks: 0, errors: 0, generated_measure_detail_characters: 0 };
const stop_cleanup = start_react_performance_retention_cleanup(
    __SOAK_DEVELOPMENT_BUILD__ && __SOAK_CLEANUP_ENABLED__,
);


/**
 * 함수 이름: count_measure_characters()
 * 기능: 새 측정의 고정 properties 배열에서 문자열 길이만 합산하고 내용·객체는 보관하지 않는다.
 * 인자: entries -> 이번 observer 통지의 성능 측정 목록
 * 반환값: 없음
 * 작성 날짜: 2026/10/04
 */
function count_measure_characters(entries: readonly PerformanceEntry[]): void {
    for (const entry of entries) {
        const detail: unknown = (entry as PerformanceMeasure).detail;
        if (typeof detail !== 'object' || detail === null || !('devtools' in detail)) continue;
        const devtools = detail.devtools;
        if (typeof devtools !== 'object' || devtools === null || !('properties' in devtools)
            || !Array.isArray(devtools.properties)) continue;
        for (const property of devtools.properties) {
            if (!Array.isArray(property)) continue;
            for (const value of property) {
                if (typeof value === 'string') counters.generated_measure_detail_characters += value.length;
            }
        }
    }
}

const measure_observer = new PerformanceObserver((entries) => count_measure_characters(entries.getEntries()));
measure_observer.observe({ type: 'measure', buffered: true });


/**
 * 함수 이름: create_synthetic_candles()
 * 기능: 고정 시각과 수식만 사용해 외부 I/O 없는 1000개 ETHUSDT 봉을 만든다.
 * 인자: 없음
 * 반환값: 검증 전용 1분 봉 목록
 * 작성 날짜: 2026/10/04
 */
function create_synthetic_candles(): readonly NormalizedKline[] {
    return Array.from({ length: CANDLE_COUNT }, (_, index) => {
        const open_time = Date.UTC(2026, 8, 1) + index * 60_000;
        const open = 2400 + Math.sin(index / 20) * 80;
        const close = open + Math.sin(index / 3) * 6;

        return {
            symbol: 'ETHUSDT', interval: '1m', open_time, close_time: open_time + 59_999,
            open, close, high: Math.max(open, close) + 2, low: Math.min(open, close) - 2,
            volume: 100 + index % 30, is_closed: index < CANDLE_COUNT - 1,
        };
    });
}


/**
 * 함수 이름: read_heap_bytes()
 * 기능: 지원되는 Chromium heap 지표를 읽고 미지원·비정상 값은 null로 구분한다.
 * 인자: name -> 허용한 heap 통계 필드
 * 반환값: byte 단위 숫자 또는 null; GC 후 retained heap으로 해석하지 않음
 * 작성 날짜: 2026/10/04
 */
function read_heap_bytes(name: 'usedJSHeapSize' | 'totalJSHeapSize'): number | null {
    const memory = (performance as Performance & { memory?: Record<string, unknown> }).memory;
    const value = memory?.[name];

    return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
}


/**
 * 함수 이름: start_samples()
 * 기능: 실제 DOM·timeline·heap 수치를 5초마다 한 개의 미완료 IPC 상한으로 기록한다.
 * 인자: 없음
 * 반환값: 수집 타이머 종료 함수
 * 작성 날짜: 2026/10/04
 */
function start_samples(): () => void {
    let pending = false;
    let stopped = false;

    /**
     * 함수 이름: sample()
     * 기능: 하나의 제한된 수치 표본을 native 전용 검증 로그로 전달한다.
     * 인자: 없음
     * 반환값: 진단 전송 완료 Promise
     * 작성 날짜: 2026/10/04
     */
    const sample = async () => {
        if (pending || stopped) return;
        pending = true;
        try {
            await invoke('record_renderer_soak_sample', { sample: {
                elapsed_ms: Math.round(performance.now() - started_at), ticks: counters.ticks,
                candle_count: CANDLE_COUNT, dom_nodes: document.querySelectorAll('*').length,
                measure_count: performance.getEntriesByType('measure').length,
                generated_measure_detail_characters: counters.generated_measure_detail_characters,
                js_heap_used_bytes: read_heap_bytes('usedJSHeapSize'),
                js_heap_total_bytes: read_heap_bytes('totalJSHeapSize'),
                cleanup_enabled: __SOAK_CLEANUP_ENABLED__, development_build: __SOAK_DEVELOPMENT_BUILD__,
                errors: counters.errors,
            } });
        } catch { counters.errors++; }
        finally { pending = false; }
    };
    const timer = setInterval(() => void sample(), 5_000);
    void sample();

    return () => { stopped = true; clearInterval(timer); };
}


/**
 * 함수 이름: RendererChartProbe()
 * 기능: 운영 PriceChartPanel과 지표 presenter를 250ms마다 실제 React·canvas 경로로 실행한다.
 * 인자: 없음
 * 반환값: 격리된 1000봉 가격 차트
 * 작성 날짜: 2026/10/04
 */
function RendererChartProbe() {
    const [klines, set_klines] = useState(create_synthetic_candles);
    useEffect(() => {
        const timer = setInterval(() => {
            const freeze_after_ticks = (window as Window & {
                __RENDERER_SOAK_FREEZE_AFTER_TICKS__?: number;
            }).__RENDERER_SOAK_FREEZE_AFTER_TICKS__ ?? 0;
            if (freeze_after_ticks > 0 && counters.ticks >= freeze_after_ticks) return;
            counters.ticks++;
            set_klines((previous) => {
                const last = previous[previous.length - 1]!;
                const close = last.open + Math.sin(counters.ticks / 7) * 8;

                return [...previous.slice(0, -1), {
                    ...last, close, high: Math.max(last.open, close) + 2,
                    low: Math.min(last.open, close) - 2, volume: 100 + counters.ticks % 30,
                    event_time: last.open_time + counters.ticks,
                }];
            });
            observe_renderer_connection({ last_chart_received_at_ms: Date.now() });
        }, TICK_INTERVAL_MS);

        return () => clearInterval(timer);
    }, []);
    const model = create_realtime_chart_view_model({
        symbol: 'ETHUSDT', data_status: 'live', status_message: null,
        data_revision: counters.ticks, updated_at: klines[klines.length - 1]!.open_time + counters.ticks * TICK_INTERVAL_MS,
        klines_by_interval: { '1m': klines, '30m': [], '4h': [], '1d': [] },
    }, '1m');

    return (
        <main style={{ padding: 24, height: '100vh', display: 'grid' }}>
            <PriceChartPanel {...model} activeState="READY" interval="1m"
                indicatorSettings={{ ema9: true, bollingerBand: true, volume: true }} />
        </main>
    );
}

const root = createRoot(document.getElementById('root')!);
const stop_samples = start_samples();
window.addEventListener('error', () => { counters.errors++; });
window.addEventListener('unhandledrejection', () => { counters.errors++; });
window.addEventListener('pagehide', () => {
    stop_samples();
    stop_cleanup();
    measure_observer.disconnect();
    root.unmount();
}, { once: true });
root.render(<RendererChartProbe />);
