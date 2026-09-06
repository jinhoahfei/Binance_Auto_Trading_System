//! LocalAppData known-folder, no-reparse path pin과 Windows runtime ownership lock을 담당한다.

use super::super::{
    RuntimeOwnershipArtifactWire, SidecarFailure, BACKEND_SCHEMA_VERSION,
    MAXIMUM_RUNTIME_OWNERSHIP_BYTES, RUNTIME_OWNERSHIP_FILE_NAME,
};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read};
use std::ops::{Deref, DerefMut};
use std::os::windows::fs::OpenOptionsExt;
use std::os::windows::io::AsRawHandle;
use std::path::{Component, Path, PathBuf, Prefix};
use windows_sys::Win32::Storage::FileSystem::{
    GetFileInformationByHandle, LockFileEx, BY_HANDLE_FILE_INFORMATION, FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_READ_ATTRIBUTES, FILE_SHARE_READ, FILE_SHARE_WRITE, LOCKFILE_EXCLUSIVE_LOCK,
    LOCKFILE_FAIL_IMMEDIATELY,
};
use windows_sys::Win32::System::Com::CoTaskMemFree;
use windows_sys::Win32::System::IO::OVERLAPPED;
use windows_sys::Win32::UI::Shell::{FOLDERID_LocalAppData, SHGetKnownFolderPath};

const APPLICATION_DIRECTORY_NAME: &str = "com.binance-auto.trader";

/// 클래스 이름: LockedRuntimeOwnershipFile
/// 기능: artifact handle과 모든 parent directory handle을 같은 scope 동안 pin한다.
/// 작성 날짜: 2026/09/06
pub(in crate::sidecar) struct LockedRuntimeOwnershipFile {
    file: File,
    _parent_guards: Vec<File>,
}

impl Deref for LockedRuntimeOwnershipFile {
    type Target = File;

    /// 함수 이름: deref()
    /// 기능: 공통 lifecycle에 locked file의 read-only 참조를 제공한다.
    /// 인자: 없음
    /// 반환값: artifact File 참조
    /// 작성 날짜: 2026/09/06
    fn deref(&self) -> &File {
        &self.file // Parent guards는 참조 접근 중에도 wrapper 안에 유지된다.
    }
}

impl DerefMut for LockedRuntimeOwnershipFile {
    /// 함수 이름: deref_mut()
    /// 기능: 공통 RELEASED publication에 locked file의 mutable 참조를 제공한다.
    /// 인자: 없음
    /// 반환값: artifact File mutable 참조
    /// 작성 날짜: 2026/09/06
    fn deref_mut(&mut self) -> &mut File {
        &mut self.file // File을 wrapper 밖으로 이동시키지 않는다.
    }
}

/// 함수 이름: local_app_data_directory()
/// 기능: environment 대신 현재 사용자 native known-folder 경로를 조회한다.
/// 인자: 없음
/// 반환값: 현재 사용자 LocalAppData absolute path
/// 작성 날짜: 2026/09/06
fn local_app_data_directory() -> Result<PathBuf, SidecarFailure> {
    let mut output = std::ptr::null_mut();
    if unsafe { SHGetKnownFolderPath(&FOLDERID_LocalAppData, 0, std::ptr::null_mut(), &mut output) }
        < 0
        || output.is_null()
    {
        // 실패 HRESULT에도 OS가 할당한 output이 있으면 COM 메모리를 반환한다.
        if !output.is_null() {
            unsafe { CoTaskMemFree(output.cast()) };
        }
        return Err(SidecarFailure::startup());
    }

    // Windows 최대 경로 범위 안에서 NUL을 찾고 COM allocation을 모든 결과에서 해제한다.
    let mut size = 0;
    while size < 32768 && unsafe { *output.add(size) } != 0 {
        size += 1;
    }
    let decoded = if size < 32768 {
        String::from_utf16(unsafe { std::slice::from_raw_parts(output, size) }).ok()
    } else {
        None
    };
    unsafe { CoTaskMemFree(output.cast()) }; // Known-folder memory는 native allocator에 반환한다.
    decoded
        .map(PathBuf::from)
        .filter(|path| is_local_absolute_path(path))
        .ok_or_else(SidecarFailure::startup)
}

