"""Phase 12 release identity preflight의 exact identity와 secret redaction을 검증한다."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_phase12_release_identity import (
    ReleaseIdentityPreflightError,
    create_sanitized_environment,
    main,
    parse_developer_identity,
    run_command,
    validate_notary_profile_name,
    verify_release_identity_environment,
)


TEST_COMMIT_ID = "a" * 40
TEST_TEAM_ID = "A1B2C3D4E5"
TEST_IDENTITY = f"Developer ID Application: Release Owner ({TEST_TEAM_ID})"
TEST_PROFILE = "phase12-notary"


class FakeCommandRunner:
    """
    클래스 이름: FakeCommandRunner
    기능: release preflight command별 deterministic 결과와 sanitized environment를 기록한다.
    작성 날짜: 2026/08/24
    """

    def __init__(
        self,
        status_output: bytes = b"",
        commit_output: bytes = (TEST_COMMIT_ID + "\n").encode("ascii"),
        identity_output: bytes | None = None,
        notary_returncode: int = 0,
    ) -> None:
        """
        함수 이름: __init__()
        기능: command별 fake output과 invocation capture를 초기화한다.
        인자: status_output -> git status stdout
            commit_output -> git rev-parse stdout
            identity_output -> security stdout 또는 기본 exact identity
            notary_returncode -> notarytool history status
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        self.status_output = status_output
        self.commit_output = commit_output
        self.identity_output = identity_output or (
            f'  1) {"B" * 40} "{TEST_IDENTITY}"\n'
            "     1 valid identities found\n"
        ).encode("utf-8")
        self.notary_returncode = notary_returncode
        self.invocations: list[tuple[list[str], Path, dict[str, str]]] = []

    def __call__(
        self,
        command: list[str] | tuple[str, ...],
        working_directory: Path,
        process_environment: dict[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        """
        함수 이름: __call__()
        기능: executable별 fake result를 반환하고 raw secret 비상속 검증용 호출을 저장한다.
        인자: command -> production command
            working_directory -> production cwd
            process_environment -> sanitized environment
        반환값: fake completed process
        작성 날짜: 2026/08/24
        """

        command_list = list(command)
        self.invocations.append(
            (command_list, working_directory, dict(process_environment))
        )
        if command_list[0] == "/usr/bin/git" and "status" in command_list:
            return subprocess.CompletedProcess(command_list, 0, self.status_output, b"")
        if command_list[0] == "/usr/bin/git" and "rev-parse" in command_list:
            return subprocess.CompletedProcess(command_list, 0, self.commit_output, b"")
        if command_list[1:3] == ["find-identity", "-v"]:
            return subprocess.CompletedProcess(command_list, 0, self.identity_output, b"")
        if command_list[1:3] == ["notarytool", "history"]:
            return subprocess.CompletedProcess(
                command_list,
                self.notary_returncode,
                b'{"history":[]}',
                b"raw-notary-error",
            )
        raise AssertionError(f"unexpected command shape: {command_list[:3]}")


class PhaseTwelveReleaseIdentityTests(unittest.TestCase):
    """
    클래스 이름: PhaseTwelveReleaseIdentityTests
    기능: clean commit, exact Developer ID와 Keychain profile release gate를 검증한다.
    작성 날짜: 2026/08/24
    """

    def _environment(self) -> dict[str, str]:
        """
        함수 이름: _environment()
        기능: 정상 release identity와 제거 대상 secret을 가진 environment를 만든다.
        인자: 없음
        반환값: test environment
        작성 날짜: 2026/08/24
        """

        return {
            "APPLE_SIGNING_IDENTITY": TEST_IDENTITY,
            "NOTARY_PROFILE": TEST_PROFILE,
            "APPLE_CERTIFICATE": "certificate-secret",
            "APPLE_CERTIFICATE_PASSWORD": "password-secret",
            "APPLE_ID": "account-secret",
            "PATH": "/usr/bin:/bin",
        }

    def test_clean_candidate_exact_identity_and_notary_profile_pass(self) -> None:
        """
        함수 이름: test_clean_candidate_exact_identity_and_notary_profile_pass()
        기능: 세 release prerequisite가 모두 맞을 때 non-secret evidence를 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        command_runner = FakeCommandRunner()

        evidence = verify_release_identity_environment(
            self._environment(),
            command_runner=command_runner,
            repository_root=Path("/test/repository"),
        )

        self.assertEqual(evidence.commit_id, TEST_COMMIT_ID)
        self.assertEqual(evidence.identity, TEST_IDENTITY)
        self.assertEqual(evidence.team_id, TEST_TEAM_ID)
        self.assertEqual(len(command_runner.invocations), 4)
        resolved_repository = Path("/test/repository").resolve()
        fixed_git_prefix = [
            "/usr/bin/git",
            f"--git-dir={resolved_repository / '.git'}",
            f"--work-tree={resolved_repository}",
        ]
        self.assertEqual(
            command_runner.invocations[0][0],
            fixed_git_prefix
            + ["status", "--porcelain=v1", "--untracked-files=all"],
        )
        self.assertEqual(
            command_runner.invocations[1][0],
            fixed_git_prefix + ["rev-parse", "--verify", "HEAD^{commit}"],
        )
        for _, _, process_environment in command_runner.invocations:
            self.assertNotIn("APPLE_CERTIFICATE", process_environment)
            self.assertNotIn("APPLE_CERTIFICATE_PASSWORD", process_environment)
            self.assertNotIn("APPLE_ID", process_environment)
            self.assertNotIn("APPLE_SIGNING_IDENTITY", process_environment)
            self.assertNotIn("NOTARY_PROFILE", process_environment)

    def test_git_environment_cannot_redirect_dirty_candidate_to_clean_repo(
        self,
    ) -> None:
        """
        함수 이름: test_git_environment_cannot_redirect_dirty_candidate_to_clean_repo()
        기능: GIT_* override가 clean decoy repository로 dirty release root 검사를 우회하지 못하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        source_environment = self._environment()
        source_environment.update(
            {
                "GIT_DIR": "/clean-decoy/.git",
                "GIT_WORK_TREE": "/clean-decoy",
                "GIT_INDEX_FILE": "/clean-decoy/.git/index",
                "GIT_OBJECT_DIRECTORY": "/clean-decoy/.git/objects",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/clean-decoy/objects",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.worktree",
                "GIT_CONFIG_VALUE_0": "/clean-decoy",
            }
        )
        command_runner = FakeCommandRunner(status_output=b" M dirty-file\n")
        release_root = Path("/actual-dirty-release-root")

        with self.assertRaises(ReleaseIdentityPreflightError):
            verify_release_identity_environment(
                source_environment,
                command_runner=command_runner,
                repository_root=release_root,
            )

        self.assertEqual(len(command_runner.invocations), 1)
        command, working_directory, process_environment = (
            command_runner.invocations[0]
        )
        resolved_release_root = release_root.resolve()
        self.assertEqual(working_directory, resolved_release_root)
        self.assertEqual(
            command[:3],
            [
                "/usr/bin/git",
                f"--git-dir={resolved_release_root / '.git'}",
                f"--work-tree={resolved_release_root}",
            ],
        )
        self.assertFalse(
            any(name.upper().startswith("GIT_") for name in process_environment)
        )

    def test_real_git_ignores_clean_decoy_environment_for_dirty_root(
        self,
    ) -> None:
        """
        함수 이름: test_real_git_ignores_clean_decoy_environment_for_dirty_root()
        기능: 실제 Git에서도 clean decoy override 대신 고정 dirty root의 status를 읽는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        if not Path("/usr/bin/git").is_file():
            self.skipTest("macOS system Git is unavailable")

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            dirty_release_root = temporary_root / "dirty-release"
            clean_decoy_root = temporary_root / "clean-decoy"
            for repository_path in (dirty_release_root, clean_decoy_root):
                subprocess.run(
                    ["/usr/bin/git", "init", "--quiet", str(repository_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                )
            (dirty_release_root / "untracked-release-drift").write_text(
                "dirty\n",
                encoding="utf-8",
            )

            source_environment = self._environment()
            source_environment.update(
                {
                    "GIT_DIR": str(clean_decoy_root / ".git"),
                    "GIT_WORK_TREE": str(clean_decoy_root),
                    "GIT_INDEX_FILE": str(clean_decoy_root / ".git" / "index"),
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "core.worktree",
                    "GIT_CONFIG_VALUE_0": str(clean_decoy_root),
                }
            )
            invocations: list[
                tuple[list[str], Path, dict[str, str]]
            ] = []

            def recording_runner(
                command: list[str] | tuple[str, ...],
                working_directory: Path,
                process_environment: dict[str, str],
            ) -> subprocess.CompletedProcess[bytes]:
                invocations.append(
                    (
                        list(command),
                        working_directory,
                        dict(process_environment),
                    )
                )
                return run_command(
                    command,
                    working_directory,
                    process_environment,
                )

            with self.assertRaises(ReleaseIdentityPreflightError):
                verify_release_identity_environment(
                    source_environment,
                    command_runner=recording_runner,
                    repository_root=dirty_release_root,
                )

            self.assertEqual(len(invocations), 1)
            command, working_directory, process_environment = invocations[0]
            resolved_dirty_root = dirty_release_root.resolve()
            self.assertEqual(working_directory, resolved_dirty_root)
            self.assertEqual(
                command[:3],
                [
                    "/usr/bin/git",
                    f"--git-dir={resolved_dirty_root / '.git'}",
                    f"--work-tree={resolved_dirty_root}",
                ],
            )
            self.assertFalse(
                any(
                    name.upper().startswith("GIT_")
                    for name in process_environment
                )
            )

    def test_dirty_candidate_fails_before_identity_and_notary_commands(self) -> None:
        """
        함수 이름: test_dirty_candidate_fails_before_identity_and_notary_commands()
        기능: tracked 또는 untracked drift가 identity lookup 전 release를 막는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        command_runner = FakeCommandRunner(status_output=b"?? untracked\n")

        with self.assertRaises(ReleaseIdentityPreflightError):
            verify_release_identity_environment(
                self._environment(),
                command_runner=command_runner,
            )
        self.assertEqual(len(command_runner.invocations), 1)

    def test_invalid_commit_fails_before_identity_lookup(self) -> None:
        """
        함수 이름: test_invalid_commit_fails_before_identity_lookup()
        기능: symbolic·short·non-hex commit output을 release evidence로 사용하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        command_runner = FakeCommandRunner(commit_output=b"main\n")

        with self.assertRaises(ReleaseIdentityPreflightError):
            verify_release_identity_environment(
                self._environment(),
                command_runner=command_runner,
            )
        self.assertEqual(len(command_runner.invocations), 2)

    def test_similar_or_duplicate_identity_is_rejected(self) -> None:
        """
        함수 이름: test_similar_or_duplicate_identity_is_rejected()
        기능: prefix가 같은 identity와 exact 중복을 단일 valid identity로 오인하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        similar_identity = (
            f'  1) {"B" * 40} "{TEST_IDENTITY} Extra"\n'
            f'  2) {"C" * 40} "{TEST_IDENTITY}"\n'
            f'  3) {"D" * 40} "{TEST_IDENTITY}"\n'
        ).encode("utf-8")
        command_runner = FakeCommandRunner(identity_output=similar_identity)

        with self.assertRaises(ReleaseIdentityPreflightError):
            verify_release_identity_environment(
                self._environment(),
                command_runner=command_runner,
            )
        self.assertEqual(len(command_runner.invocations), 3)

    def test_notary_profile_failure_is_fail_closed(self) -> None:
        """
        함수 이름: test_notary_profile_failure_is_fail_closed()
        기능: identity가 있어도 Keychain profile을 사용할 수 없으면 R-01을 닫지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        command_runner = FakeCommandRunner(notary_returncode=1)

        with self.assertRaises(ReleaseIdentityPreflightError):
            verify_release_identity_environment(
                self._environment(),
                command_runner=command_runner,
            )
        self.assertEqual(len(command_runner.invocations), 4)

    def test_identity_and_profile_shapes_fail_before_subprocess(self) -> None:
        """
        함수 이름: test_identity_and_profile_shapes_fail_before_subprocess()
        기능: pseudo/non-Developer identity와 control-character profile을 command 전에 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        invalid_identities = [None, "-", "Apple Development: Owner (A1B2C3D4E5)"]
        for invalid_identity in invalid_identities:
            with self.subTest(invalid_identity=invalid_identity):
                with self.assertRaises(ReleaseIdentityPreflightError):
                    parse_developer_identity(invalid_identity)
        for invalid_profile in (None, "", " leading", "line\nbreak"):
            with self.subTest(invalid_profile=invalid_profile):
                with self.assertRaises(ReleaseIdentityPreflightError):
                    validate_notary_profile_name(invalid_profile)

    def test_environment_sanitizer_removes_secrets_and_process_controls(self) -> None:
        """
        함수 이름: test_environment_sanitizer_removes_secrets_and_process_controls()
        기능: preflight child가 current·future secret과 Git·Xcode·Python 제어값 원문을 상속하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        source_environment = self._environment()
        removed_values = {
            "APPLE_FUTURE_PRIVATE_KEY": "future-apple-private-key-raw",
            "ASC_FUTURE_TOKEN": "future-asc-token-raw",
            "BINANCE_TESTNET_API_KEY": "binance-key-raw",
            "BINANCE_FUTURE_CREDENTIAL": "future-binance-credential-raw",
            "GIT_DIR": "/raw/alternate/repository",
            "GIT_CONFIG_VALUE_0": "raw-git-config-value",
            "DEVELOPER_DIR": "/raw/fake/xcode",
            "TOOLCHAINS": "raw-fake-toolchain",
            "SDKROOT": "/raw/fake/sdk",
            "XCODE_XCCONFIG_FILE": "/raw/fake/config",
            "PYTHONPATH": "/raw/python/import-path",
            "PYTHONHOME": "/raw/python/home",
            "_PYTHON_SYSCONFIGDATA_NAME": "raw-python-sysconfig",
            "__PYVENV_LAUNCHER__": "/raw/python/launcher",
            "DYLD_INSERT_LIBRARIES": "/raw/injected/library",
        }
        source_environment.update(removed_values)
        source_environment["BINANCE_RUN_TESTNET"] = "0"

        sanitized_environment = create_sanitized_environment(source_environment)

        self.assertEqual(sanitized_environment["PATH"], "/usr/bin:/bin")
        self.assertEqual(sanitized_environment["BINANCE_RUN_TESTNET"], "0")
        for removed_name in (
            "APPLE_CERTIFICATE",
            "APPLE_CERTIFICATE_PASSWORD",
            "APPLE_ID",
            "APPLE_SIGNING_IDENTITY",
            "NOTARY_PROFILE",
            *removed_values,
        ):
            self.assertNotIn(removed_name, sanitized_environment)
        retained_values = "\0".join(sanitized_environment.values())
        for removed_value in removed_values.values():
            self.assertNotIn(removed_value, retained_values)

    def test_xcrun_child_does_not_receive_toolchain_or_python_controls(
        self,
    ) -> None:
        """
        함수 이름: test_xcrun_child_does_not_receive_toolchain_or_python_controls()
        기능: notarytool을 찾는 xcrun child에 toolchain·Python override가 전파되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        source_environment = self._environment()
        control_names = (
            "DEVELOPER_DIR",
            "TOOLCHAINS",
            "SDKROOT",
            "XCODE_XCCONFIG_FILE",
            "PYTHONPATH",
            "PYTHONHOME",
        )
        for index, control_name in enumerate(control_names):
            source_environment[control_name] = f"raw-control-{index}"
        command_runner = FakeCommandRunner()

        verify_release_identity_environment(
            source_environment,
            command_runner=command_runner,
            repository_root=Path("/test/repository"),
        )

        xcrun_command, _, xcrun_environment = command_runner.invocations[-1]
        self.assertEqual(xcrun_command[:2], ["/usr/bin/xcrun", "notarytool"])
        for control_name in control_names:
            self.assertNotIn(control_name, xcrun_environment)
        inherited_values = "\0".join(xcrun_environment.values())
        for index in range(len(control_names)):
            self.assertNotIn(f"raw-control-{index}", inherited_values)

    @patch("scripts.check_phase12_release_identity.verify_release_identity_environment")
    def test_main_failure_does_not_echo_identity_profile_or_command_output(
        self,
        verify_environment_mock,
    ) -> None:
        """
        함수 이름: test_main_failure_does_not_echo_identity_profile_or_command_output()
        기능: top-level 오류가 identity, profile과 helper detail을 출력하지 않는지 검증한다.
        인자: verify_environment_mock -> release preflight test double
        반환값: 없음
        작성 날짜: 2026/08/24
        """

        secret_detail = (
            f"{TEST_IDENTITY} {TEST_PROFILE} raw-command-secret "
            "raw-git-control raw-xcrun-control raw-future-binance-secret"
        )
        verify_environment_mock.side_effect = ReleaseIdentityPreflightError(
            secret_detail
        )
        captured_stdout = StringIO()
        captured_stderr = StringIO()

        with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
            exit_status = main()

        command_output = captured_stdout.getvalue() + captured_stderr.getvalue()
        self.assertEqual(exit_status, 1)
        self.assertEqual(
            captured_stderr.getvalue(),
            "phase12-release-identity: ERROR: release preflight failed.\n",
        )
        self.assertNotIn(TEST_IDENTITY, command_output)
        self.assertNotIn(TEST_PROFILE, command_output)
        self.assertNotIn("raw-command-secret", command_output)
        self.assertNotIn("raw-git-control", command_output)
        self.assertNotIn("raw-xcrun-control", command_output)
        self.assertNotIn("raw-future-binance-secret", command_output)


if __name__ == "__main__":
    unittest.main()
