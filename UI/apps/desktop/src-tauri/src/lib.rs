/// 함수 이름: run()
/// 기능: 최소 권한으로 Tauri 데스크톱 셸을 시작한다.
/// 인자: 없음
/// 반환값: 없음
/// 작성 날짜: 2026/08/12
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("Tauri 애플리케이션을 실행할 수 없습니다.");
}
