import type {
    BackendCsvExportReceipt,
    BackendCsvExportRequest,
    BackendCsvPeriod,
    BackendRegimeType,
    BackendTradingLogicCoverage,
    BackendTradingLogicStartGuard,
    BackendTradingLogicSupportStatus,
} from './backendContracts.generated';

/**
 * UI와 제어 actor 사이에서 사용하는 소수 문자열이다.
 */
export type DecimalString = string;

/**
 * UI가 선택하거나 표시할 수 있는 REGIME 유형이다.
 */
export type RegimeType = BackendRegimeType;

/**
 * TradingSTM coverage gate가 REGIME별로 공개하는 지원 상태이다.
 */
export type TradingLogicSupportStatus = BackendTradingLogicSupportStatus;

/**
 * TradingSTM 시작 가능 여부를 설명하는 backend 소유 guard 결과이다.
 */
export type TradingLogicStartGuard = BackendTradingLogicStartGuard;

/**
 * 한 REGIME의 TradingSTM coverage와 시작 guard를 함께 보존하는 UI 계약이다.
 */
export type TradingLogicCoverage = BackendTradingLogicCoverage;

/**
 * backend 미연결 UI와 fail-closed 초기 상태가 공유하는 Phase 6 coverage 기본값이다.
 */
export const DEFAULT_TRADING_LOGIC_COVERAGE = [
    {
        regime_type: 'type0',
        support_status: 'supported',
        start_guard: 'READY',
    },
    {
        regime_type: 'type1',
        support_status: 'unsupported',
        start_guard: 'UNSUPPORTED_TRADING_LOGIC',
    },
    {
        regime_type: 'type2',
        support_status: 'unsupported',
        start_guard: 'UNSUPPORTED_TRADING_LOGIC',
    },
    {
        regime_type: 'type3',
        support_status: 'unsupported',
        start_guard: 'UNSUPPORTED_TRADING_LOGIC',
    },
    {
        regime_type: 'type4',
        support_status: 'unsupported',
        start_guard: 'UNSUPPORTED_TRADING_LOGIC',
    },
] as const satisfies ReadonlyArray<TradingLogicCoverage>;

/**
 * 가격 차트가 지원하는 봉 주기이다.
 */
export type ChartInterval = '1m' | '30m' | '4h' | '1d';

/**
 * 거래 내역 조회에 사용할 기간 필터이다.
 */
export type HistoryPeriod = 'today' | 'last7days' | 'last30days' | 'all';

/**
 * CSV 내보내기에 사용할 기간 필터이다.
 */
export type CsvPeriod = BackendCsvPeriod;

/**
 * 거래 방향 또는 전체 방향을 나타낸다.
 */
export type TradeSideFilter = 'all' | 'buy' | 'sell';

/**
 * 실제 체결의 거래 방향이다.
 */
export type TradeSide = Exclude<TradeSideFilter, 'all'>;

/**
 * YYYY-MM-DD 형식으로 전달되는 로컬 날짜 문자열이다.
 */
export type LocalDateString = string;

/**
 * REGIME 판단 패널에 표시할 실시간 지표 값이다.
 */
export interface RegimeMetric {
    readonly id: 'emaSlope' | 'ema' | 'swingLow' | 'swingHigh';
    readonly label: string;
    readonly value: DecimalString;
    readonly tone: 'positive' | 'negative' | 'neutral';
}

/**
 * 화면에 표시할 정규화된 체결 내역이다.
 */
export interface TradeRecord {
    readonly id: string;
    readonly symbol?: string;
    readonly quote_asset?: string;
    readonly occurred_at: string;
    readonly side: TradeSide;
    readonly regime: RegimeType;
    readonly strategy: string;
    readonly price: DecimalString;
    readonly entry_price: DecimalString | null;
    readonly market_price_at_decision: DecimalString | null;
    readonly quantity: DecimalString;
    readonly total: DecimalString;
    readonly fee: DecimalString;
    readonly fee_amount?: DecimalString;
    readonly fee_asset?: string;
    readonly fee_note?: string;
    readonly profit_rate: DecimalString | null;
    readonly realized_pnl: DecimalString | null;
    readonly exit_reason: string | null;
}

/**
 * 거래 내역을 조회할 때 사용하는 필터 계약이다.
 */
export interface TradeHistoryQuery {
    readonly period: HistoryPeriod;
    readonly side: TradeSideFilter;
}

/**
 * D-12 범위를 유지해 거래 내역 상세 상단에 표시할 account-day/전체-history 요약이다.
 */
export interface TradeHistorySummary {
    readonly dailyReturn: {
        readonly value: string;
        readonly tone: 'positive' | 'negative' | 'neutral';
    };
    readonly sellPerformance: {
        readonly winRate: string;
        readonly completedCount: string;
        readonly averageRealizedReturn: string;
        readonly totalRealizedPnl: string;
        readonly tone: 'positive' | 'negative' | 'neutral';
    };
    readonly position: {
        readonly quantity: string;
    };
    readonly fees: {
        readonly amount: string;
        readonly totalExecutedAmount: string;
        readonly averageSlippage: string;
    };
}

/**
 * 하나의 상세 조회에서 필터된 거래 행과 filter-independent 요약을 함께 전달하는 UI 계약이다.
 */
export interface TradeHistoryDetails {
    readonly records: ReadonlyArray<TradeRecord>;
    readonly summary: TradeHistorySummary;
}

/**
 * 사용자가 차트에 저장한 단일 선의 직렬화 가능한 표현이다.
 * 시각과 가격이 정규 좌표이며 x_ratio는 이전 fixture와의 호환에만 사용한다.
 */
export interface ChartDrawing {
    readonly id: string;
    readonly points: ReadonlyArray<{
        readonly time: string;
        readonly price: DecimalString;
        readonly x_ratio?: number;
    }>;
}

/**
 * CSV 내보내기에 필요한 검증 완료 옵션이다.
 */
export type CsvExportOptions = Omit<BackendCsvExportRequest, 'schema_version'>;

/**
 * CSV 내보내기 완료 후 adapter가 반환하는 결과이다.
 */
export type CsvExportReceipt = BackendCsvExportReceipt;

/**
 * 화면에서 사용하는 공통 명령 실패 표현이다.
 */
export interface UiCommandFailure {
    readonly code: string;
    readonly message: string;
}
