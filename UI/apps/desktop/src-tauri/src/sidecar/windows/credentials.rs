//! 고정된 Windows Generic Credential 두 항목만 native 메모리로 읽는다.

use super::super::{NativeCredentials, SidecarFailure, MAXIMUM_SECRET_BYTES};
use windows_sys::Win32::Security::Credentials::{
    CredFree, CredReadW, CREDENTIALW, CRED_TYPE_GENERIC,
};
use zeroize::Zeroize;

const API_KEY_TARGET: &str = "com.binance-auto.trader.testnet/api-key";
const API_SECRET_TARGET: &str = "com.binance-auto.trader.testnet/api-secret";

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
fn read_secret(target: &str) -> Result<String, SidecarFailure> {
    let target_wide: Vec<u16> = target.encode_utf16().chain(Some(0)).collect();
    let mut allocation = std::ptr::null_mut();

    // 사용자 현재 logon credential set만 조회하며 target·secret을 로그로 내보내지 않는다.
    if unsafe { CredReadW(target_wide.as_ptr(), CRED_TYPE_GENERIC, 0, &mut allocation) } == 0
        || allocation.is_null()
    {
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
    Ok(String::from_utf8(bytes.to_vec()).expect("validated ASCII credential")) // 반환 직후 OS 복사본은 Drop에서 지운다.
}

/// 함수 이름: read_credentials()
/// 기능: fixed generic target pair를 renderer와 분리된 zeroizing native owner로 묶는다.
/// 인자: 없음
/// 반환값: 두 credential 또는 secret-free failure
/// 작성 날짜: 2026/09/06
pub(super) fn read_credentials() -> Result<NativeCredentials, SidecarFailure> {
    // 두 번째 조회가 실패해도 첫 번째 secret은 temporary zeroizing owner가 지운다.
    let mut credentials = NativeCredentials {
        api_key: read_secret(API_KEY_TARGET)?,
        api_secret: String::new(),
    };
    credentials.api_secret = read_secret(API_SECRET_TARGET)?;
    Ok(credentials)
}
