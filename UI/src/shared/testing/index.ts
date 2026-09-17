// UI 테스트 모듈의 공개 타입과 진입점을 한 경로로 재수출한다.

export { FakeUiCommandAdapter } from './FakeUiCommandAdapter';
export type { FakeCommandRecord } from './FakeUiCommandAdapter';
export {
    CHART_DRAWING_FIXTURE,
    FIXTURE_TODAY,
    REGIME_METRIC_FIXTURES,
    TRADE_RECORD_FIXTURES,
} from './fixtures';

