import type { BackendBalanceReconciliation } from '../contracts';

export const earned_balance_fixture: BackendBalanceReconciliation = {
    asset: 'ETH', status: 'verified', position_quantity: '0', residual_principal_quantity: '0.00009600',
    earn_rewards_quantity: '0.00000001', earn_quantity: '0', expected_spot_quantity: '0.00009601',
    exchange_spot_quantity: '0.00009601', difference_quantity: '0.00000000',
    checked_at: '2026-09-16T06:04:03Z', reason_code: null, retryable: false,
};
