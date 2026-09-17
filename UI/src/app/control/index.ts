// UI 제어 계층의 공개 진입점이다.
/**
 * 파일 역할: UI 제어 계층의 공개 진입점이다.
 * Facade와 화면 모델 selector, 외부 입력·초기 설정·snapshot 타입을 한곳에서 내보낸다.
 * 다른 계층은 이 경로를 통해 UI의 생성·입력 전달·상태 조회에 필요한 계약을 가져온다.
 */

export {
    select_app_view_model,
    UiApplicationFacade,
} from './UiApplicationFacade';
export type {
    AppViewModel,
    UiApplicationFacadeOptions,
    UiApplicationIntent,
    UiApplicationSnapshot,
    UiServerOwnedSnapshot,
} from './UiApplicationFacade';

export { UIStateController } from './UIStateController';
