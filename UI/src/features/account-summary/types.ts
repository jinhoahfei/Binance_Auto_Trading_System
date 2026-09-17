// 계좌 요약에서 사용하는 데이터 구조와 입력·표시 타입을 정의한다.

import type { BackendBalanceReconciliation } from '../../shared/contracts';
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
    readonly balanceReconciliation?: BackendBalanceReconciliation | null;
    readonly ethAmount: string;
    readonly ethValue: string;
    readonly krwValue: string;
    readonly quoteAsset?: string;
    readonly quoteValue?: string;
    readonly profitLoss: string;
    readonly totalValue: string;
}

export interface AccountSectionProps {
    readonly asset: AssetSummaryViewModel;
    readonly children?: ReactNode | undefined;
    readonly strategy: StrategySummaryViewModel;
}
