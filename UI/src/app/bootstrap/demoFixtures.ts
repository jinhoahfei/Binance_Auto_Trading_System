import { TRADE_HISTORY_ROWS_FIXTURE } from '../../features/trade-history';
import { DEFAULT_DASHBOARD_PROPS } from '../../routes/dashboard';
import type { RegimeMetric, TradeRecord } from '../../shared/contracts';

const REALTIME_METRIC_IDS: ReadonlyArray<RegimeMetric['id']> = [
    'emaSlope',
    'ema',
    'swingLow',
    'swingHigh',
];

/**
 * 함수 이름: normalize_decimal_text()
 * 기능: Figma 표시 fixture의 통화·단위 문자열에서 actor 계약용 소수 문자열을 추출한다.
 * 인자: display_value -> 통화 기호, 쉼표 또는 단위가 포함될 수 있는 화면 문자열
 * 반환값: 부호와 소수점을 보존한 숫자 문자열
 * 작성 날짜: 2026/08/12
 */
function normalize_decimal_text(display_value: string): string {
    const normalized_value = display_value.replaceAll(',', '').replace(/[^0-9.+-]/gu, '');

    return normalized_value.startsWith('+')
        ? normalized_value.slice(1)
        : normalized_value;
}

/**
 * 함수 이름: convert_history_time_to_iso()
 * 기능: 거래 내역 Figma fixture의 KST 표시 시각을 actor 계약의 ISO 시각으로 변환한다.
 * 인자: display_time -> YY/MM/DD - HH:mm:ss 형식의 KST 표시 문자열
 * 반환값: UTC ISO 시각 문자열
 * 작성 날짜: 2026/08/12
 */
function convert_history_time_to_iso(display_time: string): string {
    const [date_part = '26/06/22', time_part = '00:00:00'] = display_time.split(' - ');
    const [year = '26', month = '01', day = '01'] = date_part.split('/');

    return new Date(`20${year}-${month}-${day}T${time_part}+09:00`).toISOString();
}

/**
 * 함수 이름: create_demo_history_record()
 * 기능: 상세 거래 화면 fixture 한 행을 제어 actor가 보관할 정규화 TradeRecord로 변환한다.
 * 인자: row -> Figma 거래 내역 행 fixture
 * 반환값: 같은 id를 유지하는 actor용 거래 기록
 * 작성 날짜: 2026/08/12
 */
function create_demo_history_record(
    row: (typeof TRADE_HISTORY_ROWS_FIXTURE)[number],
): TradeRecord {
    return {
        id: row.id,
        occurred_at: convert_history_time_to_iso(row.time),
        side: row.side.toLowerCase() as TradeRecord['side'],
        regime: row.regime as TradeRecord['regime'],
        strategy: row.strategy,
        price: normalize_decimal_text(row.executionPrice),
        entry_price: normalize_decimal_text(row.entryPrice),
        market_price_at_decision: normalize_decimal_text(row.entryPrice),
        quantity: normalize_decimal_text(row.quantity),
        total: normalize_decimal_text(row.orderAmount),
        fee: normalize_decimal_text(row.fee),
        profit_rate: row.previousBuyReturn === '-'
            ? null
            : normalize_decimal_text(row.previousBuyReturn),
        realized_pnl: row.realizedPnl === '-'
            ? null
            : normalize_decimal_text(row.realizedPnl),
        exit_reason: null,
    };
}

/**
 * 함수 이름: create_demo_realtime_indicators()
 * 기능: 대시보드 Figma fixture의 첫 실시간 지표 그룹을 actor의 공통 지표 계약으로 변환한다.
 * 인자: 없음
 * 반환값: 초기 실시간 지표 목록
 * 작성 날짜: 2026/08/12
 */
function create_demo_realtime_indicators(): ReadonlyArray<RegimeMetric> {
    const primary_indicator_group = DEFAULT_DASHBOARD_PROPS.trader.indicatorGroups[0];

    return primary_indicator_group?.indicators.map((indicator, index) => ({
        id: REALTIME_METRIC_IDS[index] ?? 'emaSlope',
        label: indicator.label,
        value: indicator.value,
        tone: indicator.tone,
    })) ?? [];
}

/**
 * backend 미연결 데모에서 Figma의 최근 체결 목록을 재현하는 actor fixture이다.
 */
export const DEMO_RECENT_TRADE_RECORDS: ReadonlyArray<TradeRecord> = [
    {
        id: 'order-1',
        occurred_at: '2026-06-22T01:42:18.000Z',
        side: 'buy',
        regime: 'type0',
        strategy: 'Basic Iterative',
        price: '5218000',
        entry_price: '5218000',
        market_price_at_decision: '5218000',
        quantity: '0.0958',
        total: '499984',
        fee: '249',
        profit_rate: null,
        realized_pnl: null,
        exit_reason: null,
    },
    {
        id: 'order-2',
        occurred_at: '2026-06-22T00:54:06.000Z',
        side: 'sell',
        regime: 'type0',
        strategy: 'First Buy',
        price: '5186000',
        entry_price: '5142000',
        market_price_at_decision: '5186000',
        quantity: '0.0964',
        total: '499930',
        fee: '250',
        profit_rate: '0.86',
        realized_pnl: '4240',
        exit_reason: 'strategy_exit',
    },
    {
        id: 'order-3',
        occurred_at: '2026-06-21T23:31:40.000Z',
        side: 'buy',
        regime: 'type0',
        strategy: 'First Buy',
        price: '5142000',
        entry_price: '5142000',
        market_price_at_decision: '5142000',
        quantity: '0.0964',
        total: '495689',
        fee: '248',
        profit_rate: null,
        realized_pnl: null,
        exit_reason: null,
    },
];

/**
 * 상세 거래 화면의 Figma 행과 id로 다시 결합할 수 있는 actor fixture이다.
 */
export const DEMO_HISTORY_TRADE_RECORDS: ReadonlyArray<TradeRecord> =
    TRADE_HISTORY_ROWS_FIXTURE.map(create_demo_history_record);

/**
 * 실시간 지표 탭의 Figma 기본 상태를 생성하는 actor fixture이다.
 */
export const DEMO_REALTIME_INDICATORS = create_demo_realtime_indicators();
