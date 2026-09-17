import { useEffect, useState } from 'react';

import type { BackendBinanceConnectionStatus } from '../../shared/contracts';


/**
 * 함수 이름: use_binance_connection_status()
 * 기능: 연결 툴팁이 열린 동안에만 백엔드의 Binance 진단을 조회하고 주기적으로 갱신한다.
 * 인자: load_status -> runtime이 제공하는 취소 가능한 연결 상태 조회
 *      enabled -> 앱 종료 전 조회 허용 여부
 *      transport_online -> renderer와 backend 사이의 현재 연결 여부
 * 반환값: 최신 연결 상태, 오류 여부와 툴팁 열림 상태 처리 함수
 * 작성 날짜: 2026/09/05
 */
export function use_binance_connection_status(
    load_status: (signal?: AbortSignal) => Promise<BackendBinanceConnectionStatus>,
    enabled = true,
    transport_online = true,
) {
    // 툴팁의 열림 상태와 마지막 진단 결과를 화면 수명 안에 보관한다.
    const [is_open, set_is_open] = useState(false);
    const [connection_details, set_connection_details] = useState<BackendBinanceConnectionStatus | null>(null);
    const [has_error, set_has_error] = useState(false);
    const [checked_at_ms, set_checked_at_ms] = useState<number | null>(null);

    // 툴팁이 열려 있고 연결이 유효한 동안만 진단 조회를 유지한다.
    useEffect(() => {
        if (!is_open || !enabled || !transport_online) {
            set_connection_details(null);
            set_checked_at_ms(null);

            return;
        }

        // 열린 툴팁마다 조회 취소와 갱신 타이머의 수명을 새로 만든다.
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
                // 현재 조회가 살아 있을 때만 진단 결과와 확인 시각을 반영한다.
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

            // 한 요청이 끝난 뒤 다음 조회를 예약해 중복 요청을 피한다.
            refresh_timer = setTimeout(() => void refresh_connection_status(), 5_000);
        }

        void refresh_connection_status();

        return () => {
            // 툴팁을 닫거나 연결이 바뀌면 진행 조회와 다음 갱신을 함께 정리한다.
            abort_controller.abort();
            clearTimeout(refresh_timer);
        };
    }, [enabled, is_open, load_status, transport_online]);

    // 전송 연결이 끊긴 경우 이전 진단값을 현재 상태처럼 노출하지 않는다.
    return { connection_details: transport_online ? connection_details : null,
        checked_at_ms: transport_online ? checked_at_ms : null, has_error, set_is_open };
}
