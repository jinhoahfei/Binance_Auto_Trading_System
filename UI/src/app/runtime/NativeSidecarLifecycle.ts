import { BackendContractError } from '../../shared/api';

/**
 * native sidecar exit event에서 renderer가 소비하는 secret 없는 strict payload이다.
 */
export interface NativeSidecarExitPayload {
    readonly expected: boolean;
    readonly code: number | null;
}

/**
 * native가 renderer listener 등록 전에도 보존하는 종료 intent의 exact payload이다.
 */
export interface NativeExitRequestPayload {
    readonly source: 'window' | 'application';
}

/**
 * renderer listener가 준비된 뒤 native latch를 release한 command receipt이다.
 */
export interface NativeExitIntentBridgeReceipt {
    readonly armed: true;
}


/**
 * 함수 이름: validate_native_sidecar_exit_payload()
 * 기능: native child monitor event를 exact expected/code 계약으로 제한한다.
 * 인자: value -> Tauri event의 미검증 payload
 * 반환값: 검증된 secret 없는 exit payload, malformed면 예외
 * 작성 날짜: 2026/08/24
 */
export function validate_native_sidecar_exit_payload(
    value: unknown,
): NativeSidecarExitPayload {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
        throw new BackendContractError(
            'MALFORMED_NATIVE_SIDECAR_EVENT',
            'Native sidecar exit event is invalid',
        );
    }

    const payload = value as Record<string, unknown>;
    const keys = Object.keys(payload);
    const is_valid_code = payload.code === null
        || (Number.isSafeInteger(payload.code) && (payload.code as number) >= 0);

    // Exact 두 필드만 허용해 native diagnostics나 future secret이 renderer state로 번지지 않게 한다.
    if (keys.length !== 2
        || !Object.hasOwn(payload, 'expected')
        || !Object.hasOwn(payload, 'code')
        || typeof payload.expected !== 'boolean'
        || !is_valid_code) {
        throw new BackendContractError(
            'MALFORMED_NATIVE_SIDECAR_EVENT',
            'Native sidecar exit event is invalid',
        );
    }

    return payload as unknown as NativeSidecarExitPayload;
}


/**
 * 함수 이름: is_expected_normal_sidecar_exit()
 * 기능: shutdown 의도뿐 아니라 실제 정상 exit code까지 만족한 event만 정상 종료로 분류한다.
 * 인자: payload -> strict validation을 마친 native sidecar exit payload
 * 반환값: expected=true 및 code=0을 모두 만족하면 true
 * 작성 날짜: 2026/08/24
 */
export function is_expected_normal_sidecar_exit(
    payload: NativeSidecarExitPayload,
): boolean {
    return payload.expected && payload.code === 0;
}


/**
 * 함수 이름: validate_native_exit_request_payload()
 * 기능: native close·application quit intent를 exact source 한 필드로 제한한다.
 * 인자: value -> Tauri event의 미검증 payload
 * 반환값: 검증된 native exit request payload, malformed면 예외
 * 작성 날짜: 2026/08/24
 */
export function validate_native_exit_request_payload(
    value: unknown,
): NativeExitRequestPayload {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
        throw new BackendContractError(
            'MALFORMED_NATIVE_EXIT_REQUEST',
            'Native exit request event is invalid',
        );
    }

    const payload = value as Record<string, unknown>;
    const keys = Object.keys(payload);

    // Window close와 application quit 이외의 future source는 UI 안전 분기로 자동 유입시키지 않는다.
    if (keys.length !== 1
        || !Object.hasOwn(payload, 'source')
        || (payload.source !== 'window' && payload.source !== 'application')) {
        throw new BackendContractError(
            'MALFORMED_NATIVE_EXIT_REQUEST',
            'Native exit request event is invalid',
        );
    }

    return payload as unknown as NativeExitRequestPayload;
}


/**
 * 함수 이름: validate_native_exit_intent_bridge_receipt()
 * 기능: native pre-listener latch를 arm한 command 결과를 exact boolean receipt로 검증한다.
 * 인자: value -> Tauri invoke의 미검증 반환값
 * 반환값: exact armed receipt, malformed면 예외
 * 작성 날짜: 2026/08/24
 */
export function validate_native_exit_intent_bridge_receipt(
    value: unknown,
): NativeExitIntentBridgeReceipt {
    // 네이티브 수신 확인이 객체인지 먼저 확인한다.
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
        throw new BackendContractError(
            'MALFORMED_NATIVE_EXIT_BRIDGE_RECEIPT',
            'Native exit bridge receipt is invalid',
        );
    }

    const receipt = value as Record<string, unknown>;

    // armed 외의 필드나 미등록 상태를 성공 수신으로 수락하지 않는다.
    if (Object.keys(receipt).length !== 1
        || !Object.hasOwn(receipt, 'armed')
        || receipt.armed !== true) {
        throw new BackendContractError(
            'MALFORMED_NATIVE_EXIT_BRIDGE_RECEIPT',
            'Native exit bridge receipt is invalid',
        );
    }

    return receipt as unknown as NativeExitIntentBridgeReceipt;
}
