import { act, renderHook } from '@testing-library/react';

import type { BackendBinanceConnectionStatus } from '../../shared/contracts';
import { use_binance_connection_status } from './useBinanceConnectionStatus';

const ONLINE_STATUS: BackendBinanceConnectionStatus = {
    api: 'online', market_stream: 'online', account_stream: 'online',
};

describe('Binance connection status query lifecycle', () => {
    afterEach(() => vi.useRealTimers());

    it('조회는 툴팁이 열린 동안만 갱신하고 실패한 상태는 확인 불가로 비운다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        vi.useFakeTimers();
        const load_status = vi.fn().mockResolvedValue(ONLINE_STATUS);
        const { result, unmount } = renderHook(() => use_binance_connection_status(load_status));
        expect(load_status).not.toHaveBeenCalled();  // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        await act(async () => result.current.set_is_open(true));

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(result.current.connection_details).toEqual(ONLINE_STATUS);

        load_status.mockRejectedValueOnce(new Error('unavailable'));

        // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
        await act(async () => vi.advanceTimersByTimeAsync(5_000));

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(result.current.connection_details).toBeNull();
        expect(result.current.has_error).toBe(true);

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        await act(async () => result.current.set_is_open(false));
        expect(load_status.mock.calls[0]?.[0].aborted).toBe(true);  // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.

        // 가짜 시간을 진행해 예약된 작업과 후속 상태 반영을 실행한다.
        await act(async () => vi.advanceTimersByTimeAsync(10_000));

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(load_status).toHaveBeenCalledTimes(2);
        unmount();
    });

    it('화면 통신이 끊기면 과거 정상 표시를 즉시 비우고 백엔드 확인 시각을 보존한다', async () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const load = vi.fn().mockResolvedValue({ ...ONLINE_STATUS, checked_at_ms: 123456 });
        const { result, rerender, unmount } = renderHook(({ online }) => use_binance_connection_status(load, true, online), { initialProps: { online: true } });

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        await act(async () => result.current.set_is_open(true));

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(result.current.checked_at_ms).toBe(123456);
        rerender({ online: false });
        expect(result.current.connection_details).toBeNull();
        expect(result.current.checked_at_ms).toBeNull();
        expect(load.mock.calls[0]?.[0].aborted).toBe(true);
        expect(load).toHaveBeenCalledTimes(1);
        unmount();
    });

    it('닫은 뒤 늦게 끝난 이전 요청은 다시 연 팝업의 상태를 덮어쓰지 않는다', async () => {
        /**
         * 함수 이름: resolve_stale()
         * 기능: 더 최신 결과와 경쟁시킬 오래된 조회 Promise를 뒤늦게 완료한다.
         * 인자: 없음
         * 반환값: 없음
         * 작성 날짜: 2026/09/17
         */
        let resolve_stale: (status: BackendBinanceConnectionStatus) => void = () => undefined;
        const pending_status = new Promise<BackendBinanceConnectionStatus>((resolve) => {
            resolve_stale = resolve;
        });
        const load_status = vi.fn()
            .mockReturnValueOnce(pending_status)
            .mockResolvedValueOnce({ ...ONLINE_STATUS, account_stream: 'offline' });
        const { result, unmount } = renderHook(() => use_binance_connection_status(load_status));

        // 입력을 전달하고 후속 이벤트 처리가 반영되도록 실행한다.
        await act(async () => result.current.set_is_open(true));
        await act(async () => result.current.set_is_open(false));
        await act(async () => result.current.set_is_open(true));
        await act(async () => resolve_stale(ONLINE_STATUS));

        // 외부 경계의 호출 여부·인자와 관찰한 결과를 검증한다.
        expect(result.current.connection_details?.account_stream).toBe('offline');
        unmount();
        expect(load_status.mock.calls[1]?.[0].aborted).toBe(true);
    });
});
