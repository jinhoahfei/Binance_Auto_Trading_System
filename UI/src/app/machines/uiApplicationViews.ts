import { matchesState, type StateValue } from 'xstate';

import type { FeatureContexts, FeatureKey, UiApplicationSnapshot } from './uiApplicationTypes';

export const main_screen_path = ['ETIRE_UI_SYSTEM', 'SCREEN', 'MAIN_SCREEN_WRAPPER'];
export const details_screen_path = ['ETIRE_UI_SYSTEM', 'SCREEN', 'TRADING_DETAILS'];
export const upper_status_bar_path = ['ETIRE_UI_SYSTEM', 'UPPER_STATUS_BAR'];
const feature_paths: Record<FeatureKey, readonly string[]> = {
    connection: [...upper_status_bar_path, 'API_DISPLAY'],
    trading: [...upper_status_bar_path, 'START_BUTTON'],
    app_exit: ['ETIRE_UI_SYSTEM', 'EXIT'],
    regime: [...main_screen_path, 'REGIME_PANEL', 'selection'],
    chart: [...main_screen_path, 'DISPLAY_CHART'],
    account_summary: [...main_screen_path, 'DISPLAY_ACCOUNT_INFO'],
    split_order: [...main_screen_path, 'DISPLAY_ACCOUNT_INFO', 'SPLIT_ORDER'],
    recent_orders: [...main_screen_path, 'TRADER_PANEL'],
    trade_history: [...details_screen_path, 'PERIOD', 'query'],
    trade_history_summary: [...details_screen_path, 'ACCOUNT_DETAILS'],
    csv_export: [...details_screen_path, 'CSV_EXPORT'],
};


/**
 * 함수 이름: value_at()
 * 기능: 중첩된 상태값에서 주어진 상태 경로의 활성 값을 찾는다.
 * 인자: value -> 루트 또는 보관된 상태값, path -> 순서대로 탐색할 상태 이름
 * 반환값: 경로의 상태값 또는 undefined
 * 작성 날짜: 2026/09/16
 */
export function value_at(value: StateValue | undefined, path: readonly string[]): StateValue | undefined {
    let current_value: StateValue | undefined = value;

    for (const key of path) {
        current_value = typeof current_value === 'object' ? current_value[key] : undefined;
    }

    return current_value;
}


/**
 * 함수 이름: route_of()
 * 기능: 루트의 상세 화면 활성 여부를 화면 route로 변환한다.
 * 인자: snapshot -> 현재 UI 루트 snapshot
 * 반환값: dashboard 또는 trade_history
 * 작성 날짜: 2026/09/16
 */
export function route_of(snapshot: UiApplicationSnapshot): 'dashboard' | 'trade_history' {
    return value_at(snapshot.value, details_screen_path) === undefined ? 'dashboard' : 'trade_history';
}


/**
 * 함수 이름: feature_state()
 * 기능: 활성 경로와 보관된 경로에서 기능의 화면 표시 상태를 선택한다.
 * 인자: snapshot -> 현재 UI 루트 snapshot, feature -> 조회할 기능
 * 반환값: 해당 기능의 표시용 상태값
 * 작성 날짜: 2026/09/16
 */
export function feature_state(snapshot: UiApplicationSnapshot, feature: FeatureKey): StateValue {
    // 루트 final과 병렬 중지 Region의 상태를 표시용 기능 상태에 반영한다.
    if (feature === 'app_exit' && snapshot.value === 'UI_FINAL_STATE') {
        return 'ui_final_state';
    }

    if (feature === 'trading') {
        const stop_state = value_at(snapshot.value, [...upper_status_bar_path, 'STOP_BUTTON']);

        if (stop_state !== undefined && stop_state !== 'idle') {
            return stop_state;
        }
    }

    // 분할 주문은 활성 요청과 오류를 기준으로 저장 상태를 읽는다.
    if (feature === 'split_order') {
        return Object.entries(snapshot.context.requests).some(([key, request]) => (
            key.startsWith('split_order.') && request.status === 'pending'
        ))
            ? 'saving'
            : snapshot.context.features.split_order.error ? 'failed' : 'ready';
    }

    // 활성 화면의 실제 상태 경로를 우선 사용한다.
    const active_state = value_at(snapshot.value, feature_paths[feature]);

    if (active_state !== undefined) {
        return active_state;
    }

    // 비활성 상세 조회는 idle로, CSV와 대시보드는 보관된 상태로 표시한다.
    if (feature === 'trade_history') {
        return 'idle';
    }

    if (feature === 'csv_export') {
        return value_at(snapshot.context.retained_details, feature_paths[feature]) ?? 'closed';
    }

    return value_at(snapshot.context.retained, feature_paths[feature]) ?? '';
}


type FeatureView<K extends FeatureKey> = {
    context: FeatureContexts[K];
    value: StateValue;
    matches(value: StateValue): boolean;
};
type ShellView = {
    context: {
        route: 'dashboard' | 'trade_history';
        active_modal: null;
    };
    value: StateValue;
    matches(value: StateValue): boolean;
};


/**
 * 함수 이름: feature_view()
 * 기능: 기능의 현재 데이터와 표시용 상태를 읽기 전용 객체로 묶는다.
 * 인자: snapshot -> 현재 UI 루트 snapshot, key -> 기능 이름 또는 shell
 * 반환값: context·value·matches를 제공하는 기능 view
 * 작성 날짜: 2026/09/16
 */
export function feature_view<K extends FeatureKey | 'shell'>(snapshot: UiApplicationSnapshot, key: K): K extends FeatureKey ? FeatureView<K> : ShellView {
    const value = key === 'shell' ? route_of(snapshot) : feature_state(snapshot, key);

    return {
        value,
        context: key === 'shell' ? {
            route: route_of(snapshot),
            active_modal: null,
        } : snapshot.context.features[key as FeatureKey],
        matches: (pattern: StateValue) => matchesState(pattern, value),
    } as K extends FeatureKey ? FeatureView<K> : ShellView;
}


/**
 * 함수 이름: select_feature_views()
 * 기능: 화면 모델 계산에 필요한 기능별 읽기 전용 view를 모은다.
 * 인자: snapshot -> 현재 UI 루트 snapshot
 * 반환값: 화면 선택과 각 기능 view를 포함한 객체
 * 작성 날짜: 2026/09/16
 */
export function select_feature_views(snapshot: UiApplicationSnapshot) {
    // 현재 루트에서 모든 기능의 context와 표시 상태를 같은 시점에 읽는다.
    return {
        shell: feature_view(snapshot, 'shell'),
        account_summary: feature_view(snapshot, 'account_summary'),
        app_exit: feature_view(snapshot, 'app_exit'),
        connection: feature_view(snapshot, 'connection'),
        csv_export: feature_view(snapshot, 'csv_export'),
        chart: feature_view(snapshot, 'chart'),
        recent_orders: feature_view(snapshot, 'recent_orders'),
        regime: feature_view(snapshot, 'regime'),
        split_order: feature_view(snapshot, 'split_order'),
        trade_history: feature_view(snapshot, 'trade_history'),
        trade_history_summary: feature_view(snapshot, 'trade_history_summary'),
        trading: feature_view(snapshot, 'trading'),
    };
}
