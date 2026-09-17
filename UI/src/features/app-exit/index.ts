// 앱 종료 모듈의 공개 타입과 진입점을 한 경로로 재수출한다.

export { create_app_exit_machine } from './machines/appExitMachine';
export type {
    AppExitMachineContext,
    AppExitMachineEvent,
} from './machines/appExitMachine';

