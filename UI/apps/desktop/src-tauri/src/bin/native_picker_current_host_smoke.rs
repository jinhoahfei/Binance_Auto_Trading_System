#[cfg(not(target_os = "macos"))]
compile_error!("native-picker-current-host-smoke supports only the current macOS host");

#[cfg(target_os = "macos")]
mod macos_smoke {
    use binance_auto_trader_lib::choose_csv_export_directory_for_current_host_smoke;
    use std::path::Path;
    use std::time::Duration;
    use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder};

    const CURRENT_HOST_INTERACTION_TIMEOUT: Duration = Duration::from_secs(60);
    const EXIT_SUCCESS: i32 = 0;
    const EXIT_CONTRACT_FAILURE: i32 = 2;
    const EXIT_HOST_FAILURE: i32 = 3;
    const EXIT_INTERACTION_TIMEOUT: i32 = 4;

    /// Current-host smoke가 실제 picker에서 검증할 두 terminal 결과이다.
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    enum ExpectedPickerOutcome {
        Selected,
        Cancelled,
    }

    /// 함수 이름: parse_expected_outcome()
    /// 기능: smoke 실행 인자를 selected 또는 cancelled 한 값으로 제한한다.
    /// 인자: argument -> 첫 번째 CLI 인자
    /// 반환값: 검증할 picker terminal 결과 또는 고정 오류 문구
    /// 작성 날짜: 2026/08/29
    fn parse_expected_outcome(
        argument: Option<&str>,
    ) -> Result<ExpectedPickerOutcome, &'static str> {
        match argument {
            Some("selected") => Ok(ExpectedPickerOutcome::Selected),
            Some("cancelled") => Ok(ExpectedPickerOutcome::Cancelled),
            _ => Err("usage: native-picker-current-host-smoke <selected|cancelled>"),
        }
    }

    /// 함수 이름: picker_result_matches_expected_outcome()
    /// 기능: path를 출력하지 않고 production picker 결과의 absolute UTF-8 또는 null 계약을 검증한다.
    /// 인자: outcome -> 실행 시 선택한 기대 결과, picker_result -> production command 결과
    /// 반환값: 기대 결과와 native 반환 계약의 일치 여부
    /// 작성 날짜: 2026/08/29
    fn picker_result_matches_expected_outcome(
        outcome: ExpectedPickerOutcome,
        picker_result: &Option<String>,
    ) -> bool {
        match (outcome, picker_result) {
            (ExpectedPickerOutcome::Selected, Some(directory)) => {
                Path::new(directory).is_absolute()
            }
            (ExpectedPickerOutcome::Cancelled, None) => true,
            _ => false,
        }
    }

    /// 함수 이름: schedule_interaction_timeout()
    /// 기능: operator가 native picker에 응답하지 않은 current-host run을 60초 뒤 fail closed한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/29
    fn schedule_interaction_timeout() {
        std::thread::spawn(move || {
            std::thread::sleep(CURRENT_HOST_INTERACTION_TIMEOUT);

            // 별도 smoke process에는 backend/order가 없으므로 미응답 panel을 process와 함께 bounded 종료한다.
            eprintln!("native-picker-current-host: FAIL stage=operator-interaction-timeout");
            std::process::exit(EXIT_INTERACTION_TIMEOUT);
        });
    }

    /// 함수 이름: start_picker_validation_worker()
    /// 기능: production picker command를 worker에서 실행하고 path-free terminal evidence만 출력한다.
    /// 인자: app_handle -> dialog plugin과 exit를 소유한 application, outcome -> 기대 terminal 결과
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/29
    fn start_picker_validation_worker(app_handle: AppHandle, outcome: ExpectedPickerOutcome) {
        std::thread::spawn(move || {
            // Production async command와 같은 worker 경계에서 blocking native picker를 기다린다.
            let picker_result = tauri::async_runtime::block_on(
                choose_csv_export_directory_for_current_host_smoke(app_handle.clone()),
            );
            match picker_result {
                Ok(selected_directory)
                    if picker_result_matches_expected_outcome(outcome, &selected_directory) =>
                {
                    let outcome_label = match outcome {
                        ExpectedPickerOutcome::Selected => "selected-absolute-utf8",
                        ExpectedPickerOutcome::Cancelled => "cancelled-null",
                    };
                    println!(
                        "native-picker-current-host: PASS outcome={outcome_label} source=tauri-dialog-plugin"
                    );
                    app_handle.exit(EXIT_SUCCESS);
                }
                Ok(_) => {
                    eprintln!("native-picker-current-host: FAIL stage=picker-result-contract");
                    app_handle.exit(EXIT_CONTRACT_FAILURE);
                }
                Err(failure_code) => {
                    eprintln!(
                        "native-picker-current-host: FAIL stage=production-command code={failure_code}"
                    );
                    app_handle.exit(EXIT_CONTRACT_FAILURE);
                }
            }
        });
    }

    /// 함수 이름: run()
    /// 기능: backend와 network 없이 최소 Tauri window/plugin에서 production directory picker를 실제 실행한다.
    /// 인자: 없음
    /// 반환값: process exit code
    /// 작성 날짜: 2026/08/29
    pub fn run() -> i32 {
        let expected_outcome = match parse_expected_outcome(std::env::args().nth(1).as_deref()) {
            Ok(expected_outcome) => expected_outcome,
            Err(usage) => {
                eprintln!("{usage}");
                return EXIT_CONTRACT_FAILURE;
            }
        };

        // 별도 최소 Builder는 production sidecar/setup을 실행하지 않고 dialog plugin과 host window만 만든다.
        let application_result = tauri::Builder::default()
            .plugin(tauri_plugin_dialog::init())
            .setup(move |application| {
                let host_window = WebviewWindowBuilder::new(
                    application,
                    "native-picker-smoke-host",
                    WebviewUrl::External("about:blank".parse()?),
                )
                .title("Binance Auto Trader Native Picker Smoke")
                .inner_size(480.0, 180.0)
                .resizable(false)
                .build()?;
                let app_handle = host_window.app_handle().clone();

                // Operator interaction은 actual native 선택/취소를 증명하며 timeout은 무기한 GUI 대기를 막는다.
                schedule_interaction_timeout();
                start_picker_validation_worker(app_handle, expected_outcome);
                Ok(())
            })
            .build(tauri::generate_context!());
        let application = match application_result {
            Ok(application) => application,
            Err(_) => {
                eprintln!("native-picker-current-host: FAIL stage=tauri-build");
                return EXIT_HOST_FAILURE;
            }
        };

        application.run_return(|_, _| {})
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        /// 함수 이름: selected_result_requires_absolute_directory()
        /// 기능: selected smoke가 relative 또는 null 값을 성공 근거로 수락하지 않는지 검증한다.
        /// 인자: 없음
        /// 반환값: 없음
        /// 작성 날짜: 2026/08/29
        #[test]
        fn selected_result_requires_absolute_directory() {
            // Selected 결과는 absolute UTF-8만 허용하고 relative 또는 explicit cancel은 거부한다.
            assert!(picker_result_matches_expected_outcome(
                ExpectedPickerOutcome::Selected,
                &Some("/private/tmp".to_owned()),
            ));
            assert!(!picker_result_matches_expected_outcome(
                ExpectedPickerOutcome::Selected,
                &Some("relative/export".to_owned()),
            ));
            assert!(!picker_result_matches_expected_outcome(
                ExpectedPickerOutcome::Selected,
                &None,
            ));
        }

        /// 함수 이름: cancelled_result_requires_explicit_null()
        /// 기능: cancelled smoke가 string 값을 취소 성공으로 잘못 수락하지 않는지 검증한다.
        /// 인자: 없음
        /// 반환값: 없음
        /// 작성 날짜: 2026/08/29
        #[test]
        fn cancelled_result_requires_explicit_null() {
            // Cancel 결과는 explicit null만 허용하고 어떤 directory string도 성공으로 완화하지 않는다.
            assert!(picker_result_matches_expected_outcome(
                ExpectedPickerOutcome::Cancelled,
                &None,
            ));
            assert!(!picker_result_matches_expected_outcome(
                ExpectedPickerOutcome::Cancelled,
                &Some("/private/tmp".to_owned()),
            ));
        }
    }
}

/// 함수 이름: main()
/// 기능: macOS current-host native picker smoke를 실행하고 정확한 process exit code를 반환한다.
/// 인자: 없음
/// 반환값: 없음
/// 작성 날짜: 2026/08/29
#[cfg(target_os = "macos")]
fn main() {
    std::process::exit(macos_smoke::run());
}
