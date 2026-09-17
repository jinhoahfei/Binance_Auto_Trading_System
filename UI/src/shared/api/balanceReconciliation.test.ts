import { describe, expect, it } from 'vitest';
import { is_balance_reconciliation, balance_reconciliation_text } from './balanceReconciliation';
import { earned_balance_fixture } from './balanceReconciliationFixture';

describe('ETH balance reconciliation', () => {
    it('preserves the reward precision and separates principal from rewards', () => {
        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(is_balance_reconciliation(earned_balance_fixture)).toBe(true);
        const text = balance_reconciliation_text(earned_balance_fixture);
        expect(text).toContain('잔여 원금 0.00009600');
        expect(text).toContain('Earn 보상 0.00000001');
        expect(text).toContain('거래소 현물 0.00009601');
        expect(text).toContain('일치 확인');
    });

    it.each([
        { earn_rewards_quantity: 0.00000001 }, { earn_rewards_quantity: 'NaN' },
        { earn_quantity: '-0.1' }, { status: 'verified', difference_quantity: '0.00000001' },
        { checked_at: '2026-09-16' }, { status: 'unavailable', reason_code: '<invalid>' },
        { retryable: true }, { extra: true },
    ])('rejects a malformed or contradictory receipt %j', (changes) => {
        expect(is_balance_reconciliation({ ...earned_balance_fixture, ...changes })).toBe(false);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    });

    it('shows a signed shortage and a stale result without calling it verified', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const receipt = { ...earned_balance_fixture, status: 'stale' as const,
            difference_quantity: '-0.00000001', reason_code: 'BALANCE_OBSERVATION_CHANGED', retryable: true };

        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(is_balance_reconciliation(receipt)).toBe(true);
        expect(balance_reconciliation_text(receipt)).toContain('재확인 필요');
        expect(balance_reconciliation_text(receipt)).toContain('-0.00000001 ETH');
    });
});
