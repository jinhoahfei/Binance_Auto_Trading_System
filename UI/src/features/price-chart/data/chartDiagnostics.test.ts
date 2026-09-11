import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { invoke } from '@tauri-apps/api/core';

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }));

beforeEach(() => {
    vi.resetModules();
    vi.mocked(invoke).mockReset().mockResolvedValue(undefined);
    vi.useFakeTimers();
});
afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.useRealTimers();
});

describe('chart diagnostic persistence', () => {
    it('브라우저 preview에서는 native 저장을 호출하지 않는다', async () => {
        const { record_chart_diagnostic } = await import('./chartDiagnostics');
        const { describe_chart_data_error } = await import('./chartDataError');
        record_chart_diagnostic({ event: 'heartbeat' });
        expect(invoke).not.toHaveBeenCalled();
        expect(describe_chart_data_error(new Error('secret raw error'))).toEqual({ error_kind: 'unknown' });
        expect(describe_chart_data_error(new Error('Binance combined WebSocket payload is not valid JSON.')))
            .toEqual({ error_kind: 'invalid_json' });
    });

    it('실행 식별자와 수신 시각을 포함한 고정 schema를 native에 저장한다', async () => {
        vi.stubGlobal('__TAURI_INTERNALS__', {});
        const { record_chart_diagnostic } = await import('./chartDiagnostics');
        record_chart_diagnostic({ event: 'stream_stale', connection_id: 3, interval: '30m', elapsed_ms: 15_000 });
        await vi.advanceTimersByTimeAsync(0);
        expect(invoke).toHaveBeenCalledWith('record_chart_diagnostics', { records: [expect.objectContaining({
            event: 'stream_stale', connection_id: 3, interval: '30m', elapsed_ms: 15_000,
            renderer_id: expect.any(String), at_ms: expect.any(Number), sequence: 1, dropped_before: 0,
        })] });
    });

    it('저장 실패 동안 queue를 제한하고 복구 시 누락 수와 함께 재전송한다', async () => {
        vi.stubGlobal('__TAURI_INTERNALS__', {});
        vi.spyOn(console, 'error').mockImplementation(() => undefined);
        vi.mocked(invoke).mockRejectedValueOnce(new Error('private native detail'));
        const { record_chart_diagnostic } = await import('./chartDiagnostics');
        for (let index = 0; index < 300; index += 1) record_chart_diagnostic({ event: 'heartbeat' });
        await vi.advanceTimersByTimeAsync(0);
        expect(console.error).toHaveBeenCalledWith('CHART_DIAGNOSTIC_WRITE_FAILED');
        expect(invoke).toHaveBeenCalledTimes(1);
        await vi.advanceTimersByTimeAsync(5_000);
        const accepted = vi.mocked(invoke).mock.calls.slice(1).flatMap((call) => {
            return (call[1] as { records: Array<{ sequence: number; dropped_before: number }> }).records;
        });
        expect(accepted).toHaveLength(256);
        expect(accepted.at(-1)?.sequence).toBe(300);
        expect(accepted.reduce((total, record) => total + record.dropped_before, 0)).toBe(44);
        expect(vi.getTimerCount()).toBe(0);
        expect(JSON.stringify(vi.mocked(invoke).mock.calls)).not.toContain('private native detail');
    });
});
