//! 명령행·경로·payload를 읽지 않고 Windows와 WebView2의 프로세스별 메모리를 관찰한다.
use super::Shared;
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::Instant;
use tauri::WebviewWindow;
use webview2_com::Microsoft::Web::WebView2::Win32::{
    ICoreWebView2Environment8, COREWEBVIEW2_PROCESS_KIND, COREWEBVIEW2_PROCESS_KIND_BROWSER,
    COREWEBVIEW2_PROCESS_KIND_GPU, COREWEBVIEW2_PROCESS_KIND_PPAPI_BROKER,
    COREWEBVIEW2_PROCESS_KIND_PPAPI_PLUGIN, COREWEBVIEW2_PROCESS_KIND_RENDERER,
    COREWEBVIEW2_PROCESS_KIND_SANDBOX_HELPER, COREWEBVIEW2_PROCESS_KIND_UTILITY,
};
use windows::core::Interface;
use windows_sys::Win32::Foundation::{CloseHandle, FILETIME};
use windows_sys::Win32::System::ProcessStatus::{
    K32GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS, PROCESS_MEMORY_COUNTERS_EX,
};
use windows_sys::Win32::System::Threading::{
    GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
};

const MAX_WEBVIEW_PROCESSES: u32 = 64;

/// 클래스 이름: WebviewProcessSnapshot
/// 기능: COM 객체 대신 제한된 PID·종류·수집 시점만 감시 worker와 공유한다.
/// 작성 날짜: 2026/10/04
#[derive(Clone)]
pub(super) struct WebviewProcessSnapshot {
    observed_at: Option<Instant>,
    status: &'static str,
    processes: Vec<(u32, &'static str)>,
}

impl Default for WebviewProcessSnapshot {
    /// 함수 이름: default()
    /// 기능: 아직 수집하지 않은 빈 프로세스 관찰을 생성한다.
    /// 인자: 없음
    /// 반환값: 초기 관찰 상태
    /// 작성 날짜: 2026/10/04
    fn default() -> Self {
        Self {
            observed_at: None,
            status: "not_sampled",
            processes: Vec::new(),
        }
    }
}

impl WebviewProcessSnapshot {
    /// 함수 이름: renderer_candidates()
    /// 기능: 최근 환경에서 확인한 renderer 후보와 표본 나이를 고정 필드로 반환한다.
    /// 인자: self -> 제한된 WebView2 환경 관찰
    /// 반환값: renderer PID 후보와 표본 나이·상태
    /// 작성 날짜: 2026/10/04
    pub(super) fn renderer_candidates(&self) -> Value {
        json!({
            "process_ids":self.processes.iter().filter(|(_, kind)| *kind == "renderer")
                .map(|(process_id, _)| *process_id).collect::<Vec<_>>(),
            "sample_age_ms":self.observed_at.map(|at| at.elapsed().as_millis() as u64),
            "status":self.status,
        })
    }
}

/// 함수 이름: process_kind_name()
/// 기능: WebView2 프로세스 종류를 고정된 진단 문자열로 변환한다.
/// 인자: kind -> WebView2가 제공한 종류
/// 반환값: 허용한 종류 또는 unknown
/// 작성 날짜: 2026/10/04
fn process_kind_name(kind: COREWEBVIEW2_PROCESS_KIND) -> &'static str {
    match kind {
        COREWEBVIEW2_PROCESS_KIND_BROWSER => "browser",
        COREWEBVIEW2_PROCESS_KIND_RENDERER => "renderer",
        COREWEBVIEW2_PROCESS_KIND_GPU => "gpu",
        COREWEBVIEW2_PROCESS_KIND_UTILITY => "utility",
        COREWEBVIEW2_PROCESS_KIND_SANDBOX_HELPER => "sandbox_helper",
        COREWEBVIEW2_PROCESS_KIND_PPAPI_PLUGIN => "ppapi_plugin",
        COREWEBVIEW2_PROCESS_KIND_PPAPI_BROKER => "ppapi_broker",
        _ => "unknown",
    }
}

