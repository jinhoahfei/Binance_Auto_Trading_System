"""실제 Windows 실행 증거와 구분한 ABI·경합·path·durability adapter 계약을 검증한다."""

import ctypes
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from binance_auto_trader.adapters.platform import file_durability, runtime_lock
from binance_auto_trader.adapters.platform import windows_api, windows_paths, windows_runtime


class WindowsPlatformContractTests(unittest.TestCase):
    """
    클래스 이름: WindowsPlatformContractTests
    기능: host에 관계없이 Windows ABI와 fail-closed 기술 경계의 계약을 검증한다.
    작성 날짜: 2026/09/06
    """

    def test_windows_structures_keep_llp64_layout(self) -> None:
        """
        함수 이름: test_windows_structures_keep_llp64_layout()
        기능: non-Windows C long 크기가 Windows DWORD와 struct layout을 바꾸지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        # Windows x64 OVERLAPPED와 file information은 각각 32 bytes와 52 bytes다.
        self.assertEqual(ctypes.sizeof(windows_api.DWORD), 4)
        self.assertEqual(ctypes.sizeof(windows_api.WindowsFileInformation), 52)
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(ctypes.sizeof(windows_api.WindowsOverlapped), 32)

    def test_import_transport_does_not_require_fcntl(self) -> None:
        """
        함수 이름: test_import_transport_does_not_require_fcntl()
        기능: POSIX-only module을 사용할 수 없는 interpreter에서도 transport import가 가능한지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        # sys.modules의 None marker는 import fcntl 시 즉시 실패하므로 eager import 회귀를 탐지한다.
        result = subprocess.run(
            [sys.executable, "-c", (
                "import sys; sys.modules['fcntl'] = None; "
                "import binance_auto_trader.transport.app"
            )],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_app_data_rejects_other_user_unc_ads_and_traversal(self) -> None:
        """
        함수 이름: test_app_data_rejects_other_user_unc_ads_and_traversal()
        기능: local current-user exact app-data 경로 밖을 directory로 승인하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        expected = PureWindowsPath(r"C:\Users\user\AppData\Local\com.binance-auto.trader")
        windows_paths.validate_app_data_path(expected, expected)

        # 대소문자 비교는 Windows 경로 의미를 따르되 다른 사용자와 namespace 우회는 거부한다.
        windows_paths.validate_app_data_path(PureWindowsPath(str(expected).upper()), expected)
        for invalid in (
            r"C:\Users\other\AppData\Local\com.binance-auto.trader",
            r"\\server\share\com.binance-auto.trader",
            r"\\?\C:\Users\user\AppData\Local\com.binance-auto.trader",
            str(expected) + r"\..\com.binance-auto.trader",
            str(expected) + ":alternate",
            "com.binance-auto.trader",
        ):
            with self.subTest(path=invalid), self.assertRaises(OSError):
                windows_paths.validate_app_data_path(PureWindowsPath(invalid), expected)

    def test_file_metadata_rejects_reparse_directory_and_hardlink(self) -> None:
        """
        함수 이름: test_file_metadata_rejects_reparse_directory_and_hardlink()
        기능: path 사전 검사와 무관하게 실제 open handle의 unsafe metadata를 거부하는지 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        for attributes, links in ((0x400, 1), (0x10, 1), (0, 2), (0, 0)):
            with self.subTest(attributes=attributes, links=links):
                def provide_metadata(handle, information_pointer):
                    """
                    함수 이름: provide_metadata()
                    기능: 검증할 synthetic native handle metadata를 output pointer에 채운다.
                    인자: handle -> 조회 대상 synthetic handle
                        information_pointer -> WindowsFileInformation output pointer
                    반환값: native success 값 1
                    작성 날짜: 2026/09/06
                    """
                    # ctypes 구조에 직접 값을 채워 native output ABI를 사용하는 분기를 검증한다.
                    information = ctypes.cast(
                        information_pointer, ctypes.POINTER(windows_api.WindowsFileInformation)
                    ).contents
                    information.attributes = attributes
                    information.number_of_links = links
                    return 1

                kernel = SimpleNamespace(GetFileInformationByHandle=provide_metadata)
                with self.assertRaises(OSError):
                    windows_api.validate_file_handle(kernel, 30, directory=False)

    def test_metadata_api_failure_is_not_treated_as_safe(self) -> None:
        """
        함수 이름: test_metadata_api_failure_is_not_treated_as_safe()
        기능: Windows metadata 조회 실패가 zero-initialized struct 승인으로 이어지지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        # 실패한 API는 파일 종류가 알려지지 않았으므로 directory와 file 모두 거부한다.
        kernel = MagicMock()
        kernel.GetFileInformationByHandle.return_value = 0
        for directory in (False, True):
            with self.assertRaises(OSError):
                windows_api.validate_file_handle(kernel, 30, directory=directory)

    def test_directory_chain_pins_ancestors_without_delete_sharing(self) -> None:
        """
        함수 이름: test_directory_chain_pins_ancestors_without_delete_sharing()
        기능: root부터 leaf까지 no-reparse/no-delete-share handle을 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        directory = Path(tempfile.gettempdir()) / "platform-contract"
        kernel = MagicMock()
        kernel.CreateFileW.side_effect = range(10, 10 + len(directory.parents) + 1)
        with (
            patch.object(windows_paths, "load_kernel32", return_value=kernel),
            patch.object(windows_paths, "validate_file_handle") as validate,
        ):
            handles = windows_paths.pin_directory_chain(directory)

        # FILE_SHARE_DELETE 비트 4는 없고 OPEN_REPARSE_POINT와 BACKUP_SEMANTICS를 함께 요청한다.
        self.assertEqual(len(handles), len(directory.parents) + 1)
        self.assertEqual(validate.call_count, len(handles))
        calls = kernel.CreateFileW.call_args_list
        self.assertEqual(calls[0].args[0], str(directory.anchor))
        for call in calls:
            self.assertEqual(call.args[2], 3)
            self.assertEqual(call.args[5], 0x02200000)

    def test_directory_metadata_failure_closes_all_handles(self) -> None:
        """
        함수 이름: test_directory_metadata_failure_closes_all_handles()
        기능: 중간 ancestor가 reparse인 경우 이미 열린 chain도 누수 없이 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        kernel = MagicMock()
        kernel.CreateFileW.side_effect = (30, 31)
        with (
            patch.object(windows_paths, "load_kernel32", return_value=kernel),
            patch.object(windows_paths, "validate_file_handle", side_effect=[None, OSError("reparse")]),
        ):
            with self.assertRaises(OSError):
                windows_paths.pin_directory_chain(Path(tempfile.gettempdir()) / "child")

        # 실패 handle까지 포함한 역순 close가 경로 pin을 모두 반환한다.
        self.assertEqual([call.args[0] for call in kernel.CloseHandle.call_args_list], [31, 30])

    def test_lock_is_nonblocking_and_matches_native_parent_range(self) -> None:
        """
        함수 이름: test_lock_is_nonblocking_and_matches_native_parent_range()
        기능: Python이 Rust parent와 동일 byte 범위·즉시 실패 옵션으로 lock을 요청하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        kernel = MagicMock()
        kernel.CreateFileW.return_value = 50
        kernel.LockFileEx.return_value = 0
        with (
            patch.dict(sys.modules, {"msvcrt": MagicMock()}),
            patch.object(windows_runtime, "load_kernel32", return_value=kernel),
            patch.object(windows_runtime, "validate_app_data_path"),
            patch.object(windows_runtime, "get_local_app_data_directory", return_value=Path("app")),
            patch.object(windows_runtime, "pin_directory_chain", return_value=(10, 11)),
            patch.object(windows_runtime, "validate_file_handle"),
            patch.object(windows_runtime, "close_directory_handles") as close_directories,
        ):
            with self.assertRaises(OSError):
                windows_runtime.acquire_windows_runtime_lock(Path("app/.backend-runtime.lock"))

        # 충돌 실패는 retry/sleep이나 artifact 읽기 없이 native handle과 pin을 모두 반환한다.
        self.assertEqual(kernel.LockFileEx.call_args.args[:5], (50, 3, 0, 1, 0))
        kernel.CloseHandle.assert_called_once_with(50)
        close_directories.assert_called_once_with((10, 11))

    def test_unlock_uses_same_file_handle_and_byte_range(self) -> None:
        """
        함수 이름: test_unlock_uses_same_file_handle_and_byte_range()
        기능: CRT descriptor의 원래 handle과 matching range로 unlock하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        # File을 다시 열면 Windows의 동일-process 다른 handle에도 lock이 적용되므로 원 handle을 쓴다.
        kernel = MagicMock()
        crt = MagicMock()
        crt.get_osfhandle.return_value = 80
        with (
            patch.dict(sys.modules, {"msvcrt": crt}),
            patch.object(windows_runtime, "load_kernel32", return_value=kernel),
        ):
            windows_runtime.unlock_windows_runtime_file(9)

        self.assertEqual(kernel.UnlockFileEx.call_args.args[:4], (80, 0, 1, 0))
        crt.get_osfhandle.assert_called_once_with(9)

    def test_windows_flush_failure_propagates_and_releases_handles(self) -> None:
        """
        함수 이름: test_windows_flush_failure_propagates_and_releases_handles()
        기능: FlushFileBuffers 실패를 durable 성공으로 숨기지 않고 모든 native handle을 닫는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        kernel = MagicMock()
        kernel.CreateFileW.return_value = 60
        kernel.FlushFileBuffers.return_value = 0
        with (
            patch.object(windows_api, "load_kernel32", return_value=kernel),
            patch.object(windows_api, "validate_file_handle"),
            patch.object(windows_paths, "pin_directory_chain", return_value=(10, 11)),
            patch.object(windows_paths, "close_directory_handles") as close_directories,
        ):
            with self.assertRaises(OSError):
                file_durability._flush_windows_file_buffers(Path("journal.jsonl"))

        # OPEN_EXISTING은 실패 때 없는 journal을 생성하지 않으며 writable 권한을 명시한다.
        self.assertEqual(kernel.CreateFileW.call_args.args[1], 0xC0000000)
        self.assertEqual(kernel.CreateFileW.call_args.args[4], 3)
        kernel.CloseHandle.assert_called_once_with(60)
        close_directories.assert_called_once_with((10, 11))

    @unittest.skipIf(os.name == "nt", "POSIX regression requires POSIX host")
    def test_posix_lock_rejects_hardlink_and_symlink(self) -> None:
        """
        함수 이름: test_posix_lock_rejects_hardlink_and_symlink()
        기능: Windows adapter 추가 뒤에도 POSIX single-link no-follow 보안 경계가 유지되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.write_bytes(b"canary")
            os.link(target, root / "hardlink")
            (root / "symlink").symlink_to(target)

            # 두 우회 모두 읽기·truncate·permission 변경 전에 거부하고 원본 bytes를 보존한다.
            for name in ("hardlink", "symlink"):
                with self.assertRaises((OSError, RuntimeError)):
                    runtime_lock.acquire_runtime_file_lock(root / name)
            self.assertEqual(target.read_bytes(), b"canary")

    @unittest.skipUnless(os.name == "nt", "Windows native Session 6 execution required")
    def test_native_windows_lock_contention_and_reacquire(self) -> None:
        """
        함수 이름: test_native_windows_lock_contention_and_reacquire()
        기능: native Windows에서 실제 LockFileEx 경합과 해제 후 재획득을 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            lock_path = directory / ".backend-runtime.lock"
            with patch.object(windows_runtime, "get_local_app_data_directory", return_value=directory):
                descriptor, handles = windows_runtime.acquire_windows_runtime_lock(lock_path)
                try:
                    with self.assertRaises(OSError):
                        windows_runtime.acquire_windows_runtime_lock(lock_path)
                finally:
                    windows_runtime.unlock_windows_runtime_file(descriptor)
                    os.close(descriptor)
                    windows_paths.close_directory_handles(handles)

                # 같은 파일 이름을 삭제하지 않고 OS lock 해제 뒤 다음 owner를 허용한다.
                descriptor, handles = windows_runtime.acquire_windows_runtime_lock(lock_path)
                windows_runtime.unlock_windows_runtime_file(descriptor)
                os.close(descriptor)
                windows_paths.close_directory_handles(handles)
