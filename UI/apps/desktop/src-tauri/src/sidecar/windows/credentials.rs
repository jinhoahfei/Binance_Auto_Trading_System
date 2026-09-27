//! 고정된 Windows Generic Credential 두 항목만 native 메모리로 읽는다.

use super::super::{NativeCredentials, SidecarFailure, MAXIMUM_SECRET_BYTES};
use windows_sys::Win32::Security::Credentials::{
    CredFree, CredReadW, CREDENTIALW, CRED_TYPE_GENERIC,
};
use zeroize::Zeroize;
use std::sync::OnceLock;
use super::super::execution_profile::{parse_profile, ExecutionProfile};
use windows_sys::Win32::Foundation::ERROR_NOT_FOUND;

const PROFILE_TARGET: &str = "com.binance-auto.trader.desktop-profile/execution-profile";
static PROFILE: OnceLock<Result<ExecutionProfile, SidecarFailure>> = OnceLock::new();

/// 클래스 이름: CredentialAllocation
/// 기능: CredReadW가 할당한 blob을 모든 반환 경로에서 덮어쓰고 해제한다.
/// 작성 날짜: 2026/09/06
struct CredentialAllocation(*mut CREDENTIALW);

impl Drop for CredentialAllocation {
    /// 함수 이름: drop()
    /// 기능: OS credential allocation을 zeroize한 뒤 CredFree로 정확히 한 번 반환한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/06
    fn drop(&mut self) {
        // CredReadW의 단일 allocation 안에서 제공된 blob만 길이 그대로 덮어쓴다.
        unsafe {
            let credential = &mut *self.0;
            if !credential.CredentialBlob.is_null() {
                std::slice::from_raw_parts_mut(
                    credential.CredentialBlob,
                    credential.CredentialBlobSize as usize,
                )
                .zeroize();
            }
            CredFree(self.0.cast()); // OS allocation은 Rust allocator로 해제하지 않는다.
        }
    }
}


/// 함수 이름: read_secret()
/// 기능: 고정 target의 generic blob을 bounded printable ASCII로 검증한다.
/// 인자: target -> 코드에 고정한 두 target 중 하나
/// 반환값: native secret string 또는 값 없는 고정 오류
/// 작성 날짜: 2026/09/06
fn read_optional_secret(target: &str) -> Result<Option<String>, SidecarFailure> {
    let target_wide: Vec<u16> = target.encode_utf16().chain(Some(0)).collect();
    let mut allocation = std::ptr::null_mut();

    // 사용자 현재 logon credential set만 조회하며 target·secret을 로그로 내보내지 않는다.
    if unsafe { CredReadW(target_wide.as_ptr(), CRED_TYPE_GENERIC, 0, &mut allocation) } == 0 {
        return if std::io::Error::last_os_error().raw_os_error() == Some(ERROR_NOT_FOUND as i32) {
            Ok(None)
        } else {
            Err(SidecarFailure::credentials_unavailable())
        };
    }
    if allocation.is_null() {
        return Err(SidecarFailure::credentials_unavailable());
    }
    let allocation = CredentialAllocation(allocation);
    let credential = unsafe { &*allocation.0 };
    if credential.Type != CRED_TYPE_GENERIC
        || credential.CredentialBlob.is_null()
        || credential.CredentialBlobSize == 0
        || credential.CredentialBlobSize as usize > MAXIMUM_SECRET_BYTES
    {
        return Err(SidecarFailure::credentials_unavailable());
    }
    let bytes = unsafe {
        std::slice::from_raw_parts(
            credential.CredentialBlob,
            credential.CredentialBlobSize as usize,
        )
    };
    if !bytes.iter().all(|byte| matches!(byte, 0x21..=0x7e)) {
        return Err(SidecarFailure::credentials_unavailable());
    }
    Ok(Some(String::from_utf8(bytes.to_vec()).expect("validated ASCII credential"))) // 반환 직후 OS 복사본은 Drop에서 지운다.
}


/// 함수 이름: read_credentials()
/// 기능: fixed generic target pair를 renderer와 분리된 zeroizing native owner로 묶는다.
/// 인자: 없음
/// 반환값: 두 credential 또는 secret-free failure
/// 작성 날짜: 2026/09/06
pub(super) fn read_credentials(profile: ExecutionProfile) -> Result<NativeCredentials, SidecarFailure> {
    let (key_target, secret_target) = credential_targets(profile);
    // 두 번째 조회가 실패해도 첫 번째 secret은 temporary zeroizing owner가 지운다.
    let mut credentials = NativeCredentials {
        api_key: read_optional_secret(key_target)?.ok_or_else(SidecarFailure::credentials_unavailable)?,
        api_secret: String::new(),
    };
    credentials.api_secret = read_optional_secret(secret_target)?.ok_or_else(SidecarFailure::credentials_unavailable)?;
    Ok(credentials)
}


/// Freeze the native profile before selecting paths or credentials for this launch.
pub(super) fn selected_profile() -> Result<ExecutionProfile, SidecarFailure> {
    PROFILE.get_or_init(|| {
        let value = read_optional_secret(PROFILE_TARGET)?;
        parse_profile(value.as_deref().map(str::as_bytes))
    }).clone()
}

fn credential_targets(profile: ExecutionProfile) -> (&'static str, &'static str) {
    if profile == ExecutionProfile::Testnet {
        ("com.binance-auto.trader.testnet/api-key", "com.binance-auto.trader.testnet/api-secret")
    } else {
        ("com.binance-auto.trader.live/api-key", "com.binance-auto.trader.live/api-secret")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn credential_namespace_matches_the_frozen_execution_profile() {
        assert_eq!(credential_targets(ExecutionProfile::Testnet).0, "com.binance-auto.trader.testnet/api-key");
        for profile in [ExecutionProfile::LiveReadOnly, ExecutionProfile::LiveOrders] {
            assert_eq!(credential_targets(profile), ("com.binance-auto.trader.live/api-key", "com.binance-auto.trader.live/api-secret"));
        }
    }
}
