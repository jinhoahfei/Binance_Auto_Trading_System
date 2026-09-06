"""Windows x64 kernel ABI와 handle metadata를 lazy loading하는 기술 경계를 정의한다."""

import ctypes


# Windows LLP64 크기를 host의 C long 크기와 분리해 non-Windows ABI test도 정확하게 만든다.
DWORD = ctypes.c_uint32
HANDLE = ctypes.c_void_p
BOOL = ctypes.c_int32
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_WRITE_THROUGH = 0x80000000
FILE_SHARE_READ_WRITE = 0x3
GENERIC_READ_WRITE = 0xC0000000


class WindowsOverlapped(ctypes.Structure):
    """
    클래스 이름: WindowsOverlapped
    기능: LockFileEx의 zero-offset synchronous OVERLAPPED ABI를 표현한다.
    작성 날짜: 2026/09/06
    """

    _fields_ = [
        ("internal", ctypes.c_size_t),
        ("internal_high", ctypes.c_size_t),
        ("offset", DWORD),
        ("offset_high", DWORD),
        ("event", HANDLE),
    ]


class WindowsFileInformation(ctypes.Structure):
    """
    클래스 이름: WindowsFileInformation
    기능: 경로 재해석과 hard link를 거부할 BY_HANDLE_FILE_INFORMATION ABI를 표현한다.
    작성 날짜: 2026/09/06
    """

    _fields_ = [
        ("attributes", DWORD),
        ("creation_time_low", DWORD),
        ("creation_time_high", DWORD),
        ("access_time_low", DWORD),
        ("access_time_high", DWORD),
        ("write_time_low", DWORD),
        ("write_time_high", DWORD),
        ("volume_serial", DWORD),
        ("file_size_high", DWORD),
        ("file_size_low", DWORD),
        ("number_of_links", DWORD),
        ("file_index_high", DWORD),
        ("file_index_low", DWORD),
    ]


def load_kernel32():
    """
    함수 이름: load_kernel32()
    기능: Windows API를 명시적 x64 handle·DWORD signature로 불러온다.
    인자: 없음
    반환값: signature 설정이 완료된 kernel32 library
    작성 날짜: 2026/09/06
    """
    # Module import는 어떤 OS에서도 가능하지만 native library 실행은 Windows 호출에만 허용한다.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        ctypes.c_wchar_p, DWORD, DWORD, ctypes.c_void_p, DWORD, DWORD, HANDLE,
    ]
    kernel.CreateFileW.restype = HANDLE
    kernel.GetFileInformationByHandle.argtypes = [
        HANDLE, ctypes.POINTER(WindowsFileInformation),
    ]
    kernel.GetFileInformationByHandle.restype = BOOL
    kernel.LockFileEx.argtypes = [
        HANDLE, DWORD, DWORD, DWORD, DWORD, ctypes.POINTER(WindowsOverlapped),
    ]
    kernel.LockFileEx.restype = BOOL
    kernel.UnlockFileEx.argtypes = [
        HANDLE, DWORD, DWORD, DWORD, ctypes.POINTER(WindowsOverlapped),
    ]
    kernel.UnlockFileEx.restype = BOOL
    kernel.FlushFileBuffers.argtypes = [HANDLE]
    kernel.FlushFileBuffers.restype = BOOL
    kernel.CloseHandle.argtypes = [HANDLE]
    kernel.CloseHandle.restype = BOOL
    return kernel  # ctypes의 기본 int 반환형이 64-bit HANDLE을 자르지 못하게 한다.


def validate_file_handle(kernel, handle: int, *, directory: bool) -> None:
    """
    함수 이름: validate_file_handle()
    기능: 열린 handle이 reparse point가 아니며 요구한 directory 또는 single-link 파일인지 검증한다.
    인자: kernel -> 명시적 ABI의 kernel32 library
        handle -> 실제 열어 고정한 파일 또는 directory handle
        directory -> directory이면 True, ownership regular file이면 False
    반환값: 없음
    작성 날짜: 2026/09/06
    """
    # Path string의 사전 검사 대신 실제 open object metadata를 판정한다.
    information = WindowsFileInformation()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(information)):
        raise OSError("Windows file metadata is unavailable")
    if information.attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError("Windows reparse point is not allowed")
    if bool(information.attributes & FILE_ATTRIBUTE_DIRECTORY) != directory:
        raise OSError("Windows file type is invalid")
    if not directory and information.number_of_links != 1:
        raise OSError("Windows runtime file must have one link")
