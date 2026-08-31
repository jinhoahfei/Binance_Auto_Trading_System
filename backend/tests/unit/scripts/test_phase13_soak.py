"""Phase 13 soak configuration, order gate와 secret-free metric/report schema를 검증한다."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


# Standard backend discovery에서도 repository-root scripts namespace를 import할 수 있게 test 경계만 보강한다.
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.phase13_soak import (  # noqa: E402
    APPLICATION_METRIC_KEYS,
    MINIMUM_FORMAL_SOAK_SECONDS,
    RISK_POLICY_UNAVAILABLE_CODE,
    REPORT_FILE_NAME,
    METRICS_FILE_NAME,
    SoakConfiguration,
    SoakConfigurationError,
    SoakMode,
    build_metric_record,
    build_soak_configuration,
    build_soak_report,
    main,
    run_soak,
)


FIXED_TIME = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)
TESTNET_ENVIRONMENT = {
    "BINANCE_RUN_TESTNET": "1",
    "BINANCE_TESTNET_API_KEY": "testnet-key-secret-canary",
    "BINANCE_TESTNET_API_SECRET": "testnet-secret-secret-canary",
    "BINANCE_RUN_TESTNET_ORDERS": "0",
}


class PhaseThirteenSoakTests(unittest.TestCase):
    """
    클래스 이름: PhaseThirteenSoakTests
    기능: local default, Testnet 다중 opt-in과 24시간 report 경계를 결정론적으로 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_local_default_ignores_hostile_testnet_order_environment(self) -> None:
        """
        함수 이름: test_local_default_ignores_hostile_testnet_order_environment()
        기능: CLI Testnet flag가 없으면 inherited credential/order 환경을 읽지 않고 local no-order가 되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 주문 opt-in과 credential이 모두 켜진 상속 환경을 local 기본 실행에 주입한다.
        hostile_environment = {
            **TESTNET_ENVIRONMENT,
            "BINANCE_RUN_TESTNET_ORDERS": "1",
            "BINANCE_TESTNET_MAX_NOTIONAL": "100",
        }
        configuration = build_soak_configuration(  # CLI는 Testnet을 요청하지 않는다.
            testnet_requested=False,
            testnet_orders_requested=False,
            duration_hours_text="25",
            sample_interval_seconds=60,
            output_directory=None,
            max_order_mutations=None,
            environment=hostile_environment,
        )

        # Hostile 환경만으로 mode나 주문 mutation 권한이 상승하지 않는지 검사한다.
        self.assertIs(configuration.mode, SoakMode.LOCAL_NO_ORDER)
        self.assertFalse(configuration.order_mutation_enabled)
        self.assertIsNone(configuration.max_order_mutations)
        self.assertNotIn(hostile_environment["BINANCE_TESTNET_API_KEY"], repr(configuration))
        self.assertNotIn(hostile_environment["BINANCE_TESTNET_API_SECRET"], repr(configuration))

    def test_testnet_read_only_reuses_existing_configuration_gate(self) -> None:
        """
        함수 이름: test_testnet_read_only_reuses_existing_configuration_gate()
        기능: read-only Testnet이 기존 explicit flag와 credential pair를 요구하되 주문 권한은 보존하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 기존 Testnet credential gate를 통과하되 별도 주문 CLI opt-in은 제공하지 않는다.
        configuration = build_soak_configuration(
            testnet_requested=True,
            testnet_orders_requested=False,
            duration_hours_text="24",
            sample_interval_seconds=60,
            output_directory=None,
            max_order_mutations=None,
            environment=TESTNET_ENVIRONMENT,
        )

        self.assertIs(  # 결과 mode는 반드시 read-only로 고정되어야 한다.
            configuration.mode, SoakMode.TESTNET_READ_ONLY
        )
        self.assertFalse(configuration.order_mutation_enabled)
        self.assertIsNone(configuration.max_order_mutations)

    def test_environment_order_opt_in_without_cli_flag_is_rejected(self) -> None:
        """
        함수 이름: test_environment_order_opt_in_without_cli_flag_is_rejected()
        기능: 환경 flag와 cap만으로 read-only CLI가 주문 mode로 상승하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        environment = {
            **TESTNET_ENVIRONMENT,
            "BINANCE_RUN_TESTNET_ORDERS": "1",
            "BINANCE_TESTNET_MAX_NOTIONAL": "10",
        }
        with self.assertRaisesRegex(SoakConfigurationError, "separate CLI"):
            build_soak_configuration(
                testnet_requested=True,
                testnet_orders_requested=False,
                duration_hours_text="24",
                sample_interval_seconds=60,
                output_directory=None,
                max_order_mutations=None,
                environment=environment,
            )

    def test_complete_legacy_order_opt_in_still_requires_risk_policy(self) -> None:
        """
        함수 이름: test_complete_legacy_order_opt_in_still_requires_risk_policy()
        기능: CLI, Testnet env, positive notional과 mutation cap이 있어도 미확정 누적 RiskPolicy로 주문을 열지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        environment = {
            **TESTNET_ENVIRONMENT,
            "BINANCE_RUN_TESTNET_ORDERS": "1",
            "BINANCE_TESTNET_MAX_NOTIONAL": "10",
        }
        with self.assertRaisesRegex(
            SoakConfigurationError,
            RISK_POLICY_UNAVAILABLE_CODE,
        ):
            build_soak_configuration(
                testnet_requested=True,
                testnet_orders_requested=True,
                duration_hours_text="24",
                sample_interval_seconds=60,
                output_directory=None,
                max_order_mutations=1,
                environment=environment,
            )

    def test_report_below_twenty_four_hours_is_development_only(self) -> None:
        """
        함수 이름: test_report_below_twenty_four_hours_is_development_only()
        기능: requested duration과 무관하게 실제 관찰이 24시간 미만이면 정식 report가 되지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 요청 시간은 정식 기준을 넘기되 관찰 시간만 경계 아래와 정확한 경계로 나눈다.
        configuration = SoakConfiguration(
            mode=SoakMode.LOCAL_NO_ORDER,
            duration_seconds=MINIMUM_FORMAL_SOAK_SECONDS + 3600,
            sample_interval_seconds=60,
            output_directory=None,
            order_mutation_enabled=False,
            max_order_mutations=None,
        )
        short_report = build_soak_report(  # 24시간보다 1초 짧은 관찰이다.
            configuration,
            started_at=FIXED_TIME,
            ended_at=FIXED_TIME,
            observed_duration_seconds=MINIMUM_FORMAL_SOAK_SECONDS - 1,
            sample_count=10,
            missing_application_metrics=True,
            interrupted=False,
        )
        boundary_report = build_soak_report(  # 정확히 24시간을 관찰한 경계다.
            configuration,
            started_at=FIXED_TIME,
            ended_at=FIXED_TIME,
            observed_duration_seconds=MINIMUM_FORMAL_SOAK_SECONDS,
            sample_count=10,
            missing_application_metrics=False,
            interrupted=False,
        )

        # 실제 관찰 시간이 report class를 결정하고 readiness GAP은 별도로 유지되는지 검사한다.
        self.assertEqual(short_report["report_class"], "DEVELOPMENT")
        self.assertIn("DURATION_BELOW_24_HOURS", short_report["gap_codes"])
        self.assertEqual(boundary_report["report_class"], "FORMAL")
        self.assertNotIn("DURATION_BELOW_24_HOURS", boundary_report["gap_codes"])
        self.assertEqual(boundary_report["readiness_status"], "GAP")

    def test_single_sample_writes_secret_free_jsonl_and_gap_report(self) -> None:
        """
        함수 이름: test_single_sample_writes_secret_free_jsonl_and_gap_report()
        기능: 실제 sleep 없이 한 metric과 development GAP report를 canonical file로 생성하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            # 단일 sample로 종료되는 local evidence 경로와 결정론 clock을 구성한다.
            output_directory = Path(temporary_directory) / "evidence"
            configuration = SoakConfiguration(
                mode=SoakMode.LOCAL_NO_ORDER,
                duration_seconds=MINIMUM_FORMAL_SOAK_SECONDS,
                sample_interval_seconds=60,
                output_directory=output_directory,
                order_mutation_enabled=False,
                max_order_mutations=None,
            )
            wall_times = iter((FIXED_TIME, FIXED_TIME, FIXED_TIME))
            monotonic_times = iter((0.0, 0.0, 0.0))
            report = run_soak(  # Sleep 없이 첫 sample 직후 종료한다.
                configuration,
                wall_clock=lambda: next(wall_times),
                monotonic_clock=lambda: next(monotonic_times),
                sleeper=lambda _seconds: self.fail("single sample must not sleep"),
                sample_limit=1,
            )

            # 생성된 JSONL과 report를 다시 읽어 schema, canonical 내용과 secret 비노출을 검사한다.
            metric_text = (output_directory / METRICS_FILE_NAME).read_text(
                encoding="utf-8"
            )
            report_text = (output_directory / REPORT_FILE_NAME).read_text(
                encoding="utf-8"
            )
            metric_lines = metric_text.splitlines()
            self.assertEqual(len(metric_lines), 1)
            metric_record = json.loads(metric_lines[0])
            self.assertFalse(metric_record["order_mutation_enabled"])
            self.assertEqual(
                set(metric_record["application"]),
                set(APPLICATION_METRIC_KEYS),
            )
            self.assertTrue(
                all(value is None for value in metric_record["application"].values())
            )
            self.assertEqual(json.loads(report_text), report)
            self.assertEqual(report["report_class"], "DEVELOPMENT")
            combined_evidence = metric_text + report_text
            self.assertNotIn(TESTNET_ENVIRONMENT["BINANCE_TESTNET_API_KEY"], combined_evidence)
            self.assertNotIn(TESTNET_ENVIRONMENT["BINANCE_TESTNET_API_SECRET"], combined_evidence)

    def test_metric_schema_rejects_unknown_application_field(self) -> None:
        """
        함수 이름: test_metric_schema_rejects_unknown_application_field()
        기능: raw payload나 credential field가 application metric mapping에 추가되지 못하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 허용 application metric key로 정상 mapping을 만든 뒤 credential key를 추가한다.
        configuration = SoakConfiguration(
            mode=SoakMode.LOCAL_NO_ORDER,
            duration_seconds=3600,
            sample_interval_seconds=60,
            output_directory=None,
            order_mutation_enabled=False,
            max_order_mutations=None,
        )
        invalid_metrics = {  # Canonical metric key마다 안전한 정수 값을 배치한다.
            metric_key: 0 for metric_key in APPLICATION_METRIC_KEYS
        }
        invalid_metrics["credential"] = "must-not-be-serialized"

        with self.assertRaisesRegex(  # Unknown key는 serialization 전에 거부되어야 한다.
            ValueError, "exact Phase 13 keys"
        ):
            build_metric_record(
                configuration,
                sequence=1,
                observed_at=FIXED_TIME,
                elapsed_seconds=0,
                application_metrics=invalid_metrics,
            )

    def test_validate_only_defaults_to_local_no_order(self) -> None:
        """
        함수 이름: test_validate_only_defaults_to_local_no_order()
        기능: CLI 기본 validation이 inherited Testnet order 환경에서도 file/network mutation 없이 성공하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        hostile_environment = {
            "BINANCE_RUN_TESTNET": "1",
            "BINANCE_RUN_TESTNET_ORDERS": "1",
            "BINANCE_TESTNET_API_KEY": "hostile-key-canary",
            "BINANCE_TESTNET_API_SECRET": "hostile-secret-canary",
            "BINANCE_TESTNET_MAX_NOTIONAL": "999",
        }
        with patch.dict(os.environ, hostile_environment, clear=True):
            self.assertEqual(main(["--validate-only"]), 0)

    def test_main_returns_gap_for_development_evidence(self) -> None:
        """
        함수 이름: test_main_returns_gap_for_development_evidence()
        기능: 한 sample 개발 report가 evidence 생성 성공만으로 readiness PASS가 되지 않음을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        with tempfile.TemporaryDirectory() as output_directory:
            exit_code = main(
                [
                    "--duration-hours",
                    "1",
                    "--output-directory",
                    output_directory,
                    "--single-sample",
                ]
            )

        self.assertEqual(exit_code, 1)  # 실제 24시간 관찰과 app metric이 없으므로 readiness는 GAP이다.


if __name__ == "__main__":
    unittest.main()
