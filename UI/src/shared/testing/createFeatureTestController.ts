import { enqueueActions, setup, type AnyStateMachine, type SnapshotFrom, type EventFrom } from 'xstate';
import { UIStateController } from '../../app/control/UIStateController';
import { UISTM } from '../../app/machines/UISTM';
import { UiRegionComposition } from '../../app/machines/uiRegionComposition';
import { create_feature_definitions } from '../../app/machines/uiFeatureDefinitions';
import type { FeatureKey, FeatureContexts, UiApplicationContext, UiDomainEvent } from '../../app/machines/uiApplicationTypes';
import type { UIActionRequest } from '../../app/machines/uiActions';
import type { UiCommandPort } from '../ports';
import type { TradeHistorySummary } from '../contracts';

interface TestObservers {
    today?: string;
    get_current_kst_date?: () => string;
    on_details_loaded?: (summary: TradeHistorySummary, revision: number) => void;
}

/** 기능 단위의 기존 상태 계약을 실제 순수 STM + Controller로 검증하는 테스트 경계다. */
export function create_feature_test_actor<F extends (options: any) => AnyStateMachine>(
    factory: F,
    port: UiCommandPort,
    options: (Parameters<F> extends [] ? object : NonNullable<Parameters<F>[0]>) & TestObservers = {} as never,
) {
    const today = options.today ?? '2026-09-17';
    const { on_details_loaded: _summary_observer, get_current_kst_date: _date_source, ...initial_options } = options;
    const definition = factory(initial_options);
    const definitions = create_feature_definitions({ today });
    const feature = (Object.keys(definitions) as FeatureKey[]).find(key => definitions[key].id === definition.id)!;
    if (!feature) throw new Error(`Unknown test feature ${definition.id}`);
    const entries = { ...definitions, [feature]: definition };
    const composition = new UiRegionComposition(entries);
    const node = composition.compile(feature, definition.config);
    composition.connect_commands(node);
    if (feature === 'app_exit') node.states.ui_final_state.id = 'UI_FINAL_STATE';
    const machine = setup({
        types: { context: {} as UiApplicationContext, events: {} as UiDomainEvent },
        actions: { 'ui.request': (_args, _request: UIActionRequest) => { throw new Error('Use the test Controller'); } },
    }).createMachine({
        ...node,
        context: {
            evaluation: { now_epoch_ms: 0, today }, server_snapshot: null,
            features: Object.fromEntries(Object.entries(entries).map(([key, value]) => [key, value.config.context])) as FeatureContexts,
            requests: {}, request_sequence: 0, summary_revision: 0,
            retained: undefined, retained_details: undefined, deferred: [],
        },
        on: {
            ...node.on,
            'ui.evaluate': { actions: enqueueActions(({ event, enqueue }) => {
                enqueue.assign({ evaluation: event.evaluation! });
                for (const item of event.events ?? []) enqueue.raise(item);
            }) },
            'ui.batch': { actions: enqueueActions(({ event, enqueue }) => {
                for (const item of event.events ?? []) enqueue.raise(item);
            }) },
        },
    });
    const controller = new UIStateController(port, {
        today,
        ...(options.get_current_kst_date ? { get_current_kst_date: options.get_current_kst_date } : {}),
    }, new UISTM(machine));
    let last_summary = controller.get_snapshot().context.features.trade_history_summary.summary;
    controller.subscribe(snapshot => {
        const summary = snapshot.context.features.trade_history_summary.summary;
        if (summary !== last_summary) {
            last_summary = summary;
            options.on_details_loaded?.(summary, snapshot.context.summary_revision);
        }
    });
    return {
        start: () => controller.start(),
        stop: () => controller.stop(),
        send: (event: EventFrom<ReturnType<F>>) => {
            const source = event as { type: string; [key: string]: unknown };
            controller.handle_event({ type: `${feature}.${source.type}`, source });
        },
        getSnapshot: () => {
            const snapshot = controller.get_snapshot();
            return { ...snapshot, context: snapshot.context.features[feature] } as unknown as SnapshotFrom<ReturnType<F>>;
        },
    };
}
