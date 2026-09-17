export interface ChartErrorDiagnostic {
    readonly error_kind?: 'timeout' | 'http_error' | 'invalid_json' | 'invalid_payload' | 'type_error' | 'range_error' | 'unknown';
    readonly http_status?: number;
}


/**
 * 클래스 이름: ChartHttpError
 * 기능: HTTP 상태 코드를 원본 URL·응답 body 없이 진단에 전달한다.
 * 작성 날짜: 2026/09/11
 */
export class ChartHttpError extends Error {
    /**
     * 함수 이름: ChartHttpError.constructor()
     * 기능: 캔들 조회 HTTP 상태와 주기를 오류 객체에 보관한다.
     * 인자: http_status -> 응답 상태, interval -> 조회 주기
     * 반환값: 생성된 ChartHttpError
     * 작성 날짜: 2026/09/17
     */
    constructor(readonly http_status: number, interval: string) {
        // function Object() { [native code] }
        super(`Binance ${interval} kline request failed with HTTP ${http_status}.`);
    }
}


/**
 * 함수 이름: describe_chart_data_error()
 * 기능: 임의 오류 원문을 고정 분류와 HTTP 상태로 변환한다.
 * 인자: error -> 요청·파싱·화면 반영 오류
 * 반환값: 공개 가능한 원인 분류
 * 작성 날짜: 2026/09/11
 */
export function describe_chart_data_error(error: unknown): ChartErrorDiagnostic {
    if (error instanceof ChartHttpError) return { error_kind: 'http_error', http_status: error.http_status };
    if ((error instanceof Error || error instanceof DOMException) && error.name === 'TimeoutError') return { error_kind: 'timeout' };
    if (error instanceof TypeError) return { error_kind: 'type_error' };
    if (error instanceof RangeError) return { error_kind: 'range_error' };
    if (error instanceof Error && error.message.startsWith('Binance ')) {
        return { error_kind: error.message.includes('not valid JSON') ? 'invalid_json' : 'invalid_payload' };
    }

    return { error_kind: 'unknown' };
}
