//! 격리 chart fixture의 같은 입력·viewport 화면만 PNG로 보존하는 opt-in 검증 helper다.
use base64::Engine;
use std::io::Write;
use std::path::PathBuf;
use std::sync::mpsc::sync_channel;
use std::time::Duration;
use tauri::WebviewWindow;
use webview2_com::CallDevToolsProtocolMethodCompletedHandler;
use windows::core::w;

/// 함수 이름: capture_fixture()
/// 기능: 검증 전용 창의 viewport만 PNG로 저장하고 worker에서 완료를 최대 10초 기다린다.
/// 인자: window -> 격리 main WebView, output -> 해당 실행의 새 결과 디렉터리
/// 반환값: 유효한 PNG 저장 성공 여부; 원본 프로토콜 응답·오류는 기록하지 않음
/// 작성 날짜: 2026/10/04
pub(super) fn capture_fixture(window: &WebviewWindow, output: PathBuf) -> bool {
    let (sender, receiver) = sync_channel(1);
    let scheduled = window.with_webview(move |platform| {
        let Ok(webview) = (unsafe { platform.controller().CoreWebView2() }) else {
            let _ = sender.try_send(false);
            return;
        };
        let completion_sender = sender.clone();
        let completion = CallDevToolsProtocolMethodCompletedHandler::create(Box::new(
            move |result, response| {
                let saved = result.is_ok() && save_png(&output, &response);
                let _ = completion_sender.try_send(saved);
                Ok(())
            },
        ));
        let captured = unsafe {
            webview.CallDevToolsProtocolMethod(
                w!("Page.captureScreenshot"),
                w!("{\"format\":\"png\",\"captureBeyondViewport\":false,\"fromSurface\":true}"),
                &completion,
            )
        };
        if captured.is_err() {
            let _ = sender.try_send(false);
        }
    });
    scheduled.is_ok()
        && receiver
            .recv_timeout(Duration::from_secs(10))
            .unwrap_or(false)
}

/// 함수 이름: save_png()
/// 기능: CDP의 제한된 PNG 데이터만 decode해 해당 실행에 새 파일로 저장한다.
/// 인자: output -> 검증 결과 디렉터리, response -> screenshot 응답 JSON
/// 반환값: PNG signature와 쓰기 완료가 확인되면 true
/// 작성 날짜: 2026/10/04
fn save_png(output: &std::path::Path, response: &str) -> bool {
    if response.len() > 12 * 1024 * 1024 {
        return false;
    }
    let Ok(value) = serde_json::from_str::<serde_json::Value>(response) else {
        return false;
    };
    let Some(encoded) = value["data"].as_str() else {
        return false;
    };
    let Ok(bytes) = base64::engine::general_purpose::STANDARD.decode(encoded) else {
        return false;
    };
    if bytes.len() > 8 * 1024 * 1024 || !bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        return false;
    }
    std::fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(output.join("fixture.png"))
        .and_then(|mut file| {
            file.write_all(&bytes)?;
            file.sync_data()
        })
        .is_ok()
}
