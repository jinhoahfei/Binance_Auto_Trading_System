import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { invoke } from '@tauri-apps/api/core';
import { BackendUiAdapter } from '../../src/shared/api/BackendUiAdapter';
import { start_renderer_liveness } from '../../src/shared/api/rendererLiveness';
import { use_realtime_chart_data } from '../../src/features/price-chart/hooks/useRealtimeChartData';

const started = performance.now();
const stats = { events: 0, recoveries: 0, errors: 0 };
start_renderer_liveness();
async function connect() {
  const descriptor = await invoke('get_background_soak_descriptor');
  const adapter = new BackendUiAdapter(descriptor);
  const snapshot = await adapter.load_snapshot();
  adapter.start_live_events(snapshot, { on_event: () => { stats.events++; }, on_full_resync: () => {},
    on_reconnecting: () => {}, on_ready: () => { stats.recoveries++; }, on_failure: () => { stats.errors++; } });
}
void connect().catch(() => { stats.errors++; });
function Probe() {
  const chart = use_realtime_chart_data();
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const interval = setInterval(() => { const elapsed_ms = Math.round(performance.now() - started); setElapsed(elapsed_ms);
      void invoke('record_background_soak_summary', { summary: { elapsed_ms, ...stats, chart_live: chart.data_status === 'live' } });
    }, 5000);
    return () => clearInterval(interval);
  }, [chart.data_status]);
  return <main style={{ padding: 32, fontFamily: 'sans-serif' }}><h1>연결 유지 검증</h1><p>주문 기능 없음 · 실제 WebView와 공개 차트 수신</p>
    <p>경과 {Math.round(elapsed / 1000)}초 · 화면 이벤트 {stats.events}개 · 복구 {stats.recoveries}회</p>
    <p>차트: {chart.data_status} · 오류 {stats.errors}회</p><p>전체화면 가림·다른 데스크톱 전환 시험 시 이 창을 그대로 유지하세요.</p></main>;
}
createRoot(document.getElementById('root')!).render(<Probe />);
