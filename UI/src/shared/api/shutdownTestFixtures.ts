// 안전 종료 준비 완료 fixture에 시나리오별 값을 덮어쓴다.

import type { BackendShutdownPreparation } from '../contracts';
export const SHUTDOWN_OPERATION_ID = '2522ef0c-d88d-42b3-a22f-fc7bdd09a662';


/**
 * 함수 이름: shutdown_preparation_fixture()
 * 기능: 안전 종료 준비 완료 fixture에 시나리오별 값을 덮어쓴다.
 * 인자: overrides -> 변경할 준비 결과 필드
 * 반환값: 종료 준비 응답 fixture
 * 작성 날짜: 2026/09/17
 */
export function shutdown_preparation_fixture(overrides: Partial<BackendShutdownPreparation> = {}): BackendShutdownPreparation {
    return { operation_id: SHUTDOWN_OPERATION_ID, phase: 'ready', step: 'complete', version: 0,
        reason_code: null, retryable: false, ...overrides };
}
