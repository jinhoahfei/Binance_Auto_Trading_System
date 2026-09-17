import type { CsvExportOptions, RegimeType, TradeHistoryQuery } from '../../shared/contracts';
import type { UiApplicationSnapshot } from './uiApplicationTypes';

/** STM이 결정한 실행 입력이다. 실행 가능한 객체나 외부 서비스 참조를 포함하지 않는다. */
export interface UICommandInputs {
    start_trading: RegimeType;
    stop_trading: undefined;
    force_sell_and_stop: undefined;
    liquidate_recovered_position: undefined;
    apply_regime: RegimeType;
    update_split_order: { order_side: 'scale_in' | 'scale_out'; percentage: number };
    load_trade_history: { query: TradeHistoryQuery; summary_revision: number; publish_summary: boolean };
    pick_directory: undefined;
    export_csv: CsvExportOptions;
    shutdown_application: boolean;
}

export type UICommandName = keyof UICommandInputs;
export const UI_COMMAND_NAMES: readonly UICommandName[] = [
    'start_trading', 'stop_trading', 'force_sell_and_stop', 'liquidate_recovered_position',
    'apply_regime', 'update_split_order', 'load_trade_history', 'pick_directory',
    'export_csv', 'shutdown_application',
];

export type UIRunCommand = { [K in UICommandName]: {
    readonly type: 'run_command';
    readonly key: string;
    readonly token: number;
    readonly operation: K;
    readonly input: UICommandInputs[K];
} }[UICommandName];

export type UIActionRequest = UIRunCommand | {
    readonly type: 'start_timer';
    readonly key: string;
    readonly token: number;
    readonly due_at_ms: number;
} | {
    readonly type: 'cancel_command' | 'cancel_timer';
    readonly key: string;
} | { readonly type: 'stop_all' };

export interface UITransitionResult {
    readonly snapshot: UiApplicationSnapshot;
    readonly actions: readonly UIActionRequest[];
}

/** Controller가 읽은 현재 시각과 날짜. STM은 이 값으로만 시간 규칙을 계산한다. */
export interface UIEvaluationInput {
    readonly now_epoch_ms: number;
    readonly monotonic_ms?: number;
    readonly today: string;
}
