import type { ChartDrawing } from '../../shared/contracts';

export type ChartInterval = '1m' | '30m' | '4h' | '1d';

export type PriceChartIntent =
    | { readonly type: 'CHART_INTERVAL_REQUESTED'; readonly interval: ChartInterval }
    | { readonly type: 'INDICATOR_SETTINGS_REQUESTED' }
    | { readonly type: 'INDICATOR_SETTINGS_CLOSED' }
    | { readonly type: 'INDICATOR_VISIBILITY_REQUESTED'; readonly indicator: ChartIndicator; readonly visible: boolean }
    | { readonly type: 'DRAWING_MODE_REQUESTED' }
    | { readonly type: 'DRAWING_STARTED' }
    | { readonly type: 'DRAWING_FINISHED'; readonly drawing: ChartDrawing }
    | { readonly type: 'DRAWING_CANCELED' }
    | { readonly type: 'CHART_FULLSCREEN_REQUESTED' }
    | { readonly type: 'DRAWING_LINE_HOVER_ENTERED'; readonly lineId: string }
    | { readonly type: 'DRAWING_LINE_HOVER_EXITED' }
    | {
        readonly type: 'DRAWING_LINE_CONTEXT_MENU_REQUESTED';
        readonly x: number;
        readonly y: number;
    }
    | { readonly type: 'DRAWING_LINE_DELETE_REQUESTED' }
    | { readonly type: 'DRAWING_LINE_CONTEXT_MENU_CLOSED' };

export type ChartIndicator = 'ema9' | 'bollingerBand' | 'volume';

export type PriceChartDataStatus = 'idle' | 'loading' | 'live' | 'reconnecting' | 'error';

/** 실제 상호작용 화면과 결정적 Figma 검증 화면의 chart 표현 계약이다. */
export type PriceChartPresentationMode = 'interactive' | 'fixture';

export interface IndicatorSettingsViewModel {
    readonly bollingerBand: boolean;
    readonly ema9: boolean;
    readonly volume: boolean;
}

export interface CandleViewModel {
    readonly close: number;
    readonly high: number;
    readonly is_closed?: boolean;
    readonly low: number;
    readonly open: number;
    readonly open_time?: number;
    readonly volume?: number;
}

export interface LinePointViewModel {
    readonly open_time?: number;
    readonly value: number;
}

export interface PriceChartViewModel {
    /** 열린 포지션의 원본 평단가이며 null 또는 미제공이면 기준선을 숨긴다. */
    readonly position_average_entry_price?: string | null;
    readonly activeState: string;
    readonly bollingerLower: ReadonlyArray<LinePointViewModel>;
    readonly bollingerUpper: ReadonlyArray<LinePointViewModel>;
    readonly candles: ReadonlyArray<CandleViewModel>;
    readonly dataStatus?: PriceChartDataStatus;
    readonly ema: ReadonlyArray<LinePointViewModel>;
    readonly interval: ChartInterval;
    readonly statusMessage?: string | null;
    readonly symbol?: string;
    readonly timestampLabel: string;
}

export interface PriceChartPanelProps extends PriceChartViewModel {
    readonly drawingActive?: boolean;
    readonly drawings?: ReadonlyArray<ChartDrawing>;
    readonly isFullscreen?: boolean;
    readonly selectedLineId?: string | null;
    readonly lineContextMenuOpen?: boolean;
    readonly contextMenuPosition?: { readonly x: number; readonly y: number } | null;
    readonly indicatorSettings?: IndicatorSettingsViewModel | undefined;
    readonly indicatorSettingsOpen?: boolean;
    readonly historyErrorMessage?: string | null;
    readonly historyExhausted?: boolean;
    readonly historyLoading?: boolean;
    readonly onLoadEarlier?: (() => void) | undefined;
    readonly onIntent?: ((intent: PriceChartIntent) => void) | undefined;
    readonly presentationMode?: PriceChartPresentationMode;
}
