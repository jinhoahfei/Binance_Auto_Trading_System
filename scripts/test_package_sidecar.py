"""Phase 12 sidecar packaging의 signing identity와 child environment 계약을 검증한다."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


PACKAGE_SIDECAR_SCRIPT_PATH = Path(__file__).resolve().parent / "package_sidecar.sh"
TEST_TARGET_TRIPLE = "aarch64-apple-darwin"
VALID_SIGNING_IDENTITY = (
    "Developer ID Application: Phase 12 Test Identity (TEAM123456)"
)
VALID_SIGNED_TAURI_CONFIG = json.dumps(
    {
        "version": "1.0.0-rc.1+release.42",
        "bundle": {"macOS": {"bundleVersion": "42.1"}},
    },
    separators=(",", ":"),
)
SENSITIVE_PACKAGING_ENVIRONMENT = {
    "AC_PASSWORD": "ac-password-secret-canary",
    "APPLE_API_ISSUER": "apple-issuer-secret-canary",
    "APPLE_API_KEY": "apple-api-key-secret-canary",
    "APPLE_API_KEY_ID": "apple-key-id-secret-canary",
    "APPLE_API_KEY_PATH": "/private/apple-key-path-secret-canary.p8",
    "APPLE_CERTIFICATE": "certificate-secret-canary",
    "APPLE_CERTIFICATE_PASSWORD": "certificate-password-secret-canary",
    "APPLE_ID": "apple-account-secret-canary@example.invalid",
    "APPLE_PASSWORD": "apple-password-secret-canary",
    "APPLE_TEAM_ID": "apple-team-account-secret-canary",
    "APPLE_NOTARY_PROFILE": "apple-notary-profile-secret-canary",
    "APPLE_KEYCHAIN_PROFILE": "apple-keychain-profile-secret-canary",
    "ASC_API_KEY": "asc-api-key-secret-canary",
    "ASC_API_KEY_PATH": "/private/asc-key-path-secret-canary.p8",
    "ASC_ISSUER_ID": "asc-issuer-secret-canary",
    "NOTARY_PROFILE": "notary-profile-secret-canary",
    "BINANCE_API_KEY": "binance-api-key-secret-canary",
    "BINANCE_API_SECRET": "binance-api-secret-canary",
    "BINANCE_TESTNET_API_KEY": "binance-testnet-key-secret-canary",
    "BINANCE_TESTNET_API_SECRET": "binance-testnet-secret-canary",
    "PYTHONHOME": "/private/python-home-secret-canary",
    "PYTHONPATH": "/private/python-path-secret-canary",
    "PYTHONSTARTUP": "/private/python-startup-secret-canary.py",
    "PYTHONUSERBASE": "/private/python-user-base-secret-canary",
}


@unittest.skipUnless(os.name == "posix", "macOS packaging shell 및 POSIX executable fixture 전용")
class PhaseTwelvePackageSidecarTests(unittest.TestCase):
    """
    클래스 이름: PhaseTwelvePackageSidecarTests
    기능: 실제 shell 분기와 PyInstaller argv·environment 경계를 isolated repository에서 검증한다.
    작성 날짜: 2026/08/24
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: production package script를 실행할 최소 repository와 fake toolchain을 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 각 test는 production output과 기존 virtual environment를 건드리지 않는 별도 root를 사용한다.
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository_root = Path(self.temporary_directory.name)
        self.script_directory = self.repository_root / "scripts"
        self.package_script_path = self.script_directory / "package_sidecar.sh"
        self.backend_directory = self.repository_root / "backend"
        self.tauri_directory = (
            self.repository_root / "UI" / "apps" / "desktop" / "src-tauri"
        )
        self.capture_path = self.repository_root / "python-invocations.jsonl"
        self.fake_tool_directory = self.repository_root / "fake-tools"
        self.command_temporary_directory = (
            self.repository_root / "command-temporary-root"
        )
        self.path_hijack_capture_path = (
            self.repository_root / "path-hijack-invocations.txt"
        )

        # Production script가 기대하는 source·configuration·tool 경로를 최소 fixture로 구성한다.
        self.script_directory.mkdir(parents=True)
        shutil.copyfile(PACKAGE_SIDECAR_SCRIPT_PATH, self.package_script_path)
        self.package_script_path.chmod(0o755)
        entrypoint_path = (
            self.backend_directory
            / "src"
            / "binance_auto_trader"
            / "sidecar.py"
        )
        entrypoint_path.parent.mkdir(parents=True)
        entrypoint_path.write_text("raise SystemExit(0)\n", encoding="utf-8")
        self.tauri_directory.mkdir(parents=True)
        (self.tauri_directory / "tauri.conf.json").write_text(
            "{}\n",
            encoding="utf-8",
        )
        self.fake_tool_directory.mkdir()
        self.command_temporary_directory.mkdir()

        # Host triple은 실제 rustup 상태와 무관하게 macOS arm64로 고정한다.
        self._write_executable(
            self.fake_tool_directory / "rustc",
            """
            #!/bin/sh

            # package script가 사용하는 exact host-tuple query만 허용한다.
            if [ "$#" -eq 2 ] && [ "$1" = "--print" ] && [ "$2" = "host-tuple" ]; then
                printf '%s\n' 'aarch64-apple-darwin'
                exit 0
            fi

            exit 1
            """,
        )

        # Cleanup이 PATH의 rm/mktemp shim을 실행하면 즉시 기록하고 실패하는다.
        for hijacked_tool_name in ("mktemp", "rm"):
            self._write_executable(
                self.fake_tool_directory / hijacked_tool_name,
                f"""
                #!/bin/sh

                printf '%s\\n' '{hijacked_tool_name}' >> "${{PACKAGE_TEST_PATH_HIJACK_CAPTURE}}"
                exit 97
                """,
            )

        # Fake packaging Python은 호출 argv와 secret 변수 존재 여부만 기록하고 executable을 모사한다.
        fake_packaging_python_path = (
            self.backend_directory / ".venv" / "bin" / "python"
        )
        self._write_executable(
            fake_packaging_python_path,
            """
            #!/usr/bin/env python3
            \"\"\"Package script behavior test를 위한 최소 packaging Python 대역이다.\"\"\"

            import json
            import os
            from pathlib import Path
            import sys


            capture_path = Path(os.environ["PACKAGE_TEST_CAPTURE_PATH"])
            invocation_record = {
                "argv": sys.argv[1:],
                "certificate_present": "APPLE_CERTIFICATE" in os.environ,
                "certificate_password_present": (
                    "APPLE_CERTIFICATE_PASSWORD" in os.environ
                ),
                "sensitive_environment_names": sorted(
                    environment_name
                    for environment_name in (
                        "AC_PASSWORD",
                        "APPLE_API_ISSUER",
                        "APPLE_API_KEY",
                        "APPLE_API_KEY_ID",
                        "APPLE_API_KEY_PATH",
                        "APPLE_CERTIFICATE",
                        "APPLE_CERTIFICATE_PASSWORD",
                        "APPLE_ID",
                        "APPLE_PASSWORD",
                        "APPLE_TEAM_ID",
                        "APPLE_NOTARY_PROFILE",
                        "APPLE_KEYCHAIN_PROFILE",
                        "ASC_API_KEY",
                        "ASC_API_KEY_PATH",
                        "ASC_ISSUER_ID",
                        "NOTARY_PROFILE",
                        "TAURI_CONFIG",
                        "BINANCE_API_KEY",
                        "BINANCE_API_SECRET",
                        "BINANCE_TESTNET_API_KEY",
                        "BINANCE_TESTNET_API_SECRET",
                        "PYTHONHOME",
                        "PYTHONPATH",
                        "PYTHONSTARTUP",
                        "PYTHONUSERBASE",
                    )
                    if environment_name in os.environ
                ),
                "python_no_user_site": os.environ.get("PYTHONNOUSERSITE"),
                "release_commit": os.environ.get("PHASE12_RELEASE_COMMIT"),
            }
            add_data_values = [
                sys.argv[argument_index + 1]
                for argument_index, argument_value in enumerate(sys.argv[:-1])
                if argument_value == "--add-data"
            ]
            invocation_record["add_data_count"] = len(add_data_values)
            if len(add_data_values) == 1:
                provenance_source, add_data_destination = add_data_values[0].rsplit(
                    ":", 1
                )
                invocation_record["add_data_destination"] = add_data_destination
                invocation_record["provenance_source_name"] = Path(
                    provenance_source
                ).name
                invocation_record["provenance_raw"] = Path(
                    provenance_source
                ).read_text(encoding="utf-8")
                invocation_record["provenance"] = json.loads(
                    invocation_record["provenance_raw"]
                )

            # Secret 값은 기록하지 않고 각 child invocation의 구조만 JSON line으로 남긴다.
            with capture_path.open("a", encoding="utf-8") as capture_file:
                capture_file.write(json.dumps(invocation_record) + "\\n")

            # Configuration validator marker가 있으면 production inline Python을 그대로 실행한다.
            if len(sys.argv) >= 4 and sys.argv[3] in {
                "--phase12-validate-tauri-config",
                "--phase12-create-release-provenance",
            }:
                validator_source = sys.argv[2]
                sys.argv = ["-c", *sys.argv[3:]]
                exec(compile(validator_source, "<string>", "exec"))

            # Dependency probe는 import를 실행하지 않고 성공 status만 반환한다.
            if sys.argv[1:3] != ["-m", "PyInstaller"]:
                raise SystemExit(0)

            # PyInstaller branch는 --distpath에 production binary placeholder를 생성한다.
            distribution_path = Path(
                sys.argv[sys.argv.index("--distpath") + 1]
            )
            packaged_binary_path = distribution_path / "binance-auto-sidecar"
            distribution_path.mkdir(parents=True, exist_ok=True)
            packaged_binary_path.write_text(
                "#!/bin/sh\\nexit 0\\n",
                encoding="utf-8",
            )
            packaged_binary_path.chmod(0o755)

            # Cleanup race test는 소유 directory를 옮기고 같은 path에 foreign tree를 만든다.
            if os.environ.get("PACKAGE_TEST_SWAP_WORK_DIRECTORY") == "1":
                work_directory = distribution_path.parent
                moved_work_directory = work_directory.with_name(
                    work_directory.name + ".owned-original"
                )
                work_directory.rename(moved_work_directory)
                work_directory.mkdir()
                (work_directory / "foreign-sentinel.txt").write_text(
                    "must survive cleanup\\n",
                    encoding="utf-8",
                )
            """,
        )

        # Developer-ID commit gate를 검증할 clean fixed repository fixture를 만든다.
        (self.repository_root / ".gitignore").write_text(
            "python-invocations.jsonl\n"
            "path-hijack-invocations.txt\n"
            "command-temporary-root/\n"
            "UI/apps/desktop/src-tauri/binaries/\n",
            encoding="utf-8",
        )
        fixture_git_environment = {
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
        }
        fixture_git_commands = (
            ["/usr/bin/git", "init", "--quiet"],
            ["/usr/bin/git", "add", "-A"],
            [
                "/usr/bin/git",
                "-c",
                "user.name=Phase 12 Test",
                "-c",
                "user.email=phase12@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--quiet",
                "-m",
                "fixture",
            ],
        )
        for fixture_git_command in fixture_git_commands:
            subprocess.run(
                fixture_git_command,
                cwd=self.repository_root,
                env=fixture_git_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                timeout=30,
            )
        self.repository_commit = subprocess.run(
            ["/usr/bin/git", "rev-parse", "HEAD"],
            cwd=self.repository_root,
            env=fixture_git_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()

    def tearDown(self) -> None:
        """
        함수 이름: tearDown()
        기능: test별 isolated repository를 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        self.temporary_directory.cleanup()  # 이번 test가 소유한 temporary root만 삭제한다.

    def _write_executable(self, executable_path: Path, source: str) -> None:
        """
        함수 이름: _write_executable()
        기능: 들여쓰기를 제거한 fixture source를 executable mode로 저장한다.
        인자: executable_path -> 생성할 fixture executable 경로
            source -> dedent할 source 문자열
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Shebang이 첫 byte에 오도록 leading newline을 제거한 뒤 실행 권한을 부여한다.
        executable_path.parent.mkdir(parents=True, exist_ok=True)
        executable_path.write_text(
            textwrap.dedent(source).lstrip(),
            encoding="utf-8",
        )
        executable_path.chmod(0o755)

    def _run_package_script(
        self,
        extra_environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """
        함수 이름: _run_package_script()
        기능: Apple signing 변수를 초기화한 environment에서 package script를 실행한다.
        인자: extra_environment -> test case별 추가 environment
        반환값: stdout과 stderr를 포착한 completed process
        작성 날짜: 2026/08/24
        """
        # Host environment의 signing 설정이 fixture 결과에 섞이지 않도록 관련 변수만 제거한다.
        process_environment = os.environ.copy()
        for variable_name in (
            "APPLE_SIGNING_IDENTITY",
            "PHASE12_RELEASE_COMMIT",
            "TAURI_CONFIG",
            *SENSITIVE_PACKAGING_ENVIRONMENT,
        ):
            process_environment.pop(variable_name, None)
        process_environment["PACKAGE_TEST_CAPTURE_PATH"] = str(self.capture_path)
        process_environment["PACKAGE_TEST_PATH_HIJACK_CAPTURE"] = str(
            self.path_hijack_capture_path
        )
        process_environment["TMPDIR"] = str(self.command_temporary_directory)
        process_environment["PATH"] = os.pathsep.join(
            (
                str(self.fake_tool_directory),
                process_environment["PATH"],
            )
        )
        if extra_environment is not None:
            process_environment.update(extra_environment)
            if (
                extra_environment.get("APPLE_SIGNING_IDENTITY")
                and "PHASE12_RELEASE_COMMIT" not in extra_environment
            ):
                process_environment["PHASE12_RELEASE_COMMIT"] = (
                    self.repository_commit
                )

        # Shell 전체를 실행해 함수 미호출·잘못된 branch·output publication 회귀까지 함께 검증한다.
        return subprocess.run(
            [str(self.package_script_path)],
            cwd=self.repository_root,
            env=process_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )

    def _load_python_invocations(self) -> list[dict[str, object]]:
        """
        함수 이름: _load_python_invocations()
        기능: fake packaging Python이 기록한 child invocation을 순서대로 읽는다.
        인자: 없음
        반환값: JSON invocation object 목록
        작성 날짜: 2026/08/24
        """
        if not self.capture_path.exists():
            return []

        # Empty line을 제외하고 각 record를 독립적으로 decode한다.
        return [
            json.loads(record_line)
            for record_line in self.capture_path.read_text(
                encoding="utf-8"
            ).splitlines()
            if record_line
        ]

    def _load_pyinstaller_invocation(self) -> dict[str, object]:
        """
        함수 이름: _load_pyinstaller_invocation()
        기능: 전체 child 호출 중 exact PyInstaller invocation 하나를 선택한다.
        인자: 없음
        반환값: PyInstaller JSON invocation object
        작성 날짜: 2026/08/24
        """
        # Dependency probe와 build invocation을 argv prefix로 구분한다.
        pyinstaller_invocations = [
            invocation
            for invocation in self._load_python_invocations()
            if invocation["argv"][:2] == ["-m", "PyInstaller"]
        ]
        self.assertEqual(len(pyinstaller_invocations), 1)

        return pyinstaller_invocations[0]

    def test_no_explicit_identity_omits_codesign_option(self) -> None:
        """
        함수 이름: test_no_explicit_identity_omits_codesign_option()
        기능: identity가 없는 invocation이 PyInstaller codesign option을 만들지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        process_result = self._run_package_script()

        # No-identity branch의 실제 argv와 target-suffixed placeholder publication을 함께 검사한다.
        self.assertEqual(process_result.returncode, 0, msg=process_result.stderr)
        pyinstaller_invocation = self._load_pyinstaller_invocation()
        self.assertNotIn("--codesign-identity", pyinstaller_invocation["argv"])
        self.assertEqual(pyinstaller_invocation["add_data_count"], 0)
        self.assertNotIn("--add-data", pyinstaller_invocation["argv"])
        hidden_imports = [
            pyinstaller_invocation["argv"][argument_index + 1]
            for argument_index, argument_value in enumerate(
                pyinstaller_invocation["argv"][:-1]
            )
            if argument_value == "--hidden-import"
        ]
        self.assertEqual(
            hidden_imports,
            ["websocket", "_ssl", "_hashlib"],
        )
        packaged_target_path = (
            self.tauri_directory
            / "binaries"
            / f"binance-auto-sidecar-{TEST_TARGET_TRIPLE}"
        )
        self.assertTrue(packaged_target_path.is_file())
        self.assertTrue(os.access(packaged_target_path, os.X_OK))
        self.assertFalse(self.path_hijack_capture_path.exists())
        self.assertEqual(
            list(
                self.command_temporary_directory.glob(
                    "binance-auto-sidecar-package.*"
                )
            ),
            [],
        )

    def test_explicit_identity_is_forwarded_without_certificate_material(self) -> None:
        """
        함수 이름: test_explicit_identity_is_forwarded_without_certificate_material()
        기능: explicit identity는 argv로 전달하고 certificate material은 child에서 제거하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        signing_identity = VALID_SIGNING_IDENTITY

        # Explicit identity와 certificate input을 함께 제공해 signing-option branch를 실행한다.
        process_result = self._run_package_script(
            {
                "APPLE_SIGNING_IDENTITY": signing_identity,
                "TAURI_CONFIG": VALID_SIGNED_TAURI_CONFIG,
                **SENSITIVE_PACKAGING_ENVIRONMENT,
            }
        )

        # Identity option의 위치와 certificate 변수 비상속을 실제 child capture로 고정한다.
        self.assertEqual(process_result.returncode, 0, msg=process_result.stderr)
        pyinstaller_invocation = self._load_pyinstaller_invocation()
        self.assertEqual(
            pyinstaller_invocation["argv"][:4],
            ["-m", "PyInstaller", "--codesign-identity", signing_identity],
        )
        self.assertEqual(
            pyinstaller_invocation["argv"].count("--codesign-identity"),
            1,
        )
        self.assertEqual(pyinstaller_invocation["argv"].count("--add-data"), 1)
        self.assertEqual(pyinstaller_invocation["add_data_count"], 1)
        self.assertEqual(pyinstaller_invocation["add_data_destination"], ".")
        self.assertEqual(
            pyinstaller_invocation["provenance_source_name"],
            "phase12-release-provenance.json",
        )
        self.assertEqual(
            pyinstaller_invocation["provenance"],
            {
                "schema_version": 1,
                "commit": self.repository_commit,
                "version": "1.0.0-rc.1+release.42",
                "build_version": "42.1",
            },
        )
        self.assertEqual(
            pyinstaller_invocation["provenance_raw"],
            '{"build_version":"42.1","commit":"'
            + self.repository_commit
            + '","schema_version":1,"version":"1.0.0-rc.1+release.42"}\n',
        )
        for invocation in self._load_python_invocations():
            self.assertFalse(invocation["certificate_present"])
            self.assertFalse(invocation["certificate_password_present"])
            self.assertEqual(invocation["sensitive_environment_names"], [])
            self.assertEqual(invocation["python_no_user_site"], "1")
            self.assertEqual(invocation["release_commit"], self.repository_commit)
        command_output = process_result.stdout + process_result.stderr
        for secret_canary in SENSITIVE_PACKAGING_ENVIRONMENT.values():
            self.assertNotIn(secret_canary, command_output)

    def test_explicit_identity_requires_tauri_release_configuration(self) -> None:
        """
        함수 이름: test_explicit_identity_requires_tauri_release_configuration()
        기능: signed build가 base Tauri version을 묵시적으로 재사용하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        process_result = self._run_package_script(
            {"APPLE_SIGNING_IDENTITY": VALID_SIGNING_IDENTITY}
        )

        # Missing runtime release metadata는 dependency probe나 PyInstaller 전에 즉시 거부한다.
        self.assertNotEqual(process_result.returncode, 0)
        self.assertIn("TAURI_CONFIG version/build", process_result.stderr)
        self.assertEqual(
            [
                invocation
                for invocation in self._load_python_invocations()
                if invocation["argv"][:2] == ["-m", "PyInstaller"]
            ],
            [],
        )

    def test_signed_tauri_configuration_requires_valid_json(self) -> None:
        """
        함수 이름: test_signed_tauri_configuration_requires_valid_json()
        기능: malformed signed runtime JSON을 내용 반사 없이 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        secret_configuration_fragment = "release-json-secret-sentinel"
        process_result = self._run_package_script(
            {
                "APPLE_SIGNING_IDENTITY": VALID_SIGNING_IDENTITY,
                "TAURI_CONFIG": '{"version":"1.0.0","private":"'
                + secret_configuration_fragment,
            }
        )

        # Parser detail과 source JSON은 stdout나 stderr에 반사하지 않는다.
        self.assertNotEqual(process_result.returncode, 0)
        self.assertIn("유효한 JSON", process_result.stderr)
        self.assertNotIn(secret_configuration_fragment, process_result.stdout)
        self.assertNotIn(secret_configuration_fragment, process_result.stderr)
        self.assertEqual(
            [
                invocation
                for invocation in self._load_python_invocations()
                if invocation["argv"][:2] == ["-m", "PyInstaller"]
            ],
            [],
        )

    def test_signed_tauri_configuration_rejects_missing_or_invalid_version(
        self,
    ) -> None:
        """
        함수 이름: test_signed_tauri_configuration_rejects_missing_or_invalid_version()
        기능: signed release root version이 explicit strict SemVer인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_versions: tuple[object | None, ...] = (
            None,
            1,
            "1.2",
            "01.2.3",
            "1.2.3-01",
        )

        for invalid_version in invalid_versions:
            with self.subTest(version=invalid_version):
                runtime_configuration: dict[str, object] = {
                    "bundle": {"macOS": {"bundleVersion": "42"}}
                }
                if invalid_version is not None:
                    runtime_configuration["version"] = invalid_version
                process_result = self._run_package_script(
                    {
                        "APPLE_SIGNING_IDENTITY": VALID_SIGNING_IDENTITY,
                        "TAURI_CONFIG": json.dumps(runtime_configuration),
                    }
                )

                self.assertNotEqual(process_result.returncode, 0)
                self.assertIn("root version", process_result.stderr)
                self.assertEqual(
                    [
                        invocation
                        for invocation in self._load_python_invocations()
                        if invocation["argv"][:2] == ["-m", "PyInstaller"]
                    ],
                    [],
                )
                self.capture_path.unlink(missing_ok=True)

    def test_signed_tauri_configuration_rejects_missing_or_invalid_build(
        self,
    ) -> None:
        """
        함수 이름: test_signed_tauri_configuration_rejects_missing_or_invalid_build()
        기능: signed release bundleVersion이 canonical nonzero 1~3 component인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_bundle_versions: tuple[object | None, ...] = (
            None,
            42,
            "0",
            "0.0.0",
            "01",
            "1.2.3.4",
        )

        for invalid_bundle_version in invalid_bundle_versions:
            with self.subTest(bundle_version=invalid_bundle_version):
                macos_configuration: dict[str, object] = {}
                if invalid_bundle_version is not None:
                    macos_configuration["bundleVersion"] = invalid_bundle_version
                process_result = self._run_package_script(
                    {
                        "APPLE_SIGNING_IDENTITY": VALID_SIGNING_IDENTITY,
                        "TAURI_CONFIG": json.dumps(
                            {
                                "version": "1.0.0",
                                "bundle": {"macOS": macos_configuration},
                            }
                        ),
                    }
                )

                self.assertNotEqual(process_result.returncode, 0)
                self.assertIn("bundle.macOS.bundleVersion", process_result.stderr)
                self.assertEqual(
                    [
                        invocation
                        for invocation in self._load_python_invocations()
                        if invocation["argv"][:2] == ["-m", "PyInstaller"]
                    ],
                    [],
                )
                self.capture_path.unlink(missing_ok=True)

    def test_certificate_without_identity_fails_before_python(self) -> None:
        """
        함수 이름: test_certificate_without_identity_fails_before_python()
        기능: clean CI certificate 추론 경로가 packaging child 실행 전에 닫히는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Certificate만 제공해 Tauri identity 자동 추론에 기대는 clean CI 구성을 모사한다.
        process_result = self._run_package_script(
            {"APPLE_CERTIFICATE": "test-certificate-material"}
        )

        # Tauri의 후행 certificate import에 기대지 않고 explicit identity 누락을 즉시 거부한다.
        self.assertNotEqual(process_result.returncode, 0)
        self.assertIn("APPLE_SIGNING_IDENTITY", process_result.stderr)
        self.assertEqual(self._load_python_invocations(), [])

    def test_certificate_password_without_certificate_fails_before_python(self) -> None:
        """
        함수 이름: test_certificate_password_without_certificate_fails_before_python()
        기능: certificate 없는 password 구성이 packaging child 실행 전에 닫히는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Password만 제공해 import할 certificate가 없는 잘못된 release 구성을 모사한다.
        process_result = self._run_package_script(
            {"APPLE_CERTIFICATE_PASSWORD": "test-certificate-password"}
        )

        # 불완전한 certificate pair는 credential을 child에 전달하기 전에 즉시 거부한다.
        self.assertNotEqual(process_result.returncode, 0)
        self.assertIn("APPLE_CERTIFICATE_PASSWORD", process_result.stderr)
        self.assertEqual(self._load_python_invocations(), [])

    def test_pseudo_identity_fails_before_python(self) -> None:
        """
        함수 이름: test_pseudo_identity_fails_before_python()
        기능: pseudo '-' identity가 unsigned branch로 취급되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # '-'를 non-empty identity로 전달해 signed branch 진입 전에 검증되는지 확인한다.
        process_result = self._run_package_script(
            {"APPLE_SIGNING_IDENTITY": "-"}
        )

        # Invalid identity는 dependency probe나 PyInstaller를 시작하기 전에 종료해야 한다.
        self.assertNotEqual(process_result.returncode, 0)
        self.assertIn("APPLE_SIGNING_IDENTITY '-'", process_result.stderr)
        self.assertEqual(self._load_python_invocations(), [])

    def test_signed_branch_rejects_noncanonical_developer_identities(self) -> None:
        """
        함수 이름: test_signed_branch_rejects_noncanonical_developer_identities()
        기능: signed branch가 exact Developer ID Application subject와 uppercase 10자 Team ID만 허용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_identities = (
            "Test Signing Identity",
            "Apple Development: Phase 12 Test (TEAM123456)",
            "Developer ID Application: Phase 12 Test (team123456)",
            "Developer ID Application: Phase 12 Test (TEAM12345)",
            "Developer ID Application: Phase 12 Test (TEAM1234567)",
            "Developer ID Application: Phase 12 Test (TEAM12345!)",
            "Developer ID Application:  (TEAM123456)",
            "Developer ID Application: Phase 12 Test (TEAM123456) trailing",
        )

        for invalid_identity in invalid_identities:
            with self.subTest(invalid_identity=invalid_identity):
                process_result = self._run_package_script(
                    {
                        "APPLE_SIGNING_IDENTITY": invalid_identity,
                        "TAURI_CONFIG": VALID_SIGNED_TAURI_CONFIG,
                    }
                )

                self.assertNotEqual(process_result.returncode, 0)
                self.assertIn("canonical Developer ID", process_result.stderr)
                self.assertEqual(self._load_python_invocations(), [])
                self.capture_path.unlink(missing_ok=True)

    def test_signed_branch_requires_exact_current_release_commit(self) -> None:
        """
        함수 이름: test_signed_branch_requires_exact_current_release_commit()
        기능: signed build가 lowercase 40-hex current HEAD 외의 release commit을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        invalid_release_commits = (
            "",
            "a" * 39,
            "A" * 40,
            "g" * 40,
            "0" * 40,
        )

        for invalid_release_commit in invalid_release_commits:
            with self.subTest(release_commit=invalid_release_commit):
                process_result = self._run_package_script(
                    {
                        "APPLE_SIGNING_IDENTITY": VALID_SIGNING_IDENTITY,
                        "TAURI_CONFIG": VALID_SIGNED_TAURI_CONFIG,
                        "PHASE12_RELEASE_COMMIT": invalid_release_commit,
                    }
                )

                self.assertNotEqual(process_result.returncode, 0)
                self.assertIn("PHASE12_RELEASE_COMMIT", process_result.stderr)
                self.assertEqual(
                    [
                        invocation
                        for invocation in self._load_python_invocations()
                        if invocation["argv"][:2] == ["-m", "PyInstaller"]
                    ],
                    [],
                )
                self.capture_path.unlink(missing_ok=True)

    def test_signed_branch_requires_clean_fixed_head(self) -> None:
        """
        함수 이름: test_signed_branch_requires_clean_fixed_head()
        기능: explicit commit이 맞아도 tracked worktree가 dirty이면 signed build를 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        (self.tauri_directory / "tauri.conf.json").write_text(
            '{"dirty":true}\n',
            encoding="utf-8",
        )

        process_result = self._run_package_script(
            {
                "APPLE_SIGNING_IDENTITY": VALID_SIGNING_IDENTITY,
                "TAURI_CONFIG": VALID_SIGNED_TAURI_CONFIG,
                "PHASE12_RELEASE_COMMIT": self.repository_commit,
            }
        )

        self.assertNotEqual(process_result.returncode, 0)
        self.assertIn("clean fixed HEAD", process_result.stderr)
        self.assertEqual(
            [
                invocation
                for invocation in self._load_python_invocations()
                if invocation["argv"][:2] == ["-m", "PyInstaller"]
            ],
            [],
        )

    def test_release_git_ignores_caller_git_controls(self) -> None:
        """
        함수 이름: test_release_git_ignores_caller_git_controls()
        기능: fixed HEAD gate가 caller GIT_DIR·worktree·index·config injection을 상속하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        process_result = self._run_package_script(
            {
                "APPLE_SIGNING_IDENTITY": VALID_SIGNING_IDENTITY,
                "TAURI_CONFIG": VALID_SIGNED_TAURI_CONFIG,
                "PHASE12_RELEASE_COMMIT": self.repository_commit,
                "GIT_DIR": str(self.repository_root / "missing-git-directory"),
                "GIT_WORK_TREE": str(self.repository_root / "missing-worktree"),
                "GIT_INDEX_FILE": str(self.repository_root / "missing-index"),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "status.showUntrackedFiles",
                "GIT_CONFIG_VALUE_0": "no",
            }
        )

        self.assertEqual(process_result.returncode, 0, msg=process_result.stderr)
        self._load_pyinstaller_invocation()

    def test_tauri_file_signing_identity_is_rejected(self) -> None:
        """
        함수 이름: test_tauri_file_signing_identity_is_rejected()
        기능: 저장된 Tauri signingIdentity가 environment 단일 출처 정책을 우회하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Unicode escape를 decode해야만 signingIdentity가 되는 유효한 JSON override를 준비한다.
        configuration_path = self.tauri_directory / "tauri.macos.conf.json"
        configuration_path.write_text(
            '{"bundle":{"macOS":{"signing\\u0049dentity":'
            '"Config Identity"}}}',
            encoding="utf-8",
        )

        # File override는 explicit environment identity가 함께 있어도 모호한 이중 출처로 거부한다.
        process_result = self._run_package_script(
            {"APPLE_SIGNING_IDENTITY": VALID_SIGNING_IDENTITY}
        )
        self.assertNotEqual(process_result.returncode, 0)
        self.assertIn("Tauri configuration signingIdentity", process_result.stderr)
        self.assertEqual(
            [
                invocation
                for invocation in self._load_python_invocations()
                if invocation["argv"][:2] == ["-m", "PyInstaller"]
            ],
            [],
        )

    def test_tauri_json5_configuration_is_rejected(self) -> None:
        """
        함수 이름: test_tauri_json5_configuration_is_rejected()
        기능: Comment로 text 검사를 우회할 수 있는 JSON5 configuration을 지원하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # JSON5 comment가 key와 separator 사이에 있는 configuration source를 준비한다.
        configuration_path = self.tauri_directory / "tauri.macos.conf.json5"
        configuration_path.write_text(
            '{"bundle":{"macOS":{"signingIdentity"/* comment */:'
            '"Config Identity"}}}',
            encoding="utf-8",
        )

        # Unsupported source는 dependency probe와 PyInstaller build 전에 거부한다.
        process_result = self._run_package_script()
        self.assertNotEqual(process_result.returncode, 0)
        self.assertIn("JSON5/TOML", process_result.stderr)
        self.assertEqual(
            [
                invocation
                for invocation in self._load_python_invocations()
                if invocation["argv"][:2] == ["-m", "PyInstaller"]
            ],
            [],
        )

    def test_tauri_runtime_signing_identity_is_rejected(self) -> None:
        """
        함수 이름: test_tauri_runtime_signing_identity_is_rejected()
        기능: runtime merged Tauri signingIdentity가 environment 단일 출처 정책을 우회하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Runtime merge는 줄바꿈과 Unicode escape를 함께 포함한 유효한 JSON으로 구성한다.
        runtime_configuration = (
            '{"bundle":{"macOS":{"signing\\u0049dentity"\n:'
            '"Runtime Identity"}}}'
        )

        # TAURI_CONFIG를 explicit environment identity와 함께 전달해 이중 출처를 모사한다.
        process_result = self._run_package_script(
            {
                "APPLE_SIGNING_IDENTITY": VALID_SIGNING_IDENTITY,
                "TAURI_CONFIG": runtime_configuration,
            }
        )

        # Runtime merge도 file configuration과 같은 시점에 fail-closed 처리한다.
        self.assertNotEqual(process_result.returncode, 0)
        self.assertIn("Tauri runtime signingIdentity", process_result.stderr)
        self.assertEqual(
            [
                invocation
                for invocation in self._load_python_invocations()
                if invocation["argv"][:2] == ["-m", "PyInstaller"]
            ],
            [],
        )

    def test_cleanup_rejects_swapped_work_directory_and_path_shims(self) -> None:
        """
        함수 이름: test_cleanup_rejects_swapped_work_directory_and_path_shims()
        기능: mktemp inode swap 후 cleanup이 foreign tree나 PATH rm shim을 건드리지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        process_result = self._run_package_script(
            {"PACKAGE_TEST_SWAP_WORK_DIRECTORY": "1"}
        )

        self.assertNotEqual(process_result.returncode, 0)
        replacement_directories = [
            candidate
            for candidate in self.command_temporary_directory.glob(
                "binance-auto-sidecar-package.*"
            )
            if not candidate.name.endswith(".owned-original")
        ]
        self.assertEqual(len(replacement_directories), 1)
        self.assertEqual(
            (
                replacement_directories[0] / "foreign-sentinel.txt"
            ).read_text(encoding="utf-8"),
            "must survive cleanup\n",
        )
        self.assertFalse(self.path_hijack_capture_path.exists())


if __name__ == "__main__":
    unittest.main()  # 단독 실행에서도 unittest discovery와 같은 suite를 실행한다.
