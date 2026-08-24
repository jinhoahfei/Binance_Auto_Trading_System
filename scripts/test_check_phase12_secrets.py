"""Phase 12 secret scanner의 path·content 경계와 secret-free 출력을 검증한다."""

from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch

from scripts.check_phase12_secrets import (
    KEYCHAIN_READ_TIMEOUT_SECONDS,
    KEYCHAIN_SECURITY_COMMAND,
    MAXIMUM_KEYCHAIN_SECRET_BYTES,
    MINIMUM_CREDENTIAL_CANARY_BYTES,
    SCAN_CHUNK_SIZE,
    file_contains_canary,
    iter_artifact_entries,
    iter_regular_files,
    load_keychain_credential_canaries,
    main,
    parse_arguments,
    path_contains_canary_component,
    validate_keychain_source,
    zeroize_credential_canaries,
)


class PhaseTwelveSecretScannerTests(unittest.TestCase):
    """
    클래스 이름: PhaseTwelveSecretScannerTests
    기능: target 정책, Keychain canary, path·content 누출과 비식별 출력 회귀를 차단한다.
    작성 날짜: 2026/08/24
    """

    def test_missing_explicit_target_fails_closed(self) -> None:
        """
        함수 이름: test_missing_explicit_target_fails_closed()
        기능: 존재하지 않는 산출물 경로가 빈 iterator로 성공하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            missing_target = temporary_path / "missing.app"
            env_path = temporary_path / ".env"

            # Generator를 실제 소비해 target validation이 실행되는 지점을 고정한다.
            with self.assertRaisesRegex(RuntimeError, "존재하지 않거나 읽을 수 없습니다"):
                tuple(iter_regular_files(missing_target, env_path))

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "requires symlink support")
    def test_explicit_symlink_target_fails_closed(self) -> None:
        """
        함수 이름: test_explicit_symlink_target_fails_closed()
        기능: 검사 root를 다른 위치로 바꾸는 symlink target을 명시적으로 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            real_target = temporary_path / "real-artifact"
            real_target.mkdir()
            symlink_target = temporary_path / "artifact-link"
            symlink_target.symlink_to(real_target, target_is_directory=True)

            # Root symlink는 내부 dependency symlink skip과 달리 운영자 입력 오류로 처리한다.
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                tuple(iter_regular_files(symlink_target, temporary_path / ".env"))

    def test_binary_canary_crossing_chunk_boundary_is_detected(self) -> None:
        """
        함수 이름: test_binary_canary_crossing_chunk_boundary_is_detected()
        기능: credential bytes가 두 read chunk에 걸쳐 있어도 exact match를 찾는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        credential_canary = b"phase12-secret-canary"
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_path = Path(temporary_directory) / "artifact.bin"
            prefix = b"x" * (SCAN_CHUNK_SIZE - 5)
            artifact_path.write_bytes(prefix + credential_canary + b"suffix")

            self.assertTrue(
                file_contains_canary(artifact_path, credential_canary)
            )  # 첫 chunk의 마지막 5 byte와 다음 chunk를 overlap해 탐지한다.

    def test_explicit_target_below_default_excluded_directory_is_scanned(
        self,
    ) -> None:
        """
        함수 이름: test_explicit_target_below_default_excluded_directory_is_scanned()
        기능: 기본 root 순회의 target 제외와 target 아래 명시 root 검사를 구분하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            env_path = temporary_path / ".env"
            explicit_root = temporary_path / "target" / "release"
            explicit_root.mkdir(parents=True)
            artifact_path = explicit_root / "artifact.bin"
            artifact_path.write_bytes(b"safe-artifact")

            # Repository root와 같은 기본 순회는 하위 target build directory에 진입하지 않는다.
            default_scan_files = tuple(iter_regular_files(temporary_path, env_path))
            self.assertNotIn(artifact_path, default_scan_files)

            # target 아래 release directory를 root로 명시하면 제외 정책을 우회하지 않고 그 root를 정상 검사한다.
            explicit_scan_files = tuple(iter_regular_files(explicit_root, env_path))
            self.assertEqual(explicit_scan_files, (artifact_path,))

    @patch("scripts.check_phase12_secrets.parse_arguments")
    def test_embedded_file_and_directory_name_canaries_fail_without_raw_paths(
        self,
        parse_arguments_mock,
    ) -> None:
        """
        함수 이름: test_embedded_file_and_directory_name_canaries_fail_without_raw_paths()
        기능: component 접두·접미에 포함된 credential을 탐지하고 raw 값·path를 숨기는지 검증한다.
        인자: parse_arguments_mock -> main argument test double
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        file_name_canary = "phase12-file-name-canary"
        directory_name_canary = "phase12-directory-name-canary"
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            env_path = temporary_path / ".env"
            artifact_root = temporary_path / "artifacts"
            artifact_root.mkdir()

            # 두 credential을 각각 file component 접두와 directory component 접미에 넣는다.
            env_path.write_text(
                f"API_KEY={file_name_canary}\nAPI_SECRET={directory_name_canary}\n",
                encoding="utf-8",
            )
            canary_file_path = artifact_root / f"prefix-{file_name_canary}"
            canary_file_path.write_bytes(b"safe-file-content")
            canary_directory_path = artifact_root / f"{directory_name_canary}-suffix"
            canary_directory_path.mkdir()
            (canary_directory_path / "safe.bin").write_bytes(b"safe-directory-content")
            parse_arguments_mock.return_value = Namespace(
                env=env_path,
                keychain_service=None,
                keychain_accounts=None,
                targets=[artifact_root],
            )
            captured_stdout = StringIO()
            captured_stderr = StringIO()

            # Main output에는 credential 원문과 그 값이 포함된 raw path 대신 ordinal만 남는다.
            with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                exit_status = main()
            scanner_output = captured_stdout.getvalue() + captured_stderr.getvalue()
            self.assertEqual(exit_status, 1)
            self.assertIn("credential canary #1 matched artifact entry #", scanner_output)
            self.assertIn("credential canary #2 matched artifact entry #", scanner_output)
            self.assertIn("path component", scanner_output)
            self.assertNotIn(file_name_canary, scanner_output)
            self.assertNotIn(directory_name_canary, scanner_output)
            self.assertNotIn(str(artifact_root), scanner_output)

    @patch("scripts.check_phase12_secrets.parse_arguments")
    def test_file_content_canary_fails_without_value_or_raw_path(
        self,
        parse_arguments_mock,
    ) -> None:
        """
        함수 이름: test_file_content_canary_fails_without_value_or_raw_path()
        기능: file content 누출을 ordinal로 보고하고 credential 값·raw path를 숨기는지 검증한다.
        인자: parse_arguments_mock -> main argument test double
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        credential_canary = "phase12-content-canary"
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            env_path = temporary_path / ".env"
            artifact_path = temporary_path / "safe-artifact.bin"

            # Safe filename의 content에만 credential exact bytes를 넣어 content 경계를 분리한다.
            env_path.write_text(f"API_KEY={credential_canary}\n", encoding="utf-8")
            artifact_path.write_bytes(
                b"prefix-" + credential_canary.encode("ascii") + b"-suffix"
            )
            parse_arguments_mock.return_value = Namespace(
                env=env_path,
                keychain_service=None,
                keychain_accounts=None,
                targets=[artifact_path],
            )
            captured_stdout = StringIO()
            captured_stderr = StringIO()

            # Content match에도 canary와 file path 대신 secret-free ordinal만 출력한다.
            with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                exit_status = main()
            scanner_output = captured_stdout.getvalue() + captured_stderr.getvalue()
            self.assertEqual(exit_status, 1)
            self.assertIn("credential canary #1 matched artifact entry #1", scanner_output)
            self.assertIn("file content", scanner_output)
            self.assertNotIn(credential_canary, scanner_output)
            self.assertNotIn(str(artifact_path), scanner_output)

    @patch("scripts.check_phase12_secrets.parse_arguments")
    def test_artifact_read_error_does_not_echo_helper_details(
        self,
        parse_arguments_mock,
    ) -> None:
        """
        함수 이름: test_artifact_read_error_does_not_echo_helper_details()
        기능: content read 오류의 credential·raw path 세부사항을 고정 오류로 대체하는지 검증한다.
        인자: parse_arguments_mock -> main argument test double
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        credential_canary = "phase12-read-error-canary"
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            env_path = temporary_path / ".env"
            artifact_path = temporary_path / "safe-artifact.bin"
            env_path.write_text(f"API_KEY={credential_canary}\n", encoding="utf-8")
            artifact_path.write_bytes(b"safe-content")
            parse_arguments_mock.return_value = Namespace(
                env=env_path,
                keychain_service=None,
                keychain_accounts=None,
                targets=[artifact_path],
            )
            captured_stderr = StringIO()
            raw_helper_error = f"read failed: {artifact_path}: {credential_canary}"

            # Helper의 raw detail이 RuntimeError에 있어도 main은 고정 error message만 출력한다.
            with patch(
                "scripts.check_phase12_secrets.file_contains_canary",
                side_effect=RuntimeError(raw_helper_error),
            ):
                with redirect_stderr(captured_stderr):
                    exit_status = main()
            scanner_error = captured_stderr.getvalue()
            self.assertEqual(exit_status, 2)
            self.assertEqual(
                scanner_error,
                "phase12-secret-scan: ERROR: artifact scan failed.\n",
            )
            self.assertNotIn(credential_canary, scanner_error)
            self.assertNotIn(str(artifact_path), scanner_error)

    @patch("scripts.check_phase12_secrets.parse_arguments")
    def test_setup_error_does_not_echo_secret_bearing_target_path(
        self,
        parse_arguments_mock,
    ) -> None:
        """
        함수 이름: test_setup_error_does_not_echo_secret_bearing_target_path()
        기능: 존재하지 않는 target path에 canary가 있어도 고정 오류만 출력하는지 검증한다.
        인자: parse_arguments_mock -> main argument test double
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        credential_canary = "phase12-missing-target-canary"
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            env_path = temporary_path / ".env"
            missing_target = temporary_path / credential_canary
            env_path.write_text(f"API_KEY={credential_canary}\n", encoding="utf-8")
            parse_arguments_mock.return_value = Namespace(
                env=env_path,
                keychain_service=None,
                keychain_accounts=None,
                targets=[missing_target],
            )
            captured_stderr = StringIO()

            # Setup failure는 helper exception의 path를 연결하지 않고 고정 message로 닫는다.
            with redirect_stderr(captured_stderr):
                exit_status = main()
            scanner_error = captured_stderr.getvalue()
            self.assertEqual(exit_status, 2)
            self.assertEqual(
                scanner_error,
                "phase12-secret-scan: ERROR: scan setup failed.\n",
            )
            self.assertNotIn(credential_canary, scanner_error)
            self.assertNotIn(str(missing_target), scanner_error)

    def test_canary_substring_matches_any_path_component(self) -> None:
        """
        함수 이름: test_canary_substring_matches_any_path_component()
        기능: artifact file·directory component의 접두·접미 canary substring을 찾는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        credential_canary = bytearray(b"phase12-parent-canary")
        canary_text = credential_canary.decode("ascii")

        # Canary는 component 전체와 directory 접두, file 접미 위치에서 모두 탐지된다.
        self.assertTrue(
            path_contains_canary_component(
                Path("safe-root") / canary_text / "safe.bin",
                credential_canary,
            )
        )
        self.assertTrue(
            path_contains_canary_component(
                Path("safe-root") / f"prefix-{canary_text}" / "safe.bin",
                credential_canary,
            )
        )
        self.assertTrue(
            path_contains_canary_component(
                Path("safe-root") / "safe-directory" / f"{canary_text}-suffix",
                credential_canary,
            )
        )
        self.assertFalse(
            path_contains_canary_component(
                Path("safe-root") / "safe-directory" / "safe.bin",
                credential_canary,
            )
        )

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "requires symlink support")
    @patch("scripts.check_phase12_secrets.parse_arguments")
    def test_symlink_entry_names_are_scanned_without_following_targets(
        self,
        parse_arguments_mock,
    ) -> None:
        """
        함수 이름: test_symlink_entry_names_are_scanned_without_following_targets()
        기능: file·directory symlink 이름을 검사하되 대상 content와 하위 entry는 따라가지 않는지 검증한다.
        인자: parse_arguments_mock -> main argument test double
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        file_name_canary = "phase12-symlink-file-canary"
        directory_name_canary = "phase12-symlink-directory-canary"
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            env_path = temporary_path / ".env"
            artifact_root = temporary_path / "artifacts"
            artifact_root.mkdir()
            safe_artifact_path = artifact_root / "safe.bin"
            safe_artifact_path.write_bytes(b"safe-content")

            # Link target은 scan root 밖에 두고 content와 하위 이름에도 canary를 넣어 진입 여부를 구분한다.
            external_file = temporary_path / "external-file.bin"
            external_file.write_bytes(file_name_canary.encode("ascii"))
            external_directory = temporary_path / "external-directory"
            external_directory.mkdir()
            external_nested_path = external_directory / directory_name_canary
            external_nested_path.write_bytes(directory_name_canary.encode("ascii"))
            file_symlink_path = artifact_root / f"prefix-{file_name_canary}"
            file_symlink_path.symlink_to(external_file)
            directory_symlink_path = (
                artifact_root / f"{directory_name_canary}-suffix"
            )
            directory_symlink_path.symlink_to(
                external_directory,
                target_is_directory=True,
            )

            artifact_entries = tuple(iter_artifact_entries(artifact_root, env_path))
            regular_files = tuple(iter_regular_files(artifact_root, env_path))
            self.assertIn(file_symlink_path, artifact_entries)
            self.assertIn(directory_symlink_path, artifact_entries)
            self.assertNotIn(external_nested_path, artifact_entries)
            self.assertEqual(regular_files, (safe_artifact_path,))

            env_path.write_text(
                f"API_KEY={file_name_canary}\nAPI_SECRET={directory_name_canary}\n",
                encoding="utf-8",
            )
            parse_arguments_mock.return_value = Namespace(
                env=env_path,
                keychain_service=None,
                keychain_accounts=None,
                targets=[artifact_root],
            )
            captured_stdout = StringIO()
            captured_stderr = StringIO()

            # Link의 lexical path가 ordinal로만 보고되고 raw credential·path는 출력되지 않는다.
            with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                exit_status = main()
            scanner_output = captured_stdout.getvalue() + captured_stderr.getvalue()
            self.assertEqual(exit_status, 1)
            self.assertIn("credential canary #1 matched artifact entry #", scanner_output)
            self.assertIn("credential canary #2 matched artifact entry #", scanner_output)
            self.assertNotIn(file_name_canary, scanner_output)
            self.assertNotIn(directory_name_canary, scanner_output)
            self.assertNotIn(str(artifact_root), scanner_output)

    @patch("scripts.check_phase12_secrets.subprocess.run")
    def test_keychain_accounts_load_from_captured_stdout_without_secret_argv(
        self,
        run_mock,
    ) -> None:
        """
        함수 이름: test_keychain_accounts_load_from_captured_stdout_without_secret_argv()
        기능: 두 Keychain item을 captured stdout으로 읽고 secret이 command argv에 없는지 검증한다.
        인자: run_mock -> subprocess.run test double
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Production security command와 같은 immutable output을 가진 두 item을 준비한다.
        keychain_service = "com.binance-auto.trader.testnet"
        keychain_accounts = ["api-key", "api-secret"]
        api_key_canary = b"phase12-api-key-canary"
        api_secret_canary = b"phase12-api-secret-canary"
        api_key_process_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=api_key_canary + b"\n",
            stderr=b"",
        )
        api_secret_process_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=api_secret_canary + b"\n",
            stderr=b"",
        )
        run_mock.side_effect = [api_key_process_result, api_secret_process_result]

        # 실제 subprocess의 immutable bytes를 사용해 command와 captured reference 폐기를 고정한다.
        credential_canaries = load_keychain_credential_canaries(
            keychain_service,
            keychain_accounts,
        )
        api_key_mutable_canary = credential_canaries["api-key"]
        api_secret_mutable_canary = credential_canaries["api-secret"]
        try:
            self.assertEqual(credential_canaries["api-key"], api_key_canary)
            self.assertEqual(credential_canaries["api-secret"], api_secret_canary)
            expected_common_options = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "check": False,
                "timeout": KEYCHAIN_READ_TIMEOUT_SECONDS,
            }
            run_mock.assert_has_calls([
                call(
                    [
                        KEYCHAIN_SECURITY_COMMAND,
                        "find-generic-password",
                        "-s",
                        keychain_service,
                        "-a",
                        "api-key",
                        "-w",
                    ],
                    **expected_common_options,
                ),
                call(
                    [
                        KEYCHAIN_SECURITY_COMMAND,
                        "find-generic-password",
                        "-s",
                        keychain_service,
                        "-a",
                        "api-secret",
                        "-w",
                    ],
                    **expected_common_options,
                ),
            ])
            command_arguments = [
                argument
                for process_call in run_mock.call_args_list
                for argument in process_call.args[0]
            ]
            self.assertNotIn(api_key_canary.decode("ascii"), command_arguments)
            self.assertNotIn(api_secret_canary.decode("ascii"), command_arguments)
            self.assertEqual(api_key_process_result.stdout, b"")
            self.assertEqual(api_secret_process_result.stdout, b"")
        finally:
            zeroize_credential_canaries(credential_canaries)  # Test에서도 mutable credential lifetime을 제한한다.
        self.assertEqual(credential_canaries, {})  # 폐기된 mapping에는 credential reference가 남지 않는다.
        self.assertEqual(api_key_mutable_canary, bytearray())
        self.assertEqual(api_secret_mutable_canary, bytearray())

    @patch("scripts.check_phase12_secrets.subprocess.run")
    def test_missing_keychain_item_fails_without_raw_error_output(
        self,
        run_mock,
    ) -> None:
        """
        함수 이름: test_missing_keychain_item_fails_without_raw_error_output()
        기능: security의 missing-item status와 raw stderr를 generic scanner 오류로 닫는지 검증한다.
        인자: run_mock -> subprocess.run test double
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        raw_error_output = b"raw-keychain-detail-must-not-leak"
        run_mock.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=44,
            stdout=b"",
            stderr=raw_error_output,
        )

        # Non-zero status의 세부 출력과 account별 실패 정보는 RuntimeError에 반사하지 않는다.
        with self.assertRaises(RuntimeError) as raised_context:
            load_keychain_credential_canaries("valid.service", ["api-key"])
        self.assertEqual(
            str(raised_context.exception),
            "Keychain credential을 읽을 수 없습니다.",
        )
        self.assertNotIn(raw_error_output.decode("ascii"), str(raised_context.exception))

    @patch("scripts.check_phase12_secrets.subprocess.run")
    def test_short_keychain_item_fails_scanner_canary_contract(
        self,
        run_mock,
    ) -> None:
        """
        함수 이름: test_short_keychain_item_fails_scanner_canary_contract()
        기능: packaged app의 non-empty 경계와 별개인 scanner 8-byte canary 최소 길이를 검증한다.
        인자: run_mock -> subprocess.run test double
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        short_outputs = [b"\n", b"x\n"]

        # Empty와 packaged app가 non-empty로 받을 수 있는 1 byte 모두 scanner canary로는 충분하지 않다.
        for short_output in short_outputs:
            with self.subTest(output_size=max(0, len(short_output) - 1)):
                run_mock.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=short_output,
                    stderr=b"",
                )
                with self.assertRaisesRegex(RuntimeError, "scanner canary 계약"):
                    load_keychain_credential_canaries("valid.service", ["api-key"])
        self.assertEqual(MINIMUM_CREDENTIAL_CANARY_BYTES, 8)  # Scanner 전용 최소 길이를 명시한다.

    @patch("scripts.check_phase12_secrets.subprocess.run")
    def test_keychain_item_outside_scanner_canary_contract_fails_closed(
        self,
        run_mock,
    ) -> None:
        """
        함수 이름: test_keychain_item_outside_scanner_canary_contract_fails_closed()
        기능: 공백·non-ASCII·초과 길이 credential을 scanner canary 계약에서 거부하는지 검증한다.
        인자: run_mock -> subprocess.run test double
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_credential_outputs = [
            b"contains space\n",
            b"non-ascii-\xff\n",
            (b"x" * (MAXIMUM_KEYCHAIN_SECRET_BYTES + 1)) + b"\n",
        ]

        # 각 invalid output은 canary scan 전에 같은 generic configuration 오류로 닫는다.
        for invalid_credential_output in invalid_credential_outputs:
            with self.subTest(output_kind=type(invalid_credential_output).__name__):
                run_mock.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=invalid_credential_output,
                    stderr=b"",
                )
                with self.assertRaisesRegex(RuntimeError, "scanner canary 계약"):
                    load_keychain_credential_canaries(
                        "valid.service",
                        ["api-key"],
                    )

    @patch("scripts.check_phase12_secrets.subprocess.run")
    def test_keychain_subprocess_error_fails_without_partial_output(
        self,
        run_mock,
    ) -> None:
        """
        함수 이름: test_keychain_subprocess_error_fails_without_partial_output()
        기능: timeout의 partial credential output을 exception message와 chain에서 제거하는지 검증한다.
        인자: run_mock -> subprocess.run test double
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        partial_secret = b"partial-secret-must-not-leak"
        run_mock.side_effect = subprocess.TimeoutExpired(
            cmd=[KEYCHAIN_SECURITY_COMMAND],
            timeout=KEYCHAIN_READ_TIMEOUT_SECONDS,
            output=partial_secret,
            stderr=b"raw-timeout-detail",
        )

        # Timeout 세부 객체를 원인으로 연결하지 않아 traceback에도 partial secret을 싣지 않는다.
        with self.assertRaises(RuntimeError) as raised_context:
            load_keychain_credential_canaries("valid.service", ["api-secret"])
        self.assertEqual(
            str(raised_context.exception),
            "Keychain credential을 읽을 수 없습니다.",
        )
        self.assertIsNone(raised_context.exception.__cause__)
        self.assertNotIn(partial_secret.decode("ascii"), str(raised_context.exception))
        self.assertIsNone(run_mock.side_effect.output)  # Timeout 객체의 partial stdout 참조도 제거한다.
        self.assertIsNone(run_mock.side_effect.stderr)  # Raw stderr도 RuntimeError 밖에서 즉시 폐기한다.

    def test_keychain_source_validation_rejects_missing_invalid_and_duplicate_values(
        self,
    ) -> None:
        """
        함수 이름: test_keychain_source_validation_rejects_missing_invalid_and_duplicate_values()
        기능: service/account 누락, 제어 가능 문자와 중복 account를 subprocess 전에 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_sources = [
            ("", ["api-key"]),
            ("valid.service", None),
            ("valid.service", []),
            ("valid.service", [""]),
            ("../invalid", ["api-key"]),
            ("valid.service", ["api-key", "api-key"]),
        ]

        # 모든 invalid 조합이 같은 fail-closed validation 경계를 통과하지 못하게 한다.
        for keychain_service, keychain_accounts in invalid_sources:
            with self.subTest(
                keychain_service=keychain_service,
                keychain_accounts=keychain_accounts,
            ):
                with self.assertRaises(RuntimeError):
                    validate_keychain_source(keychain_service, keychain_accounts)

    def test_explicit_env_and_keychain_sources_are_mutually_exclusive(self) -> None:
        """
        함수 이름: test_explicit_env_and_keychain_sources_are_mutually_exclusive()
        기능: 한 command에서 .env와 Keychain source를 함께 지정할 수 없는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        captured_stderr = StringIO()

        # argparse의 source group이 scan이나 subprocess 실행 전에 충돌을 exit status 2로 닫는다.
        with redirect_stderr(captured_stderr):
            with self.assertRaises(SystemExit) as raised_context:
                parse_arguments([
                    "--env",
                    ".env",
                    "--keychain-service",
                    "valid.service",
                    "--keychain-account",
                    "api-key",
                ])
        self.assertEqual(raised_context.exception.code, 2)  # CLI usage 오류의 표준 status를 유지한다.


if __name__ == "__main__":
    unittest.main()  # 단독 실행과 unittest discovery가 같은 검증 집합을 사용한다.
