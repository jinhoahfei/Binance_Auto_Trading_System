/// 함수 이름: main()
/// 기능: renderer가 호출할 application command만 allow/deny permission으로 생성한다.
/// 인자: 없음
/// 반환값: 없음
/// 작성 날짜: 2026/08/24
fn main() {
    // Developer ID release에서는 package_sidecar.sh가 clean HEAD를 검증한 뒤 이 값을 전달한다.
    // 일반 local build도 가능하게 하되 verifier가 절대 승인하지 않는 명시 marker로 분리한다.
    println!("cargo:rerun-if-env-changed=PHASE12_RELEASE_COMMIT");
    let release_commit =
        std::env::var("PHASE12_RELEASE_COMMIT").unwrap_or_else(|_| "UNVERIFIED".to_owned());
    if release_commit != "UNVERIFIED"
        && (release_commit.len() != 40
            || !release_commit
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)))
    {
        panic!("PHASE12_RELEASE_COMMIT must be a canonical lowercase commit id");
    }
    println!(
        "cargo:rustc-env=BINANCE_AUTO_RELEASE_PROVENANCE=BINANCE_AUTO_RELEASE_COMMIT={release_commit}"
    );

    // Custom command default 공개를 제거하고 main-window capability가 선택할 exact allow 목록을 만든다.
    let application_manifest = tauri_build::AppManifest::new().commands(&[
        "get_backend_connection_descriptor",
        "record_chart_diagnostics",
        "record_backend_connection_diagnostics",
        "record_renderer_heartbeat",
        "record_background_soak_summary",
        "get_background_soak_descriptor",
        "choose_csv_export_directory",
        "await_backend_sidecar_exit",
        "arm_native_exit_intent_bridge",
        "arm_sidecar_exit_event_bridge",
    ]);
    let build_attributes = tauri_build::Attributes::new().app_manifest(application_manifest);

    tauri_build::try_build(build_attributes).expect("Tauri build metadata를 생성할 수 없습니다.");
}
