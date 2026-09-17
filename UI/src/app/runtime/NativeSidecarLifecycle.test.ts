import { describe, expect, it } from 'vitest';

import { BackendContractError } from '../../shared/api';
import {
    is_expected_normal_sidecar_exit,
    validate_native_exit_intent_bridge_receipt,
    validate_native_exit_request_payload,
    validate_native_sidecar_exit_payload,
} from './NativeSidecarLifecycle';

describe('NativeSidecarLifecycle Phase 12 contract', () => {
    it.each([
        { expected: true, code: 0 },
        { expected: false, code: null },
    ])('secret 없는 exact exit payload를 보존한다', (payload) => {
        expect(validate_native_sidecar_exit_payload(payload)).toEqual(payload);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    });

    it.each([
        null,
        { expected: false },
        { expected: false, code: -1 },
        { expected: false, code: 1, detail: 'must-not-cross-boundary' },
        { expected: 'false', code: null },
    ])('malformed 또는 추가 native detail을 fail closed한다', (payload) => {
        // 잘못된 입력이나 실행 실패가 정해진 오류로 전달되는지 검증한다.
        expect(() => validate_native_sidecar_exit_payload(payload)).toThrowError(
            BackendContractError,
        );
    });

    it.each([
        { payload: { expected: true, code: 0 }, expected_result: true },
        { payload: { expected: true, code: 17 }, expected_result: false },
        { payload: { expected: true, code: null }, expected_result: false },
        { payload: { expected: false, code: 0 }, expected_result: false },
    ])(
        'expected flag와 정상 code를 함께 검사한다: $payload',
        ({ payload, expected_result }) => {
            expect(is_expected_normal_sidecar_exit(payload)).toBe(expected_result);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        },
    );

    it.each([
        { source: 'window' },
        { source: 'application' },
    ])('native 종료 intent source를 exact 보존한다', (payload) => {
        expect(validate_native_exit_request_payload(payload)).toEqual(payload);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    });

    it.each([
        { source: 'menu' },
        { source: 'window', detail: 'must-not-cross-boundary' },
        {},
    ])('unknown 또는 추가 native 종료 intent를 거부한다', (payload) => {
        // 잘못된 입력이나 실행 실패가 정해진 오류로 전달되는지 검증한다.
        expect(() => validate_native_exit_request_payload(payload)).toThrowError(
            BackendContractError,
        );
    });

    it('native 종료 intent bridge arm receipt를 exact 검증한다', () => {
        // 잘못된 입력이나 실행 실패가 정해진 오류로 전달되는지 검증한다.
        expect(validate_native_exit_intent_bridge_receipt({ armed: true })).toEqual({
            armed: true,
        });
        expect(() => validate_native_exit_intent_bridge_receipt({ armed: false })).toThrowError(
            BackendContractError,
        );
        expect(() => validate_native_exit_intent_bridge_receipt({
            armed: true,
            pending: false,
        })).toThrowError(BackendContractError);
    });
});