/// 함수 이름: read_processes()
/// 기능: 현재 WebView2 환경에 속한 프로세스의 PID와 종류를 최대 64개 읽는다.
/// 인자: environment -> main 스레드의 WebView2 환경 인터페이스
/// 반환값: 시점과 상한 상태를 포함한 관찰 또는 COM 오류
/// 작성 날짜: 2026/10/04
fn read_processes(
    environment: ICoreWebView2Environment8,
) -> windows::core::Result<WebviewProcessSnapshot> {
    let collection = unsafe { environment.GetProcessInfos()? };
    let mut count = 0;
    unsafe { collection.Count(&mut count)? };
    let mut processes = Vec::with_capacity(count.min(MAX_WEBVIEW_PROCESSES) as usize);
    for index in 0..count.min(MAX_WEBVIEW_PROCESSES) {
        let information = unsafe { collection.GetValueAtIndex(index)? };
        let mut process_id = 0;
        let mut kind = COREWEBVIEW2_PROCESS_KIND_BROWSER;
        unsafe {
            information.ProcessId(&mut process_id)?;
            information.Kind(&mut kind)?;
        }
        if process_id > 0 {
            processes.push((process_id as u32, process_kind_name(kind)));
        }
    }
    Ok(WebviewProcessSnapshot {
        observed_at: Some(Instant::now()),
        status: if count > MAX_WEBVIEW_PROCESSES {
            "truncated"
        } else {
            "ok"
        },
        processes,
    })
}

/// 함수 이름: refresh_processes()
/// 기능: main 스레드에서 환경 PID를 갱신하고 COM 객체를 공유 상태에 남기지 않는다.
/// 인자: window -> main WebView 창, shared -> 제한된 프로세스 관찰을 보관할 상태
/// 반환값: 없음; 사용할 수 없는 환경은 고정 상태로 기록
/// 작성 날짜: 2026/10/04
pub(super) fn refresh_processes(window: &WebviewWindow, shared: Arc<Shared>) {
    let state = shared.clone();
    let scheduled = window.with_webview(move |webview| {
        let observation = webview
            .environment()
            .cast::<ICoreWebView2Environment8>()
            .and_then(read_processes)
            .unwrap_or_else(|_| WebviewProcessSnapshot {
                observed_at: Some(Instant::now()),
                status: "unavailable",
                processes: Vec::new(),
            });
        if let Ok(mut stored) = state.webview_processes.try_lock() {
            *stored = observation;
        }
    });
    if scheduled.is_err() {
        if let Ok(mut stored) = shared.webview_processes.try_lock() {
            *stored = WebviewProcessSnapshot {
                observed_at: Some(Instant::now()),
                status: "unavailable",
                processes: Vec::new(),
            };
        }
    }
}

/// 함수 이름: filetime_ticks()
/// 기능: Windows FILETIME의 두 DWORD를 100나노초 단위 값으로 결합한다.
/// 인자: time -> Windows 시간 구조체
/// 반환값: 손실 없는 64비트 시간 값
/// 작성 날짜: 2026/10/04
fn filetime_ticks(time: FILETIME) -> u64 {
    ((time.dwHighDateTime as u64) << 32) | time.dwLowDateTime as u64
}

/// 함수 이름: process_memory()
/// 기능: 제한된 조회 권한으로 작업 집합·전용 commit·생성 시각·CPU 시간을 읽는다.
/// 인자: process_id -> 현재 관찰할 PID, kind -> 고정 프로세스 종류
/// 반환값: byte 단위 관찰 또는 조회 불가 상태; 모든 실제 process handle을 닫음
/// 작성 날짜: 2026/10/04
fn process_memory(process_id: u32, kind: &'static str) -> Value {
    let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, process_id) };
    if handle.is_null() {
        return json!({"process_id":process_id,"kind":kind,"status":"unavailable"});
    }
    let mut memory: PROCESS_MEMORY_COUNTERS_EX = unsafe { std::mem::zeroed() };
    memory.cb = std::mem::size_of::<PROCESS_MEMORY_COUNTERS_EX>() as u32;
    let memory_available = unsafe {
        K32GetProcessMemoryInfo(
            handle,
            &mut memory as *mut _ as *mut PROCESS_MEMORY_COUNTERS,
            memory.cb,
        ) != 0
    };
    let mut created = FILETIME::default();
    let mut exited = FILETIME::default();
    let mut kernel = FILETIME::default();
    let mut user = FILETIME::default();
    let times_available =
        unsafe { GetProcessTimes(handle, &mut created, &mut exited, &mut kernel, &mut user) != 0 };
    unsafe { CloseHandle(handle) };
    if !memory_available {
        return json!({"process_id":process_id,"kind":kind,"status":"unavailable"});
    }
    json!({
        "process_id":process_id, "kind":kind, "status":"ok",
        "working_set_bytes":memory.WorkingSetSize,
        "peak_working_set_bytes":memory.PeakWorkingSetSize,
        "private_commit_bytes":memory.PrivateUsage,
        "creation_filetime_100ns":times_available.then(|| filetime_ticks(created)),
        "cpu_total_ms":times_available.then(|| (filetime_ticks(kernel) + filetime_ticks(user)) / 10_000),
    })
}

