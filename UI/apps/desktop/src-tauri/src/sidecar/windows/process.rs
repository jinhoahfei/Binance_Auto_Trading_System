//! Windows process handle liveness와 crash-report policy를 담당한다.

use super::super::SidecarFailure;
use std::io;
use windows_sys::Win32::Foundation::{CloseHandle, ERROR_INVALID_PARAMETER, WAIT_OBJECT_0};
use windows_sys::Win32::System::Diagnostics::Debug::{
    GetErrorMode, SetErrorMode, SEM_FAILCRITICALERRORS, SEM_NOGPFAULTERRORBOX,
};
use windows_sys::Win32::System::Threading::{
    OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE,
};

/// 함수 이름: runtime_process_exists()
/// 기능: signal이나 kill 없이 native process handle의 signaled 상태만 확인한다.
/// 인자: runtime_pid -> ownership artifact의 positive PID
/// 반환값: process 생존 또는 permission/OS ambiguity면 true
/// 작성 날짜: 2026/09/06
pub(in crate::sidecar) fn runtime_process_exists(runtime_pid: u32) -> bool {
    if runtime_pid == 0 {
        return true;
    }

    // 부재가 확정된 ERROR_INVALID_PARAMETER만 false로 처리해 access denied를 stale로 오인하지 않는다.
    let process_handle = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, runtime_pid) };
    if process_handle.is_null() {
        return io::Error::last_os_error().raw_os_error() != Some(ERROR_INVALID_PARAMETER as i32);
    }
    let result = unsafe { WaitForSingleObject(process_handle, 0) };
    unsafe { CloseHandle(process_handle) }; // 조회용 handle을 항상 닫고 process 자체는 변경하지 않는다.
    result != WAIT_OBJECT_0
}

/// 함수 이름: disable_process_core_dumps()
/// 기능: 현재 process와 상속 child에서 자동 Windows Error Reporting invocation을 막는다.
/// 인자: 없음
/// 반환값: process policy 확인 성공 또는 고정 startup 오류
/// 작성 날짜: 2026/09/06
pub(in crate::sidecar) fn disable_process_core_dumps() -> Result<(), SidecarFailure> {
    // SEM_NOGPFAULTERRORBOX는 UI 숨김만이 아니라 WER invocation을 끄며 child에 상속된다.
    let required_flags = SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX;
    unsafe { SetErrorMode(GetErrorMode() | required_flags) };
    if unsafe { GetErrorMode() } & required_flags != required_flags {
        return Err(SidecarFailure::startup());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 함수 이름: current_process_handle_is_live_and_zero_is_ambiguous()
    /// 기능: 실제 현재 process 조회와 invalid zero PID가 fail-closed 계약을 만족하는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/06
    #[test]
    fn current_process_handle_is_live_and_zero_is_ambiguous() {
        // 조회는 process handle만 사용하고 signal이나 credential 접근은 하지 않는다.
        assert!(runtime_process_exists(std::process::id()));
        assert!(runtime_process_exists(0));
    }
}
