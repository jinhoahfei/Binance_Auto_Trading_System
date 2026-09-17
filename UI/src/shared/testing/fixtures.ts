// 상태 전이와 화면 테스트에서 재사용할 날짜·지표·거래·드로잉 fixture를 제공한다.

import type {
    ChartDrawing,
    RegimeMetric,
    TradeRecord,
} from '../contracts';

/**
 * 테스트와 backend 미연결 UI에서 공통으로 사용하는 기준 날짜이다.
 */
export const FIXTURE_TODAY = '2026-08-12';

/**
 * REGIME 패널의 결정적 초기 지표 fixture이다.
 */
export const REGIME_METRIC_FIXTURES: ReadonlyArray<RegimeMetric> = [
    {
        id: 'emaSlope',
        label: 'EMA9 Slope',
        value: '0.42',
        tone: 'positive',
    },
    {
        id: 'ema',
        label: 'Live EMA9',
        value: '3,421.18',
        tone: 'neutral',
    },
    // 스윙 fixture도 가격 대신 구조 판정을 사용해 실제 REGIME 패널의 표시 계약을 따른다.
    {
        id: 'swingLow',
        label: 'Swing Low',
        value: 'LL',  // 낮아진 저점 판정은 기존 negative tone과 일치시킨다.
        tone: 'negative',
    },
    {
        id: 'swingHigh',
        label: 'Swing High',
        value: 'HH',  // 높아진 고점 판정은 기존 positive tone과 일치시킨다.
        tone: 'positive',
    },
];

/**
 * 최근 체결과 거래 내역 화면에서 공통으로 사용하는 결정적 fixture이다.
 */
export const TRADE_RECORD_FIXTURES: ReadonlyArray<TradeRecord> = [
    {
        id: 'trade-20260812-003',
        occurred_at: '2026-08-12T05:24:00.000Z',
        side: 'sell',
        regime: 'type0',
        strategy: 'Basic Iterative',
        price: '3421.18',
        entry_price: '3341.65',
        market_price_at_decision: '3420.80',
        quantity: '0.18000000',
        total: '615.8124',
        fee: '0.6158124',
        profit_rate: '2.38',
        realized_pnl: '14.3154',
        exit_reason: 'strategy_exit',
    },
    {
        id: 'trade-20260812-002',
        occurred_at: '2026-08-12T03:10:00.000Z',
        side: 'buy',
        regime: 'type0',
        strategy: 'Basic Iterative',
        price: '3341.65',
        entry_price: '3341.65',
        market_price_at_decision: '3340.90',
        quantity: '0.18000000',
        total: '601.4970',
        fee: '0.6014970',
        profit_rate: null,
        realized_pnl: null,
        exit_reason: null,
    },
    {
        id: 'trade-20260811-001',
        occurred_at: '2026-08-11T14:42:00.000Z',
        side: 'sell',
        regime: 'type1',
        strategy: 'Risk Exit',
        price: '3310.44',
        entry_price: '3335.12',
        market_price_at_decision: '3311.02',
        quantity: '0.12000000',
        total: '397.2528',
        fee: '0.3972528',
        profit_rate: '-0.74',
        realized_pnl: '-2.9616',
        exit_reason: 'risk_exit',
    },
];

/**
 * 차트 interval 보존 동작을 검증하는 결정적 drawing fixture이다.
 */
export const CHART_DRAWING_FIXTURE: ChartDrawing = {
    id: 'drawing-001',
    points: [
        { time: '2026-08-12T04:00:00.000Z', price: '3375.00' },
        { time: '2026-08-12T05:00:00.000Z', price: '3421.18' },
    ],
};
