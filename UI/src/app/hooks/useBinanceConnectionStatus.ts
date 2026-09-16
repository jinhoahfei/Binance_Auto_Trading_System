import { useEffect, useState } from 'react';

import type { BackendBinanceConnectionStatus } from '../../shared/contracts';

/**
 * 함수 이름: use_binance_connection_status()
 * 기능: 연결 툴팁이 열린 동안에만 백엔드의 Binance 진단을 조회하고 주기적으로 갱신한다.
 * 인자: load_status -> runtime이 제공하는 취소 가능한 연결 상태 조회
 *      enabled -> 앱 종료 전 조회 허용 여부
 * 반환값: 최신 연결 상태, 오류 여부와 툴팁 열림 상태 처리 함수
 * 작성 날짜: 2026/09/05
 */
export function use_binance_connection_status(
    load_status: (signal?: AbortSignal) => Promise<BackendBinanceConnectionStatus>,
    enabled = true,
    transport_online = true,
) {
    const [is_open, set_is_open] = useState(false);
    const [connection_details, set_connection_details] = useState<BackendBinanceConnectionStatus | null>(null);
    const [has_error, set_has_error] = useState(false);
    const [checked_at_ms, set_checked_at_ms] = useState<number | null>(null);

    useEffect(() => {
        if (!is_open || !enabled || !transport_online) {
            set_connection_details(null);
            set_checked_at_ms(null);
            return;
        }

        const abort_controller = new AbortController();
        let refresh_timer: ReturnType<typeof setTimeout> | undefined;
        set_connection_details(null);
        set_has_error(false);

        /**
         * 함수 이름: refresh_connection_status()
         * 기능: 요청을 겹치지 않게 실행하고 닫힌 툴팁의 늦은 응답을 폐기한다.
         * 인자: 없음
         * 반환값: 단일 갱신 완료 Promise
         * 작성 날짜: 2026/09/05
         */
        async function refresh_connection_status(): Promise<void> {
            try {
                const next_details = await load_status(abort_controller.signal);
                if (abort_controller.signal.aborted) {
                    return;
                }
                set_connection_details(next_details);
                set_checked_at_ms(next_details.checked_at_ms ?? null);
                set_has_error(false);
            } catch {
                if (abort_controller.signal.aborted) {
                    return;
                }
                set_connection_details(null);
                set_checked_at_ms(null);
                set_has_error(true);
            }

            refresh_timer = setTimeout(() => void refresh_connection_status(), 5_000);
        }

        void refresh_connection_status();
        return () => {
            abort_controller.abort();
            clearTimeout(refresh_timer);
        };
    }, [enabled, is_open, load_status, transport_online]);

    return { connection_details: transport_online ? connection_details : null,
        checked_at_ms: transport_online ? checked_at_ms : null, has_error, set_is_open };
}
