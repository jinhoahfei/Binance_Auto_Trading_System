import type { ReactNode } from 'react';

export type AccountStatusTone = 'positive' | 'negative' | 'neutral';

export interface StrategySummaryViewModel {
    readonly appliedState: string;
    readonly profitAmount: string;
    readonly profitRate: string;
    readonly status: string;
    readonly statusTone: AccountStatusTone;
}

export interface AssetSummaryViewModel {
    readonly ethAmount: string;
    readonly ethValue: string;
    readonly krwValue: string;
    readonly profitLoss: string;
    readonly totalValue: string;
}

export interface AccountSectionProps {
    readonly asset: AssetSummaryViewModel;
    readonly children?: ReactNode | undefined;
    readonly strategy: StrategySummaryViewModel;
}