/// 함수 이름: collect_resources()
/// 기능: COM 없는 감시 worker에서 native·backend·WebView2별 메모리를 구분해 수집한다.
/// 인자: snapshot -> 마지막 환경 프로세스 관찰, backend_pid -> 확인한 backend PID
/// 반환값: 수집 범위와 PID 표본 나이를 포함한 고정 JSON
/// 작성 날짜: 2026/10/04
pub(super) fn collect_resources(
    snapshot: Option<&WebviewProcessSnapshot>,
    backend_pid: Option<u32>,
) -> Value {
    let processes = snapshot
        .map(|value| {
            value
                .processes
                .iter()
                .map(|(process_id, kind)| process_memory(*process_id, kind))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    json!({
        "scope":"native_backend_webview2_environment", "units":"bytes",
        "native":process_memory(std::process::id(), "native"),
        "backend":backend_pid.map(|process_id| process_memory(process_id, "backend")),
        "webview2":{
            "status":snapshot.map(|value| value.status).unwrap_or("unavailable"),
            "process_list_age_ms":snapshot.and_then(|value| value.observed_at).map(|at| at.elapsed().as_millis() as u64),
            "processes":processes,
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 함수 이름: reads_native_memory_in_bytes_without_process_text()
    /// 기능: 실제 Windows native 메모리가 byte 단위로 조회되고 경로·명령행이 없는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음; 조회 또는 privacy 계약 실패 시 테스트 실패
    /// 작성 날짜: 2026/10/04
    #[test]
    fn reads_native_memory_in_bytes_without_process_text() {
        let result = process_memory(std::process::id(), "native");
        assert_eq!(result["status"], "ok");
        assert!(result["working_set_bytes"].as_u64().unwrap() > 0);
        assert!(result["private_commit_bytes"].as_u64().unwrap() > 0);
        assert!(
            result["peak_working_set_bytes"].as_u64().unwrap()
                >= result["working_set_bytes"].as_u64().unwrap()
        );
        assert!(result["creation_filetime_100ns"].as_u64().unwrap() > 0);
        assert_eq!(result.as_object().unwrap().len(), 8);
    }

    /// 함수 이름: unavailable_process_is_explicit_and_candidates_are_scoped()
    /// 기능: 사라진 PID는 0으로 오인하지 않고 renderer 후보는 환경 관찰로 한정한다.
    /// 인자: 없음
    /// 반환값: 없음; 조회 실패 또는 후보 범위 계약 실패 시 테스트 실패
    /// 작성 날짜: 2026/10/04
    #[test]
    fn unavailable_process_is_explicit_and_candidates_are_scoped() {
        assert_eq!(process_memory(0, "renderer")["status"], "unavailable");
        let snapshot = WebviewProcessSnapshot {
            observed_at: Some(Instant::now()),
            status: "ok",
            processes: vec![
                (20, "browser"),
                (21, "renderer"),
                (22, "gpu"),
                (23, "renderer"),
            ],
        };
        assert_eq!(
            snapshot.renderer_candidates()["process_ids"],
            json!([21, 23])
        );
        assert_eq!(process_kind_name(COREWEBVIEW2_PROCESS_KIND(99)), "unknown");
    }
}
