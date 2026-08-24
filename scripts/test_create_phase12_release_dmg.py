"""Phase 12 final DMG builder의 입력·순서·서명·cleanup 계약을 검증한다."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


CREATE_RELEASE_DMG_SCRIPT_PATH = (
    Path(__file__).resolve().parent / "create_phase12_release_dmg.sh"
)
VALID_SIGNING_IDENTITY = (
    "Developer ID Application: Phase 12 Test Identity (TEAM123456)"
)
SENSITIVE_APPLE_ENVIRONMENT = {
    "AC_PASSWORD": "ac-password-secret-canary",
    "APPLE_API_ISSUER": "issuer-secret-canary",
    "APPLE_API_KEY": "api-key-secret-canary",
    "APPLE_API_KEY_ID": "api-key-id-secret-canary",
    "APPLE_API_KEY_PATH": "/secret/api-key-path-canary.p8",
    "APPLE_CERTIFICATE": "certificate-secret-canary",
    "APPLE_CERTIFICATE_PASSWORD": "certificate-password-secret-canary",
    "APPLE_ID": "account-secret-canary@example.invalid",
    "APPLE_PASSWORD": "account-password-secret-canary",
    "APPLE_TEAM_ID": "account-team-secret-canary",
    "ASC_API_KEY": "asc-api-key-secret-canary",
    "ASC_API_KEY_PATH": "/secret/asc-api-key-path-canary.p8",
    "ASC_ISSUER_ID": "asc-issuer-secret-canary",
    "CODESIGN_ALLOCATE": "/secret/codesign-allocate-canary",
    "DEVELOPER_DIR": "/secret/developer-directory-canary",
    "NOTARY_PROFILE": "notary-profile-secret-canary",
    "PYTHONHOME": "/secret/python-home-canary",
    "PYTHONPATH": "/secret/python-path-canary",
    "PYTHONSTARTUP": "/secret/python-startup-canary.py",
    "PYTHONUSERBASE": "/secret/python-user-base-canary",
    "SDKROOT": "/secret/sdk-root-canary",
    "TOOLCHAINS": "secret-toolchain-canary",
}


class PhaseTwelveReleaseDmgBuilderTests(unittest.TestCase):
    """
    클래스 이름: PhaseTwelveReleaseDmgBuilderTests
    기능: 실제 shell builder를 fake macOS toolchain과 isolated filesystem에서 검증한다.
    작성 날짜: 2026/08/24
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: test별 app·output parent·temporary parent와 fake toolchain을 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Production artifact와 실제 macOS signing/notarization state를 건드리지 않는 root를 사용한다.
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository_root = Path(self.temporary_directory.name)
        self.script_directory = self.repository_root / "scripts"
        self.fake_tool_directory = self.repository_root / "fake-tools"
        self.script_directory.mkdir()
        self.builder_script_path = (
            self.script_directory / "create_phase12_release_dmg.sh"
        )
        # Production의 absolute Apple tool pinning은 유지하되, isolated copy에서만 fake absolute path로 치환한다.
        builder_source = CREATE_RELEASE_DMG_SCRIPT_PATH.read_text(encoding="utf-8")
        for release_tool_name in ("codesign", "ditto", "hdiutil", "xcrun"):
            builder_source = builder_source.replace(
                f"/usr/bin/{release_tool_name}",
                str(self.fake_tool_directory / release_tool_name),
            )
        self.builder_script_path.write_text(builder_source, encoding="utf-8")
        self.builder_script_path.chmod(0o755)

        self.app_path = (
            self.repository_root / "phase12-app-path-canary.app"
        )
        app_contents_directory = self.app_path / "Contents"
        app_contents_directory.mkdir(parents=True)
        (app_contents_directory / "Info.plist").write_text(
            "phase12 fixture\n",
            encoding="utf-8",
        )
        self.output_directory = self.repository_root / "release-output"
        self.output_directory.mkdir()
        self.output_dmg_path = (
            self.output_directory / "phase12-output-path-canary.dmg"
        )
        self.command_temporary_directory = (
            self.repository_root / "command-temporary-root"
        )
        self.command_temporary_directory.mkdir()
        self.temporary_sibling_path = (
            self.command_temporary_directory / "must-survive-cleanup.txt"
        )
        self.temporary_sibling_path.write_text("keep\n", encoding="utf-8")

        self.fake_tool_directory.mkdir()
        self.invocation_log_path = self.repository_root / "tool-invocations.jsonl"
        self._write_fake_toolchain()

    def tearDown(self) -> None:
        """
        함수 이름: tearDown()
        기능: test가 소유한 isolated root만 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        self.temporary_directory.cleanup()

    def _write_executable(self, executable_path: Path, source: str) -> None:
        """
        함수 이름: _write_executable()
        기능: dedent한 fake tool source를 executable file로 저장한다.
        인자: executable_path -> 생성할 tool path
            source -> Python fixture source
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        executable_path.write_text(
            textwrap.dedent(source).lstrip(),
            encoding="utf-8",
        )
        executable_path.chmod(0o755)

    def _write_fake_toolchain(self) -> None:
        """
        함수 이름: _write_fake_toolchain()
        기능: ditto·hdiutil·codesign·xcrun의 최소 release behavior와 capture를 만든다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        self._write_executable(
            self.fake_tool_directory / "xcrun",
            r'''
            #!/usr/bin/env python3
            """App stapler precondition만 모사하는 xcrun 대역이다."""

            import json
            import os
            from pathlib import Path
            import sys


            arguments = sys.argv[1:]
            sensitive_environment_names = sorted(
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
                    "ASC_API_KEY",
                    "ASC_API_KEY_PATH",
                    "ASC_ISSUER_ID",
                    "CODESIGN_ALLOCATE",
                    "DEVELOPER_DIR",
                    "NOTARY_PROFILE",
                    "PYTHONHOME",
                    "PYTHONPATH",
                    "PYTHONSTARTUP",
                    "PYTHONUSERBASE",
                    "TOOLCHAINS",
                )
                if environment_name in os.environ
            )
            with Path(os.environ["PHASE12_DMG_TEST_LOG"]).open(
                "a", encoding="utf-8"
            ) as invocation_log:
                invocation_log.write(
                    json.dumps(
                        {
                            "tool": "xcrun",
                            "argv": arguments,
                            "sensitive_environment_names": (
                                sensitive_environment_names
                            ),
                            "signing_identity": os.environ.get(
                                "APPLE_SIGNING_IDENTITY"
                            ),
                            "sdkroot_matches_source_canary": os.environ.get(
                                "SDKROOT"
                            )
                            == os.environ.get(
                                "PHASE12_DMG_SOURCE_SDKROOT_CANARY"
                            ),
                        }
                    )
                    + "\n"
                )

            if arguments[:2] != ["stapler", "validate"] or len(arguments) != 3:
                raise SystemExit(90)
            failure_step = os.environ.get("PHASE12_DMG_FAIL_STEP")
            is_staged_app = ".phase12-work." in arguments[2]
            if failure_step == "app-stapler" and not is_staged_app:
                raise SystemExit(41)
            if failure_step == "staged-stapler" and is_staged_app:
                raise SystemExit(47)
            ''',
        )
        self._write_executable(
            self.fake_tool_directory / "ditto",
            r'''
            #!/usr/bin/env python3
            """Bundle directory metadata-preserving copy 경계를 모사하는 ditto 대역이다."""

            import json
            import os
            from pathlib import Path
            import shutil
            import sys


            arguments = sys.argv[1:]
            sensitive_environment_names = sorted(
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
                    "ASC_API_KEY",
                    "ASC_API_KEY_PATH",
                    "ASC_ISSUER_ID",
                    "CODESIGN_ALLOCATE",
                    "DEVELOPER_DIR",
                    "NOTARY_PROFILE",
                    "PYTHONHOME",
                    "PYTHONPATH",
                    "PYTHONSTARTUP",
                    "PYTHONUSERBASE",
                    "TOOLCHAINS",
                )
                if environment_name in os.environ
            )
            with Path(os.environ["PHASE12_DMG_TEST_LOG"]).open(
                "a", encoding="utf-8"
            ) as invocation_log:
                invocation_log.write(
                    json.dumps(
                        {
                            "tool": "ditto",
                            "argv": arguments,
                            "sensitive_environment_names": (
                                sensitive_environment_names
                            ),
                            "signing_identity": os.environ.get(
                                "APPLE_SIGNING_IDENTITY"
                            ),
                            "sdkroot_matches_source_canary": os.environ.get(
                                "SDKROOT"
                            )
                            == os.environ.get(
                                "PHASE12_DMG_SOURCE_SDKROOT_CANARY"
                            ),
                        }
                    )
                    + "\n"
                )

            if len(arguments) != 2:
                raise SystemExit(90)
            if os.environ.get("PHASE12_DMG_FAIL_STEP") == "ditto":
                raise SystemExit(42)
            shutil.copytree(
                Path(arguments[0]),
                Path(arguments[1]),
                symlinks=True,
            )
            ''',
        )
        self._write_executable(
            self.fake_tool_directory / "hdiutil",
            r'''
            #!/usr/bin/env python3
            """UDZO create와 image verify 경계를 모사하는 hdiutil 대역이다."""

            import json
            import os
            from pathlib import Path
            import sys


            arguments = sys.argv[1:]
            sensitive_environment_names = sorted(
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
                    "ASC_API_KEY",
                    "ASC_API_KEY_PATH",
                    "ASC_ISSUER_ID",
                    "CODESIGN_ALLOCATE",
                    "DEVELOPER_DIR",
                    "NOTARY_PROFILE",
                    "PYTHONHOME",
                    "PYTHONPATH",
                    "PYTHONSTARTUP",
                    "PYTHONUSERBASE",
                    "TOOLCHAINS",
                )
                if environment_name in os.environ
            )
            invocation = {
                "tool": "hdiutil",
                "argv": arguments,
                "sensitive_environment_names": sensitive_environment_names,
                "signing_identity": os.environ.get("APPLE_SIGNING_IDENTITY"),
                "sdkroot_matches_source_canary": os.environ.get("SDKROOT")
                == os.environ.get("PHASE12_DMG_SOURCE_SDKROOT_CANARY"),
            }
            failure_step = os.environ.get("PHASE12_DMG_FAIL_STEP")

            if arguments and arguments[0] == "create":
                try:
                    source_directory = Path(
                        arguments[arguments.index("-srcfolder") + 1]
                    )
                    image_format = arguments[arguments.index("-format") + 1]
                except (ValueError, IndexError):
                    raise SystemExit(90)
                applications_link = source_directory / "Applications"
                staged_apps = sorted(source_directory.glob("*.app"))
                invocation.update(
                    {
                        "source_is_directory": source_directory.is_dir(),
                        "applications_is_symlink": applications_link.is_symlink(),
                        "applications_target": (
                            os.readlink(applications_link)
                            if applications_link.is_symlink()
                            else None
                        ),
                        "staged_app_count": len(staged_apps),
                        "format": image_format,
                    }
                )
                with Path(os.environ["PHASE12_DMG_TEST_LOG"]).open(
                    "a", encoding="utf-8"
                ) as invocation_log:
                    invocation_log.write(json.dumps(invocation) + "\n")
                if failure_step == "hdiutil-create":
                    raise SystemExit(43)
                if (
                    not source_directory.is_dir()
                    or not applications_link.is_symlink()
                    or os.readlink(applications_link) != "/Applications"
                    or len(staged_apps) != 1
                    or image_format != "UDZO"
                ):
                    raise SystemExit(91)
                Path(arguments[-1]).write_bytes(b"fake-read-only-udzo-dmg\n")
                if failure_step == "output-parent-swap":
                    output_parent = Path(
                        os.environ["PHASE12_DMG_OUTPUT_PARENT"]
                    )
                    moved_output_parent = output_parent.with_name(
                        output_parent.name + ".owned-original"
                    )
                    output_parent.rename(moved_output_parent)
                    output_parent.mkdir()
                    (output_parent / "foreign-parent-sentinel.txt").write_text(
                        "must survive cleanup\n",
                        encoding="utf-8",
                    )
                raise SystemExit(0)

            with Path(os.environ["PHASE12_DMG_TEST_LOG"]).open(
                "a", encoding="utf-8"
            ) as invocation_log:
                invocation_log.write(json.dumps(invocation) + "\n")
            if len(arguments) != 2 or arguments[0] != "verify":
                raise SystemExit(90)
            if failure_step == "hdiutil-verify":
                raise SystemExit(44)
            if not Path(arguments[1]).is_file():
                raise SystemExit(91)
            if failure_step == "publish-race":
                Path(os.environ["PHASE12_DMG_OUTPUT_PATH"]).write_bytes(
                    b"competing-release-artifact\n"
                )
            ''',
        )
        self._write_executable(
            self.fake_tool_directory / "codesign",
            r'''
            #!/usr/bin/env python3
            """DMG sign과 signature verify를 구분해 모사하는 codesign 대역이다."""

            import json
            import os
            from pathlib import Path
            import sys


            arguments = sys.argv[1:]
            sensitive_environment_names = sorted(
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
                    "ASC_API_KEY",
                    "ASC_API_KEY_PATH",
                    "ASC_ISSUER_ID",
                    "CODESIGN_ALLOCATE",
                    "DEVELOPER_DIR",
                    "NOTARY_PROFILE",
                    "PYTHONHOME",
                    "PYTHONPATH",
                    "PYTHONSTARTUP",
                    "PYTHONUSERBASE",
                    "TOOLCHAINS",
                )
                if environment_name in os.environ
            )
            with Path(os.environ["PHASE12_DMG_TEST_LOG"]).open(
                "a", encoding="utf-8"
            ) as invocation_log:
                invocation_log.write(
                    json.dumps(
                        {
                            "tool": "codesign",
                            "argv": arguments,
                            "sensitive_environment_names": (
                                sensitive_environment_names
                            ),
                            "signing_identity": os.environ.get(
                                "APPLE_SIGNING_IDENTITY"
                            ),
                            "sdkroot_matches_source_canary": os.environ.get(
                                "SDKROOT"
                            )
                            == os.environ.get(
                                "PHASE12_DMG_SOURCE_SDKROOT_CANARY"
                            ),
                        }
                    )
                    + "\n"
                )

            failure_step = os.environ.get("PHASE12_DMG_FAIL_STEP")
            image_path = Path(arguments[-1]) if arguments else Path("missing")
            if "--verify" in arguments:
                if "--deep" in arguments:
                    if failure_step == "staged-codesign-verify":
                        raise SystemExit(48)
                    if arguments[:4] != [
                        "--verify",
                        "--deep",
                        "--strict",
                        "--verbose=4",
                    ]:
                        raise SystemExit(90)
                    if not image_path.is_dir():
                        raise SystemExit(91)
                else:
                    if failure_step == "codesign-verify":
                        raise SystemExit(46)
                    if arguments[:2] != ["--verify", "--verbose=4"]:
                        raise SystemExit(90)
                    if not image_path.is_file():
                        raise SystemExit(91)
                raise SystemExit(0)

            if failure_step == "codesign-sign":
                raise SystemExit(45)
            if (
                arguments.count("--sign") != 1
                or arguments.count("--timestamp") != 1
                or "--deep" in arguments
                or not image_path.is_file()
            ):
                raise SystemExit(90)
            with image_path.open("ab") as image_file:
                image_file.write(b"fake-developer-id-signature\n")
            if failure_step == "staging-inode-swap":
                image_path.unlink()
                image_path.write_bytes(b"foreign-replacement-inode\n")
            if failure_step == "staging-symlink-swap":
                image_path.unlink()
                image_path.symlink_to(
                    Path(os.environ["PHASE12_DMG_SWAP_TARGET"])
                )
            ''',
        )

    def _run_builder(
        self,
        *,
        arguments: list[str] | None = None,
        signing_identity: str | None = VALID_SIGNING_IDENTITY,
        failure_step: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """
        함수 이름: _run_builder()
        기능: host signing 환경을 제거하고 fake toolchain으로 builder 전체를 실행한다.
        인자: arguments -> 기본 app/output을 대체할 argument 목록
            signing_identity -> 설정할 identity, None이면 environment에서 제거
            failure_step -> 실패시킬 fake tool 단계
        반환값: stdout·stderr를 포착한 completed process
        작성 날짜: 2026/08/24
        """
        process_environment = os.environ.copy()
        process_environment.pop("APPLE_SIGNING_IDENTITY", None)
        process_environment.pop("PHASE12_DMG_FAIL_STEP", None)
        if signing_identity is not None:
            process_environment["APPLE_SIGNING_IDENTITY"] = signing_identity
        if failure_step is not None:
            process_environment["PHASE12_DMG_FAIL_STEP"] = failure_step
        process_environment.update(SENSITIVE_APPLE_ENVIRONMENT)
        process_environment["PHASE12_DMG_SOURCE_SDKROOT_CANARY"] = (
            SENSITIVE_APPLE_ENVIRONMENT["SDKROOT"]
        )
        process_environment["PHASE12_DMG_TEST_LOG"] = str(
            self.invocation_log_path
        )
        process_environment["PHASE12_DMG_OUTPUT_PATH"] = str(
            self.output_dmg_path
        )
        process_environment["PHASE12_DMG_OUTPUT_PARENT"] = str(
            self.output_directory
        )
        process_environment["PHASE12_DMG_SWAP_TARGET"] = str(
            self.temporary_sibling_path
        )
        process_environment["TMPDIR"] = str(self.command_temporary_directory)
        process_environment["PATH"] = os.pathsep.join(
            (str(self.fake_tool_directory), process_environment["PATH"])
        )

        selected_arguments = (
            [str(self.app_path), str(self.output_dmg_path)]
            if arguments is None
            else arguments
        )
        return subprocess.run(
            [str(self.builder_script_path), *selected_arguments],
            cwd=self.repository_root,
            env=process_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )

    def _load_invocations(self) -> list[dict[str, object]]:
        """
        함수 이름: _load_invocations()
        기능: fake tool JSON line을 실제 호출 순서대로 decode한다.
        인자: 없음
        반환값: tool invocation object 목록
        작성 날짜: 2026/08/24
        """
        if not self.invocation_log_path.exists():
            return []
        return [
            json.loads(invocation_line)
            for invocation_line in self.invocation_log_path.read_text(
                encoding="utf-8"
            ).splitlines()
            if invocation_line
        ]

    def _staging_dmg_candidates(self) -> list[Path]:
        """
        함수 이름: _staging_dmg_candidates()
        기능: final output parent에 남은 Phase 12 partial DMG entry를 선택한다.
        인자: 없음
        반환값: regular file·symlink을 포함한 partial path 목록
        작성 날짜: 2026/08/24
        """
        staging_candidates: list[Path] = []
        for work_directory in self._release_work_directories():
            staging_path = work_directory / "phase12-staging-output.dmg"
            if staging_path.exists() or staging_path.is_symlink():
                staging_candidates.append(staging_path)
        return staging_candidates

    def _release_work_directories(self) -> list[Path]:
        """
        함수 이름: _release_work_directories()
        기능: output parent의 exact Phase 12 mktemp-owned work directory를 선택한다.
        인자: 없음
        반환값: work directory path 목록
        작성 날짜: 2026/08/24
        """
        return list(
            self.output_directory.glob(
                f".{self.output_dmg_path.name}.phase12-work.*"
            )
        )

    def test_success_preserves_release_order_and_signs_dmg_once(self) -> None:
        """
        함수 이름: test_success_preserves_release_order_and_signs_dmg_once()
        기능: app ticket 선검증부터 DMG create·sign·verify까지 exact 순서를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        process_result = self._run_builder()

        self.assertEqual(process_result.returncode, 0, msg=process_result.stderr)
        self.assertTrue(self.output_dmg_path.is_file())
        invocations = self._load_invocations()
        self.assertEqual(
            [invocation["tool"] for invocation in invocations],
            [
                "xcrun",
                "ditto",
                "xcrun",
                "codesign",
                "hdiutil",
                "codesign",
                "codesign",
                "hdiutil",
            ],
        )
        self.assertEqual(
            invocations[0]["argv"],
            ["stapler", "validate", str(self.app_path)],
        )

        staged_app_path = Path(invocations[1]["argv"][1])
        self.assertEqual(
            invocations[2]["argv"],
            ["stapler", "validate", str(staged_app_path)],
        )
        self.assertEqual(
            invocations[3]["argv"],
            [
                "--verify",
                "--deep",
                "--strict",
                "--verbose=4",
                str(staged_app_path),
            ],
        )

        create_invocation = invocations[4]
        self.assertEqual(create_invocation["format"], "UDZO")
        self.assertNotIn("-ov", create_invocation["argv"])
        self.assertTrue(create_invocation["source_is_directory"])
        self.assertTrue(create_invocation["applications_is_symlink"])
        self.assertEqual(create_invocation["applications_target"], "/Applications")
        self.assertEqual(create_invocation["staged_app_count"], 1)
        staging_dmg_path = Path(create_invocation["argv"][-1])
        self.assertEqual(staging_dmg_path.parent.parent, self.output_directory)
        self.assertNotEqual(staging_dmg_path, self.output_dmg_path)
        self.assertTrue(
            staging_dmg_path.parent.name.startswith(
                f".{self.output_dmg_path.name}.phase12-work."
            )
        )
        self.assertEqual(staging_dmg_path.name, "phase12-staging-output.dmg")

        codesign_invocations = [
            invocation
            for invocation in invocations
            if invocation["tool"] == "codesign"
        ]
        signing_invocations = [
            invocation
            for invocation in codesign_invocations
            if "--sign" in invocation["argv"]
        ]
        self.assertEqual(len(signing_invocations), 1)
        self.assertEqual(
            signing_invocations[0]["argv"],
            [
                "--force",
                "--sign",
                VALID_SIGNING_IDENTITY,
                "--timestamp",
                str(staging_dmg_path),
            ],
        )
        dmg_codesign_invocations = [
            invocation
            for invocation in codesign_invocations
            if invocation["argv"][-1] == str(staging_dmg_path)
        ]
        for invocation in dmg_codesign_invocations:
            self.assertNotIn("--deep", invocation["argv"])
        self.assertEqual(
            [
                invocation
                for invocation in codesign_invocations
                if "--deep" in invocation["argv"]
            ],
            [invocations[3]],
        )
        self.assertEqual(
            invocations[-1]["argv"],
            ["verify", str(staging_dmg_path)],
        )
        self.assertFalse(staging_dmg_path.exists())
        self.assertEqual(self._staging_dmg_candidates(), [])
        self.assertEqual(self._release_work_directories(), [])

        # 모든 release tool child는 certificate·account secret 없이 exact identity만 상속한다.
        for invocation in invocations:
            self.assertEqual(invocation["sensitive_environment_names"], [])
            self.assertFalse(invocation["sdkroot_matches_source_canary"])
            self.assertEqual(
                invocation["signing_identity"],
                VALID_SIGNING_IDENTITY,
            )

        # Ditto destination으로 확인한 mktemp root만 사라지고 같은 TMPDIR의 sibling은 남아야 한다.
        release_work_directory = staged_app_path.parent.parent
        self.assertFalse(release_work_directory.exists())
        self.assertTrue(self.temporary_sibling_path.is_file())

        # 고정 success output에는 identity나 user-supplied artifact path가 반사되지 않는다.
        command_output = process_result.stdout + process_result.stderr
        self.assertNotIn(VALID_SIGNING_IDENTITY, command_output)
        self.assertNotIn(str(self.app_path), command_output)
        self.assertNotIn(str(self.output_dmg_path), command_output)
        for secret_canary in SENSITIVE_APPLE_ENVIRONMENT.values():
            self.assertNotIn(secret_canary, command_output)

    def test_existing_or_dangling_output_is_refused_without_tool_calls(self) -> None:
        """
        함수 이름: test_existing_or_dangling_output_is_refused_without_tool_calls()
        기능: stale regular DMG와 dangling symlink를 mutation 전에 모두 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        stale_bytes = b"stale-release-artifact\n"
        self.output_dmg_path.write_bytes(stale_bytes)
        process_result = self._run_builder()

        self.assertNotEqual(process_result.returncode, 0)
        self.assertEqual(self.output_dmg_path.read_bytes(), stale_bytes)
        self.assertEqual(self._load_invocations(), [])

        self.output_dmg_path.unlink()
        self.output_dmg_path.symlink_to(
            self.output_directory / "missing-release-target.dmg"
        )
        process_result = self._run_builder()

        self.assertNotEqual(process_result.returncode, 0)
        self.assertTrue(self.output_dmg_path.is_symlink())
        self.assertEqual(self._load_invocations(), [])

    def test_missing_wrong_suffix_and_symlink_apps_are_refused(self) -> None:
        """
        함수 이름: test_missing_wrong_suffix_and_symlink_apps_are_refused()
        기능: exact existing non-symlink .app 이외 source를 staging 전에 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        missing_app_path = self.repository_root / "missing.app"
        wrong_suffix_path = self.repository_root / "not-an-app.bundle"
        wrong_suffix_path.mkdir()
        symlink_app_path = self.repository_root / "linked.app"
        symlink_app_path.symlink_to(self.app_path, target_is_directory=True)

        for invalid_app_path in (
            missing_app_path,
            wrong_suffix_path,
            symlink_app_path,
        ):
            with self.subTest(invalid_app_path=invalid_app_path):
                process_result = self._run_builder(
                    arguments=[
                        str(invalid_app_path),
                        str(self.output_dmg_path),
                    ]
                )
                self.assertNotEqual(process_result.returncode, 0)
                self.assertFalse(self.output_dmg_path.exists())
                self.assertEqual(self._load_invocations(), [])

    def test_missing_adhoc_and_non_developer_identities_are_refused(self) -> None:
        """
        함수 이름: test_missing_adhoc_and_non_developer_identities_are_refused()
        기능: explicit Developer ID Application identity prefix와 non-empty suffix를 강제한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        for invalid_identity in (
            None,
            "",
            "-",
            "Apple Development: Phase 12 Test (TEAM123456)",
            "Developer ID Application:",
            "Developer ID Application: ",
            "Developer ID Application: Phase 12 Test",
            "Developer ID Application: Phase 12 Test (team123456)",
            "Developer ID Application: Phase 12 Test (TEAM12345)",
            "Developer ID Application: Phase 12 Test (TEAM1234567)",
        ):
            with self.subTest(invalid_identity=invalid_identity):
                process_result = self._run_builder(
                    signing_identity=invalid_identity
                )
                self.assertNotEqual(process_result.returncode, 0)
                self.assertFalse(self.output_dmg_path.exists())
                self.assertEqual(self._load_invocations(), [])

    def test_app_stapler_failure_stops_before_staging(self) -> None:
        """
        함수 이름: test_app_stapler_failure_stops_before_staging()
        기능: app ticket precondition 실패 시 mktemp·copy·DMG mutation으로 진행하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        process_result = self._run_builder(failure_step="app-stapler")

        self.assertNotEqual(process_result.returncode, 0)
        self.assertFalse(self.output_dmg_path.exists())
        invocations = self._load_invocations()
        self.assertEqual(len(invocations), 1)
        self.assertEqual(invocations[0]["tool"], "xcrun")
        self.assertEqual(
            invocations[0]["argv"],
            ["stapler", "validate", str(self.app_path)],
        )
        self.assertEqual(invocations[0]["sensitive_environment_names"], [])
        self.assertFalse(invocations[0]["sdkroot_matches_source_canary"])
        self.assertEqual(
            invocations[0]["signing_identity"],
            VALID_SIGNING_IDENTITY,
        )
        self.assertEqual(
            list(
                self.command_temporary_directory.glob(
                    "binance-auto-phase12-dmg.*"
                )
            ),
            [],
        )

    def test_staged_app_ticket_and_signature_fail_before_dmg_creation(self) -> None:
        """
        함수 이름: test_staged_app_ticket_and_signature_fail_before_dmg_creation()
        기능: ditto copy의 ticket·strict signature 손실을 각각 DMG 생성 전에 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        expected_tools_by_failure = {
            "staged-stapler": ["xcrun", "ditto", "xcrun"],
            "staged-codesign-verify": [
                "xcrun",
                "ditto",
                "xcrun",
                "codesign",
            ],
        }

        for failure_step, expected_tools in expected_tools_by_failure.items():
            with self.subTest(failure_step=failure_step):
                process_result = self._run_builder(failure_step=failure_step)

                self.assertNotEqual(process_result.returncode, 0)
                self.assertFalse(self.output_dmg_path.exists())
                invocations = self._load_invocations()
                self.assertEqual(
                    [invocation["tool"] for invocation in invocations],
                    expected_tools,
                )
                self.assertNotIn(
                    "hdiutil",
                    [invocation["tool"] for invocation in invocations],
                )
                staged_app_path = Path(invocations[1]["argv"][1])
                self.assertEqual(
                    invocations[2]["argv"],
                    ["stapler", "validate", str(staged_app_path)],
                )
                if failure_step == "staged-codesign-verify":
                    self.assertEqual(
                        invocations[3]["argv"],
                        [
                            "--verify",
                            "--deep",
                            "--strict",
                            "--verbose=4",
                            str(staged_app_path),
                        ],
                    )
                for invocation in invocations:
                    self.assertEqual(
                        invocation["sensitive_environment_names"],
                        [],
                    )
                    self.assertFalse(
                        invocation["sdkroot_matches_source_canary"]
                    )
                    self.assertEqual(
                        invocation["signing_identity"],
                        VALID_SIGNING_IDENTITY,
                    )

                # 두 validation failure에서도 builder가 소유한 mktemp root만 정리한다.
                release_work_directory = staged_app_path.parent.parent
                self.assertFalse(release_work_directory.exists())
                self.assertTrue(self.temporary_sibling_path.is_file())
                self.assertEqual(self._staging_dmg_candidates(), [])
                if self.invocation_log_path.exists():
                    self.invocation_log_path.unlink()

    def test_each_post_staging_failure_cleans_only_owned_temp_directory(self) -> None:
        """
        함수 이름: test_each_post_staging_failure_cleans_only_owned_temp_directory()
        기능: create·sign·verify 실패마다 mktemp root만 정리하고 sibling은 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        for failure_step in (
            "hdiutil-create",
            "codesign-sign",
            "codesign-verify",
            "hdiutil-verify",
        ):
            with self.subTest(failure_step=failure_step):
                process_result = self._run_builder(failure_step=failure_step)
                self.assertNotEqual(process_result.returncode, 0)
                invocations = self._load_invocations()
                ditto_invocation = next(
                    invocation
                    for invocation in invocations
                    if invocation["tool"] == "ditto"
                )
                staged_app_path = Path(ditto_invocation["argv"][1])
                release_work_directory = staged_app_path.parent.parent
                self.assertFalse(release_work_directory.exists())
                self.assertTrue(self.temporary_sibling_path.is_file())
                self.assertFalse(self.output_dmg_path.exists())
                self.assertEqual(self._staging_dmg_candidates(), [])

                # 다음 subtest가 stale-output gate가 아닌 요청된 failure branch에 도달하도록 초기화한다.
                if self.output_dmg_path.exists() or self.output_dmg_path.is_symlink():
                    self.output_dmg_path.unlink()
                if self.invocation_log_path.exists():
                    self.invocation_log_path.unlink()

    def test_publish_race_preserves_contender_and_removes_owned_partial(self) -> None:
        """
        함수 이름: test_publish_race_preserves_contender_and_removes_owned_partial()
        기능: final path race에서 기존 contender를 덮어쓰지 않고 소유 partial만 정리하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        process_result = self._run_builder(failure_step="publish-race")

        self.assertNotEqual(process_result.returncode, 0)
        self.assertEqual(
            self.output_dmg_path.read_bytes(),
            b"competing-release-artifact\n",
        )
        self.assertEqual(self._staging_dmg_candidates(), [])
        self.assertEqual(self._release_work_directories(), [])
        invocations = self._load_invocations()
        staging_dmg_path = Path(invocations[-1]["argv"][-1])
        self.assertFalse(staging_dmg_path.exists())

    def test_staging_inode_and_symlink_swaps_fail_closed(self) -> None:
        """
        함수 이름: test_staging_inode_and_symlink_swaps_fail_closed()
        기능: signing child가 staging path의 inode·type을 바꾸면 final publish와 foreign cleanup을 모두 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        for failure_step in ("staging-inode-swap", "staging-symlink-swap"):
            with self.subTest(failure_step=failure_step):
                process_result = self._run_builder(failure_step=failure_step)

                self.assertNotEqual(process_result.returncode, 0)
                self.assertFalse(self.output_dmg_path.exists())
                partial_candidates = self._staging_dmg_candidates()
                self.assertEqual(len(partial_candidates), 1)
                if failure_step == "staging-inode-swap":
                    self.assertEqual(
                        partial_candidates[0].read_bytes(),
                        b"foreign-replacement-inode\n",
                    )
                else:
                    self.assertTrue(partial_candidates[0].is_symlink())
                    self.assertEqual(
                        os.readlink(partial_candidates[0]),
                        str(self.temporary_sibling_path),
                    )
                self.assertTrue(self.temporary_sibling_path.is_file())

                partial_candidates[0].unlink()
                if self.invocation_log_path.exists():
                    self.invocation_log_path.unlink()

    def test_output_parent_inode_swap_preserves_foreign_replacement(self) -> None:
        """
        함수 이름: test_output_parent_inode_swap_preserves_foreign_replacement()
        기능: output parent path가 바뀌면 publication을 차단하고 replacement tree를 정리 대상으로 오인하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        process_result = self._run_builder(failure_step="output-parent-swap")

        self.assertNotEqual(process_result.returncode, 0)
        self.assertEqual(
            (
                self.output_directory / "foreign-parent-sentinel.txt"
            ).read_text(encoding="utf-8"),
            "must survive cleanup\n",
        )
        self.assertFalse(self.output_dmg_path.exists())
        moved_output_directory = self.output_directory.with_name(
            self.output_directory.name + ".owned-original"
        )
        moved_partials = list(
            moved_output_directory.glob(
                f".{self.output_dmg_path.name}.phase12-work.*/"
                "phase12-staging-output.dmg"
            )
        )
        self.assertEqual(len(moved_partials), 1)
        self.assertTrue(moved_partials[0].is_file())

    def test_argument_count_relative_dot_component_and_broad_targets_fail(self) -> None:
        """
        함수 이름: test_argument_count_relative_dot_component_and_broad_targets_fail()
        기능: default target과 option-like·dot-component·root 범위 입력을 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        nested_directory = self.repository_root / "nested"
        nested_directory.mkdir()
        ambiguous_app_path = nested_directory / ".." / self.app_path.name
        ambiguous_output_path = (
            self.output_directory / ".." / self.output_directory.name / "new.dmg"
        )
        invalid_argument_lists = (
            [],
            [str(self.app_path)],
            [str(self.app_path), str(self.output_dmg_path), "extra"],
            [self.app_path.name, str(self.output_dmg_path)],
            [str(self.app_path), self.output_dmg_path.name],
            [str(ambiguous_app_path), str(self.output_dmg_path)],
            [str(self.app_path), str(ambiguous_output_path)],
            [str(self.app_path), "/"],
            [str(self.app_path), "/phase12-root-output.dmg"],
        )

        for invalid_arguments in invalid_argument_lists:
            with self.subTest(invalid_arguments=invalid_arguments):
                process_result = self._run_builder(arguments=list(invalid_arguments))
                self.assertNotEqual(process_result.returncode, 0)
                self.assertFalse(self.output_dmg_path.exists())
                self.assertEqual(self._load_invocations(), [])


if __name__ == "__main__":
    unittest.main()
