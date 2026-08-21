import type {
    RegimeType,
    TradingLogicCoverage,
} from '../../shared/contracts';

export type { RegimeType } from '../../shared/contracts';

export type RegimeMetricTone = 'positive' | 'negative' | 'neutral';

export interface RegimeOption {
    readonly label: string;
    readonly type: RegimeType;
}

export interface RegimeMetricViewModel {
    readonly id: 'emaSlope' | 'ema' | 'swingLow' | 'swingHigh';
    readonly label: string;
    readonly tone: RegimeMetricTone;
    readonly value: string;
}

export interface RegimePanelIntent {
    readonly regime: RegimeType;
    readonly type: 'REGIME_TYPE_REQUESTED';
}

export interface RegimePanelProps {
    readonly applied: RegimeType | null;
    readonly candidate?: RegimeType | null;
    readonly disabled?: boolean;
    readonly highlight?: boolean;
    readonly logicCoverage?: ReadonlyArray<TradingLogicCoverage>;
    readonly metrics: ReadonlyArray<RegimeMetricViewModel>;
    readonly onIntent?: (intent: RegimePanelIntent) => void;
    readonly recommended: RegimeType | null;
}
