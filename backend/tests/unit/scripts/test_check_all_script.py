"""Phase 13 check_all의 안전환경, core fail-fast와 evidence 집계를 검증한다."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


# 실제 repository script 경로는 호출 working directory와 무관하게 test file에서 계산한다.
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
CHECK_ALL_SCRIPT = REPOSITORY_ROOT / "scripts" / "check_all.sh"


class CheckAllScriptTests(unittest.TestCase):
    """
    클래스 이름: CheckAllScriptTests
    기능: local readiness shell이 주문 환경을 닫고 full suite를 중첩 없이 고정 순서로 호출하는지 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_shell_syntax_and_list_mode_are_side_effect_free(self) -> None:
        """
        함수 이름: test_shell_syntax_and_list_mode_are_side_effect_free()
        기능: POSIX syntax와 dry-run 단계 목록이 Testnet mutation 0 계약을 명시하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        syntax_result = subprocess.run(
            ["/bin/sh", "-n", os.fspath(CHECK_ALL_SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(syntax_result.returncode, 0, syntax_result.stderr)

        # Hostile inherited opt-in이 있어도 list output은 실제 child command를 실행하지 않고 0을 고정한다.
        environment = os.environ.copy()
        environment.update(
            {
                "BINANCE_RUN_TESTNET": "1",
                "BINANCE_RUN_TESTNET_ORDERS": "1",
                "BINANCE_RUN_PHASE13_PUBLIC_CASE2": "1",
                "BINANCE_TESTNET_API_KEY": "secret-key-canary",
                "BINANCE_TESTNET_API_SECRET": "secret-value-canary",
                "BINANCE_TESTNET_MAX_NOTIONAL": "999",
                "BINANCE_TESTNET_BASELINE_HISTORY_PATH": "path-canary",
                "BINANCE_TESTNET_BASELINE_HISTORY_FD": "31",
                "BINANCE_TESTNET_BASELINE_HISTORY_SHA256": "digest-canary",
                "BINANCE_TESTNET_BASELINE_PENDING_FD": "32",
                "BINANCE_TESTNET_BASELINE_PENDING_SHA256": "pending-canary",
                "PYTHONWARNINGS": "ignore",
            }
        )
        list_result = subprocess.run(
            ["/bin/sh", os.fspath(CHECK_ALL_SCRIPT), "--list"],
            cwd=Path(tempfile.gettempdir()),
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(list_result.returncode, 0, list_result.stderr)
        self.assertIn("BINANCE_RUN_TESTNET=0", list_result.stdout)
        self.assertIn("BINANCE_RUN_TESTNET_ORDERS=0", list_result.stdout)
        self.assertIn(
            "BINANCE_RUN_PHASE13_PUBLIC_CASE2=unset",
            list_result.stdout,
        )
        self.assertIn("testnet-credentials-and-cap=unset", list_result.stdout)
        self.assertIn("testnet-baseline-evidence=unset", list_result.stdout)
        self.assertIn("PYTHONWARNINGS=error", list_result.stdout)
        self.assertIn("backend-unittests", list_result.stdout)
        self.assertIn("ui-contract-drift-check", list_result.stdout)
        self.assertIn("phase13-deterministic-replay", list_result.stdout)
        self.assertIn("phase13-soak-no-order-preflight", list_result.stdout)
        self.assertIn("ui-typecheck", list_result.stdout)
        self.assertIn("ui-production-build", list_result.stdout)
        self.assertIn("rust-format-check", list_result.stdout)
        self.assertIn("rust-clippy-all-targets", list_result.stdout)
        self.assertIn("phase13-supply-chain-evidence-binding", list_result.stdout)
        self.assertIn("phase13-offline-vulnerability-scan", list_result.stdout)
        self.assertIn("phase13-offline-license-scan", list_result.stdout)
        self.assertIn("phase13-visual-regression", list_result.stdout)
        self.assertIn("communication-traceability-checker", list_result.stdout)
        self.assertIn("phase13-readiness-gap-gate", list_result.stdout)
        self.assertNotIn("secret-key-canary", list_result.stdout + list_result.stderr)
        self.assertNotIn("secret-value-canary", list_result.stdout + list_result.stderr)

    def test_fake_repository_runs_steps_in_order_with_sanitized_environment(
        self,
    ) -> None:
        """
        함수 이름: test_fake_repository_runs_steps_in_order_with_sanitized_environment()
        기능: lightweight fake executables로 실제 suite 대신 command 순서와 child 안전환경을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_root = Path(temporary_directory) / "repository"
            command_log = Path(temporary_directory) / "commands.log"
            self._prepare_fake_repository(fake_root)
            environment = self._build_fake_environment(fake_root, command_log)

            # Repository 밖 cwd에서 실행해도 copied script의 실제 parent를 root로 선택해야 한다.
            result = subprocess.run(
                ["/bin/sh", os.fspath(fake_root / "scripts" / "check_all.sh")],
                cwd=Path(temporary_directory),
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            command_lines = command_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(command_lines), 19)
            self.assertIn("-m unittest discover -s tests", command_lines[0])
            self.assertIn("-m unittest discover -s scripts", command_lines[1])
            self.assertIn("generate_ui_contracts.py --check", command_lines[2])
            self.assertIn("phase13_deterministic_replay.py", command_lines[3])
            self.assertIn("canonical_fault_trace.json", command_lines[3])
            self.assertIn("--repeat 5 --quiet", command_lines[3])
            self.assertIn("phase13_soak.py --validate-only", command_lines[4])
            self.assertIn("vitest", command_lines[5])
            self.assertIn("|run", command_lines[5])
            self.assertIn("tsc", command_lines[6])
            self.assertIn("-b --pretty false", command_lines[6])
            self.assertIn("tsc", command_lines[7])
            self.assertTrue(command_lines[7].endswith("|-b"))
            self.assertIn("vite", command_lines[8])
            self.assertIn("|build", command_lines[8])
            self.assertIn("fmt --all -- --check", command_lines[9])
            self.assertIn("test --locked --offline", command_lines[10])
            self.assertIn("clippy --locked --offline --all-targets", command_lines[11])
            self.assertIn("check_phase12_secrets.py", command_lines[12])
            self.assertIn("check_phase13_supply_chain_evidence.py", command_lines[13])
            self.assertIn("run_phase13_offline_osv.py vulnerability", command_lines[14])
            self.assertIn("run_phase13_offline_osv.py license", command_lines[15])
            self.assertIn("check_phase13_visual_regression.py", command_lines[16])
            self.assertIn("--current-directory", command_lines[16])
            self.assertIn("check_communication_traceability.py", command_lines[17])
            self.assertIn("phase13_readiness.py gate", command_lines[18])
            for command_line in command_lines:
                self.assertIn("|0|0|unset|unset|unset|", command_line)
                self.assertIn("|unset|error|", command_line)
            self.assertIn(
                "check_all: PASS: all local no-order readiness checks completed.",
                result.stdout,
            )

    def test_osv_steps_delegate_only_to_local_offline_policy_runner(self) -> None:
        """
        함수 이름: test_osv_steps_delegate_only_to_local_offline_policy_runner()
        기능: check_all이 raw OSV command나 remote option seam 없이 fixed wrapper만 호출하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        script_text = CHECK_ALL_SCRIPT.read_text(encoding="utf-8")

        # Shell layer에는 scanner argv를 두지 않아 모든 실제 OSV 실행을 한 local policy로 수렴시킨다.
        self.assertNotIn("osv-scanner scan", script_text)
        self.assertNotIn("--download-offline-databases", script_text)
        self.assertIn('"${PHASE13_OFFLINE_OSV_RUNNER}" vulnerability', script_text)
        self.assertIn('"${PHASE13_OFFLINE_OSV_RUNNER}" license', script_text)

    def test_first_failed_step_prevents_all_later_commands(self) -> None:
        """
        함수 이름: test_first_failed_step_prevents_all_later_commands()
        기능: 첫 backend fake failure 뒤 UI/Rust/scanner command가 실행되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_root = Path(temporary_directory) / "repository"
            command_log = Path(temporary_directory) / "commands.log"
            self._prepare_fake_repository(fake_root)
            environment = self._build_fake_environment(fake_root, command_log)
            environment["PHASE13_FAKE_FAIL_MATCH"] = "unittest"

            # 첫 Python unittest command만 log에 남고 set -e가 이후 단계 publication을 차단해야 한다.
            result = subprocess.run(
                ["/bin/sh", os.fspath(fake_root / "scripts" / "check_all.sh")],
                cwd=Path(temporary_directory),
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            command_lines = command_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(command_lines), 1)
            self.assertIn("-m unittest discover -s tests", command_lines[0])
            self.assertNotIn("check_all: PASS: backend-unittests", result.stdout)

    def test_replay_failure_prevents_ui_and_later_commands(self) -> None:
        """
        함수 이름: test_replay_failure_prevents_ui_and_later_commands()
        기능: canonical replay CLI 실패 뒤 UI, Rust와 scanner 단계가 실행되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_root = Path(temporary_directory) / "repository"
            command_log = Path(temporary_directory) / "commands.log"
            self._prepare_fake_repository(fake_root)
            environment = self._build_fake_environment(fake_root, command_log)
            environment["PHASE13_FAKE_FAIL_MATCH"] = "phase13_deterministic_replay.py"

            # Backend와 script unit test 뒤 replay가 실패하면 expensive suite와 scanner를 시작하지 않는다.
            result = subprocess.run(
                ["/bin/sh", os.fspath(fake_root / "scripts" / "check_all.sh")],
                cwd=Path(temporary_directory),
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            command_lines = command_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(command_lines), 4)
            self.assertIn("phase13_deterministic_replay.py", command_lines[3])
            self.assertNotIn("check_all: RUN: ui-vitest", result.stdout)

    def test_failed_offline_license_gate_still_runs_trace_and_readiness_gates(
        self,
    ) -> None:
        """
        함수 이름: test_failed_offline_license_gate_still_runs_trace_and_readiness_gates()
        기능: Offline license GAP 뒤에도 trace와 readiness gate를 실행하고 NO_GO로 집계한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_root = Path(temporary_directory) / "repository"
            command_log = Path(temporary_directory) / "commands.log"
            self._prepare_fake_repository(fake_root)
            environment = self._build_fake_environment(fake_root, command_log)
            environment["PHASE13_FAKE_FAIL_MATCH"] = (
                "run_phase13_offline_osv.py license"
            )

            # License command 한 건만 실패해도 trace와 readiness command까지 정확히 한 번씩 남긴다.
            result = subprocess.run(
                ["/bin/sh", os.fspath(fake_root / "scripts" / "check_all.sh")],
                cwd=Path(temporary_directory),
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            command_lines = command_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(command_lines), 19)
            self.assertIn("check_phase13_supply_chain_evidence.py", command_lines[13])
            self.assertIn("run_phase13_offline_osv.py vulnerability", command_lines[14])
            self.assertIn("run_phase13_offline_osv.py license", command_lines[15])
            self.assertIn("check_phase13_visual_regression.py", command_lines[16])
            self.assertIn("check_communication_traceability.py", command_lines[17])
            self.assertIn("phase13_readiness.py gate", command_lines[18])
            self.assertIn(
                "check_all: BLOCKED: phase13-offline-license-scan",
                result.stderr,
            )
            self.assertIn(
                "readiness evidence gate가 차단됐습니다",
                result.stderr,
            )
            self.assertNotIn(
                "check_all: PASS: all local no-order readiness checks completed.",
                result.stdout,
            )

    def test_failed_supply_evidence_binding_still_runs_other_evidence_gates(
        self,
    ) -> None:
        """
        함수 이름: test_failed_supply_evidence_binding_still_runs_other_evidence_gates()
        기능: Stale supply artifact 뒤에도 offline scan, trace와 readiness GAP을 모두 수집한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_root = Path(temporary_directory) / "repository"
            command_log = Path(temporary_directory) / "commands.log"
            self._prepare_fake_repository(fake_root)
            environment = self._build_fake_environment(fake_root, command_log)
            environment["PHASE13_FAKE_FAIL_MATCH"] = (
                "check_phase13_supply_chain_evidence.py"
            )

            # Binding 하나가 실패해도 뒤 세 evidence command는 누락 없이 실행돼야 한다.
            result = subprocess.run(
                ["/bin/sh", os.fspath(fake_root / "scripts" / "check_all.sh")],
                cwd=Path(temporary_directory),
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            command_lines = command_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(command_lines), 19)
            self.assertIn("run_phase13_offline_osv.py vulnerability", command_lines[14])
            self.assertIn("run_phase13_offline_osv.py license", command_lines[15])
            self.assertIn("check_phase13_visual_regression.py", command_lines[16])
            self.assertIn("check_communication_traceability.py", command_lines[17])
            self.assertIn("phase13_readiness.py gate", command_lines[18])
            self.assertIn(
                "check_all: BLOCKED: phase13-supply-chain-evidence-binding",
                result.stderr,
            )

    def test_missing_offline_vulnerability_database_does_not_hide_license_gap(
        self,
    ) -> None:
        """
        함수 이름: test_missing_offline_vulnerability_database_does_not_hide_license_gap()
        기능: Vulnerability DB 오류 뒤에도 별도 license, trace와 readiness evidence를 실행한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_root = Path(temporary_directory) / "repository"
            command_log = Path(temporary_directory) / "commands.log"
            self._prepare_fake_repository(fake_root)
            environment = self._build_fake_environment(fake_root, command_log)
            environment["PHASE13_FAKE_FAIL_MATCH"] = (
                "run_phase13_offline_osv.py vulnerability"
            )

            # 첫 OSV failure를 집계하되 다음 독립 license command는 반드시 실행한다.
            result = subprocess.run(
                ["/bin/sh", os.fspath(fake_root / "scripts" / "check_all.sh")],
                cwd=Path(temporary_directory),
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            command_lines = command_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(command_lines), 19)
            self.assertIn("run_phase13_offline_osv.py vulnerability", command_lines[14])
            self.assertIn("run_phase13_offline_osv.py license", command_lines[15])
            self.assertIn("check_phase13_visual_regression.py", command_lines[16])
            self.assertIn("check_communication_traceability.py", command_lines[17])
            self.assertIn("phase13_readiness.py gate", command_lines[18])
            self.assertIn(
                "check_all: BLOCKED: phase13-offline-vulnerability-scan",
                result.stderr,
            )

    def test_failed_visual_gate_still_runs_trace_and_readiness_gates(self) -> None:
        """
        함수 이름: test_failed_visual_gate_still_runs_trace_and_readiness_gates()
        기능: Browser pixel mismatch 뒤에도 Communication과 readiness 근거를 수집하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/29
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_root = Path(temporary_directory) / "repository"
            command_log = Path(temporary_directory) / "commands.log"
            self._prepare_fake_repository(fake_root)
            environment = self._build_fake_environment(fake_root, command_log)
            environment["PHASE13_FAKE_FAIL_MATCH"] = (
                "check_phase13_visual_regression.py"
            )

            # Visual NO_GO도 다른 evidence gate를 생략하지 않고 마지막에 함께 집계한다.
            result = subprocess.run(
                ["/bin/sh", os.fspath(fake_root / "scripts" / "check_all.sh")],
                cwd=Path(temporary_directory),
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            command_lines = command_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(command_lines), 19)
            self.assertIn("check_phase13_visual_regression.py", command_lines[16])
            self.assertIn("check_communication_traceability.py", command_lines[17])
            self.assertIn("phase13_readiness.py gate", command_lines[18])
            self.assertIn(
                "check_all: BLOCKED: phase13-visual-regression",
                result.stderr,
            )

    def _prepare_fake_repository(self, fake_root: Path) -> None:
        """
        함수 이름: _prepare_fake_repository()
        기능: copied gate가 요구하는 최소 directory와 command logger executable을 생성한다.
        인자: fake_root -> 임시 repository root
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        script_directory = fake_root / "scripts"
        backend_python = fake_root / "backend" / ".venv" / "bin" / "python"
        fake_bin_directory = fake_root / "fake-bin"
        ui_node_bin_directory = fake_root / "UI" / "node_modules" / ".bin"
        (fake_root / "backend" / "tests").mkdir(parents=True)
        replay_fixture_directory = (
            fake_root / "backend" / "tests" / "fixtures" / "phase13"
        )
        replay_fixture_directory.mkdir(parents=True)
        (fake_root / "UI" / "apps" / "desktop" / "src-tauri").mkdir(parents=True)
        script_directory.mkdir(parents=True)
        backend_python.parent.mkdir(parents=True)
        fake_bin_directory.mkdir(parents=True)
        ui_node_bin_directory.mkdir(parents=True)
        shutil.copyfile(CHECK_ALL_SCRIPT, script_directory / "check_all.sh")
        (script_directory / "check_phase12_secrets.py").write_text(
            "# fake secret checker\n",
            encoding="utf-8",
        )
        (script_directory / "check_communication_traceability.py").write_text(
            "# fake traceability checker\n",
            encoding="utf-8",
        )
        (script_directory / "check_phase13_supply_chain_evidence.py").write_text(
            "# fake supply evidence checker\n",
            encoding="utf-8",
        )
        (script_directory / "check_phase13_visual_regression.py").write_text(
            "# fake visual regression checker\n",
            encoding="utf-8",
        )
        (script_directory / "run_phase13_offline_osv.py").write_text(
            "# fake offline OSV runner\n",
            encoding="utf-8",
        )
        (script_directory / "phase13_deterministic_replay.py").write_text(
            "# fake deterministic replay runner\n",
            encoding="utf-8",
        )
        (script_directory / "phase13_soak.py").write_text(
            "# fake soak runner\n",
            encoding="utf-8",
        )
        (script_directory / "phase13_readiness.py").write_text(
            "# fake readiness runner\n",
            encoding="utf-8",
        )
        (replay_fixture_directory / "canonical_fault_trace.json").write_text(
            "{}\n",
            encoding="utf-8",
        )
        (fake_root / "backend" / "scripts").mkdir(parents=True)
        (fake_root / "backend" / "scripts" / "generate_ui_contracts.py").write_text(
            "# fake contract generator\n",
            encoding="utf-8",
        )
        (fake_root / "backend" / "uv.lock").write_text("# fake uv lock\n", encoding="utf-8")
        (fake_root / "UI" / "pnpm-lock.yaml").write_text("# fake pnpm lock\n", encoding="utf-8")
        (
            fake_root / "UI" / "apps" / "desktop" / "src-tauri" / "Cargo.lock"
        ).write_text("# fake cargo lock\n", encoding="utf-8")
        (fake_root / "UI" / "package.json").write_text("{}\n", encoding="utf-8")
        (fake_root / "UI" / "apps" / "desktop" / "src-tauri" / "Cargo.toml").write_text(
            "[package]\nname='fake'\nversion='0.0.0'\n",
            encoding="utf-8",
        )

        # 모든 fake executable은 동일 logger로 cwd, 안전 flag, credential unset과 argv를 남긴다.
        fake_executable = """#!/bin/sh
key_state=${BINANCE_TESTNET_API_KEY+set}
secret_state=${BINANCE_TESTNET_API_SECRET+set}
public_case2_state=${BINANCE_RUN_PHASE13_PUBLIC_CASE2+set}
baseline_state=unset
if [ "${BINANCE_TESTNET_BASELINE_HISTORY_PATH+set}" = "set" ] || \
    [ "${BINANCE_TESTNET_BASELINE_HISTORY_FD+set}" = "set" ] || \
    [ "${BINANCE_TESTNET_BASELINE_HISTORY_SHA256+set}" = "set" ] || \
    [ "${BINANCE_TESTNET_BASELINE_PENDING_FD+set}" = "set" ] || \
    [ "${BINANCE_TESTNET_BASELINE_PENDING_SHA256+set}" = "set" ]; then
    baseline_state=set
fi
printf '%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\\n' \
    "$(basename "$0")" "$PWD" "$BINANCE_RUN_TESTNET" \
    "$BINANCE_RUN_TESTNET_ORDERS" "${key_state:-unset}" \
    "${secret_state:-unset}" "${public_case2_state:-unset}" \
    "$baseline_state" "$PYTHONWARNINGS" "$*" >> "$PHASE13_COMMAND_LOG"
if [ -n "${PHASE13_FAKE_FAIL_MATCH:-}" ]; then
    case "$*" in
        *"$PHASE13_FAKE_FAIL_MATCH"*) exit 9 ;;
    esac
fi
exit 0
"""
        for executable_path in (
            backend_python,
            ui_node_bin_directory / "vitest",
            ui_node_bin_directory / "tsc",
            ui_node_bin_directory / "vite",
            fake_bin_directory / "cargo",
        ):
            executable_path.write_text(fake_executable, encoding="utf-8")
            executable_path.chmod(0o755)

    def _build_fake_environment(
        self,
        fake_root: Path,
        command_log: Path,
    ) -> dict[str, str]:
        """
        함수 이름: _build_fake_environment()
        기능: fake command PATH와 hostile Testnet opt-in을 함께 가진 child 환경을 만든다.
        인자: fake_root -> fake-bin을 가진 임시 repository
            command_log -> fake executable이 append할 log file
        반환값: subprocess용 environment dictionary
        작성 날짜: 2026/08/24
        """
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": os.pathsep.join(
                    (
                        os.fspath(fake_root / "fake-bin"),
                        environment.get("PATH", ""),
                    )
                ),
                "PHASE13_COMMAND_LOG": os.fspath(command_log),
                "BINANCE_RUN_TESTNET": "1",
                "BINANCE_RUN_TESTNET_ORDERS": "1",
                "BINANCE_RUN_PHASE13_PUBLIC_CASE2": "1",
                "BINANCE_TESTNET_API_KEY": "must-be-unset",
                "BINANCE_TESTNET_API_SECRET": "must-also-be-unset",
                "BINANCE_TESTNET_MAX_NOTIONAL": "1000",
                "BINANCE_TESTNET_BASELINE_HISTORY_PATH": "must-be-unset",
                "BINANCE_TESTNET_BASELINE_HISTORY_FD": "41",
                "BINANCE_TESTNET_BASELINE_HISTORY_SHA256": "must-be-unset",
                "BINANCE_TESTNET_BASELINE_PENDING_FD": "42",
                "BINANCE_TESTNET_BASELINE_PENDING_SHA256": "must-be-unset",
                "PYTHONWARNINGS": "ignore",
            }
        )
        return environment


if __name__ == "__main__":
    unittest.main()  # Direct 실행도 hostile environment 격리와 fail-fast 순서를 모두 검증한다.
