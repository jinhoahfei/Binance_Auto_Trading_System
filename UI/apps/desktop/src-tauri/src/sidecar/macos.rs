//! macOS Keychain, fixed FD 3~6, Unix ownership와 process/path adapter.

use super::macos_profile::{
    profile_directory, selected_profile, MacosExecutionProfile, LIVE_SERVICE,
};
use super::*;
use security_framework::passwords::{generic_password, PasswordOptions};
use std::fs::{self, OpenOptions};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};

const SIDECAR_BINARY_NAME: &str = "binance-auto-sidecar";
const KEYCHAIN_SERVICE: &str = "com.binance-auto.trader.testnet";
const KEYCHAIN_API_KEY_ACCOUNT: &str = "api-key";
const KEYCHAIN_API_SECRET_ACCOUNT: &str = "api-secret";
const TOKEN_CHILD_FD: RawFd = 3;
const READY_CHILD_FD: RawFd = 4;
const STOP_CHILD_FD: RawFd = 5;
const CONFIG_CHILD_FD: RawFd = 6;
const MINIMUM_STAGING_FD: RawFd = 16;

/// 함수 이름: open_locked_runtime_ownership_artifact()
/// 기능: owner-only regular artifact를 no-follow로 열고 nonblocking exclusive lock 아래 exact JSON을 읽는다.
/// 인자: directory -> Tauri per-user app-data directory
/// 반환값: artifact가 없으면 None, 있으면 lock을 보유한 File, wire, device ID와 inode tuple
/// 작성 날짜: 2026/08/29
pub(super) fn open_locked_runtime_ownership_artifact(
    directory: &Path,
) -> Result<Option<(File, RuntimeOwnershipArtifactWire, u64, u64)>, SidecarFailure> {
    if !directory.is_dir() {
        return Ok(None);
    }
    let ownership_path = directory.join(RUNTIME_OWNERSHIP_FILE_NAME);
    let ownership_file = match OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
        .open(ownership_path)
    {
        Ok(ownership_file) => ownership_file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(SidecarFailure::ownership_reconciliation_required()),
    };

    // Python writer와 같은 inode·owner·permission 계약을 만족한 regular file만 조정한다.
    let metadata = ownership_file
        .metadata()
        .map_err(|_| SidecarFailure::ownership_reconciliation_required())?;
    if !metadata.is_file()
        || metadata.nlink() != 1
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.mode() & 0o777 != 0o600
        || metadata.len() == 0
        || metadata.len() > MAXIMUM_RUNTIME_OWNERSHIP_BYTES
    {
        return Err(SidecarFailure::ownership_reconciliation_required());
    }

    // Advisory lock을 얻지 못하면 runtime이 아직 살아 있다고 보고 identity를 읽지 않는다.
    let lock_result =
        unsafe { libc::flock(ownership_file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if lock_result != 0 {
        return Err(SidecarFailure::ownership_reconciliation_required());
    }

    // Bounded file을 lock 아래 읽어 duplicate·unknown field와 schema drift를 함께 거부한다.
    let mut ownership_reader = (&ownership_file).take(MAXIMUM_RUNTIME_OWNERSHIP_BYTES + 1);
    let mut ownership_bytes = Vec::with_capacity(metadata.len() as usize);
    ownership_reader
        .read_to_end(&mut ownership_bytes)
        .map_err(|_| SidecarFailure::ownership_reconciliation_required())?;
    if ownership_bytes.len() as u64 != metadata.len() {
        return Err(SidecarFailure::ownership_reconciliation_required());
    }
    let artifact: RuntimeOwnershipArtifactWire = serde_json::from_slice(&ownership_bytes)
        .map_err(|_| SidecarFailure::ownership_reconciliation_required())?;
    if artifact.schema_version != BACKEND_SCHEMA_VERSION
        || artifact.runtime_pid == 0
        || artifact.runtime_pid > libc::pid_t::MAX as u32
        || !crate::is_canonical_uuid(&artifact.process_start_id)
    {
        return Err(SidecarFailure::ownership_reconciliation_required());
    }

    Ok(Some((
        ownership_file,
        artifact,
        metadata.dev(),
        metadata.ino(),
    )))
}

/// 함수 이름: runtime_process_exists()
/// 기능: signal을 보내지 않는 kill(pid, 0)으로 recorded PID의 존재 여부를 보수적으로 판정한다.
/// 인자: runtime_pid -> artifact의 positive Python runtime PID
/// 반환값: process가 있거나 permission으로 확정할 수 없으면 true
/// 작성 날짜: 2026/08/29
pub(super) fn runtime_process_exists(runtime_pid: u32) -> bool {
    if runtime_pid == 0 {
        return true;
    }

    // Signal 0은 process를 변경하지 않으며 ESRCH만 확정된 부재로 취급한다.
    let probe_result = unsafe { libc::kill(runtime_pid as libc::pid_t, 0) };
    if probe_result == 0 {
        return true;
    }
    !matches!(io::Error::last_os_error().raw_os_error(), Some(libc::ESRCH))
}

/// 함수 이름: disable_process_core_dumps()
/// 기능: renderer token과 FD6 credential이 native/backend crash dump에 기록되지 않게 RLIMIT_CORE를 0으로 고정한다.
/// 인자: 없음
/// 반환값: 설정 성공 또는 secret 없는 startup failure
/// 작성 날짜: 2026/08/24
pub(super) fn platform_disable_process_core_dumps() -> Result<(), SidecarFailure> {
    set_zero_core_dump_limit().map_err(|_| SidecarFailure::startup())
}

/// 함수 이름: prepare_backend_sidecar()
/// 기능: Keychain/config/token pipe를 조립하고 ready를 strict 검증해 renderer publication 직전 상태를 만든다.
/// 인자: app_handle -> bundle path와 app data path를 해석할 Tauri handle
/// 반환값: ready 검증을 마친 child/descriptor 묶음 또는 secret 없는 failure
/// 작성 날짜: 2026/08/24
pub(super) fn platform_prepare_backend_sidecar(
    app_handle: &AppHandle,
) -> Result<BackendSidecarPreparation, SidecarFailure> {
    let credentials = read_keychain_credentials()?;
    let app_data_directory =
        resolve_app_data_directory(app_handle).map_err(|_| SidecarFailure::startup())?;
    ensure_private_app_data_directory(&app_data_directory)?;
    let profile = selected_profile()?;
    let history_path = if profile == MacosExecutionProfile::Testnet {
        build_history_path(&app_data_directory)?
    } else {
        app_data_directory
            .join("trade-history.jsonl")
            .to_str()
            .ok_or_else(SidecarFailure::startup)?
            .to_owned()
    };
    let allowed_origin = select_allowed_origin(tauri::is_dev());

    // Production configuration은 주문 opt-in을 false/null로 고정하고 credential을 참조로만 직렬화한다.
    let configuration = SidecarBootstrapConfiguration {
        schema_version: BACKEND_SCHEMA_VERSION,
        allowed_origin,
        history_path: &history_path,
        api_key: &credentials.api_key,
        api_secret: &credentials.api_secret,
        allow_testnet_orders: false,
        max_notional: None,
    };
    let configuration_payload =
        super::macos_profile::serialize_profile_configuration(configuration, profile)?;

    // 각 child end를 16 이상 CLOEXEC staging FD로 옮겨 dup2 target 3~6 충돌을 제거한다.
    let (token_writer, token_child_reader) =
        create_parent_writer_child_reader().map_err(|_| SidecarFailure::startup())?;
    let (mut ready_reader, ready_child_writer) =
        create_parent_reader_child_writer().map_err(|_| SidecarFailure::startup())?;
    let (stop_writer, stop_child_reader) =
        create_parent_writer_child_reader().map_err(|_| SidecarFailure::startup())?;
    let (config_writer, config_child_reader) =
        create_parent_writer_child_reader().map_err(|_| SidecarFailure::startup())?;
    let sidecar_path = resolve_sidecar_executable()?;

    // Child는 argv와 inherited environment 없이 anonymous FD 네 개와 null standard streams만 받는다.
    let mut command = Command::new(sidecar_path);
    command
        .env_clear()
        .current_dir(&app_data_directory)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    configure_child_file_descriptors(
        &mut command,
        token_child_reader.as_raw_fd(),
        ready_child_writer.as_raw_fd(),
        stop_child_reader.as_raw_fd(),
        config_child_reader.as_raw_fd(),
    );
    let child = command.spawn().map_err(|_| SidecarFailure::startup())?;

    // Spawn 뒤 parent가 가진 child staging ends를 즉시 닫아 pipe EOF ownership을 단일화한다.
    drop(token_child_reader);
    drop(ready_child_writer);
    drop(stop_child_reader);
    drop(config_child_reader);

    // Token이 없으면 backend가 runtime factory를 호출할 수 없으므로 pre-runtime child만 회수한다.
    let mut token = match generate_session_token() {
        Ok(token) => Zeroizing::new(token),
        Err(failure) => {
            abort_proven_pre_runtime_child(child, stop_writer);
            return Err(failure);
        }
    };

    // Token/config은 각 전용 pipe에 한 번 쓰고 buffer를 지우며 ready FD에는 secret이 존재하지 않는다.
    let token_result = write_pipe_payload(token_writer, token.as_bytes());
    let config_result = write_pipe_payload(config_writer, configuration_payload.as_slice());
    if token_result.is_err() || config_result.is_err() {
        token.zeroize();
        abort_proven_pre_runtime_child(child, stop_writer);
        return Err(SidecarFailure::startup());
    }
    let mut ready_payload = Zeroizing::new(Vec::with_capacity(256));
    let ready_result =
        read_ready_descriptor(&mut ready_reader, &mut ready_payload, SIDECAR_READY_TIMEOUT);
    let ready = match ready_result {
        Ok(ready) => ready,
        Err(ReadyDescriptorReadFailure::Timeout) => {
            debug_assert_eq!(
                select_startup_child_recovery_policy(true),
                StartupChildRecoveryPolicy::RetainAmbiguousExposure
            );
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
            debug_assert_eq!(
                select_startup_child_recovery_policy(true),
                StartupChildRecoveryPolicy::RetainAmbiguousExposure
            );
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

    // Ready/token은 각각 strict parser/generator를 통과했으므로 추가 fallible rollback 없이 소유권을 이전한다.
    let port = ready.port;
    let python_runtime_identity = ready.runtime_identity();
    let descriptor = build_connection_descriptor(ready, &mut token);

    Ok(BackendSidecarPreparation::Ready(PreparedBackendSidecar {
        descriptor,
        child: OwnedSidecarChild::new(child, Some(python_runtime_identity)),
        stop_writer,
        port,
    }))
}

/// 함수 이름: read_keychain_credentials()
/// 기능: macOS Security.framework에서 두 credential을 renderer와 subprocess stdout 밖에서 읽는다.
/// 인자: 없음
/// 반환값: strict native credential pair 또는 generic unavailable failure
/// 작성 날짜: 2026/08/24
pub(super) fn read_keychain_credentials() -> Result<NativeCredentials, SidecarFailure> {
    // Stable service와 별도 account는 renderer 입력이나 일반 environment 없이 Keychain item을 식별한다.
    let api_key = read_keychain_secret(KEYCHAIN_API_KEY_ACCOUNT)?;
    let api_secret = match read_keychain_secret(KEYCHAIN_API_SECRET_ACCOUNT) {
        Ok(secret) => secret,
        Err(failure) => {
            let mut api_key = api_key;
            api_key.zeroize();
            return Err(failure);
        }
    };

    Ok(NativeCredentials {
        api_key,
        api_secret,
    })
}

/// 함수 이름: read_keychain_secret()
/// 기능: generic-password bytes를 bounded printable ASCII String으로 검증하고 오류 bytes를 zeroize한다.
/// 인자: account -> stable non-secret Keychain account identifier
/// 반환값: 검증된 secret String 또는 generic unavailable failure
/// 작성 날짜: 2026/08/24
pub(super) fn read_keychain_secret(account: &str) -> Result<String, SidecarFailure> {
    // Credential과 history 선택은 같은 frozen native profile을 사용한다.
    let service = if selected_profile()? == MacosExecutionProfile::Testnet {
        KEYCHAIN_SERVICE
    } else {
        LIVE_SERVICE
    };
    let options = PasswordOptions::new_generic_password(service, account);
    let password_bytes =
        generic_password(options).map_err(|_| SidecarFailure::credentials_unavailable())?;
    let mut secret = match String::from_utf8(password_bytes) {
        Ok(secret) => secret,
        Err(error) => {
            let mut invalid_bytes = error.into_bytes();
            invalid_bytes.zeroize();
            return Err(SidecarFailure::credentials_unavailable());
        }
    };

    // Binance credential identity를 바꾸는 trim 없이 non-empty printable ASCII와 size만 제한한다.
    if secret.is_empty()
        || secret.len() > MAXIMUM_SECRET_BYTES
        || !secret.bytes().all(|byte| matches!(byte, 0x21..=0x7e))
    {
        secret.zeroize();
        return Err(SidecarFailure::credentials_unavailable());
    }

    Ok(secret)
}

/// 함수 이름: ensure_private_app_data_directory()
/// 기능: credential-adjacent history directory가 symlink가 아닌 per-user directory이며 mode 0700임을 보장한다.
/// 인자: app_data_directory -> Tauri identifier-scoped application data directory
/// 반환값: private directory 준비 성공 또는 startup failure
/// 작성 날짜: 2026/08/24
pub(super) fn ensure_private_app_data_directory(
    app_data_directory: &Path,
) -> Result<(), SidecarFailure> {
    fs::create_dir_all(app_data_directory).map_err(|_| SidecarFailure::startup())?;
    let metadata =
        fs::symlink_metadata(app_data_directory).map_err(|_| SidecarFailure::startup())?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err(SidecarFailure::startup());
    }

    // umask와 기존 directory mode에 의존하지 않고 group/other access를 매 launch에서 제거한다.
    fs::set_permissions(app_data_directory, fs::Permissions::from_mode(0o700))
        .map_err(|_| SidecarFailure::startup())
}

/// 함수 이름: resolve_sidecar_executable()
/// 기능: target suffix가 제거되어 main executable 옆에 bundle된 exact sidecar를 검증한다.
/// 인자: 없음
/// 반환값: canonical executable path 또는 startup failure
/// 작성 날짜: 2026/08/24
pub(super) fn resolve_sidecar_executable() -> Result<PathBuf, SidecarFailure> {
    let current_executable = std::env::current_exe().map_err(|_| SidecarFailure::startup())?;
    let executable_directory = current_executable
        .parent()
        .ok_or_else(SidecarFailure::startup)?;
    let canonical_directory = executable_directory
        .canonicalize()
        .map_err(|_| SidecarFailure::startup())?;
    let sidecar_path = executable_directory.join(SIDECAR_BINARY_NAME);
    let canonical_sidecar = sidecar_path
        .canonicalize()
        .map_err(|_| SidecarFailure::startup())?;
    let metadata = fs::metadata(&canonical_sidecar).map_err(|_| SidecarFailure::startup())?;

    // Bundle directory 밖 symlink와 executable bit가 없는 artifact를 process spawn 전에 거부한다.
    if !canonical_sidecar.starts_with(&canonical_directory)
        || !metadata.is_file()
        || metadata.permissions().mode() & 0o111 == 0
    {
        return Err(SidecarFailure::startup());
    }

    Ok(canonical_sidecar)
}

/// 함수 이름: create_parent_writer_child_reader()
/// 기능: parent write/child read anonymous pipe를 만들고 child end를 collision-free staging FD로 옮긴다.
/// 인자: 없음
/// 반환값: parent File과 staged child OwnedFd
/// 작성 날짜: 2026/08/24
pub(super) fn create_parent_writer_child_reader() -> io::Result<(File, OwnedFd)> {
    let (reader, writer) = create_anonymous_pipe()?;
    let child_reader = duplicate_staging_fd(reader.as_raw_fd())?;
    drop(reader);
    Ok((File::from(writer), child_reader))
}

/// 함수 이름: create_parent_reader_child_writer()
/// 기능: parent read/child write anonymous pipe를 만들고 child end를 collision-free staging FD로 옮긴다.
/// 인자: 없음
/// 반환값: parent File과 staged child OwnedFd
/// 작성 날짜: 2026/08/24
pub(super) fn create_parent_reader_child_writer() -> io::Result<(File, OwnedFd)> {
    let (reader, writer) = create_anonymous_pipe()?;
    let child_writer = duplicate_staging_fd(writer.as_raw_fd())?;
    drop(writer);
    Ok((File::from(reader), child_writer))
}

/// 함수 이름: create_anonymous_pipe()
/// 기능: 양 끝에 CLOEXEC를 적용한 macOS anonymous pipe를 만든다.
/// 인자: 없음
/// 반환값: read/write OwnedFd pair
/// 작성 날짜: 2026/08/24
pub(super) fn create_anonymous_pipe() -> io::Result<(OwnedFd, OwnedFd)> {
    let mut descriptors = [-1; 2];
    if unsafe { libc::pipe(descriptors.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error());
    }

    // Raw FD를 즉시 OwnedFd로 감싸 이후 모든 early return에서 자동 close되게 한다.
    let reader = unsafe { OwnedFd::from_raw_fd(descriptors[0]) };
    let writer = unsafe { OwnedFd::from_raw_fd(descriptors[1]) };
    set_close_on_exec(reader.as_raw_fd())?;
    set_close_on_exec(writer.as_raw_fd())?;
    Ok((reader, writer))
}

/// 함수 이름: set_close_on_exec()
/// 기능: exec에서 명시적으로 dup하지 않은 pipe end가 sidecar에 남지 않게 FD_CLOEXEC를 설정한다.
/// 인자: descriptor -> 설정할 raw FD
/// 반환값: OS 설정 결과
/// 작성 날짜: 2026/08/24
pub(super) fn set_close_on_exec(descriptor: RawFd) -> io::Result<()> {
    let existing_flags = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
    if existing_flags == -1 {
        return Err(io::Error::last_os_error());
    }
    if unsafe { libc::fcntl(descriptor, libc::F_SETFD, existing_flags | libc::FD_CLOEXEC) } == -1 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// 함수 이름: duplicate_staging_fd()
/// 기능: pre_exec dup2 source를 target 3~6과 겹치지 않는 CLOEXEC FD로 복제한다.
/// 인자: descriptor -> pipe의 원래 child end
/// 반환값: 16 이상 staged OwnedFd
/// 작성 날짜: 2026/08/24
pub(super) fn duplicate_staging_fd(descriptor: RawFd) -> io::Result<OwnedFd> {
    let staged_descriptor =
        unsafe { libc::fcntl(descriptor, libc::F_DUPFD_CLOEXEC, MINIMUM_STAGING_FD) };
    if staged_descriptor == -1 {
        return Err(io::Error::last_os_error());
    }

    Ok(unsafe { OwnedFd::from_raw_fd(staged_descriptor) })
}

/// 함수 이름: configure_child_file_descriptors()
/// 기능: pre_exec에서 staged pipe를 FD3 token/4 ready/5 stop/6 config로만 상속한다.
/// 인자: command -> sidecar Command, 각 staged descriptor -> child mapping source
/// 반환값: 없음
/// 작성 날짜: 2026/08/24
pub(super) fn configure_child_file_descriptors(
    command: &mut Command,
    token_descriptor: RawFd,
    ready_descriptor: RawFd,
    stop_descriptor: RawFd,
    config_descriptor: RawFd,
) {
    // pre_exec closure는 fork 뒤 async-signal-safe dup2/setrlimit 호출만 수행한다.
    unsafe {
        command.pre_exec(move || {
            set_zero_core_dump_limit()?;
            duplicate_to_child_fd(token_descriptor, TOKEN_CHILD_FD)?;
            duplicate_to_child_fd(ready_descriptor, READY_CHILD_FD)?;
            duplicate_to_child_fd(stop_descriptor, STOP_CHILD_FD)?;
            duplicate_to_child_fd(config_descriptor, CONFIG_CHILD_FD)?;
            Ok(())
        });
    }
}

/// 함수 이름: duplicate_to_child_fd()
/// 기능: staged descriptor를 고정 child FD로 원자적으로 복제한다.
/// 인자: source -> 16 이상 source FD, target -> ADR fixed FD
/// 반환값: OS dup 결과
/// 작성 날짜: 2026/08/24
pub(super) fn duplicate_to_child_fd(source: RawFd, target: RawFd) -> io::Result<()> {
    if unsafe { libc::dup2(source, target) } == -1 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// 함수 이름: set_zero_core_dump_limit()
/// 기능: 현재 process 또는 pre_exec child의 core dump soft/hard limit를 모두 0으로 만든다.
/// 인자: 없음
/// 반환값: OS 설정 결과
/// 작성 날짜: 2026/08/24
pub(super) fn set_zero_core_dump_limit() -> io::Result<()> {
    let limit = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    if unsafe { libc::setrlimit(libc::RLIMIT_CORE, &limit) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// 함수 이름: write_pipe_payload()
/// 기능: bounded payload를 anonymous pipe에 전부 쓰고 File drop으로 EOF를 전달한다.
/// 인자: writer -> parent pipe File, payload -> token 또는 strict config bytes
/// 반환값: write 결과
/// 작성 날짜: 2026/08/24
pub(super) fn write_pipe_payload(mut writer: File, payload: &[u8]) -> io::Result<()> {
    writer.write_all(payload)
}

/// 함수 이름: read_ready_descriptor()
/// 기능: nonblocking FD4에서 bounded framing을 보존하며 exact JSON ready descriptor를 timeout 내 읽는다.
/// 인자: reader -> parent ready File, payload -> timeout 사이에도 보존할 framing buffer,
///      timeout -> 이번 read attempt의 bounded wait
/// 반환값: strict ready wire, retryable timeout 또는 terminal failure
/// 작성 날짜: 2026/08/24
pub(super) fn read_ready_descriptor(
    reader: &mut File,
    payload: &mut Zeroizing<Vec<u8>>,
    timeout: Duration,
) -> Result<ReadyDescriptorWire, ReadyDescriptorReadFailure> {
    set_nonblocking(reader.as_raw_fd())
        .map_err(|_| ReadyDescriptorReadFailure::Terminal(SidecarFailure::startup()))?;
    let deadline = Instant::now() + timeout;
    let mut chunk = [0_u8; 512];

    // Timeout에서는 payload를 지우지 않아 split JSON의 framing과 late READY recovery를 그대로 이어 간다.
    loop {
        if Instant::now() >= deadline {
            return Err(ReadyDescriptorReadFailure::Timeout);
        }
        match reader.read(&mut chunk) {
            Ok(0) => {
                payload.zeroize();
                return Err(ReadyDescriptorReadFailure::Terminal(
                    SidecarFailure::startup(),
                ));
            }
            Ok(read_count) => {
                payload.extend_from_slice(&chunk[..read_count]);
                if payload.len() > MAXIMUM_READY_BYTES {
                    payload.zeroize();
                    return Err(ReadyDescriptorReadFailure::Terminal(
                        SidecarFailure::descriptor_rejected(),
                    ));
                }
                if let Some(newline_index) = payload.iter().position(|byte| *byte == b'\n') {
                    let trailing_is_whitespace = payload[newline_index + 1..]
                        .iter()
                        .all(u8::is_ascii_whitespace);
                    if !trailing_is_whitespace {
                        payload.zeroize();
                        return Err(ReadyDescriptorReadFailure::Terminal(
                            SidecarFailure::descriptor_rejected(),
                        ));
                    }
                    let parsed = parse_ready_descriptor(&payload[..newline_index]);
                    payload.zeroize();
                    return parsed.map_err(ReadyDescriptorReadFailure::Terminal);
                }
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(10));
            }
            Err(_) => {
                payload.zeroize();
                return Err(ReadyDescriptorReadFailure::Terminal(
                    SidecarFailure::startup(),
                ));
            }
        }
    }
}

/// 함수 이름: set_nonblocking()
/// 기능: ready FD wait에 native hard timeout을 적용할 수 있도록 O_NONBLOCK을 설정한다.
/// 인자: descriptor -> parent ready FD
/// 반환값: OS 설정 결과
/// 작성 날짜: 2026/08/24
pub(super) fn set_nonblocking(descriptor: RawFd) -> io::Result<()> {
    let existing_flags = unsafe { libc::fcntl(descriptor, libc::F_GETFL) };
    if existing_flags == -1 {
        return Err(io::Error::last_os_error());
    }
    if unsafe { libc::fcntl(descriptor, libc::F_SETFL, existing_flags | libc::O_NONBLOCK) } == -1 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// 함수 이름: resolve_app_data_directory()
/// 기능: 기존 macOS Tauri per-user app-data 경로를 유지한다.
/// 인자: app_handle -> Tauri native path owner
/// 반환값: app-data directory 또는 path error
/// 작성 날짜: 2026/09/06
pub(super) fn resolve_app_data_directory(app_handle: &AppHandle) -> tauri::Result<PathBuf> {
    let profile =
        selected_profile().map_err(|_| std::io::Error::other("native profile unavailable"))?;
    Ok(profile_directory(
        app_handle.path().app_data_dir()?,
        profile,
    )) // Live owner는 별도 directory에만 존재한다.
}

/// 함수 이름: write_closed_ack()
/// 기능: 기존 FD5의 한 byte ACK와 writer close 계약을 유지한다.
/// 인자: writer -> native parent 전용 control pipe
/// 반환값: ACK write 결과
/// 작성 날짜: 2026/09/06
pub(super) fn write_closed_ack(writer: &mut File) -> io::Result<()> {
    writer.write_all(&[0_u8]) // 실제 EOF는 공통 lifecycle이 writer를 drop할 때 전달한다.
}
