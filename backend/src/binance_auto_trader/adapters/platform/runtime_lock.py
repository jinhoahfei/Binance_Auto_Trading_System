"""OS별 nonblocking lock primitive를 runtime ownership lifecycle에 연결한다."""

import os
from pathlib import Path
import stat


def acquire_runtime_file_lock(lock_path: Path) -> tuple[int, tuple[int, ...]]:
    """
    함수 이름: acquire_runtime_file_lock()
    기능: 검증한 regular single-link 파일의 nonblocking exclusive lock을 획득한다.
    인자: lock_path -> lifetime ownership artifact 파일 경로
    반환값: 소유권을 인수할 descriptor와 Windows ancestor handle tuple
    작성 날짜: 2026/09/06
    """
    if os.name == "nt":
        from .windows_runtime import acquire_windows_runtime_lock

        return acquire_windows_runtime_lock(lock_path)  # Windows API는 선택된 OS에서만 불러온다.

    # POSIX의 owner-only permission, no-follow open과 flock 의미를 그대로 보존한다.
    import fcntl

    open_flags = os.O_CREAT | os.O_RDWR
    open_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, open_flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
        ):
            raise RuntimeError("runtime ownership lock file is invalid")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, ()


def unlock_runtime_file(descriptor: int) -> None:
    """
    함수 이름: unlock_runtime_file()
    기능: artifact descriptor를 닫기 전 현재 OS의 lifetime lock을 해제한다.
    인자: descriptor -> exclusive lock을 보유한 열린 descriptor
    반환값: 없음
    작성 날짜: 2026/09/06
    """
    if os.name == "nt":
        from .windows_runtime import unlock_windows_runtime_file

        unlock_windows_runtime_file(descriptor)
        return  # File descriptor close 책임은 호출 lifecycle에 남긴다.

    # POSIX advisory lock 해제는 기존 flock primitive를 사용한다.
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def close_runtime_directory_handles(directory_handles: tuple[int, ...]) -> None:
    """
    함수 이름: close_runtime_directory_handles()
    기능: Windows app-data 경로를 고정한 ancestor handle을 역순으로 반환한다.
    인자: directory_handles -> acquire에서 전달된 handle tuple
    반환값: 없음
    작성 날짜: 2026/09/06
    """
    if not directory_handles:
        return  # POSIX adapter는 별도 Windows handle을 만들지 않는다.

    # 파일이 닫힌 다음 경로 pin을 풀어 lifetime 중 ancestor 교체를 막는다.
    from .windows_runtime import close_directory_handles

    close_directory_handles(directory_handles)
