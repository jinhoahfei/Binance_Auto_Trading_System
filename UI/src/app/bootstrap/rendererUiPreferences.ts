import type { ChartDrawing, ChartInterval, HistoryPeriod, TradeSideFilter } from '../../shared/contracts';
import type { UiApplicationFacade } from '../control';
import type { UiApplicationSnapshot } from '../machines/uiApplicationTypes';
import { route_of } from '../machines/uiApplicationViews';

const PREFERENCE_STORAGE_KEY = 'binance.renderer-ui-preferences.v1';
const MAX_PREFERENCE_CHARACTERS = 262_144;
const MAX_DRAWING_COUNT = 1_000;
const MAX_DRAWING_POINTS = 256;
const CHART_INTERVALS = ['1m', '30m', '4h', '1d'] as const;
type PreferenceStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

export interface RendererUiPreferences {
    readonly chart_interval: ChartInterval;
    readonly chart_indicators: { readonly bollinger_bands: boolean; readonly ema9: boolean; readonly volume: boolean };
    readonly chart_drawings: Readonly<Record<ChartInterval, readonly ChartDrawing[]>>;
    readonly chart_is_fullscreen: boolean;
    readonly history_period: HistoryPeriod;
    readonly history_side: TradeSideFilter;
    readonly history_has_entered: boolean;
    readonly route: 'dashboard' | 'trade_history';
}


/**
 * 함수 이름: get_renderer_preference_storage()
 * 기능: 데스크톱 document의 같은 창 재로드에만 사용할 저장소를 안전하게 읽는다.
 * 인자: 없음
 * 반환값: sessionStorage 또는 접근할 수 없으면 null
 * 작성 날짜: 2026/10/04
 */
export function get_renderer_preference_storage(): PreferenceStorage | null {
    try {
        return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window ? window.sessionStorage : null;
    } catch {
        return null;
    }
}


/**
 * 함수 이름: is_record()
 * 기능: 저장된 JSON 값이 필드 검사를 할 수 있는 객체인지 확인한다.
 * 인자: value -> 검증할 값
 * 반환값: 배열·null이 아닌 객체 여부
 * 작성 날짜: 2026/10/04
 */
function is_record(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}


/**
 * 함수 이름: read_drawings()
 * 기능: 주기별 완성된 drawing의 크기와 좌표를 검증하고 허용한 필드만 복원한다.
 * 인자: value -> 저장된 drawing 객체
 * 반환값: 검증된 drawing map 또는 유효하지 않으면 null
 * 작성 날짜: 2026/10/04
 */
function read_drawings(value: unknown): RendererUiPreferences['chart_drawings'] | null {
    if (!is_record(value)) return null;
    const result: Record<ChartInterval, ChartDrawing[]> = { '1m': [], '30m': [], '4h': [], '1d': [] };
    let drawing_count = 0;

    for (const interval of CHART_INTERVALS) {
        const drawings = value[interval];
        if (!Array.isArray(drawings) || (drawing_count += drawings.length) > MAX_DRAWING_COUNT) return null;
        for (const drawing of drawings) {
            if (!is_record(drawing) || typeof drawing.id !== 'string' || drawing.id.length > 128
                || drawing.id.length === 0 || !Array.isArray(drawing.points)
                || drawing.points.length < 2 || drawing.points.length > MAX_DRAWING_POINTS) return null;
            const points: ChartDrawing['points'][number][] = [];
            for (const point of drawing.points) {
                if (!is_record(point) || typeof point.time !== 'string' || point.time.length > 64
                    || !Number.isFinite(Date.parse(point.time)) || typeof point.price !== 'string'
                    || point.price.length > 64 || !/^-?\d+(?:\.\d+)?$/u.test(point.price)
                    || !Number.isFinite(Number(point.price))
                    || (point.x_ratio !== undefined && (typeof point.x_ratio !== 'number'
                        || !Number.isFinite(point.x_ratio) || point.x_ratio < 0 || point.x_ratio > 1))) return null;
                points.push({ time: point.time, price: point.price,
                    ...(point.x_ratio === undefined ? {} : { x_ratio: point.x_ratio }) });
            }
            result[interval].push({ id: drawing.id, points });
        }
    }

    return result;
}


/**
 * 함수 이름: load_renderer_ui_preferences()
 * 기능: 현재 backend session과 일치하는 화면 설정만 읽고 서버·거래 상태는 받아들이지 않는다.
 * 인자: session_id -> 검증된 backend session, storage -> 같은 창의 저장소
 * 반환값: 검증된 화면 설정 또는 없거나 잘못된 경우 null
 * 작성 날짜: 2026/10/04
 */
