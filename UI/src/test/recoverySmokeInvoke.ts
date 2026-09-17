import { invoke as native_invoke, type InvokeArgs, type InvokeOptions } from '@tauri-apps/api/core';

let descriptor_fault_pending = sessionStorage.getItem('binance-desktop-recovery-smoke-phase') !== 'reload';


/**
 * 함수 이름: invoke()
 * 기능: 개발 검증의 main import에서만 최초 연결 정보 부재를 재현하고 나머지 IPC는 실제 native로 전달한다.
 * 인자: command -> native command, args -> 원본 인자, options -> 원본 IPC 옵션
 * 반환값: 최초 연결 오류 또는 실제 native 응답 Promise
 * 작성 날짜: 2026/09/05
 */
export async function invoke<T>(command: string, args?: InvokeArgs, options?: InvokeOptions): Promise<T> {
    // Native의 변경 불가능한 invoke 속성을 수정하지 않고 테스트용 module 경계에서만 실패를 주입한다.
    if (command === 'get_backend_connection_descriptor' && descriptor_fault_pending) {
        descriptor_fault_pending = false;
        throw { code: 'BACKEND_DESCRIPTOR_UNAVAILABLE' };
    }

    return native_invoke<T>(command, args, options);
}
