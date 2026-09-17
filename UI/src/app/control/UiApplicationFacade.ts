/** 기존 공개 경로를 보존한다. 실행 구현과 상태는 UIStateController 하나에만 있다. */
export { UIStateController as UiApplicationFacade } from './UIStateController';
export { select_app_view_model } from './selectAppViewModel';
export type * from './uiApplicationContracts';
export type { UiApplicationSnapshot } from '../machines/uiApplicationTypes';
