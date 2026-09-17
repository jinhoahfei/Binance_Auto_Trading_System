import { afterEach, expect, it, vi } from 'vitest';
import { invoke } from '@tauri-apps/api/core';
import { observe_renderer_connection, renderer_instance_id, start_renderer_liveness } from './rendererLiveness';
vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }));
let stop: (() => void) | undefined;
afterEach(() => { stop?.(); stop = undefined; vi.restoreAllMocks(); vi.useRealTimers();
  Reflect.deleteProperty(window, '__TAURI_INTERNALS__'); });

it('동일 renderer ID로 5초 생존 신호와 100초 실행 지연을 기록한다', async () => {
  // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
  vi.useFakeTimers();
  Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: {} });
  vi.mocked(invoke).mockResolvedValue(undefined);
  const clock = vi.spyOn(performance, 'now').mockReturnValue(1000);
  observe_renderer_connection({ last_received_at_ms: 123, last_applied_at_ms: 120, last_sequence: 9 });
  stop = start_renderer_liveness();

  // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
  await vi.advanceTimersByTimeAsync(0);
  clock.mockReturnValue(101000);
  await vi.advanceTimersByTimeAsync(5000);

  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
  expect(invoke).toHaveBeenLastCalledWith('record_renderer_heartbeat', { record: expect.objectContaining({
    renderer_id: renderer_instance_id, sequence: 2, timer_lag_ms: 95000,
    last_received_at_ms: 123, last_applied_at_ms: 120, last_sequence: 9,
  }) });
});

it('native IPC가 응답하지 않아도 호출을 누적시키지 않는다', async () => {
  // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
  vi.useFakeTimers();
  Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: {} });
  vi.mocked(invoke).mockClear().mockReturnValue(new Promise(() => {}));
  stop = start_renderer_liveness();

  // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
  await vi.advanceTimersByTimeAsync(100_000);
  expect(invoke).toHaveBeenCalledTimes(1);  // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
});
