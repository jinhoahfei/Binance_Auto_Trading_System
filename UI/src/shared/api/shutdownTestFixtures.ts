import type { BackendShutdownPreparation } from '../contracts';
export const SHUTDOWN_OPERATION_ID = '2522ef0c-d88d-42b3-a22f-fc7bdd09a662';
export function shutdown_preparation_fixture(overrides: Partial<BackendShutdownPreparation> = {}): BackendShutdownPreparation {
    return { operation_id: SHUTDOWN_OPERATION_ID, phase: 'ready', step: 'complete', version: 0,
        reason_code: null, retryable: false, ...overrides };
}
