import type { UiCommandFailure } from '../contracts';

/**
 * 함수 이름: to_ui_command_failure()
 * 기능: adapter의 typed code/message를 보존하고 untyped 오류에는 feature fallback code를 적용한다.
 * 인자: error -> XState invoke에서 전달된 unknown 오류
 *      fallback_code -> typed code가 없을 때의 feature code
 *      fallback_message -> Error message도 없을 때의 사용자 문구
 * 반환값: UI actor가 보존할 명령 실패 객체
 * 작성 날짜: 2026/08/21
 */
export function to_ui_command_failure(
    error: unknown,
    fallback_code: string,
    fallback_message: string,
): UiCommandFailure {
    if (typeof error === 'object'
        && error !== null
        && 'code' in error
        && typeof error.code === 'string'
        && error.code.length > 0
        && 'message' in error
        && typeof error.message === 'string'
        && error.message.length > 0) {
        return {
            code: error.code,
            message: error.message,
        };
    }

    return {
        code: fallback_code,
        message: error instanceof Error && error.message.length > 0
            ? error.message
            : fallback_message,
    };
}
