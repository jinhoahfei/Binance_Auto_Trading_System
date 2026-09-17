//! Windows development sidecar를 credential, filesystem, process와 IPC adapter로 조합한다.

mod credentials;
mod files;
mod ipc;
mod process;

use super::*;
use std::fs;
use std::os::windows::fs::MetadataExt;
use std::os::windows::process::CommandExt;
use std::process::{Command, Stdio};

pub(super) use files::open_locked_runtime_ownership_artifact;
pub(super) use ipc::{read_ready_descriptor, write_closed_ack};
pub(super) use process::runtime_process_exists;


/// 함수 이름: resolve_app_data_directory()
/// 기능: 공통 lifecycle에 현재 사용자 LocalAppData 경로를 제공한다.
/// 인자: _app_handle -> 공통 adapter signature를 유지할 native owner
/// 반환값: native known-folder application path
/// 작성 날짜: 2026/09/06
pub(super) fn resolve_app_data_directory(
    _app_handle: &AppHandle,
) -> Result<PathBuf, SidecarFailure> {
    files::resolve_app_data_directory() // Windows Roaming 기본값을 사용하지 않는다.
}


/// 함수 이름: ensure_private_app_data_directory()
/// 기능: Windows per-user no-reparse path validation을 공통 lifecycle에 제공한다.
/// 인자: directory -> 준비할 app-data 경로
/// 반환값: path 경계 validation 결과
/// 작성 날짜: 2026/09/06
pub(super) fn ensure_private_app_data_directory(directory: &Path) -> Result<(), SidecarFailure> {
    files::ensure_private_app_data_directory(directory) // ACL은 native known-folder의 per-user 상속을 유지한다.
}


/// 함수 이름: platform_disable_process_core_dumps()
/// 기능: credential을 조회하기 전에 Windows 자동 crash-report policy를 적용한다.
/// 인자: 없음
/// 반환값: native 설정 결과
/// 작성 날짜: 2026/09/06
pub(super) fn platform_disable_process_core_dumps() -> Result<(), SidecarFailure> {
    process::disable_process_core_dumps() // Parent 설정은 child process에도 상속된다.
}


/// 함수 이름: development_python_executable()
/// 기능: source checkout의 x64 venv interpreter만 개발 sidecar 실행 대상으로 선택한다.
/// 인자: 없음
/// 반환값: repository backend venv의 regular Python executable
/// 작성 날짜: 2026/09/06
fn development_python_executable() -> Result<PathBuf, SidecarFailure> {
    let manifest_directory = Path::new(env!("CARGO_MANIFEST_DIR"));
    let repository_directory = manifest_directory
        .ancestors()
        .nth(4)
        .ok_or_else(SidecarFailure::startup)?;
    let python_path = repository_directory.join("backend/.venv/Scripts/python.exe");
    let metadata = fs::symlink_metadata(&python_path).map_err(|_| SidecarFailure::startup())?;
    if !metadata.is_file()
        || metadata.file_attributes()
            & windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT
            != 0
    {
        return Err(SidecarFailure::startup());
    }
    Ok(python_path) // Python -I와 editable install이 source module을 선택한다.
}


