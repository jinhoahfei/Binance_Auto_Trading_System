// 앱 초기화 모듈의 공개 타입과 진입점을 한 경로로 재수출한다.

export {
    create_demo_ui_application,
    initialize_demo_ui_application,
    type DemoUiApplication,
} from './createDemoUiApplication';
export {
    DEMO_HISTORY_TRADE_RECORDS,
    DEMO_REALTIME_INDICATORS,
    DEMO_RECENT_TRADE_RECORDS,
} from './demoFixtures';
export {
    create_live_ui_application,
    create_live_ui_application_factory,
    hydrate_live_ui_application,
    type LiveBackendConnectionDescriptor,
    type LiveUiApplication,
    type LiveUiHydrationOptions,
    type LiveUiApplicationOptions,
} from './createLiveUiApplication';
export type {
    UiApplicationFactory,
    UiApplicationRuntime,
} from './types';
