import { act, renderHook } from '@testing-library/react';

import type { BackendBinanceConnectionStatus } from '../../shared/contracts';
import { use_binance_connection_status } from './useBinanceConnectionStatus';

const ONLINE_STATUS: BackendBinanceConnectionStatus = {
    api: 'online', market_stream: 'online', account_stream: 'online',
};

describe('Binance connection status query lifecycle', () => {
    afterEach(() => vi.useRealTimers());

    it('조회는 툴팁이 열린 동안만 갱신하고 실패한 상태는 확인 불가로 비운다', async () => {
        vi.useFakeTimers();
        const load_status = vi.fn().mockResolvedValue(ONLINE_STATUS);
        const { result, unmount } = renderHook(() => use_binance_connection_status(load_status));
        expect(load_status).not.toHaveBeenCalled();

        await act(async () => result.current.set_is_open(true));
        expect(result.current.connection_details).toEqual(ONLINE_STATUS);

        load_status.mockRejectedValueOnce(new Error('unavailable'));
        await act(async () => vi.advanceTimersByTimeAsync(5_000));
        expect(result.current.connection_details).toBeNull();
        expect(result.current.has_error).toBe(true);

        await act(async () => result.current.set_is_open(false));
        expect(load_status.mock.calls[0]?.[0].aborted).toBe(true);
        await act(async () => vi.advanceTimersByTimeAsync(10_000));
        expect(load_status).toHaveBeenCalledTimes(2);
        unmount();
    });

    it('닫은 뒤 늦게 끝난 이전 요청은 다시 연 팝업의 상태를 덮어쓰지 않는다', async () => {
        let resolve_stale: (status: BackendBinanceConnectionStatus) => void = () => undefined;
        const pending_status = new Promise<BackendBinanceConnectionStatus>((resolve) => {
            resolve_stale = resolve;
        });
        const load_status = vi.fn()
            .mockReturnValueOnce(pending_status)
            .mockResolvedValueOnce({ ...ONLINE_STATUS, account_stream: 'offline' });
        const { result, unmount } = renderHook(() => use_binance_connection_status(load_status));

        await act(async () => result.current.set_is_open(true));
        await act(async () => result.current.set_is_open(false));
        await act(async () => result.current.set_is_open(true));
        await act(async () => resolve_stale(ONLINE_STATUS));

        expect(result.current.connection_details?.account_stream).toBe('offline');
        unmount();
        expect(load_status.mock.calls[1]?.[0].aborted).toBe(true);
    });
});
