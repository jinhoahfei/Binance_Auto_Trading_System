"""Windows runtime artifact의 nonblocking byte lock과 handle 수명을 구현한다."""

import ctypes
import os
from pathlib import Path, PureWindowsPath

from .windows_api import (
    FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_FLAG_WRITE_THROUGH,
    FILE_SHARE_READ_WRITE,
    GENERIC_READ_WRITE,
    INVALID_HANDLE_VALUE,
    WindowsOverlapped,
    load_kernel32,
    validate_file_handle,
)
from .windows_paths import (
    close_directory_handles,
    get_local_app_data_directory,
    pin_directory_chain,
    validate_app_data_path,
)


def acquire_windows_runtime_lock(lock_path: Path) -> tuple[int, tuple[int, ...]]:
    """
    함수 이름: acquire_windows_runtime_lock()
    기능: current-user non-reparse 경로의 ownership 파일에 LockFileEx lifetime lock을 건다.
    인자: lock_path -> 고정 app-data 아래의 ownership artifact 경로
    반환값: binary CRT descriptor와 lifetime 동안 보유할 ancestor handle tuple
    작성 날짜: 2026/09/06
    """
    import msvcrt

    # Native known folder와 exact 경로를 일치시킨 뒤 ancestor 교체를 먼저 차단한다.
    validate_app_data_path(
        PureWindowsPath(lock_path.parent), PureWindowsPath(get_local_app_data_directory())
    )
    directory_handles = pin_directory_chain(lock_path.parent)
    kernel = load_kernel32()
    handle = None
    descriptor = None
    try:
        handle = kernel.CreateFileW(
            str(lock_path), GENERIC_READ_WRITE, FILE_SHARE_READ_WRITE, None, 4,
            FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_WRITE_THROUGH, None,
        )
        if handle in (None, INVALID_HANDLE_VALUE):
            raise OSError("Windows runtime lock is unavailable")
        validate_file_handle(kernel, handle, directory=False)

        # Rust와 같은 offset 0, length 1을 exclusive·fail-immediately로 잠근다.
        overlap = WindowsOverlapped()
        if not kernel.LockFileEx(handle, 0x3, 0, 1, 0, ctypes.byref(overlap)):
            raise OSError("Windows runtime lock is already held or unavailable")
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        handle = None  # open_osfhandle 성공부터 CRT descriptor가 native handle을 소유한다.
        os.set_inheritable(descriptor, False)
        return descriptor, directory_handles
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        elif handle not in (None, INVALID_HANDLE_VALUE):
            kernel.CloseHandle(handle)
        close_directory_handles(directory_handles)
        raise


def unlock_windows_runtime_file(descriptor: int) -> None:
    """
    함수 이름: unlock_windows_runtime_file()
    기능: native parent와 공유한 byte lock의 동일 offset·length를 해제한다.
    인자: descriptor -> LockFileEx를 획득한 binary CRT descriptor
    반환값: 없음
    작성 날짜: 2026/09/06
    """
    import msvcrt

    # 별도 handle로 다시 열지 않고 획득 당시 동일 handle에서만 unlock한다.
    kernel = load_kernel32()
    overlap = WindowsOverlapped()
    handle = msvcrt.get_osfhandle(descriptor)
    if not kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlap)):
        raise OSError("Windows runtime lock release failed")
