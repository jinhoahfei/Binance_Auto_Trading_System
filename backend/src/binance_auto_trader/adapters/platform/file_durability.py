"""POSIX directory fsync와 Windows file-buffer flush의 명시적 OS 경계를 제공한다."""

import os
from pathlib import Path


def flush_created_file_metadata(file_path: Path) -> None:
    """
    함수 이름: flush_created_file_metadata()
    기능: POSIX는 parent directory를 fsync하고 Windows는 write handle의 file buffers를 flush한다.
    인자: file_path -> 내용 fsync가 완료되고 이름이 존재하는 파일 경로
    반환값: OS가 제공하는 flush primitive 성공 시 없음
    작성 날짜: 2026/09/06
    """
    if os.name == "nt":
        _flush_windows_file_buffers(file_path)
        return  # Windows는 POSIX directory fsync를 지원한다고 가장하지 않는다.

    # POSIX backup 이름을 durable하게 만드는 기존 parent directory barrier를 유지한다.
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor = os.open(file_path.parent, directory_flags)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _flush_windows_file_buffers(file_path: Path) -> None:
    """
    함수 이름: _flush_windows_file_buffers()
    기능: non-reparse single-link 파일의 native writable handle을 명시적으로 FlushFileBuffers한다.
    인자: file_path -> flush할 기존 파일 경로
    반환값: native file-buffer flush 성공 시 없음
    작성 날짜: 2026/09/06
    """
    from .windows_api import (
        FILE_FLAG_OPEN_REPARSE_POINT,
        FILE_FLAG_WRITE_THROUGH,
        FILE_SHARE_READ_WRITE,
        GENERIC_READ_WRITE,
        INVALID_HANDLE_VALUE,
        load_kernel32,
        validate_file_handle,
    )
    from .windows_paths import close_directory_handles, pin_directory_chain

    # FILE_FLAG_BACKUP_SEMANTICS directory flush를 추측하지 않고 문서화된 writable file API를 쓴다.
    kernel = load_kernel32()
    directory_handles = pin_directory_chain(file_path.absolute().parent)
    handle = None
    try:
        handle = kernel.CreateFileW(
            str(file_path.absolute()), GENERIC_READ_WRITE, FILE_SHARE_READ_WRITE, None, 3,
            FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_WRITE_THROUGH, None,
        )
        if handle in (None, INVALID_HANDLE_VALUE):
            raise OSError("Windows durable file is unavailable")
        validate_file_handle(kernel, handle, directory=False)
        if not kernel.FlushFileBuffers(handle):
            raise OSError("Windows file-buffer flush failed")
    finally:
        if handle not in (None, INVALID_HANDLE_VALUE):
            kernel.CloseHandle(handle)
        close_directory_handles(directory_handles)  # Flush 오류에서도 ancestor handle을 반환한다.
