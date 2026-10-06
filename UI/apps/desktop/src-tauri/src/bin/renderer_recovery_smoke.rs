//! 주문 없는 격리 WebView2 renderer 복구 검증 진입점이다.

/// 함수 이름: main()
/// 기능: opt-in Windows 검증 앱만 실행한다.
/// 인자: 없음; 검증 모듈에서 출력 경로와 시나리오를 읽음
/// 반환값: 검증 결과에 따른 process exit
/// 작성 날짜: 2026/10/04
fn main() {
    #[cfg(target_os = "windows")]
    binance_auto_trader_lib::run_renderer_recovery_smoke();
    #[cfg(not(target_os = "windows"))]
    panic!("renderer-recovery-smoke requires Windows WebView2");
}