/// 함수 이름: platform_prepare_backend_sidecar()
/// 기능: Windows source interpreter의 stdio에 최초 bootstrap을 전달하고 공통 ready lifecycle로 소유권을 옮긴다.
/// 인자: app_handle -> native application owner
/// 반환값: ready 또는 ambiguous child owner, pre-runtime 실패
/// 작성 날짜: 2026/09/06
pub(super) fn platform_prepare_backend_sidecar(
    app_handle: &AppHandle,
) -> Result<BackendSidecarPreparation, SidecarFailure> {
    // Packaged Windows origin은 Session 6 실제 WebView 확인 전까지 credentials 조회보다 먼저 차단한다.
    if !cfg!(all(debug_assertions, target_arch = "x86_64")) || !tauri::is_dev() {
        return Err(SidecarFailure::startup());
    }
    let python_path = development_python_executable()?;
    let app_data_directory = resolve_app_data_directory(app_handle)?;
    ensure_private_app_data_directory(&app_data_directory)?;
    let history_path = build_history_path(&app_data_directory)?;
    let credentials = credentials::read_credentials()?;
    let mut token = Zeroizing::new(generate_session_token()?);
    let configuration = SidecarBootstrapConfiguration {
        schema_version: BACKEND_SCHEMA_VERSION,
        allowed_origin: select_allowed_origin(true),
        history_path: &history_path,
        api_key: &credentials.api_key,
        api_secret: &credentials.api_secret,
        allow_testnet_orders: false,
        max_notional: None,
    };
    let bootstrap_payload = framed::serialize_bootstrap_frame(&token, &configuration)?;

    // Python -I는 PYTHONPATH/user-site를 차단하며 secret 환경은 전부 제거한다.
    let mut command = Command::new(python_path);
    command
        .args(["-I", "-m", "binance_auto_trader.sidecar"])
        .env_clear()
        .current_dir(&app_data_directory)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .creation_flags(0x08000000);
    for variable_name in ["SystemRoot", "WINDIR"] {
        if let Some(value) = std::env::var_os(variable_name) {
            command.env(variable_name, value);
        }
    }
    command
        .env("TEMP", &app_data_directory)
        .env("TMP", &app_data_directory);
    let mut child = command.spawn().map_err(|_| SidecarFailure::startup())?;
    // Std child pipe owner를 그대로 보존해 Windows overlapped handle의 I/O 계약을 유지한다.
    let mut stop_writer = child.stdin.take().expect("configured piped stdin"); // Parent만 stdin writer를 소유한다.
    let mut ready_reader = child.stdout.take().expect("configured piped stdout");
    if framed::write_frame(
        &mut stop_writer,
        &bootstrap_payload,
        framed::MAXIMUM_BOOTSTRAP_FRAME_BYTES,
    )
    .is_err()
    {
        abort_proven_pre_runtime_child(child, stop_writer);
        return Err(SidecarFailure::startup());
    }
    drop(bootstrap_payload); // Bootstrap 전송 뒤 JSON과 credential 복사본을 즉시 덮어쓴다.
    drop(credentials);
    let mut ready_payload = Zeroizing::new(Vec::with_capacity(256));
    let ready =
        match read_ready_descriptor(&mut ready_reader, &mut ready_payload, SIDECAR_READY_TIMEOUT) {
            Ok(ready) => ready,
            Err(ReadyDescriptorReadFailure::Timeout) => {
                return Ok(BackendSidecarPreparation::Ambiguous(
                    AmbiguousBackendSidecar {
                        child: OwnedSidecarChild::new(child, None),
                        stop_writer,
                        late_ready_recovery: Some(LateReadySidecarRecovery {
                            ready_reader,
                            ready_payload,
                            token,
                        }),
                        startup_failure_code: "BACKEND_SIDECAR_STARTUP_FAILED",
                    },
                ));
            }
            Err(ReadyDescriptorReadFailure::Terminal(failure)) => {
                return Ok(BackendSidecarPreparation::Ambiguous(
                    AmbiguousBackendSidecar {
                        child: OwnedSidecarChild::new(child, None),
                        stop_writer,
                        late_ready_recovery: None,
                        startup_failure_code: failure.code,
                    },
                ));
            }
        };

    // Token/config publication 이후의 모든 failure는 기존 공통 lifecycle과 같이 child를 보존한다.
    let port = ready.port;
    let identity = ready.runtime_identity();
    let descriptor = build_connection_descriptor(ready, &mut token);
    Ok(BackendSidecarPreparation::Ready(PreparedBackendSidecar {
        descriptor,
        child: OwnedSidecarChild::new(child, Some(identity)),
        stop_writer,
        port,
    }))
}
