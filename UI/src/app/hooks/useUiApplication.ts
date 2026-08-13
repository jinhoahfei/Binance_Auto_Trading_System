import { useCallback, useState, useSyncExternalStore } from 'react';

import type { AppViewModel } from '../control';
import {
    UiApplicationStore,
    type UiApplicationController,
} from '../runtime';

/**
 * React App Boundary가 facade snapshot과 intent controller를 사용하는 결과이다.
 */
export interface UseUiApplicationResult {
    readonly controller: UiApplicationController;
    readonly view_model: AppViewModel;
}

/**
 * 함수 이름: create_ui_application_store()
 * 기능: 단일 App mount가 소유할 UI actor 외부 store를 생성한다.
 * 인자: 없음
 * 반환값: 초기화 전 UiApplicationStore
 * 작성 날짜: 2026/08/12
 */
function create_ui_application_store(): UiApplicationStore {
    return new UiApplicationStore();
}

/**
 * 함수 이름: use_ui_application()
 * 기능: facade를 React 외부 store로 구독하고 typed intent controller와 최신 ViewModel을 반환한다.
 * 인자: 없음
 * 반환값: 애플리케이션 controller와 렌더링용 ViewModel
 * 작성 날짜: 2026/08/12
 */
export function use_ui_application(): UseUiApplicationResult {
    const [application_store] = useState(create_ui_application_store);
    const subscribe = useCallback(
        (listener: () => void) => application_store.subscribe(listener),
        [application_store],
    );
    const get_snapshot = useCallback(
        () => application_store.get_snapshot(),
        [application_store],
    );
    const view_model = useSyncExternalStore(subscribe, get_snapshot, get_snapshot);

    return {
        controller: application_store,
        view_model,
    };
}
