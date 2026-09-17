// UI 상태 머신 계층의 공개 진입점이다.
/**
 * 파일 역할: UI 상태 머신 계층의 공개 진입점이다.
 * 전체 UI 계층을 구성하는 루트 machine 생성 함수와 snapshot·화면·모달 타입을 내보낸다.
 * 순수 STM의 평가와 외부 작업 실행 수명은 UIStateController가 조정한다.
 */

export { create_ui_application_machine } from './uiApplicationMachine';
export type { UiApplicationSnapshot } from './uiApplicationTypes';
export type { UiModalKind, UiRoute } from '../control/uiModalTypes';
