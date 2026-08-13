export type HistoryPeriod = 'TODAY' | 'WEEKLY' | 'MONTHLY' | 'ALL';

export type TradeSideFilter = 'ALL' | 'BUY' | 'SELL';

export type TradeSide = Exclude<TradeSideFilter, 'ALL'>;

export type MetricTone = 'positive' | 'negative' | 'neutral';

export interface TradeHistorySummaryViewModel {
  dailyReturn: {
    value: string;
    tone: MetricTone;
  };
  sellPerformance: {
    winRate: string;
    completedCount: string;
    averageRealizedReturn: string;
    totalRealizedPnl: string;
    tone: MetricTone;
  };
  position: {
    quantity: string;
  };
  fees: {
    amount: string;
    totalExecutedAmount: string;
    averageSlippage: string;
  };
}

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
  previousBuyReturn: string;
  realizedPnl: string;
}

export interface EmptyTradeHistoryViewModel {
  title: string;
  description: string;
  suggestion: string;
  actionLabel: string;
}
