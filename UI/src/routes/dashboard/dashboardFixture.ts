import type { DashboardPageProps } from './DashboardPage';

export const DEFAULT_DASHBOARD_PROPS: DashboardPageProps = {
    regime: {
        recommended: 'type0',
        applied: 'type0',
        candidate: null,
        metrics: [
            { id: 'emaSlope', label: 'EMA 기울기', value: '+0.23', tone: 'positive' },
            { id: 'ema', label: 'EMA', value: '위 +0.84%', tone: 'positive' },
            { id: 'swingLow', label: '스윙 저점', value: 'HL', tone: 'negative' },
            { id: 'swingHigh', label: '스윙 고점', value: 'HH', tone: 'neutral' },
        ],
    },
    chart: {
        activeState: 'Case_B',  // 정적 화면도 계좌 카드와 같은 실행 Case 예시를 사용한다.
        timestampLabel: '2026.06.22 · 10:59 KST',
        interval: '30m',
        indicatorSettingsOpen: false,
        indicatorSettings: {
            ema9: true,
            bollingerBand: true,
            volume: false,
        },
        candles: [
            { open: 5_152_000, close: 5_164_000, high: 5_176_000, low: 5_143_000 },
            { open: 5_169_000, close: 5_158_000, high: 5_181_000, low: 5_149_000 },
            { open: 5_160_000, close: 5_181_000, high: 5_188_000, low: 5_151_000 },
            { open: 5_178_000, close: 5_192_000, high: 5_201_000, low: 5_168_000 },
            { open: 5_195_000, close: 5_187_000, high: 5_205_000, low: 5_179_000 },
            { open: 5_189_000, close: 5_210_000, high: 5_217_000, low: 5_182_000 },
            { open: 5_205_000, close: 5_194_000, high: 5_215_000, low: 5_185_000 },
            { open: 5_198_000, close: 5_217_000, high: 5_226_000, low: 5_191_000 },
            { open: 5_214_000, close: 5_237_000, high: 5_246_000, low: 5_207_000 },
            { open: 5_234_000, close: 5_224_000, high: 5_245_000, low: 5_215_000 },
            { open: 5_226_000, close: 5_251_000, high: 5_261_000, low: 5_217_000 },
            { open: 5_249_000, close: 5_268_000, high: 5_279_000, low: 5_241_000 },
            { open: 5_263_000, close: 5_250_000, high: 5_272_000, low: 5_235_000 },
        ],
        ema: [
            { value: 5_148_000 }, { value: 5_154_000 }, { value: 5_165_000 },
            { value: 5_179_000 }, { value: 5_185_000 }, { value: 5_199_000 },
            { value: 5_203_000 }, { value: 5_209_000 }, { value: 5_222_000 },
            { value: 5_225_000 }, { value: 5_235_000 }, { value: 5_249_000 },
            { value: 5_247_000 },
        ],
        bollingerUpper: [
            { value: 5_169_000 }, { value: 5_173_000 }, { value: 5_186_000 },
            { value: 5_203_000 }, { value: 5_211_000 }, { value: 5_229_000 },
            { value: 5_231_000 }, { value: 5_230_000 }, { value: 5_242_000 },
            { value: 5_254_000 }, { value: 5_267_000 }, { value: 5_283_000 },
            { value: 5_278_000 },
        ],
        bollingerLower: [
            { value: 5_128_000 }, { value: 5_135_000 }, { value: 5_143_000 },
            { value: 5_156_000 }, { value: 5_160_000 }, { value: 5_169_000 },
            { value: 5_175_000 }, { value: 5_181_000 }, { value: 5_188_000 },
            { value: 5_195_000 }, { value: 5_201_000 }, { value: 5_215_000 },
            { value: 5_211_000 },
        ],
    },
    trader: {
        activeTab: 'recent',
        orders: [
            {
                id: 'order-1',
                side: 'buy',
                strategy: 'Basic Iterative',
                time: '10:42:18',
                price: '₩ 5,218,000',
                secondaryValue: '0.0958 ETH',
            },
            {
                id: 'order-2',
                side: 'sell',
                strategy: 'First Buy',
                time: '09:54:06',
                price: '₩ 5,186,000',
                secondaryValue: '+ 0.86%',
            },
            {
                id: 'order-3',
                side: 'buy',
                strategy: 'First Buy',
                time: '08:31:40',
                price: '₩ 5,142,000',
                secondaryValue: '0.0964 ETH',
            },
        ],
        indicatorGroups: [],  // 실제 전략 평가를 수신하기 전에는 예시 지표를 노출하지 않는다.
    },
    account: {
        strategy: {
            status: '정상 작동',
            statusTone: 'positive',
            appliedState: 'Case_B',  // ACTIVE STATE와 동일한 fixture 전략을 표시한다.
            profitRate: '+0.72%',
            profitAmount: '+ ₩ 345,030',
        },
        asset: {
            totalValue: '₩ 6,184,200',
            krwValue: '₩ 1,748,200',
            ethAmount: '0.8421',
            ethValue: '₩ 4,436,000',
            profitLoss: '+₩ 184,240',
        },
    },
    splitOrder: {
        buyPercentage: 40,
        sellPercentage: 40,
    },
};
