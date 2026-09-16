import { afterEach, expect, it, vi } from 'vitest';
import { invoke } from '@tauri-apps/api/core';
import { observe_renderer_connection, renderer_instance_id, start_renderer_liveness } from './rendererLiveness';
vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }));
let stop: (() => void) | undefined;
afterEach(() => { stop?.(); stop = undefined; vi.restoreAllMocks(); vi.useRealTimers();
  Reflect.deleteProperty(window, '__TAURI_INTERNALS__'); });

it('동일 renderer ID로 5초 생존 신호와 100초 실행 지연을 기록한다', async () => {
  vi.useFakeTimers();
  Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: {} });
  vi.mocked(invoke).mockResolvedValue(undefined);
  const clock = vi.spyOn(performance, 'now').mockReturnValue(1000);
  observe_renderer_connection({ last_received_at_ms: 123, last_applied_at_ms: 120, last_sequence: 9 });
  stop = start_renderer_liveness();
  await vi.advanceTimersByTimeAsync(0);
  clock.mockReturnValue(101000);
  await vi.advanceTimersByTimeAsync(5000);
  expect(invoke).toHaveBeenLastCalledWith('record_renderer_heartbeat', { record: expect.objectContaining({
    renderer_id: renderer_instance_id, sequence: 2, timer_lag_ms: 95000,
    last_received_at_ms: 123, last_applied_at_ms: 120, last_sequence: 9,
  }) });
});

it('native IPC가 응답하지 않아도 호출을 누적시키지 않는다', async () => {
  vi.useFakeTimers();
  Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: {} });
  vi.mocked(invoke).mockClear().mockReturnValue(new Promise(() => {}));
  stop = start_renderer_liveness();
  await vi.advanceTimersByTimeAsync(100_000);
  expect(invoke).toHaveBeenCalledTimes(1);
});