/// 함수 이름: is_local_absolute_path()
/// 기능: local drive absolute path만 허용해 UNC, device namespace와 traversal을 거부한다.
/// 인자: path -> 경계 검증 대상
/// 반환값: local disk absolute path 여부
/// 작성 날짜: 2026/09/06
fn is_local_absolute_path(path: &Path) -> bool {
    // 정규화 이전의 입력 component를 검사해 .. 를 canonicalize로 숨기지 않는다.
    path.is_absolute()
        && matches!(path.components().next(), Some(Component::Prefix(prefix)) if matches!(prefix.kind(), Prefix::Disk(_)))
        && path
            .components()
            .all(|component| !matches!(component, Component::ParentDir | Component::CurDir))
}

/// 함수 이름: resolve_app_data_directory()
/// 기능: Tauri Roaming path 대신 Python과 같은 LocalAppData application 경로를 선택한다.
/// 인자: 없음
/// 반환값: 현재 사용자 application directory
/// 작성 날짜: 2026/09/06
pub(in crate::sidecar) fn resolve_app_data_directory() -> Result<PathBuf, SidecarFailure> {
    Ok(local_app_data_directory()?.join(APPLICATION_DIRECTORY_NAME)) // 저장 위치는 environment로 덮어쓰지 않는다.
}

/// 함수 이름: file_information()
/// 기능: opened handle의 reparse, link count와 volume/file identity를 조회한다.
/// 인자: file -> 이미 열린 native File
/// 반환값: handle-bound Windows metadata 또는 startup failure
/// 작성 날짜: 2026/09/06
fn file_information(file: &File) -> Result<BY_HANDLE_FILE_INFORMATION, SidecarFailure> {
    let mut information = unsafe { std::mem::zeroed() };
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut information) } == 0 {
        return Err(SidecarFailure::startup());
    }
    Ok(information) // Path를 다시 따라가지 않고 실제 handle identity를 반환한다.
}

/// 함수 이름: pin_directory_chain()
/// 기능: drive root부터 leaf까지 no-reparse directory handle을 열어 rename/delete 교체를 차단한다.
/// 인자: directory -> LocalAppData 아래 검증할 absolute directory
/// 반환값: close될 때까지 경로를 pin하는 directory handle 목록
/// 작성 날짜: 2026/09/06
fn pin_directory_chain(directory: &Path) -> Result<Vec<File>, SidecarFailure> {
    if !is_local_absolute_path(directory) {
        return Err(SidecarFailure::startup());
    }
    let mut ancestors: Vec<_> = directory.ancestors().collect();
    ancestors.reverse();
    let mut guards = Vec::with_capacity(ancestors.len());

    // FILE_SHARE_DELETE를 주지 않아 이미 확인한 ancestor의 rename/restore ABA도 막는다.
    for ancestor in ancestors {
        let guard = OpenOptions::new()
            .access_mode(FILE_READ_ATTRIBUTES)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
            .open(ancestor)
            .map_err(|_| SidecarFailure::startup())?;
        let information = file_information(&guard)?;
        if information.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY == 0
            || information.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
        {
            return Err(SidecarFailure::startup());
        }
        guards.push(guard);
    }
    Ok(guards)
}

/// 함수 이름: ensure_private_app_data_directory()
/// 기능: 현재 사용자 known-folder의 direct application directory만 no-reparse로 준비한다.
/// 인자: directory -> Tauri caller가 사용할 canonical application directory
/// 반환값: 안전 경로 준비 성공 또는 startup failure
/// 작성 날짜: 2026/09/06
pub(in crate::sidecar) fn ensure_private_app_data_directory(
    directory: &Path,
) -> Result<(), SidecarFailure> {
    let expected = resolve_app_data_directory()?;
    if directory != expected {
        return Err(SidecarFailure::startup());
    }

    // 사용자 known-folder를 먼저 pin한 상태에서 direct child만 생성해 ancestor traversal을 막는다.
    let root = directory.parent().ok_or_else(SidecarFailure::startup)?;
    let _root_guards = pin_directory_chain(root)?;
    match fs::create_dir(directory) {
        Ok(()) => (),
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => (),
        Err(_) => return Err(SidecarFailure::startup()),
    }
    let _directory_guards = pin_directory_chain(directory)?;
    Ok(())
}

