use serde::Serialize;
use std::sync::Mutex;
use tauri::State;

const BACKEND_SCHEMA_VERSION: u32 = 2;  // Python transport schema와 native descriptor gate를 맞춘다.

/// renderer에 한 번만 전달되는 loopback 연결 descriptor이다.
#[derive(Serialize)]
pub struct BackendConnectionDescriptor {
    pub port: u16,
    pub session_id: String,
    pub schema_version: u32,
    pub token: String,
}

impl BackendConnectionDescriptor {
    /// 함수 이름: new()
    /// 기능: sidecar ready 값을 one-shot renderer descriptor로 옮기기 전에 strict shape를 검증한다.
    /// 인자: port -> 127.0.0.1 random port
    ///      session_id -> backend session canonical UUID
    ///      schema_version -> transport major schema
    ///      token -> CSPRNG 32-byte base64url no-padding token
    /// 반환값: 검증된 descriptor 또는 secret 없는 typed failure
    /// 작성 날짜: 2026/08/21
    pub fn new(
        port: u16,
        session_id: String,
        schema_version: u32,
        token: String,
    ) -> Result<Self, BackendDescriptorFailure> {
        let has_valid_token = token.len() == 43
            && token
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-');

        if port == 0
            || !is_canonical_uuid(&session_id)
            || schema_version != BACKEND_SCHEMA_VERSION
            || !has_valid_token
        {
            return Err(BackendDescriptorFailure::invalid());
        }

        Ok(Self {
            port,
            session_id,
            schema_version,
            token,
        })
    }
}

/// Tauri command가 renderer에 반환하는 secret 없는 typed descriptor failure이다.
#[derive(Serialize)]
pub struct BackendDescriptorFailure {
    pub code: &'static str,
    pub message: &'static str,
}

impl BackendDescriptorFailure {
    /// 함수 이름: invalid()
    /// 기능: raw descriptor 값을 반사하지 않는 validation failure를 만든다.
    /// 인자: 없음
    /// 반환값: INVALID_BACKEND_DESCRIPTOR failure
    /// 작성 날짜: 2026/08/21
    fn invalid() -> Self {
        Self {
            code: "INVALID_BACKEND_DESCRIPTOR",
            message: "Backend connection descriptor is invalid.",
        }
    }

    /// 함수 이름: unavailable()
    /// 기능: Phase 12가 descriptor를 stage하지 않았거나 이미 소비된 상태를 fail closed한다.
    /// 인자: 없음
    /// 반환값: BACKEND_DESCRIPTOR_UNAVAILABLE failure
    /// 작성 날짜: 2026/08/21
    fn unavailable() -> Self {
        Self {
            code: "BACKEND_DESCRIPTOR_UNAVAILABLE",
            message: "Backend connection descriptor is unavailable.",
        }
    }

    /// 함수 이름: state_unavailable()
    /// 기능: poisoned native mutex를 내부 상세 없이 typed state failure로 변환한다.
    /// 인자: 없음
    /// 반환값: BACKEND_DESCRIPTOR_STATE_UNAVAILABLE failure
    /// 작성 날짜: 2026/08/21
    fn state_unavailable() -> Self {
        Self {
            code: "BACKEND_DESCRIPTOR_STATE_UNAVAILABLE",
            message: "Backend connection descriptor state is unavailable.",
        }
    }
}

/// Phase 12 sidecar owner와 one-shot renderer command 사이의 memory-only native state이다.
#[derive(Default)]
pub struct BackendConnectionDescriptorState {
    pending_descriptor: Mutex<Option<BackendConnectionDescriptor>>,
}

impl BackendConnectionDescriptorState {
    /// 함수 이름: stage()
    /// 기능: Phase 12 launcher가 만든 descriptor를 아직 비어 있는 memory-only slot에 소유권 이전한다.
    /// 인자: descriptor -> strict constructor를 통과한 launch descriptor
    /// 반환값: stage 성공 또는 secret 없는 typed failure
    /// 작성 날짜: 2026/08/21
    pub fn stage(
        &self,
        descriptor: BackendConnectionDescriptor,
    ) -> Result<(), BackendDescriptorFailure> {
        let mut pending_descriptor = self
            .pending_descriptor
            .lock()
            .map_err(|_| BackendDescriptorFailure::state_unavailable())?;

        if pending_descriptor.is_some() {
            return Err(BackendDescriptorFailure::unavailable());
        }

        // Descriptor와 token은 filesystem, environment 또는 log를 거치지 않고 memory slot으로 이동한다.
        *pending_descriptor = Some(descriptor);
        Ok(())
    }

    /// 함수 이름: take()
    /// 기능: pending descriptor를 정확히 한 번 꺼내 slot의 token 참조를 즉시 제거한다.
    /// 인자: 없음
    /// 반환값: renderer에 직렬화할 descriptor 또는 unavailable failure
    /// 작성 날짜: 2026/08/21
    fn take(&self) -> Result<BackendConnectionDescriptor, BackendDescriptorFailure> {
        let mut pending_descriptor = self
            .pending_descriptor
            .lock()
            .map_err(|_| BackendDescriptorFailure::state_unavailable())?;

        pending_descriptor
            .take()
            .ok_or_else(BackendDescriptorFailure::unavailable)
    }
}

