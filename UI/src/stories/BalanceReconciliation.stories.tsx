// BalanceReconciliation의 표시 상태를 Storybook 시나리오로 제공한다.

import type { Meta, StoryObj } from '@storybook/react-vite';
import { DashboardPage } from '../routes/dashboard/DashboardPage';
import { DEFAULT_DASHBOARD_PROPS } from '../routes/dashboard/dashboardFixture';
import { ExitConfirmationDialog } from '../app/components/modals/ExitConfirmationDialog';
import { OperationStatusDialog } from '../app/components/modals/OperationStatusDialog';
import { earned_balance_fixture } from '../shared/api/balanceReconciliationFixture';
import { balance_reconciliation_text } from '../shared/api/balanceReconciliation';
import { shutdown_failure_message } from '../shared/api/shutdownMessages';

const meta = {
    title: 'Account/Balance Reconciliation',
    parameters: { layout: 'fullscreen' },
    render: () => <DashboardPage {...DEFAULT_DASHBOARD_PROPS} account={{ ...DEFAULT_DASHBOARD_PROPS.account,
        asset: { ...DEFAULT_DASHBOARD_PROPS.account.asset, quoteAsset: 'USDT', ethAmount: '0.00009601',
            balanceReconciliation: earned_balance_fixture } }} />,
} satisfies Meta;
export default meta;
type Story = StoryObj<typeof meta>;

export const Verified: Story = {};
export const ShutdownChecking: Story = {
    render: () => <OperationStatusDialog open title="프로그램 종료 준비" status="progress"
        description="거래 기록을 저장하고 있습니다." detail={balance_reconciliation_text(earned_balance_fixture)} />,
};
export const UnexplainedDifference: Story = {
    render: () => <ExitConfirmationDialog open forceSell={false} onCancel={() => {}} onConfirm={() => {}}
        error={`${shutdown_failure_message('SHUTDOWN_BALANCE_MISMATCH')}\n\n${balance_reconciliation_text({
            ...earned_balance_fixture, status: 'mismatch', reason_code: 'BALANCE_UNEXPLAINED',
            exchange_spot_quantity: '0.00009602', difference_quantity: '0.00000001',
        })}`} />,
};
