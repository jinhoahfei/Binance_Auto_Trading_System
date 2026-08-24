/// 함수 이름: main()
/// 기능: renderer가 호출할 application command만 allow/deny permission으로 생성한다.
/// 인자: 없음
/// 반환값: 없음
/// 작성 날짜: 2026/08/24
fn main() {
    // Custom command default 공개를 제거하고 main-window capability가 선택할 exact allow 목록을 만든다.
    let application_manifest = tauri_build::AppManifest::new().commands(&[
        "take_backend_connection_descriptor",
        "choose_csv_export_directory",
        "await_backend_sidecar_exit",
        "arm_native_exit_intent_bridge",
        "arm_sidecar_exit_event_bridge",
    ]);
    let build_attributes = tauri_build::Attributes::new().app_manifest(application_manifest);

    tauri_build::try_build(build_attributes).expect("Tauri build metadata를 생성할 수 없습니다.");
}
