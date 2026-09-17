/**
 * 함수 이름: connection_recovery_message()
 * 기능: 연결·화면 반영·검증 실패를 원문이나 인증 값 없이 사용자에게 구분해 설명한다.
 * 인자: code -> 고정 오류 코드
 * 반환값: 한국어 원인 설명
 * 작성 날짜: 2026/09/15
 */
export function connection_recovery_message(code: string | null | undefined): string {
    // 허용한 오류 코드만 정해진 안내 문구에 대응시키고 알 수 없는 값에는 공통 안내를 사용한다.
    switch (code) {
        case 'UI_STATE_PUBLICATION_FAILED':
            return '받은 정보를 화면에 반영하지 못했습니다.';
        case 'EVENT_STREAM_STALE':
            return '화면의 정보가 오래되어 최신 상태를 다시 확인하고 있습니다.';
        case 'BACKEND_REQUEST_TIMEOUT':
        case 'EVENT_STREAM_CONNECT_TIMEOUT':
            return '백엔드의 응답 대기 시간이 초과됐습니다.';
        case 'EVENT_STREAM_REJECTED':
        case 'EVENT_STREAM_AUTHENTICATION_FAILED':
        case 'AUTHENTICATION_REQUIRED':
            return '백엔드 연결 인증을 확인하지 못했습니다.';
        case 'EVENT_STREAM_DECODE_FAILED':
        case 'EVENT_STREAM_MAPPING_FAILED':
        case 'MALFORMED_BACKEND_PAYLOAD':
        case 'MALFORMED_BACKEND_RESPONSE':
            return '받은 정보의 형식을 확인하지 못해 화면 갱신을 멈췄습니다.';
        case 'UNSUPPORTED_SCHEMA_VERSION':
            return '화면과 백엔드가 사용하는 데이터 버전이 맞지 않습니다.';
        case 'SESSION_MISMATCH':
            return '현재 실행 중인 백엔드와 받은 정보의 실행 세션이 다릅니다.';
        case 'BACKEND_NOT_READY':
            return '백엔드가 아직 최신 상태를 제공할 준비를 마치지 못했습니다.';
        case 'EVENT_STREAM_CLOSED':
        case 'EVENT_STREAM_SOCKET_ERROR':
        case 'EVENT_STREAM_CONNECTION_FAILED':
        case 'BACKEND_UNREACHABLE':
            return '화면과 백엔드 사이의 통신이 중단됐습니다.';
        default:
            return '화면의 연결 상태를 확인하지 못했습니다.';
    }
}
