import type { BackendBinanceConnectionStatus } from '../contracts';
import { BackendContractError } from './backendEventMapper';


/**
 * 함수 이름: validate_binance_connection_status()
 * 기능: 백엔드가 관측한 Binance 연결 상태 세 값을 엄격하게 검증한다.
 * 인자: value -> HTTP envelope의 연결 진단 payload
 * 반환값: 검증된 API·시세·계좌 연결 상태
 * 작성 날짜: 2026/09/05
 */
export function validate_binance_connection_status(value: unknown): BackendBinanceConnectionStatus {
    // 진단 응답의 객체·허용 필드·상태 값을 검증한다.
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
        throw new BackendContractError('MALFORMED_BACKEND_PAYLOAD', 'Invalid Binance connection status');
    }

    const record = value as Record<string, unknown>;
    const statuses = [record.api, record.market_stream, record.account_stream];
    if (Object.keys(record).some((key) => !['api', 'market_stream', 'account_stream', 'checked_at_ms'].includes(key))
        || (record.checked_at_ms !== undefined && (!Number.isSafeInteger(record.checked_at_ms) || (record.checked_at_ms as number) < 0))
        || statuses.some((status) => status !== 'online' && status !== 'offline')) {
        throw new BackendContractError('MALFORMED_BACKEND_PAYLOAD', 'Invalid Binance connection status');
    }

    // 검증을 마친 세 연결 상태와 조회 시각만 반환한다.
    return {
        api: record.api as BackendBinanceConnectionStatus['api'],
        market_stream: record.market_stream as BackendBinanceConnectionStatus['market_stream'],
        account_stream: record.account_stream as BackendBinanceConnectionStatus['account_stream'],
        ...(record.checked_at_ms === undefined ? {} : { checked_at_ms: record.checked_at_ms as number }),
    };
}
