//! 현재 앱 실행에 귀속되는 OS 사건만 정규화한다. 원문 메시지는 파일로 내보내지 않는다.
use serde_json::{json, Value};


/// 함수 이름: collect()
/// 기능: 지원 OS에서 대상 PID의 공개 로그 증거를 제한된 시간·크기로 수집한다.
/// 인자: native_pid -> 앱 PID, backend_pid -> 선택적 backend PID
/// 반환값: 수집·정규화한 증거 또는 실패/지원 불가 상태의 JSON
/// 작성 날짜: 2026/09/17
#[cfg(target_os = "macos")]
pub fn collect(native_pid: u32, backend_pid: Option<u32>) -> Value {
    use std::{
        io::Read,
        os::fd::AsRawFd,
        process::{Command, Stdio},
        thread,
        time::{Duration, Instant},
    };
    let predicate = format!("(processIdentifier == {native_pid} OR processIdentifier == {} OR process == \"runningboardd\" OR subsystem BEGINSWITH \"com.apple.WebKit\") AND (processIdentifier == {native_pid} OR eventMessage CONTAINS \"{native_pid}\" OR eventMessage CONTAINS \"com.binance-auto.trader\")", backend_pid.unwrap_or(native_pid));
    let mut child = match Command::new("/usr/bin/log")
        .args([
            "show",
            "--style",
            "json",
            "--last",
            "5m",
            "--info",
            "--predicate",
            &predicate,
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        Err(_) => return json!({"status":"spawn_failed"}),
    };
    let mut out = child.stdout.take().unwrap();
    let mut err = child.stderr.take().unwrap();
    let nonblocking = unsafe {
        libc::fcntl(out.as_raw_fd(), libc::F_SETFL, libc::O_NONBLOCK) >= 0
            && libc::fcntl(err.as_raw_fd(), libc::F_SETFL, libc::O_NONBLOCK) >= 0
    };
    if !nonblocking {
        let _ = child.kill();
        let _ = child.wait();
        return json!({"status":"io_setup_failed"});
    }
    let began = Instant::now();
    let mut bytes = Vec::new();
    let mut errors = Vec::new();
    let mut buffer = [0u8; 8192];
    let mut status = "available";
    loop {
        if began.elapsed() >= Duration::from_secs(10) {
            status = "timeout";
            let _ = child.kill();
            break;
        }
        let mut progress = false;
        match out.read(&mut buffer) {
            Ok(n) if n > 0 => {
                if bytes.len() + n > 2 * 1024 * 1024 {
                    status = "size_limit";
                    let _ = child.kill();
                    break;
                }
                bytes.extend_from_slice(&buffer[..n]);
                progress = true;
            }
            _ => {}
        }
        match err.read(&mut buffer) {
            Ok(n) if n > 0 => {
                if errors.len() < 8192 {
                    errors.extend_from_slice(&buffer[..n]);
                }
            }
            _ => {}
        }
        if bytes.len() > 2 * 1024 * 1024 {
            status = "size_limit";
            let _ = child.kill();
            break;
        }
        if !progress && matches!(child.try_wait(), Ok(Some(_))) {
            break;
        }
        if !progress {
            thread::sleep(Duration::from_millis(25));
        }
    }
    let result = child.wait();
    let error_text = String::from_utf8_lossy(&errors).to_lowercase();
    if error_text.contains("sandbox")
        || error_text.contains("permission")
        || error_text.contains("not permitted")
    {
        status = "access_denied";
    } else if result.is_ok_and(|r| !r.success()) && status == "available" {
        status = "query_failed";
    }
    if status != "available" {
        return json!({"status":status,"elapsed_ms":began.elapsed().as_millis() as u64});
    }
    let records: Vec<Value> = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(_) => return json!({"status":"parse_failed"}),
    };
    let mut result = normalize(&records, native_pid, backend_pid);
    result["elapsed_ms"] = json!(began.elapsed().as_millis() as u64);
    result["bytes_read"] = json!(bytes.len());
    result
}


/// 함수 이름: collect()
/// 기능: 지원하지 않는 OS에서는 로그 수집을 시도하지 않고 명시적 지원 불가 상태를 반환한다.
/// 인자: _native_pid -> 사용하지 않는 앱 PID, _backend_pid -> 사용하지 않는 backend PID
/// 반환값: 수집·정규화한 증거 또는 실패/지원 불가 상태의 JSON
/// 작성 날짜: 2026/09/17
#[cfg(not(target_os = "macos"))]
pub fn collect(_native_pid: u32, _backend_pid: Option<u32>) -> Value {
    json!({"status":"unsupported"})
}


/// 함수 이름: owns_message()
/// 기능: PID 일부가 아닌 완전한 대상 식별자가 로그 메시지에 있는지 검사한다.
/// 인자: message -> 로그 메시지, pid -> 대상 프로세스 ID
/// 반환값: 대상 PID가 명시되어 있으면 true
/// 작성 날짜: 2026/09/17
fn owns_message(message: &str, pid: u32) -> bool {
    // PID 일부 일치나 다른 WebContent process를 당해 앱의 증거로 삼지 않는다.
    [
        format!("pid={pid}"),
        format!("pid {pid}"),
        format!("[{pid}]"),
        format!(":{pid}]"),
    ]
    .iter()
    .any(|needle| {
        message.match_indices(needle).any(|(i, _)| {
            message
                .as_bytes()
                .get(i + needle.len())
                .is_none_or(|c| !c.is_ascii_digit())
        })
    })
}


