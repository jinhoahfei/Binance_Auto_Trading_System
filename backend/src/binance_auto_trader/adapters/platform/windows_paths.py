"""Windows per-user app-data known folder와 교체 불가능한 ancestor handle 경계를 제공한다."""

import ctypes
from pathlib import Path, PureWindowsPath
from uuid import UUID

from .windows_api import (
    DWORD,
    FILE_FLAG_BACKUP_SEMANTICS,
    FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_SHARE_READ_WRITE,
    HANDLE,
    INVALID_HANDLE_VALUE,
    load_kernel32,
    validate_file_handle,
)


_LOCAL_APP_DATA_FOLDER_ID = "f1b32785-6fba-4fcf-9d55-7b8e7f157091"
_APP_DIRECTORY_NAME = "com.binance-auto.trader"


def get_local_app_data_directory() -> Path:
    """
    함수 이름: get_local_app_data_directory()
    기능: 환경변수 대신 current user의 Windows known folder API로 app-data root를 조회한다.
    인자: 없음
    반환값: current user LocalAppData 안의 고정 application directory
    작성 날짜: 2026/09/06
    """
    # Current-user token을 사용하고 Windows allocator가 소유한 반환 버퍼를 반드시 해제한다.
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    allocator = ctypes.WinDLL("ole32", use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [
        ctypes.c_void_p, DWORD, HANDLE, ctypes.POINTER(ctypes.c_wchar_p),
    ]
    shell.SHGetKnownFolderPath.restype = ctypes.c_int32
    allocator.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    allocator.CoTaskMemFree.restype = None
    folder_id = ctypes.create_string_buffer(UUID(_LOCAL_APP_DATA_FOLDER_ID).bytes_le)
    directory_pointer = ctypes.c_wchar_p()
    try:
        result = shell.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(directory_pointer)
        )
        if result != 0 or not directory_pointer.value:
            raise OSError("Windows user app-data folder is unavailable")
        return Path(directory_pointer.value) / _APP_DIRECTORY_NAME
    finally:
        allocator.CoTaskMemFree(directory_pointer)  # 실패에서도 API가 할당한 버퍼를 반환한다.


def validate_app_data_path(directory: PureWindowsPath, expected: PureWindowsPath) -> None:
    """
    함수 이름: validate_app_data_path()
    기능: app-data가 known-folder 고정 경로와 같고 local absolute drive 경로인지 검증한다.
    인자: directory -> backend가 사용할 directory
        expected -> native known folder로 결정한 application directory
    반환값: 없음
    작성 날짜: 2026/09/06
    """
    # UNC, device namespace, ADS와 parent traversal은 canonical local app-data 계약에서 제외한다.
    if (
        not directory.is_absolute()
        or len(directory.drive) != 2
        or directory.drive[1] != ":"
        or directory != expected
        or any(part == ".." or ":" in part for part in directory.parts[1:])
    ):
        raise OSError("Windows runtime directory is outside user app-data")


def pin_directory_chain(directory: Path) -> tuple[int, ...]:
    """
    함수 이름: pin_directory_chain()
    기능: drive root부터 leaf까지 no-follow directory handle을 delete 공유 없이 보유한다.
    인자: directory -> 검증할 absolute directory 경로
    반환값: caller가 lifetime 종료에 역순으로 닫을 directory handle tuple
    작성 날짜: 2026/09/06
    """
    # 상위 directory를 먼저 고정해 하위 open 중 junction 삽입과 ancestor 교체를 차단한다.
    kernel = load_kernel32()
    directory_handles = []
    try:
        for component in (*reversed(directory.parents), directory):
            handle = kernel.CreateFileW(
                str(component), 0, FILE_SHARE_READ_WRITE, None, 3,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None,
            )
            if handle in (None, INVALID_HANDLE_VALUE):
                raise OSError("Windows app-data directory is unavailable")
            directory_handles.append(handle)
            validate_file_handle(kernel, handle, directory=True)
    except BaseException:
        close_directory_handles(tuple(directory_handles))
        raise
    return tuple(directory_handles)  # FILE_SHARE_DELETE를 의도적으로 제외해 ancestor를 pin한다.


def close_directory_handles(directory_handles: tuple[int, ...]) -> None:
    """
    함수 이름: close_directory_handles()
    기능: directory chain을 고정한 native handle을 leaf부터 닫는다.
    인자: directory_handles -> open 순서대로 보관한 Windows handle tuple
    반환값: 없음
    작성 날짜: 2026/09/06
    """
    # 하나의 close 오류가 다른 ancestor handle 누수로 확산되지 않게 모든 handle을 반환한다.
    kernel = load_kernel32()
    for handle in reversed(directory_handles):
        kernel.CloseHandle(handle)
