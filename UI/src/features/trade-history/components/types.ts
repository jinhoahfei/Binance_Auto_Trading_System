// 거래 내역에서 사용하는 데이터 구조와 입력·표시 타입을 정의한다.

import type { TradeHistorySummary } from '../../../shared/contracts';

export type HistoryPeriod = 'TODAY' | 'WEEKLY' | 'MONTHLY' | 'ALL';

export type TradeSideFilter = 'ALL' | 'BUY' | 'SELL';

export type TradeSide = Exclude<TradeSideFilter, 'ALL'>;

export type MetricTone = TradeHistorySummary['dailyReturn']['tone'];

export type TradeHistorySummaryViewModel = TradeHistorySummary;

export interface TradeRowViewModel {
  id: string;
  time: string;
  side: TradeSide;
  regime: string;
  strategy: string;
  entryPrice: string;
  executionPrice: string;
  quantity: string;
  orderAmount: string;
  fee: string;
  feeNote?: string;
  previousBuyReturn: string;
  realizedPnl: string;
}

export interface EmptyTradeHistoryViewModel {
  title: string;
  description: string;
  suggestion: string;
  actionLabel: string;
}