/// 함수 이름: normalize()
/// 기능: 신뢰한 OS 출처와 정확한 대상 PID의 완료된 중지·제한 이벤트만 증거로 추출한다.
/// 인자: records -> 공개 로그 행, native_pid -> 앱 PID, backend_pid -> 선택적 backend PID
/// 반환값: 원문 자유 문자열을 제외한 정규화 증거 JSON
/// 작성 날짜: 2026/09/17
pub fn normalize(records: &[Value], native_pid: u32, backend_pid: Option<u32>) -> Value {
    // 정확한 대상 PID와 신뢰한 OS 출처의 기록만 분류한다.
    let mut events = Vec::new();
    let mut unclassified = 0;
    for row in records {
        let message = row["eventMessage"].as_str().unwrap_or("");
        let target = if owns_message(message, native_pid) {
            Some(native_pid)
        } else {
            backend_pid.filter(|pid| owns_message(message, *pid))
        };
        let Some(candidate_target) = target else {
            continue;
        };
        let producer = row["processImagePath"].as_str().unwrap_or("");
        let subsystem = row["subsystem"].as_str().unwrap_or("");
        if !((producer.starts_with("/System/Library/") || producer.starts_with("/usr/libexec/"))
            && (producer.ends_with("/runningboardd")
                || producer.ends_with("/powerd")
                || subsystem.starts_with("com.apple.WebKit")))
        {
            unclassified += 1;
            continue;
        }

        // 부정·요청·예정 표현을 걸러 완료된 상태를 직접 증거로 오인하지 않는다.
        let lower = message.to_lowercase();
        let negated = [
            "not ",
            "no ",
            "prevent",
            "disable",
            "requested",
            "request to",
            "assertion",
        ]
        .iter()
        .any(|word| lower.contains(word));
        // 공개 로그에서 대상이 명시된 완료 상태만 인정한다. "suspending" 같은 계획/요청은 제외한다.
        let action = if negated {
            None
        } else {
            [
                ("suspended process pid=", "process_suspend_reported"),
                ("throttled process pid=", "process_throttle_reported"),
                ("app nap state: enabled for pid=", "app_nap_reported"),
            ]
            .iter()
            .find_map(|(prefix, kind)| {
                let (_, suffix) = lower.split_once(prefix)?;
                let digits: String = suffix.chars().take_while(|c| c.is_ascii_digit()).collect();
                let target = digits.parse::<u32>().ok()?;
                (target == candidate_target).then_some((*kind, target))
            })
        };
        if let Some((kind, target)) = action {
            // OS timestamp와 PID 이외의 외부 자유 문자열은 출력하지 않는다.
            let timestamp = row["timestamp"].as_str().filter(|s| {
                s.len() <= 40
                    && s.bytes()
                        .all(|b| b.is_ascii_digit() || b"-+: .TZ".contains(&b))
            });
            if let Some(timestamp) = timestamp {
                events.push(json!({"event":kind,"target_pid":target,"timestamp":timestamp,"source":"macos_unified_log"}));
            }
        } else {
            unclassified += 1;
        }
    }
    json!({"status":if records.is_empty(){"no_records"}else{"available"},"events":events,"unclassified_count":unclassified,
        "direct_evidence":!events.is_empty(), "scope":"explicit_target_pid_only", "webcontent_attribution":"not_available"})
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 함수 이름: visibility_and_policy_are_not_suspension_evidence()
    /// 기능: 창 가시성과 요청 정책만으로 OS 중지를 판정하지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음; 추측성 증거를 수락하면 테스트 실패
    /// 작성 날짜: 2026/09/17
    #[test]
    fn visibility_and_policy_are_not_suspension_evidence() {
        let rows = vec![
            json!({"eventMessage":"pid=42 inactiveSchedulingPolicy=suspend SECRET_CANARY","timestamp":"2026-09-16 23:16:08+0900"}),
        ];
        let result = normalize(&rows, 42, None);
        assert_eq!(result["direct_evidence"], false);
        assert!(!result.to_string().contains("SECRET_CANARY"));
    }

    /// 함수 이름: negative_or_application_messages_are_not_direct_os_proof()
    /// 기능: 부정·예정 표현과 앱 자체 메시지를 직접 OS 증거로 수락하지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음; 비직접 증거를 수락하면 테스트 실패
    /// 작성 날짜: 2026/09/17
    #[test]
    fn negative_or_application_messages_are_not_direct_os_proof() {
        for message in [
            "Not suspended process pid=42",
            "Suspended process pid=999 due to pid=42",
            "Suspending process pid=42",
            "Prevent throttling process pid=42",
            "Requested suspending process pid=42",
        ] {
            let rows = vec![
                json!({"processImagePath":"/usr/libexec/runningboardd","eventMessage":message,"timestamp":"2026-09-16 23:16:08+0900"}),
            ];
            assert_eq!(normalize(&rows, 42, None)["direct_evidence"], false);
        }
        let rows = vec![
            json!({"processImagePath":"/Applications/Other.app","eventMessage":"Suspending process pid=42","timestamp":"2026-09-16 23:16:08+0900"}),
        ];
        assert_eq!(normalize(&rows, 42, None)["direct_evidence"], false);
    }

    /// 함수 이름: target_identity_and_direct_event_are_required()
    /// 기능: PID의 부분 일치를 거부하고 정확한 대상의 직접 이벤트만 수락하는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음; 대상 식별 계약을 어기면 테스트 실패
    /// 작성 날짜: 2026/09/17
    #[test]
    fn target_identity_and_direct_event_are_required() {
        let rows = vec![
            json!({"processImagePath":"/usr/libexec/runningboardd","eventMessage":"Suspended process pid=420","timestamp":"2026-09-16 23:16:08+0900"}),
        ];
        assert_eq!(normalize(&rows, 42, None)["direct_evidence"], false);
        assert_eq!(normalize(&rows, 420, None)["direct_evidence"], true);
    }
}
