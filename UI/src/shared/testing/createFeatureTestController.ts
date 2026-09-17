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


/**
 * 함수 이름: create_feature_test_actor()
 * 기능: 기능 정의를 순수 루트에 조립하고 실제 Controller로 기존 단위 테스트 입력·상태 경계를 제공한다.
 * 인자: factory -> 기능 정의 생성 함수, port -> 명령 테스트 대역, options -> 초기값·테스트 관찰 callback
 * 반환값: 시작·중지·이벤트 전송·기능 snapshot 조회 도구
 * 작성 날짜: 2026/09/17
 */
export function create_feature_test_actor<F extends (options: any) => AnyStateMachine>(
    factory: F,
    port: UiCommandPort,
    options: (Parameters<F> extends [] ? object : NonNullable<Parameters<F>[0]>) & TestObservers = {} as never,
) {
    const today = options.today ?? '2026-09-17';
    const { on_details_loaded: _summary_observer, get_current_kst_date: _date_source, ...initial_options } = options;

    // 관찰 callback은 테스트 경계에 남겨 순수 기능 정의에 외부 실행 함수를 넘기지 않는다.
    const definition = factory(initial_options);
    const definitions = create_feature_definitions({ today });
    const feature = (Object.keys(definitions) as FeatureKey[]).find(key => definitions[key].id === definition.id)!;
    if (!feature) throw new Error(`Unknown test feature ${definition.id}`);

    const entries = { ...definitions, [feature]: definition };

    // production과 같은 조립기로 기능을 루트에 연결하여 별도 테스트 전이 규칙을 만들지 않는다.
    const composition = new UiRegionComposition(entries);
    const node = composition.compile(feature, definition.config);
    composition.connect_commands(node);
    if (feature === 'app_exit') node.states.ui_final_state.id = 'UI_FINAL_STATE';

    const machine = setup({
        // 내부 context와 이벤트의 타입 계약을 연결한다.
        types: { context: {} as UiApplicationContext, events: {} as UiDomainEvent },

        // 상태 데이터 변경과 실행 요청을 Action 정의로 묶는다.
        actions: { 'ui.request': (_args, _request: UIActionRequest) => { throw new Error('Use the test Controller'); } },
    }).createMachine({
        ...node,

        // 외부 작업을 실행하지 않고 기능의 초기 데이터를 구성한다.
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

    // 기존 summary 관찰 계약은 완성된 Controller snapshot의 변경에서만 호출한다.
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
