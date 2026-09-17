use serde::Serialize;
use std::path::PathBuf;
use tauri::AppHandle;
use tauri_plugin_dialog::DialogExt;

/// Tauri command가 renderer에 반환하는 경로·secret 비노출 directory picker 실패이다.
#[derive(Serialize)]
pub struct CsvDirectoryPickerFailure {
    pub code: &'static str,
    pub message: &'static str,
}

impl CsvDirectoryPickerFailure {
    /// 함수 이름: invalid_selected_path()
    /// 기능: 폴더 선택 과정에서 문제(native picker 결과를 안전한 absolute UTF-8 path로 변환할 수 없을 때)발생 시 프론트엔드에 반환할 에러 구조체.
    /// 인자: 없음
    /// 반환값: 선택 경로를 담지 않는 typed failure
    /// 작성 날짜: 2026/08/23
    fn invalid_selected_path() -> Self {
        Self {
            code: "CSV_EXPORT_DIRECTORY_INVALID",
            message: "The selected CSV export directory is invalid.",
        }
    }
}


/// 함수 이름: absolute_utf8_directory()
/// 기능: OS의 PathBuf를 프론트엔드와 backend가 사용할 수 있는 String으로 변환
/// 인자: directory_path -> native picker가 선택한 filesystem path
/// 반환값: absolute UTF-8 path 또는 경로를 노출하지 않는 typed failure
/// 작성 날짜: 2026/08/23
fn absolute_utf8_directory(directory_path: PathBuf) -> Result<String, CsvDirectoryPickerFailure> {
    if !directory_path.is_absolute() {
        return Err(CsvDirectoryPickerFailure::invalid_selected_path());
    }

    directory_path
        .into_os_string()
        .into_string()
        .map_err(|_| CsvDirectoryPickerFailure::invalid_selected_path())
}


/// 함수 이름: choose_csv_export_directory()
/// 기능: OS 폴더 picker를 열고 CSV export에 사용할 absolute UTF-8 directory만 renderer에 반환한다.
/// 인자: app -> dialog plugin을 소유한 Tauri application handle
/// 반환값: 선택 directory, 취소 null 또는 typed path-free failure
/// 작성 날짜: 2026/08/23
#[tauri::command]
pub async fn choose_csv_export_directory(
    app: AppHandle,
) -> Result<Option<String>, CsvDirectoryPickerFailure> {
    // Tauri의 async command worker에서만 공식 blocking picker를 호출해 main thread를 차단하지 않는다.
    let selected_file_path = app.dialog().file().blocking_pick_folder();
    let Some(selected_file_path) = selected_file_path else {
        return Ok(None); // None은 실패가 아니라 사용자가 picker를 취소한 결과이다.
    };

    // URI variant도 공식 into_path 변환을 거친 후 filesystem path 불변식을 확인한다.
    let directory_path = selected_file_path
        .into_path()
        .map_err(|_| CsvDirectoryPickerFailure::invalid_selected_path())?;

    absolute_utf8_directory(directory_path).map(Some)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 함수 이름: absolute_utf8_directory_accepts_native_absolute_path()
    /// 기능: absolute UTF-8 directory가 문자열 손실 없이 반환되는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/23
    #[test]
    fn absolute_utf8_directory_accepts_native_absolute_path() {
        let absolute_path = std::env::temp_dir().join("binance-auto-csv-export");
        let expected_path = absolute_path.to_string_lossy().into_owned();
        let actual_path = match absolute_utf8_directory(absolute_path) {
            Ok(path) => path,
            Err(_) => panic!("absolute UTF-8 path must be accepted"),
        };

        assert_eq!(actual_path, expected_path);
    }

    /// 함수 이름: absolute_utf8_directory_rejects_relative_path_without_reflection()
    /// 기능: relative directory가 typed failure로 거부되고 경로 값을 노출하지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/23
    #[test]
    fn absolute_utf8_directory_rejects_relative_path_without_reflection() {
        let failure = match absolute_utf8_directory(PathBuf::from("private/export")) {
            Ok(_) => panic!("relative path must be rejected"),
            Err(failure) => failure,
        };

        assert_eq!(failure.code, "CSV_EXPORT_DIRECTORY_INVALID");
        assert!(!failure.message.contains("private/export"));
    }

    /// 함수 이름: absolute_utf8_directory_rejects_non_utf8_path_without_reflection()
    /// 기능: Unix non-UTF-8 directory가 renderer 문자열로 손실 변환되지 않도록 거부하는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/08/23
    #[cfg(unix)]
    #[test]
    fn absolute_utf8_directory_rejects_non_utf8_path_without_reflection() {
        use std::ffi::OsString;
        use std::os::unix::ffi::OsStringExt;

        // Leading slash로 absolute path를 유지하고 invalid UTF-8 byte 하나만 검증한다.
        let non_utf8_path = PathBuf::from(OsString::from_vec(vec![b'/', 0xff]));
        let failure = match absolute_utf8_directory(non_utf8_path) {
            Ok(_) => panic!("non-UTF-8 path must be rejected"),
            Err(failure) => failure,
        };

        assert_eq!(failure.code, "CSV_EXPORT_DIRECTORY_INVALID");
    }
}
