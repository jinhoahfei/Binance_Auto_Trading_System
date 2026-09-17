// 수량 문자열의 정밀도를 보존하면서 잔고 대조 payload의 필드·상태 일관성을 검사한다.

import type { BackendBalanceReconciliation } from '../contracts';

const QUANTITIES = ['position_quantity', 'residual_principal_quantity', 'earn_rewards_quantity',
    'earn_quantity', 'expected_spot_quantity', 'exchange_spot_quantity', 'difference_quantity'] as const;
const REASONS: Readonly<Record<string, string>> = {
    EARN_EVIDENCE_MISSING: '차액을 설명할 Earn 기록을 확인하지 못했습니다.',
    EARN_EVIDENCE_INVALID: 'Earn 조회 기록을 검증하지 못했습니다.',
    BALANCE_UNEXPLAINED: '보상과 보관 내역을 반영한 뒤에도 차이가 남아 있습니다.',
    POSITION_EXCEEDS_BALANCE: '기록된 포지션보다 실제 잔고가 부족합니다.',
    BALANCE_OBSERVATION_CHANGED: '잔고 또는 기록이 변경되어 다시 확인해야 합니다.',
    EARN_ACCOUNT_UNREACHABLE: '거래소 조회를 완료하지 못했습니다.',
};


/**
 * 함수 이름: is_balance_reconciliation()
 * 기능: 수량 문자열의 정밀도를 보존하면서 잔고 대조 payload의 필드·상태 일관성을 검사한다.
 * 인자: value -> 검증할 payload
 * 반환값: BackendBalanceReconciliation 계약을 만족하면 true
 * 작성 날짜: 2026/09/17
 */
export function is_balance_reconciliation(value: unknown): value is BackendBalanceReconciliation {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;

    const record = value as Record<string, unknown>;
    const keys = ['asset', 'status', ...QUANTITIES, 'checked_at', 'reason_code', 'retryable'];

    return Object.keys(record).length === keys.length && keys.every((key) => key in record)
        && record.asset === 'ETH' && ['verified', 'mismatch', 'unavailable', 'stale'].includes(String(record.status))
        && QUANTITIES.every((key) => typeof record[key] === 'string'
            && (key === 'difference_quantity' ? /^-?(0|[1-9]\d*)(\.\d+)?$/u : /^(0|[1-9]\d*)(\.\d+)?$/u).test(record[key] as string))
        && typeof record.checked_at === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/u.test(record.checked_at)
        && Number.isFinite(Date.parse(record.checked_at)) && typeof record.retryable === 'boolean'
        && (record.status === 'verified' ? record.reason_code === null && /^0(?:\.0+)?$/u.test(String(record.difference_quantity)) && record.retryable === false
            : typeof record.reason_code === 'string' && record.reason_code in REASONS);
}


/**
 * 함수 이름: balance_reconciliation_text()
 * 기능: 잔고 대조 상태·수량·KST 확인 시각과 사유를 화면 안내로 변환한다.
 * 인자: value -> 검증된 잔고 대조 결과
 * 반환값: 줄바꿈으로 구분한 안내 문자열
 * 작성 날짜: 2026/09/17
 */
export function balance_reconciliation_text(value: BackendBalanceReconciliation): string {
    const status = { verified: '일치 확인', mismatch: '차이 확인', unavailable: '조회 미완료', stale: '재확인 필요' }[value.status];

    return [
        `ETH 잔고 대조: ${status}`,
        `포지션 ${value.position_quantity} · 잔여 원금 ${value.residual_principal_quantity}`,
        `Earn 보상 ${value.earn_rewards_quantity} · Earn 보관 ${value.earn_quantity}`,
        `예상 현물 ${value.expected_spot_quantity} · 거래소 현물 ${value.exchange_spot_quantity}`,
        `차이 ${value.difference_quantity} ETH`,
        `확인 시각 ${new Date(value.checked_at).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul' })} (KST)`,
        ...(value.reason_code === null ? [] : [REASONS[value.reason_code] ?? '대조 내역을 다시 확인해 주세요.']),
    ].join('\n');
}