/// 함수 이름: open_locked_runtime_ownership_artifact()
/// 기능: locked non-reparse single-link artifact를 pinned parent 아래 읽고 exact wire를 검증한다.
/// 인자: directory -> 현재 사용자 application directory
/// 반환값: artifact 미존재 또는 locked owner와 wire/volume/file ID
/// 작성 날짜: 2026/09/06
pub(in crate::sidecar) fn open_locked_runtime_ownership_artifact(
    directory: &Path,
) -> Result<
    Option<(
        LockedRuntimeOwnershipFile,
        RuntimeOwnershipArtifactWire,
        u64,
        u64,
    )>,
    SidecarFailure,
> {
    let required_failure = SidecarFailure::ownership_reconciliation_required;
    ensure_private_app_data_directory(directory).map_err(|_| required_failure())?;
    let parent_guards = pin_directory_chain(directory).map_err(|_| required_failure())?;
    let mut file = match OpenOptions::new()
        .read(true)
        .write(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(directory.join(RUNTIME_OWNERSHIP_FILE_NAME))
    {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(required_failure()),
    };

    // Python과 같은 offset 0의 1 byte를 non-blocking exclusive lock한 뒤에만 내용을 읽는다.
    let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
    if unsafe {
        LockFileEx(
            file.as_raw_handle(),
            LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
            0,
            1,
            0,
            &mut overlapped,
        )
    } == 0
    {
        return Err(required_failure());
    }
    let information = file_information(&file).map_err(|_| required_failure())?;
    let size = ((information.nFileSizeHigh as u64) << 32) | information.nFileSizeLow as u64;
    if information.dwFileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY) != 0
        || information.nNumberOfLinks != 1
        || size == 0
        || size > MAXIMUM_RUNTIME_OWNERSHIP_BYTES
    {
        return Err(required_failure());
    }
    let mut payload = Vec::with_capacity(size as usize);
    (&mut file)
        .take(MAXIMUM_RUNTIME_OWNERSHIP_BYTES + 1)
        .read_to_end(&mut payload)
        .map_err(|_| required_failure())?;
    if payload.len() as u64 != size {
        return Err(required_failure());
    }
    let artifact: RuntimeOwnershipArtifactWire =
        serde_json::from_slice(&payload).map_err(|_| required_failure())?;
    if artifact.schema_version != BACKEND_SCHEMA_VERSION
        || artifact.runtime_pid == 0
        || !crate::is_canonical_uuid(&artifact.process_start_id)
    {
        return Err(required_failure());
    }
    let volume = information.dwVolumeSerialNumber as u64;
    let file_id = ((information.nFileIndexHigh as u64) << 32) | information.nFileIndexLow as u64;
    Ok(Some((
        LockedRuntimeOwnershipFile {
            file,
            _parent_guards: parent_guards,
        },
        artifact,
        volume,
        file_id,
    )))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 함수 이름: path_contract_rejects_unc_device_and_traversal()
    /// 기능: Windows absolute 문자열의 예외 namespace가 per-user 경계를 우회하지 않는지 검증한다.
    /// 인자: 없음
    /// 반환값: 없음
    /// 작성 날짜: 2026/09/06
    #[test]
    fn path_contract_rejects_unc_device_and_traversal() {
        // 실제 파일·credential을 만들지 않고 Windows path component 규칙만 검증한다.
        assert!(is_local_absolute_path(Path::new(
            r"C:\Users\fixture\AppData\Local"
        )));
        for value in [
            r"C:relative",
            r"\\server\share",
            r"\\?\C:\data",
            r"C:\Users\..\data",
        ] {
            assert!(!is_local_absolute_path(Path::new(value)));
        }
    }
}