/// 함수 이름: is_canonical_uuid()
/// 기능: dependency 추가 없이 lowercase RFC 4122 variant canonical UUID shape를 검증한다.
/// 인자: value -> session ID 후보
/// 반환값: canonical UUID shape 여부
/// 작성 날짜: 2026/08/21
fn is_canonical_uuid(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.len() != 36 {
        return false;
    }

    for (index, byte) in bytes.iter().enumerate() {
        if matches!(index, 8 | 13 | 18 | 23) {
            if *byte != b'-' {
                return false;
            }
        } else if !byte.is_ascii_hexdigit() || byte.is_ascii_uppercase() {
            return false;
        }
    }

    matches!(bytes[14], b'1'..=b'8') && matches!(bytes[19], b'8' | b'9' | b'a' | b'b')
}

/// 함수 이름: take_backend_connection_descriptor()
/// 기능: managed native state의 descriptor를 renderer invoke caller 하나에만 반환한다.
/// 인자: state -> memory-only descriptor state
/// 반환값: one-shot descriptor 또는 typed unavailable failure
/// 작성 날짜: 2026/08/21
#[tauri::command]
fn take_backend_connection_descriptor(
    state: State<'_, BackendConnectionDescriptorState>,
) -> Result<BackendConnectionDescriptor, BackendDescriptorFailure> {
    state.take()
}

/// 함수 이름: run()
/// 기능: 최소 권한으로 Tauri 데스크톱 셸을 시작한다.
/// 인자: 없음
/// 반환값: 없음
/// 작성 날짜: 2026/08/12
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(BackendConnectionDescriptorState::default())
        .invoke_handler(tauri::generate_handler![take_backend_connection_descriptor])
        .run(tauri::generate_context!())
        .expect("Tauri 애플리케이션을 실행할 수 없습니다.");
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEST_SESSION_ID: &str = "3c73d583-c1c8-4830-8393-cc31639a40fd";
    const TEST_TOKEN: &str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

    /// 함수 이름: create_descriptor()
    /// 기능: native one-shot state test에 사용할 valid descriptor를 생성한다.
    /// 인자: 없음
    /// 반환값: valid descriptor
    /// 작성 날짜: 2026/08/21
    fn create_descriptor() -> BackendConnectionDescriptor {
        match BackendConnectionDescriptor::new(
            42_123,
            TEST_SESSION_ID.to_owned(),
            BACKEND_SCHEMA_VERSION,
            TEST_TOKEN.to_owned(),
        ) {
            Ok(descriptor) => descriptor,
            Err(_) => panic!("valid test descriptor must be accepted"),
        }
    }

    /// 함수 이름: descriptor_is_absent_until_phase12_stages_it()
    /// 기능: 초기 native state가 demo나 임의 descriptor로 fallback하지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/21
    #[test]
    fn descriptor_is_absent_until_phase12_stages_it() {
        let state = BackendConnectionDescriptorState::default();
        let failure = match state.take() {
            Ok(_) => panic!("empty state must fail closed"),
            Err(failure) => failure,
        };

        assert_eq!(failure.code, "BACKEND_DESCRIPTOR_UNAVAILABLE");
    }

    /// 함수 이름: descriptor_can_be_taken_exactly_once()
    /// 기능: stage한 token-bearing descriptor가 한 번 이동하고 native slot에서 제거되는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/21
    #[test]
    fn descriptor_can_be_taken_exactly_once() {
        let state = BackendConnectionDescriptorState::default();
        assert!(state.stage(create_descriptor()).is_ok());

        let descriptor = match state.take() {
            Ok(descriptor) => descriptor,
            Err(_) => panic!("staged descriptor must be available once"),
        };
        assert_eq!(descriptor.port, 42_123);
        assert_eq!(descriptor.session_id, TEST_SESSION_ID);
        assert_eq!(descriptor.schema_version, BACKEND_SCHEMA_VERSION);
        assert_eq!(descriptor.token, TEST_TOKEN);

        let second_failure = match state.take() {
            Ok(_) => panic!("descriptor must not be reusable"),
            Err(failure) => failure,
        };
        assert_eq!(second_failure.code, "BACKEND_DESCRIPTOR_UNAVAILABLE");
    }

    /// 함수 이름: invalid_descriptor_never_enters_native_state()
    /// 기능: unknown transport schema가 native state stage 전에 거부되는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/21
    #[test]
    fn invalid_descriptor_never_enters_native_state() {
        // 다른 descriptor 필드는 유효하게 두고 schema mismatch 하나만 격리한다.
        let invalid_result = BackendConnectionDescriptor::new(
            42_123,
            TEST_SESSION_ID.to_owned(),
            BACKEND_SCHEMA_VERSION + 1,
            TEST_TOKEN.to_owned(),
        );
        let failure = match invalid_result {
            Ok(_) => panic!("invalid descriptor must be rejected"),
            Err(failure) => failure,
        };

        assert_eq!(failure.code, "INVALID_BACKEND_DESCRIPTOR");
    }
}
