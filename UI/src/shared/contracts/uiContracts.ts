/**
 * UI와 제어 actor 사이에서 사용하는 소수 문자열이다.
 */
export type DecimalString = string;

/**
 * UI가 선택하거나 표시할 수 있는 REGIME 유형이다.
 */
export type RegimeType = 'type0' | 'type1' | 'type2' | 'type3' | 'type4';

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
export type CsvPeriod = Exclude<HistoryPeriod, 'all'> | 'custom';

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
    readonly occurred_at: string;
    readonly side: TradeSide;
    readonly regime: RegimeType;
    readonly strategy: string;
    readonly price: DecimalString;
    readonly entry_price: DecimalString;
    readonly market_price_at_decision: DecimalString;
    readonly quantity: DecimalString;
    readonly total: DecimalString;
    readonly fee: DecimalString;
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
 * 사용자가 차트에 저장한 단일 선의 직렬화 가능한 표현이다.
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
export interface CsvExportOptions {
    readonly directory: string;
    readonly file_name: string;
    readonly period: CsvPeriod;
    readonly start_date: LocalDateString;
    readonly end_date: LocalDateString;
    readonly timezone: 'Asia/Seoul';
}

/**
 * CSV 내보내기 완료 후 adapter가 반환하는 결과이다.
 */
export interface CsvExportReceipt {
    readonly file_path: string;
    readonly exported_row_count: number;
}

/**
 * 화면에서 사용하는 공통 명령 실패 표현이다.
 */
export interface UiCommandFailure {
    readonly code: string;
    readonly message: string;
}
