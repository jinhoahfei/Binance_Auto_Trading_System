"""고정 Keychain→Testnet unittest exec runner의 memory-only 경계를 검증한다."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import redirect_stderr
import hashlib
from io import StringIO
import os
from pathlib import Path
import resource
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, call, patch

from scripts.run_testnet_from_keychain import (
    BACKEND_ROOT,
    BACKEND_SOURCE_ROOT,
    BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV,
    BINANCE_RUN_TESTNET_ENV,
    BINANCE_RUN_TESTNET_ORDERS_ENV,
    BINANCE_TESTNET_API_KEY_ENV,
    BINANCE_TESTNET_API_SECRET_ENV,
    BINANCE_TESTNET_BASELINE_HISTORY_FD_ENV,
    BINANCE_TESTNET_BASELINE_HISTORY_SHA256_ENV,
    BINANCE_TESTNET_BASELINE_PENDING_FD_ENV,
    BINANCE_TESTNET_BASELINE_PENDING_SHA256_ENV,
    BINANCE_TESTNET_MAX_NOTIONAL_ENV,
    KEYCHAIN_API_KEY_ACCOUNT,
    KEYCHAIN_API_SECRET_ACCOUNT,
    KEYCHAIN_READ_TIMEOUT_SECONDS,
    KEYCHAIN_SECURITY_COMMAND,
    KEYCHAIN_SERVICE,
    MODE_TEST_MODULES,
    TestnetKeychainRunnerError,
    TestnetRunnerArguments,
    build_child_environment,
    build_unittest_command,
    close_verified_baseline_history,
    execute_testnet_mode,
    harden_runner_process,
    main,
    open_verified_baseline_history,
    parse_arguments,
    read_keychain_credential,
    validate_baseline_history_path,
    zeroize_secret_buffer,
)


API_KEY_CANARY = b"runner-api-key-canary"
API_SECRET_CANARY = b"runner-api-secret-canary"


class TestnetKeychainRunnerTests(unittest.TestCase):
    """
    클래스 이름: TestnetKeychainRunnerTests
    기능: Runner가 fixed source, mode, environment와 secret-free failure를 강제하는지 검증한다.
    작성 날짜: 2026/08/31
    """

    def test_modes_build_only_exact_unittest_commands(self) -> None:
        """
        함수 이름: test_modes_build_only_exact_unittest_commands()
        기능: 각 허용 mode가 discovery 없이 합의된 unittest module 하나만 선택하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        python_executable = Path("/opt/fixed/python")

        # 두 mode 모두 같은 Python framing을 사용하고 마지막 module만 fixed mapping에서 선택한다.
        for mode, test_module in MODE_TEST_MODULES.items():
            with self.subTest(mode=mode):
                command = build_unittest_command(
                    mode,
                    python_executable=python_executable,
                )
                self.assertEqual(
                    command,
                    (
                        os.fspath(python_executable),
                        "-B",
                        "-m",
                        "unittest",
                        "-q",
                        "-f",
                        test_module,
                    ),
                )
                self.assertNotIn("discover", command)

        with self.assertRaises(TestnetKeychainRunnerError):
            build_unittest_command(
                "arbitrary-module",
                python_executable=python_executable,
            )

    def test_read_only_environment_has_no_order_cap_or_parent_values(self) -> None:
        """
        함수 이름: test_read_only_environment_has_no_order_cap_or_parent_values()
        기능: Read-only child가 두 credential과 닫힌 권한 외 parent 환경을 상속하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        api_key_buffer = bytearray(API_KEY_CANARY)
        api_secret_buffer = bytearray(API_SECRET_CANARY)
        try:
            # Hostile proxy와 cap은 source로 받지도 않고 새 fixed-key mapping만 생성한다.
            child_environment = build_child_environment(
                "read-only",
                api_key_buffer=api_key_buffer,
                api_secret_buffer=api_secret_buffer,
            )
            self.assertEqual(
                frozenset(child_environment),
                frozenset(
                    {
                        "PYTHONDONTWRITEBYTECODE",
                        "PYTHONPATH",
                        "PYTHONUNBUFFERED",
                        "PYTHONWARNINGS",
                        BINANCE_RUN_TESTNET_ENV,
                        BINANCE_TESTNET_API_KEY_ENV,
                        BINANCE_TESTNET_API_SECRET_ENV,
                        BINANCE_RUN_TESTNET_ORDERS_ENV,
                        BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV,
                    }
                ),
            )
            self.assertEqual(child_environment[BINANCE_RUN_TESTNET_ENV], "1")
            self.assertEqual(child_environment[BINANCE_RUN_TESTNET_ORDERS_ENV], "0")
            self.assertEqual(child_environment[BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV], "0")
            self.assertNotIn(BINANCE_TESTNET_MAX_NOTIONAL_ENV, child_environment)
            self.assertEqual(
                child_environment["PYTHONPATH"],
                os.fspath(BACKEND_SOURCE_ROOT),
            )
            self.assertNotIn("HTTP_PROXY", child_environment)
        finally:
            zeroize_secret_buffer(api_key_buffer)
            zeroize_secret_buffer(api_secret_buffer)

    def test_phase13_environment_has_three_opt_ins_and_exact_cap(self) -> None:
        """
        함수 이름: test_phase13_environment_has_three_opt_ins_and_exact_cap()
        기능: Public Case 2 child가 세 opt-in과 100 USDT cap을 정확히 활성화하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        api_key_buffer = bytearray(API_KEY_CANARY)
        api_secret_buffer = bytearray(API_SECRET_CANARY)
        try:
            # Actual mode의 추가 key는 absolute maximum notional 하나뿐이어야 한다.
            child_environment = build_child_environment(
                "phase13-public-case2",
                api_key_buffer=api_key_buffer,
                api_secret_buffer=api_secret_buffer,
            )
            self.assertEqual(child_environment[BINANCE_RUN_TESTNET_ENV], "1")
            self.assertEqual(child_environment[BINANCE_RUN_TESTNET_ORDERS_ENV], "1")
            self.assertEqual(child_environment[BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV], "1")
            self.assertEqual(child_environment[BINANCE_TESTNET_MAX_NOTIONAL_ENV], "100")
            self.assertEqual(
                child_environment[BINANCE_TESTNET_API_KEY_ENV],
                API_KEY_CANARY.decode("ascii"),
            )
            self.assertEqual(
                child_environment[BINANCE_TESTNET_API_SECRET_ENV],
                API_SECRET_CANARY.decode("ascii"),
            )
        finally:
            zeroize_secret_buffer(api_key_buffer)
            zeroize_secret_buffer(api_secret_buffer)

    def test_verified_baseline_is_optional_and_shared_by_both_modes(self) -> None:
        """
        함수 이름: test_verified_baseline_is_optional_and_shared_by_both_modes()
        기능: Workspace canonical baseline만 두 fixed mode의 optional child env로 전달되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        api_key_buffer = bytearray(API_KEY_CANARY)
        api_secret_buffer = bytearray(API_SECRET_CANARY)
        try:
            with TemporaryDirectory() as temporary_directory:
                # Test-local artifact root 아래 exact history.jsonl regular file을 만든다.
                artifact_root = Path(temporary_directory) / ".testnet-artifacts"
                run_directory = artifact_root / "verified-run"
                run_directory.mkdir(parents=True)
                baseline_history_path = run_directory / "history.jsonl"
                baseline_history_path.write_text("{}\n", encoding="utf-8")
                baseline_history_path.chmod(0o600)
                pending_history_path = baseline_history_path.with_name(
                    f"{baseline_history_path.name}.pending-orders.jsonl"
                )
                pending_history_path.write_text("{}\n", encoding="utf-8")
                pending_history_path.chmod(0o600)

                with patch(
                    "scripts.run_testnet_from_keychain.BASELINE_ARTIFACT_ROOT",
                    artifact_root,
                ):
                    verified_baseline = open_verified_baseline_history(
                        baseline_history_path
                    )
                    try:
                        # 두 mode는 path 대신 같은 pinned inode descriptor와 digest만 공유한다.
                        for mode in MODE_TEST_MODULES:
                            with self.subTest(mode=mode):
                                child_environment = build_child_environment(
                                    mode,
                                    api_key_buffer=api_key_buffer,
                                    api_secret_buffer=api_secret_buffer,
                                    verified_baseline_history=verified_baseline,
                                )
                                self.assertEqual(
                                    child_environment[
                                        BINANCE_TESTNET_BASELINE_HISTORY_FD_ENV
                                    ],
                                    str(verified_baseline.descriptor),
                                )
                                self.assertEqual(
                                    child_environment[
                                        BINANCE_TESTNET_BASELINE_HISTORY_SHA256_ENV
                                    ],
                                    hashlib.sha256(b"{}\n").hexdigest(),
                                )
                                self.assertEqual(
                                    child_environment[
                                        BINANCE_TESTNET_BASELINE_PENDING_FD_ENV
                                    ],
                                    str(verified_baseline.pending_descriptor),
                                )
                                self.assertEqual(
                                    child_environment[
                                        BINANCE_TESTNET_BASELINE_PENDING_SHA256_ENV
                                    ],
                                    hashlib.sha256(b"{}\n").hexdigest(),
                                )
                    finally:
                        close_verified_baseline_history(verified_baseline)
        finally:
            zeroize_secret_buffer(api_key_buffer)
            zeroize_secret_buffer(api_secret_buffer)

    def test_baseline_validation_rejects_external_symlink_and_wrong_name(self) -> None:
        """
        함수 이름: test_baseline_validation_rejects_external_symlink_and_wrong_name()
        기능: Relative, 외부, symlink와 history.jsonl 아닌 baseline 입력을 모두 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            artifact_root = temporary_root / ".testnet-artifacts"
            run_directory = artifact_root / "verified-run"
            run_directory.mkdir(parents=True)
            valid_history_path = run_directory / "history.jsonl"
            valid_history_path.write_text("{}\n", encoding="utf-8")
            valid_history_path.chmod(0o600)
            wrong_name_path = run_directory / "other.jsonl"
            wrong_name_path.write_text("{}\n", encoding="utf-8")
            external_history_path = temporary_root / "history.jsonl"
            external_history_path.write_text("{}\n", encoding="utf-8")
            symlink_history_path = artifact_root / "history.jsonl"
            symlink_history_path.symlink_to(valid_history_path)

            # Path shape가 달라도 모두 credential 조회 전 같은 fail-closed validator에서 중단한다.
            invalid_paths = (
                "relative/history.jsonl",
                f" {valid_history_path}",
                os.fspath(wrong_name_path),
                os.fspath(external_history_path),
                os.fspath(symlink_history_path),
                os.fspath(run_directory / "missing" / "history.jsonl"),
            )
            with patch(
                "scripts.run_testnet_from_keychain.BASELINE_ARTIFACT_ROOT",
                artifact_root,
            ):
                self.assertEqual(
                    validate_baseline_history_path(os.fspath(valid_history_path)),
                    valid_history_path.resolve(),
                )
                for mode in MODE_TEST_MODULES:
                    with self.subTest(parser_mode=mode):
                        self.assertEqual(
                            parse_arguments(
                                [
                                    mode,
                                    "--baseline-history",
                                    os.fspath(valid_history_path),
                                ]
                            ),
                            TestnetRunnerArguments(
                                mode,
                                valid_history_path.resolve(),
                            ),
                        )
                for invalid_path in invalid_paths:
                    with self.subTest(invalid_path_kind=Path(invalid_path).name):
                        with self.assertRaises(TestnetKeychainRunnerError):
                            validate_baseline_history_path(invalid_path)

    def test_baseline_descriptor_pins_inode_and_rejects_hardlinks(self) -> None:
        """
        함수 이름: test_baseline_descriptor_pins_inode_and_rejects_hardlinks()
        기능: 검증 뒤 path 교체는 old inode bytes를 바꾸지 못하고 hardlink source는 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with TemporaryDirectory() as temporary_directory:
            artifact_root = Path(temporary_directory) / ".testnet-artifacts"
            first_run_directory = artifact_root / "verified-run"
            second_run_directory = artifact_root / "linked-run"
            first_run_directory.mkdir(parents=True)
            second_run_directory.mkdir(parents=True)
            baseline_path = first_run_directory / "history.jsonl"
            baseline_path.write_bytes(b"pinned-baseline\n")
            baseline_path.chmod(0o600)

            with patch(
                "scripts.run_testnet_from_keychain.BASELINE_ARTIFACT_ROOT",
                artifact_root,
            ):
                verified_baseline = open_verified_baseline_history(baseline_path)
                try:
                    # Leaf 교체 뒤에도 child가 상속할 descriptor는 검증 시점의 exact bytes만 가리킨다.
                    moved_path = first_run_directory / "moved-history.jsonl"
                    baseline_path.replace(moved_path)
                    baseline_path.write_bytes(b"replacement\n")
                    baseline_path.chmod(0o600)
                    self.assertEqual(
                        os.pread(
                            verified_baseline.descriptor,
                            1_024,
                            0,
                        ),
                        b"pinned-baseline\n",
                    )
                finally:
                    close_verified_baseline_history(verified_baseline)

                # 두 directory entry가 같은 inode를 가리키면 외부 mutation 여지가 있어 open을 거부한다.
                hardlink_path = second_run_directory / "history.jsonl"
                os.link(baseline_path, hardlink_path)
                with self.assertRaises(TestnetKeychainRunnerError):
                    open_verified_baseline_history(hardlink_path)

    def test_production_hardener_sets_core_limit_and_owner_umask(self) -> None:
        """
        함수 이름: test_production_hardener_sets_core_limit_and_owner_umask()
        기능: Production hardener가 credential 조회 전에 core dump 0과 umask 077을 강제하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        with patch(
            "scripts.run_testnet_from_keychain.resource.setrlimit"
        ) as set_limit_mock, patch(
            "scripts.run_testnet_from_keychain.os.umask"
        ) as umask_mock:
            harden_runner_process()

        # 두 process-wide 보안 경계는 fixed production callable 내부에서 정확히 한 번 설정된다.
        set_limit_mock.assert_called_once_with(
            resource.RLIMIT_CORE,
            (0, 0),
        )
        umask_mock.assert_called_once_with(0o077)

        with patch(
            "scripts.run_testnet_from_keychain.resource.setrlimit",
            side_effect=OSError("hardening-canary"),
        ), patch("scripts.run_testnet_from_keychain.os.umask") as failed_umask:
            with self.assertRaises(TestnetKeychainRunnerError) as error_context:
                harden_runner_process()
        self.assertIsNone(error_context.exception.__cause__)
        failed_umask.assert_not_called()  # Core 제한 실패 뒤 credential-safe 실행을 계속하지 않는다.

    def test_keychain_reader_uses_fixed_service_without_secret_argv(self) -> None:
        """
        함수 이름: test_keychain_reader_uses_fixed_service_without_secret_argv()
        기능: Keychain 조회 argv에는 service/account만 있고 captured credential 참조는 제거되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        process_result = subprocess.CompletedProcess(
            args=(),
            returncode=0,
            stdout=API_KEY_CANARY + b"\n",
            stderr=b"",
        )
        command_runner = Mock(return_value=process_result)

        # 실제 bytes를 반환한 test double로 exact command와 output cleanup을 함께 고정한다.
        credential_buffer = read_keychain_credential(
            KEYCHAIN_API_KEY_ACCOUNT,
            command_runner=command_runner,
        )
        try:
            self.assertEqual(credential_buffer, API_KEY_CANARY)
            command_runner.assert_called_once_with(
                (
                    KEYCHAIN_SECURITY_COMMAND,
                    "find-generic-password",
                    "-s",
                    KEYCHAIN_SERVICE,
                    "-a",
                    KEYCHAIN_API_KEY_ACCOUNT,
                    "-w",
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
                close_fds=True,
                timeout=KEYCHAIN_READ_TIMEOUT_SECONDS,
            )
            command_arguments = command_runner.call_args.args[0]
            self.assertNotIn(API_KEY_CANARY.decode("ascii"), command_arguments)
            self.assertEqual(process_result.stdout, b"")
            self.assertEqual(process_result.stderr, b"")
        finally:
            zeroize_secret_buffer(credential_buffer)

    def test_keychain_failures_do_not_reflect_raw_output_or_chain(self) -> None:
        """
        함수 이름: test_keychain_failures_do_not_reflect_raw_output_or_chain()
        기능: Non-zero status와 timeout의 raw output이 고정 exception 또는 원인에 남지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        raw_error_canary = b"raw-keychain-error-canary"
        failed_result = subprocess.CompletedProcess(
            args=(),
            returncode=44,
            stdout=API_SECRET_CANARY,
            stderr=raw_error_canary,
        )

        # Missing item status는 account별 raw detail 대신 하나의 credential-free runner 오류가 된다.
        with self.assertRaises(TestnetKeychainRunnerError) as failed_context:
            read_keychain_credential(
                KEYCHAIN_API_SECRET_ACCOUNT,
                command_runner=Mock(return_value=failed_result),
            )
        self.assertEqual(
            str(failed_context.exception),
            "secure Testnet runner is unavailable",
        )
        self.assertNotIn(API_SECRET_CANARY.decode("ascii"), str(failed_context.exception))
        self.assertNotIn(raw_error_canary.decode("ascii"), str(failed_context.exception))
        self.assertEqual(failed_result.stdout, b"")
        self.assertEqual(failed_result.stderr, b"")

        partial_output = bytearray(API_SECRET_CANARY)
        partial_error = bytearray(raw_error_canary)
        timeout_error = subprocess.TimeoutExpired(
            cmd=(KEYCHAIN_SECURITY_COMMAND,),
            timeout=KEYCHAIN_READ_TIMEOUT_SECONDS,
            output=partial_output,
            stderr=partial_error,
        )
        with self.assertRaises(TestnetKeychainRunnerError) as timeout_context:
            read_keychain_credential(
                KEYCHAIN_API_SECRET_ACCOUNT,
                command_runner=Mock(side_effect=timeout_error),
            )
        self.assertIsNone(timeout_context.exception.__cause__)
        self.assertIsNone(timeout_error.output)
        self.assertIsNone(timeout_error.stderr)
        self.assertEqual(partial_output, bytearray())
        self.assertEqual(partial_error, bytearray())

    def test_execute_replaces_same_process_with_fixed_phase13_target(self) -> None:
        """
        함수 이름: test_execute_replaces_same_process_with_fixed_phase13_target()
        기능: Runner가 두 account를 읽고 같은 process의 exact argv/env exec만 시도하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        api_key_buffer = bytearray(API_KEY_CANARY)
        api_secret_buffer = bytearray(API_SECRET_CANARY)
        credential_reader = Mock(
            side_effect=(api_key_buffer, api_secret_buffer),
        )
        captured_exec: dict[str, object] = {}
        changed_directories: list[Path] = []
        hardening_calls: list[str] = []

        def capture_exec(
            executable: str,
            command: tuple[str, ...],
            environment: Mapping[str, str],
        ) -> None:
            """
            함수 이름: capture_exec()
            기능: Production os.execve 대신 exact 실행 경계를 test-local mapping에 복제한다.
            인자: executable -> Python executable path
                command -> fixed unittest argv
                environment -> 최소 Testnet environment
            반환값: 없음
            작성 날짜: 2026/08/31
            """
            captured_exec["executable"] = executable
            captured_exec["command"] = command
            captured_exec["environment"] = dict(environment)

        # 반환하지 않는 execve의 test double이 반환하면 runner는 이를 generic 실패로 닫아야 한다.
        with self.assertRaises(TestnetKeychainRunnerError):
            execute_testnet_mode(
                "phase13-public-case2",
                credential_reader=credential_reader,
                process_executor=capture_exec,
                change_directory=changed_directories.append,
                process_hardener=lambda: hardening_calls.append("hardened"),
                python_executable=Path(os.sys.executable),
            )
        credential_reader.assert_has_calls(
            [
                call(KEYCHAIN_API_KEY_ACCOUNT),
                call(KEYCHAIN_API_SECRET_ACCOUNT),
            ]
        )
        self.assertEqual(hardening_calls, ["hardened"])
        self.assertEqual(changed_directories, [BACKEND_ROOT])
        self.assertEqual(
            captured_exec["command"][-1],
            MODE_TEST_MODULES["phase13-public-case2"],
        )
        captured_environment = captured_exec["environment"]
        self.assertEqual(
            captured_environment[BINANCE_TESTNET_MAX_NOTIONAL_ENV],
            "100",
        )
        self.assertEqual(api_key_buffer, bytearray())
        self.assertEqual(api_secret_buffer, bytearray())

    def test_second_keychain_failure_zeroizes_first_credential(self) -> None:
        """
        함수 이름: test_second_keychain_failure_zeroizes_first_credential()
        기능: API secret 조회가 실패하면 먼저 읽은 API key도 exec 전에 폐기되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        api_key_buffer = bytearray(API_KEY_CANARY)
        credential_reader = Mock(
            side_effect=(api_key_buffer, TestnetKeychainRunnerError()),
        )

        # 두 번째 item 실패는 directory 변경이나 process exec 없이 finally cleanup으로 이동한다.
        with self.assertRaises(TestnetKeychainRunnerError):
            execute_testnet_mode(
                "read-only",
                credential_reader=credential_reader,
                process_executor=Mock(),
                change_directory=Mock(),
                process_hardener=Mock(),
                python_executable=Path(os.sys.executable),
            )
        self.assertEqual(api_key_buffer, bytearray())

    def test_parser_and_main_reject_extra_input_without_reflection(self) -> None:
        """
        함수 이름: test_parser_and_main_reject_extra_input_without_reflection()
        기능: Raw invalid argument가 usage stderr에 반사되지 않고 fixed 오류만 남는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        invalid_argument_canary = "must-not-reflect-credential-canary"
        self.assertEqual(
            parse_arguments(["read-only"]),
            TestnetRunnerArguments("read-only", None),
        )
        with self.assertRaises(TestnetKeychainRunnerError):
            parse_arguments([invalid_argument_canary])
        with self.assertRaises(TestnetKeychainRunnerError):
            parse_arguments(["read-only", "--arbitrary-option", "/tmp/history.jsonl"])

        # Main도 argparse의 invalid-choice 문장 없이 credential-free 고정 오류와 status 2만 반환한다.
        captured_stderr = StringIO()
        with redirect_stderr(captured_stderr):
            exit_status = main([invalid_argument_canary])
        self.assertEqual(exit_status, 2)
        self.assertNotIn(invalid_argument_canary, captured_stderr.getvalue())
        self.assertEqual(
            captured_stderr.getvalue(),
            "testnet-keychain-runner: ERROR: secure execution unavailable.\n",
        )

    @patch("scripts.run_testnet_from_keychain.execute_testnet_mode")
    def test_main_does_not_print_internal_runner_exception(
        self,
        execute_mock: Mock,
    ) -> None:
        """
        함수 이름: test_main_does_not_print_internal_runner_exception()
        기능: Internal runner 실패도 exception repr 없이 같은 fixed stderr로 변환되는지 검증한다.
        인자: execute_mock -> execute_testnet_mode test double
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        execute_mock.side_effect = TestnetKeychainRunnerError()
        captured_stderr = StringIO()

        # Fixed mode를 통과한 뒤의 실패도 Keychain account나 subprocess detail을 출력하지 않는다.
        with redirect_stderr(captured_stderr):
            exit_status = main(["read-only"])
        self.assertEqual(exit_status, 2)
        self.assertEqual(
            captured_stderr.getvalue(),
            "testnet-keychain-runner: ERROR: secure execution unavailable.\n",
        )

    @patch("scripts.run_testnet_from_keychain.execute_testnet_mode")
    def test_main_forwards_only_validated_baseline_path(
        self,
        execute_mock: Mock,
    ) -> None:
        """
        함수 이름: test_main_forwards_only_validated_baseline_path()
        기능: Main이 resolved workspace baseline만 fixed executor keyword로 전달하는지 검증한다.
        인자: execute_mock -> execute_testnet_mode test double
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        execute_mock.side_effect = TestnetKeychainRunnerError()

        with TemporaryDirectory() as temporary_directory:
            # Valid CLI fixture는 patched artifact root의 한 run directory에만 만든다.
            artifact_root = Path(temporary_directory) / ".testnet-artifacts"
            run_directory = artifact_root / "verified-run"
            run_directory.mkdir(parents=True)
            baseline_history_path = run_directory / "history.jsonl"
            baseline_history_path.write_text("{}\n", encoding="utf-8")
            with patch(
                "scripts.run_testnet_from_keychain.BASELINE_ARTIFACT_ROOT",
                artifact_root,
            ), redirect_stderr(StringIO()):
                exit_status = main(
                    [
                        "phase13-public-case2",
                        "--baseline-history",
                        os.fspath(baseline_history_path),
                    ]
                )

        self.assertEqual(exit_status, 2)
        execute_mock.assert_called_once_with(
            "phase13-public-case2",
            baseline_history_path=baseline_history_path.resolve(),
        )


if __name__ == "__main__":
    unittest.main()