export function load_renderer_ui_preferences(
    session_id: string,
    storage: PreferenceStorage | null,
): RendererUiPreferences | null {
    try {
        const serialized = storage?.getItem(PREFERENCE_STORAGE_KEY);
        if (serialized == null || serialized.length > MAX_PREFERENCE_CHARACTERS) return null;
        const envelope: unknown = JSON.parse(serialized);
        if (!is_record(envelope) || envelope.version !== 1 || envelope.session_id !== session_id
            || !is_record(envelope.preferences)) return null;
        const preferences = envelope.preferences;
        const indicators = preferences.chart_indicators;
        const drawings = read_drawings(preferences.chart_drawings);
        if (!CHART_INTERVALS.some((interval) => interval === preferences.chart_interval)
            || !is_record(indicators) || typeof indicators.bollinger_bands !== 'boolean'
            || typeof indicators.ema9 !== 'boolean' || typeof indicators.volume !== 'boolean'
            || typeof preferences.chart_is_fullscreen !== 'boolean' || drawings === null
            || typeof preferences.history_period !== 'string'
            || !['today', 'last7days', 'last30days', 'all'].includes(preferences.history_period)
            || typeof preferences.history_side !== 'string'
            || !['all', 'buy', 'sell'].includes(preferences.history_side)
            || typeof preferences.history_has_entered !== 'boolean'
            || (preferences.route !== 'dashboard' && preferences.route !== 'trade_history')) return null;

        return {
            chart_interval: preferences.chart_interval as ChartInterval,
            chart_indicators: { bollinger_bands: indicators.bollinger_bands, ema9: indicators.ema9, volume: indicators.volume },
            chart_drawings: drawings,
            chart_is_fullscreen: preferences.chart_is_fullscreen,
            history_period: preferences.history_period as HistoryPeriod,
            history_side: preferences.history_side as TradeSideFilter,
            history_has_entered: preferences.history_has_entered,
            route: preferences.route,
        };
    } catch {
        return null;
    }
}


/**
 * 함수 이름: select_renderer_ui_preferences()
 * 기능: 현재 snapshot에서 비거래 화면 선택과 완성된 drawing만 추출한다.
 * 인자: snapshot -> Controller가 발행한 현재 UI snapshot
 * 반환값: 저장 가능한 화면 설정
 * 작성 날짜: 2026/10/04
 */
function select_renderer_ui_preferences(snapshot: UiApplicationSnapshot): RendererUiPreferences {
    const chart = snapshot.context.features.chart;
    const history = snapshot.context.features.trade_history;

    return {
        chart_interval: chart.interval, chart_indicators: chart.indicators, chart_drawings: chart.drawings,
        chart_is_fullscreen: chart.is_fullscreen, history_period: history.period, history_side: history.side,
        history_has_entered: history.has_entered, route: route_of(snapshot),
    };
}


/**
 * 함수 이름: start_renderer_ui_preference_persistence()
 * 기능: 화면 설정이 변할 때만 제한된 sessionStorage 값을 교체하고 서버 snapshot은 저장하지 않는다.
 * 인자: facade -> 화면 Controller, session_id -> 검증된 backend session, storage -> 같은 창의 저장소
 * 반환값: Controller 구독을 해제하는 함수
 * 작성 날짜: 2026/10/04
 */
export function start_renderer_ui_preference_persistence(
    facade: UiApplicationFacade,
    session_id: string,
    storage: PreferenceStorage | null,
): () => void {
    if (storage === null) return () => {};
    let previous_preferences: RendererUiPreferences | null = null;

    return facade.subscribe((snapshot) => {
        const preferences = select_renderer_ui_preferences(snapshot);
        const previous = previous_preferences;
        previous_preferences = preferences;
        if (previous !== null && (Object.keys(preferences) as (keyof RendererUiPreferences)[])
            .every((key) => preferences[key] === previous[key])) return;

        try {
            // JSON 크기 검증 전에 배열 개수도 제한하여 실패 경로의 임시 할당을 제한한다.
            const drawings = read_drawings(preferences.chart_drawings);
            if (drawings === null) {
                storage.removeItem(PREFERENCE_STORAGE_KEY);

                return;
            }
            const serialized = JSON.stringify({ version: 1, session_id, preferences: {
                ...preferences, chart_drawings: drawings,
                chart_indicators: { bollinger_bands: preferences.chart_indicators.bollinger_bands,
                    ema9: preferences.chart_indicators.ema9, volume: preferences.chart_indicators.volume },
            } });
            if (serialized.length <= MAX_PREFERENCE_CHARACTERS) storage.setItem(PREFERENCE_STORAGE_KEY, serialized);
            else storage.removeItem(PREFERENCE_STORAGE_KEY);  // 예전 drawing을 잘못 복원하지 않는다.
        } catch {
            // 저장소 거부·용량 부족은 화면 조작이나 backend 수신에 영향을 주지 않는다.
            try { storage.removeItem(PREFERENCE_STORAGE_KEY); } catch { /* 저장소 자체 접근 거부도 허용한다. */ }
        }
    });
}
