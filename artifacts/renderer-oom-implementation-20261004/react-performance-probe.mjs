import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { writeFile } from 'node:fs/promises';
import { PerformanceObserver } from 'node:perf_hooks';
import { start_react_performance_retention_cleanup } from '../../UI/src/shared/api/reactPerformanceRetention.ts';
const require_ui = createRequire(resolve('UI/package.json'));
const { JSDOM } = require_ui('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost', pretendToBeVisual: true });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true });
const React = require_ui('react');
const { createRoot } = require_ui('react-dom/client');
const { flushSync } = require_ui('react-dom');
const root = createRoot(document.getElementById('root'));
const mode = process.argv[2] ?? 'retained';
globalThis.PerformanceObserver = PerformanceObserver;
const stop_cleanup = start_react_performance_retention_cleanup(mode === 'selective');
performance.mark('fixture_external_mark');
performance.measure('fixture_external_measure', { start: 0, end: 1, detail: { application: 'fixture' } });
const samples = [];
/**
 * Function: CandleSurface()
 * Purpose: Render a fixed-size leaf while exercising development prop timing.
 * Arguments: properties -> chart-shaped candle and indicator arrays.
 * Returns: A single fixed DOM span.
 * Date: 2026/10/04
 */
function CandleSurface(properties) {
    return React.createElement('span', null, String(properties.candles.at(-1).close));
}
/**
 * Function: ChartPanel()
 * Purpose: Match a second component boundary that receives the same chart props.
 * Arguments: properties -> chart-shaped candle and indicator arrays.
 * Returns: The fixed-size chart leaf.
 * Date: 2026/10/04
 */
function ChartPanel(properties) {
    return React.createElement(CandleSurface, properties);
}
/**
 * Function: record_sample()
 * Purpose: Collect retained JS heap and User Timing entry counts after forced GC.
 * Arguments: iteration -> committed update count.
 * Returns: None.
 * Date: 2026/10/04
 */
function record_sample(iteration) {
    globalThis.gc();
    const entries = performance.getEntriesByType('measure');
    const last_chart_entry = entries.findLast((entry) => entry.name.includes('CandleSurface'));
    samples.push({ iteration, heap_used_mib: process.memoryUsage().heapUsed / 1024 / 1024,
        measures: entries.length, last_entry_properties: last_chart_entry?.detail?.devtools?.properties?.length ?? 0,
        last_entry_json_bytes: last_chart_entry ? JSON.stringify(last_chart_entry.detail).length : 0 });
    process.stdout.write(`${JSON.stringify(samples.at(-1))}\n`);
}
record_sample(0);
for (let iteration = 1; iteration <= 600; iteration += 1) {
    const candles = Array.from({ length: 1000 }, (_, index) => ({ open_time: 1782072000000 + index * 60000,
        open: 2400 + index / 100, high: 2401 + index / 100, low: 2399 + index / 100,
        close: 2400 + index / 100 + (index === 999 ? iteration / 100 : 0), volume: 100, is_closed: index < 999 }));
    const properties = { candles, ema: candles.map((candle) => ({ open_time: candle.open_time, value: candle.close - 0.1 })),
        bollingerLower: candles.map((candle) => ({ open_time: candle.open_time, value: candle.close - 1 })),
        bollingerUpper: candles.map((candle) => ({ open_time: candle.open_time, value: candle.close + 1 })) };
    flushSync(() => root.render(React.createElement(ChartPanel, properties)));
    if (mode === 'cleared') performance.clearMeasures();
    if (iteration % 10 === 0) await new Promise((resolve_tick) => setImmediate(resolve_tick));
    if (iteration % 100 === 0) record_sample(iteration);
}
const before_clear = samples.at(-1);
const external_measure_preserved = performance.getEntriesByName('fixture_external_measure', 'measure').length === 1;
const external_mark_preserved = performance.getEntriesByName('fixture_external_mark', 'mark').length === 1;
if (mode === 'retained') {
    performance.clearMeasures();
    record_sample('after-clear');
}
root.unmount();
stop_cleanup();
dom.window.close();
await writeFile(resolve(`artifacts/renderer-oom-implementation-20261004/react-performance-${mode}.json`),
    JSON.stringify({ runtime: process.version, react: React.version, mode, candle_count: 1000, samples,
        external_measure_preserved, external_mark_preserved,
        entry_bytes_per_update: before_clear?.last_entry_json_bytes * 2 }, null, 4));
