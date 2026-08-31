#!/usr/bin/env python3
"""Phase 13 장시간 검증을 외부 주문 없는 metric과 GAP report로 기록한다."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import json
import os
from pathlib import Path
import resource
import sys
from threading import active_count
from time import monotonic, sleep
from typing import BinaryIO


# 기존 Testnet configuration loader를 복제하지 않고 backend source에서 직접 재사용한다.
_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_SOURCE_DIRECTORY = _REPOSITORY_ROOT / "backend" / "src"
if str(_BACKEND_SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SOURCE_DIRECTORY))

from binance_auto_trader.bootstrap.testnet import (  # noqa: E402
    TestnetConfigurationError,
    load_testnet_configuration,
    require_testnet_order_permission,
)


SCHEMA_VERSION = 1
MINIMUM_FORMAL_SOAK_SECONDS = 24 * 60 * 60
DEFAULT_SOAK_HOURS = Decimal("25")
DEFAULT_SAMPLE_INTERVAL_SECONDS = 60
MAXIMUM_SOAK_SECONDS = 7 * 24 * 60 * 60
MAXIMUM_SAMPLE_INTERVAL_SECONDS = 60 * 60
METRICS_FILE_NAME = "phase13-soak-metrics.jsonl"
REPORT_FILE_NAME = "phase13-soak-report.json"
RISK_POLICY_UNAVAILABLE_CODE = "RISK_POLICY_UNAVAILABLE"

# Application metric shape는 roadmap §16.8의 process/listener/queue/order/history 항목을 고정한다.
APPLICATION_METRIC_KEYS = (
    "main_process_count",
    "sidecar_process_count",
    "python_process_count",
    "task_count",
    "loopback_listener_count",
    "reconnect_generation",
    "queue_size",
    "replay_buffer_size",
    "pending_order_count",
    "unknown_order_count",
    "position_quantity",
    "history_count",
    "last_sequence",
    "orphan_process_count",
    "orphan_listener_count",
    "relaunch_ready_count",
)


class SoakConfigurationError(RuntimeError):
    """
    클래스 이름: SoakConfigurationError
    기능: soak 실행 mode, duration 또는 주문 opt-in이 fail-closed 계약을 위반함을 나타낸다.
    작성 날짜: 2026/08/24
    """


class SoakMode(str, Enum):
    """
    클래스 이름: SoakMode
    기능: local, Testnet read-only와 별도 주문 opt-in mode를 구분한다.
    작성 날짜: 2026/08/24
    """

    LOCAL_NO_ORDER = "LOCAL_NO_ORDER"
    TESTNET_READ_ONLY = "TESTNET_READ_ONLY"
    TESTNET_ORDER_OPT_IN = "TESTNET_ORDER_OPT_IN"


@dataclass(frozen=True, slots=True)
class SoakConfiguration:
    """
    클래스 이름: SoakConfiguration
    기능: secret을 보존하지 않는 soak duration, sampling, mode와 output 경계를 정의한다.
    작성 날짜: 2026/08/24
    """

    mode: SoakMode
    duration_seconds: int
    sample_interval_seconds: int
    output_directory: Path | None
    order_mutation_enabled: bool
    max_order_mutations: int | None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: soak 숫자와 mode별 주문 권한 조합을 exact type으로 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # bool이 int로 통과하거나 7일을 넘는 무제한 실행이 만들어지지 않게 범위를 고정한다.
        if (
            type(self.duration_seconds) is not int
            or self.duration_seconds <= 0
            or self.duration_seconds > MAXIMUM_SOAK_SECONDS
        ):
            raise ValueError("duration_seconds is outside the supported range")
        if (
            type(self.sample_interval_seconds) is not int
            or self.sample_interval_seconds <= 0
            or self.sample_interval_seconds > MAXIMUM_SAMPLE_INTERVAL_SECONDS
        ):
            raise ValueError("sample_interval_seconds is outside the supported range")
        if self.output_directory is not None and not isinstance(
            self.output_directory,
            Path,
        ):
            raise TypeError("output_directory must be a Path or None")
        if type(self.order_mutation_enabled) is not bool:
            raise TypeError("order_mutation_enabled must be a bool")

        # 주문 mode가 아닌 configuration은 mutation cap을 보존하지 않아 권한 상승 여지를 없앤다.
        if self.mode is SoakMode.TESTNET_ORDER_OPT_IN:
            if not self.order_mutation_enabled:
                raise ValueError("order mode requires order mutation permission")
            if (
                type(self.max_order_mutations) is not int
                or self.max_order_mutations <= 0
            ):
                raise ValueError("order mode requires a positive mutation cap")
        elif self.order_mutation_enabled or self.max_order_mutations is not None:
            raise ValueError("no-order mode cannot contain an order mutation cap")


def _parse_duration_seconds(duration_hours_text: str) -> int:
    """
    함수 이름: _parse_duration_seconds()
    기능: CLI hour 문자열을 float 없이 양의 정수 초로 변환한다.
    인자: duration_hours_text -> 최대 7일인 Decimal hour 문자열
    반환값: 정확한 정수 duration seconds
    작성 날짜: 2026/08/24
    """
    # CLI 문자열을 Decimal로 읽어 float 반올림 없이 정확한 정수 초인지 검증한다.
    if not isinstance(duration_hours_text, str) or not duration_hours_text:
        raise SoakConfigurationError("duration hours must be a non-empty string")
    try:
        duration_hours = Decimal(duration_hours_text)
    except InvalidOperation:
        raise SoakConfigurationError("duration hours must be a decimal") from None
    duration_seconds_decimal = duration_hours * Decimal(60 * 60)
    if (
        not duration_hours.is_finite()
        or duration_hours <= Decimal("0")
        or duration_seconds_decimal != duration_seconds_decimal.to_integral_value()
    ):
        raise SoakConfigurationError("duration must resolve to positive whole seconds")
    duration_seconds = int(duration_seconds_decimal)
    if duration_seconds > MAXIMUM_SOAK_SECONDS:
        raise SoakConfigurationError("duration exceeds the seven day safety bound")

    return duration_seconds  # 정식 여부는 요청 시간이 아니라 실제 관찰 시간이 24시간 이상인지로 판정한다.


def build_soak_configuration(
    *,
    testnet_requested: bool,
    testnet_orders_requested: bool,
    duration_hours_text: str,
    sample_interval_seconds: int,
    output_directory: Path | None,
    max_order_mutations: int | None,
    environment: Mapping[str, str],
) -> SoakConfiguration:
    """
    함수 이름: build_soak_configuration()
    기능: CLI와 기존 Testnet 환경을 결합하되 확정 RiskPolicy 없이는 주문 mode를 열지 않는다.
    인자: testnet_requested -> read-only Testnet 명시 flag
        testnet_orders_requested -> 별도 Testnet 주문 명시 flag
        duration_hours_text -> Decimal hour 문자열
        sample_interval_seconds -> metric 간격 초
        output_directory -> evidence directory 또는 validate-only의 None
        max_order_mutations -> 주문 scenario의 양수 총 mutation cap 또는 None
        environment -> 기존 Testnet opt-in과 credential 환경 mapping
    반환값: secret 없는 fail-closed SoakConfiguration
    작성 날짜: 2026/08/24
    """
    # CLI bool과 환경 mapping은 configuration loader 전에 검증해 truthy 우회를 막는다.
    if type(testnet_requested) is not bool:
        raise TypeError("testnet_requested must be a bool")
    if type(testnet_orders_requested) is not bool:
        raise TypeError("testnet_orders_requested must be a bool")
    if not isinstance(environment, Mapping):
        raise TypeError("environment must be a mapping")
    duration_seconds = _parse_duration_seconds(duration_hours_text)
    if (
        type(sample_interval_seconds) is not int
        or sample_interval_seconds <= 0
        or sample_interval_seconds > MAXIMUM_SAMPLE_INTERVAL_SECONDS
    ):
        raise SoakConfigurationError("sample interval is outside the supported range")

    # Local default는 hostile inherited Testnet 환경이 있어도 credential을 읽지 않고 mutation 0으로 고정한다.
    if not testnet_requested:
        if testnet_orders_requested:
            raise SoakConfigurationError("Testnet order flag requires Testnet mode")
        if max_order_mutations is not None:
            raise SoakConfigurationError("order mutation cap requires order opt-in")
        return SoakConfiguration(
            mode=SoakMode.LOCAL_NO_ORDER,
            duration_seconds=duration_seconds,
            sample_interval_seconds=sample_interval_seconds,
            output_directory=output_directory,
            order_mutation_enabled=False,
            max_order_mutations=None,
        )

    # Testnet read-only도 기존 loader의 explicit flag와 credential 검증을 그대로 재사용한다.
    try:
        testnet_configuration = load_testnet_configuration(environment)
    except TestnetConfigurationError as error:
        raise SoakConfigurationError("Testnet configuration was rejected") from error
    if not testnet_orders_requested:
        if testnet_configuration.allow_testnet_orders:
            raise SoakConfigurationError(
                "environment order opt-in requires the separate CLI flag"
            )
        if max_order_mutations is not None:
            raise SoakConfigurationError("order mutation cap requires order opt-in")
        return SoakConfiguration(
            mode=SoakMode.TESTNET_READ_ONLY,
            duration_seconds=duration_seconds,
            sample_interval_seconds=sample_interval_seconds,
            output_directory=output_directory,
            order_mutation_enabled=False,
            max_order_mutations=None,
        )

    # CLI, environment, positive notional과 총 mutation cap은 RiskPolicy 검사보다 먼저 모두 필요하다.
    try:
        require_testnet_order_permission(testnet_configuration)
    except TestnetConfigurationError as error:
        raise SoakConfigurationError("Testnet order permission was rejected") from error
    if type(max_order_mutations) is not int or max_order_mutations <= 0:
        raise SoakConfigurationError("Testnet order mode requires a positive mutation cap")

    # 누적 position/daily loss/scope/manual-kill 정책 source가 아직 없으므로 숫자를 임의 생성하지 않는다.
    raise SoakConfigurationError(RISK_POLICY_UNAVAILABLE_CODE)


def _utc_now() -> datetime:
    """
    함수 이름: _utc_now()
    기능: soak evidence에 사용할 timezone-aware UTC 현재 시각을 반환한다.
    인자: 없음
    반환값: UTC datetime
    작성 날짜: 2026/08/24
    """
    return datetime.now(timezone.utc)


def _format_utc(value: datetime) -> str:
    """
    함수 이름: _format_utc()
    기능: timezone-aware UTC datetime을 microsecond 없는 canonical Z 문자열로 변환한다.
    인자: value -> UTC datetime
    반환값: YYYY-MM-DDTHH:MM:SSZ 문자열
    작성 날짜: 2026/08/24
    """
    # Evidence timestamp는 naive 또는 UTC가 아닌 datetime을 허용하지 않는다.
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError("value must be a timezone-aware datetime")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("value must use UTC")

    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_open_file_descriptor_count() -> int | None:
    """
    함수 이름: _read_open_file_descriptor_count()
    기능: 지원 POSIX 경로에서 현재 harness process의 open FD 개수를 읽는다.
    인자: 없음
    반환값: 음이 아닌 FD 수 또는 platform에서 관찰 불가하면 None
    작성 날짜: 2026/08/24
    """
    # macOS와 Linux의 process FD directory를 순서대로 조회하고 둘 다 없으면 관찰 불가로 둔다.
    for descriptor_directory in (Path("/dev/fd"), Path("/proc/self/fd")):
        try:
            if descriptor_directory.is_dir():
                return sum(1 for _entry in descriptor_directory.iterdir())
        except OSError:
            continue  # 한 platform 경로가 사라지면 다음 공식 process FD 경로만 확인한다.
    return None


def _read_rss_high_water_bytes() -> int | None:
    """
    함수 이름: _read_rss_high_water_bytes()
    기능: getrusage의 platform 단위를 byte로 정규화한 process RSS high-water mark를 읽는다.
    인자: 없음
    반환값: 음이 아닌 byte 수 또는 관찰 실패 시 None
    작성 날짜: 2026/08/24
    """
    try:
        raw_high_water = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (OSError, ValueError):
        return None
    if isinstance(raw_high_water, bool) or raw_high_water < 0:
        return None

    # macOS는 byte, Linux와 다수 POSIX는 KiB를 반환하므로 현재 platform 계약으로 변환한다.
    multiplier = 1 if sys.platform == "darwin" else 1024
    return int(raw_high_water) * multiplier


def collect_local_process_metrics() -> dict[str, int | None]:
    """
    함수 이름: collect_local_process_metrics()
    기능: network 없이 현재 harness PID, thread, FD와 RSS high-water를 수집한다.
    인자: 없음
    반환값: secret 없는 process metric object
    작성 날짜: 2026/08/24
    """
    return {  # 외부 process 목록이나 command line을 읽지 않는 local scalar만 기록한다.
        "pid": os.getpid(),
        "thread_count": active_count(),
        "open_fd_count": _read_open_file_descriptor_count(),
        "rss_high_water_bytes": _read_rss_high_water_bytes(),
    }


def _normalize_application_metrics(
    application_metrics: Mapping[str, object] | None,
) -> dict[str, object]:
    """
    함수 이름: _normalize_application_metrics()
    기능: app observer가 없으면 exact null shape를 만들고 있으면 secret-free scalar만 검증한다.
    인자: application_metrics -> roadmap metric mapping 또는 None
    반환값: key 순서와 값 타입이 검증된 새 dictionary
    작성 날짜: 2026/08/24
    """
    # Provider 부재는 필수 key를 제거하지 않고 모두 명시적 null인 exact shape로 표현한다.
    if application_metrics is None:
        return {metric_key: None for metric_key in APPLICATION_METRIC_KEYS}
    if not isinstance(application_metrics, Mapping):
        raise TypeError("application_metrics must be a mapping or None")
    if set(application_metrics) != set(APPLICATION_METRIC_KEYS):
        raise ValueError("application_metrics must contain the exact Phase 13 keys")

    # Position은 canonical Decimal 문자열로, 나머지는 음이 아닌 exact int로 제한한다.
    normalized_metrics: dict[str, object] = {}
    for metric_key in APPLICATION_METRIC_KEYS:
        metric_value = application_metrics[metric_key]
        if metric_key == "position_quantity":
            if metric_value is not None:
                if (
                    not isinstance(metric_value, str)
                    or not metric_value
                    or len(metric_value) > 128
                    or metric_value != metric_value.strip()
                ):
                    raise TypeError(
                        "position_quantity must be a canonical string or None"
                    )
                try:
                    position_quantity = Decimal(metric_value)
                except InvalidOperation:
                    raise TypeError(
                        "position_quantity must contain a decimal or be None"
                    ) from None
                if not position_quantity.is_finite() or position_quantity < Decimal("0"):
                    raise ValueError(
                        "position_quantity must be finite and non-negative"
                    )
        elif metric_value is not None and (
            type(metric_value) is not int or metric_value < 0
        ):
            raise TypeError(f"{metric_key} must be a non-negative integer or None")
        normalized_metrics[metric_key] = metric_value

    return normalized_metrics  # credential, path와 raw exchange payload를 가질 container를 허용하지 않는다.


def build_metric_record(
    configuration: SoakConfiguration,
    *,
    sequence: int,
    observed_at: datetime,
    elapsed_seconds: int,
    application_metrics: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """
    함수 이름: build_metric_record()
    기능: 한 process/app 관찰을 canonical secret-free Phase 13 JSONL record로 만든다.
    인자: configuration -> 실행 mode와 mutation gate
        sequence -> 1부터 증가하는 metric sequence
        observed_at -> UTC 관찰 시각
        elapsed_seconds -> 시작 후 monotonic 정수 초
        application_metrics -> app observer snapshot 또는 None
    반환값: JSON 직렬화 가능한 exact metric object
    작성 날짜: 2026/08/24
    """
    # Sequence와 elapsed time을 exact 범위로 검증한 뒤 process/app 관찰을 한 record에 결합한다.
    if not isinstance(configuration, SoakConfiguration):
        raise TypeError("configuration must be a SoakConfiguration")
    if type(sequence) is not int or sequence <= 0:
        raise ValueError("sequence must be a positive integer")
    if type(elapsed_seconds) is not int or elapsed_seconds < 0:
        raise ValueError("elapsed_seconds must be a non-negative integer")

    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "phase13_soak_metric",
        "sequence": sequence,
        "observed_at": _format_utc(observed_at),
        "elapsed_seconds": elapsed_seconds,
        "mode": configuration.mode.value,
        "order_mutation_enabled": configuration.order_mutation_enabled,
        "process": collect_local_process_metrics(),
        "application": _normalize_application_metrics(application_metrics),
    }


def build_soak_report(
    configuration: SoakConfiguration,
    *,
    started_at: datetime,
    ended_at: datetime,
    observed_duration_seconds: int,
    sample_count: int,
    missing_application_metrics: bool,
    interrupted: bool,
) -> dict[str, object]:
    """
    함수 이름: build_soak_report()
    기능: 실제 관찰 duration과 미구현 leak/relaunch 판정을 과장하지 않는 GAP report를 만든다.
    인자: configuration -> 실행 요청과 mode
        started_at -> 첫 metric 전 UTC 시각
        ended_at -> 마지막 metric 뒤 UTC 시각
        observed_duration_seconds -> monotonic 실제 관찰 초
        sample_count -> durable metric line 수
        missing_application_metrics -> app metric null 관찰 여부
        interrupted -> duration 전에 operator interrupt가 있었는지 여부
    반환값: secret-free summary JSON object
    작성 날짜: 2026/08/24
    """
    # 실제 duration/sample과 report flag가 bool 우회 없는 관찰값인지 먼저 검증한다.
    for field_name, field_value in (
        ("observed_duration_seconds", observed_duration_seconds),
        ("sample_count", sample_count),
    ):
        if type(field_value) is not int or field_value < 0:
            raise ValueError(f"{field_name} must be a non-negative integer")
    if type(missing_application_metrics) is not bool or type(interrupted) is not bool:
        raise TypeError("report flags must be bool values")

    # 정식 report label은 요청값이 아니라 monotonic 실제 관찰이 24시간 이상일 때만 부여한다.
    formal_duration_met = observed_duration_seconds >= MINIMUM_FORMAL_SOAK_SECONDS
    gap_codes: list[str] = []
    if not formal_duration_met:
        gap_codes.append("DURATION_BELOW_24_HOURS")
    if missing_application_metrics:
        gap_codes.append("APPLICATION_METRICS_UNAVAILABLE")
    gap_codes.append("LEAK_ACCEPTANCE_POLICY_NOT_RUN")
    gap_codes.append("SHUTDOWN_RELAUNCH_NOT_OBSERVED")
    if configuration.mode is not SoakMode.LOCAL_NO_ORDER:
        gap_codes.append("TESTNET_RUNTIME_NOT_CONNECTED")
    if interrupted:
        gap_codes.append("RUN_INTERRUPTED")

    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "phase13_soak_report",
        "report_class": "FORMAL" if formal_duration_met else "DEVELOPMENT",
        "readiness_status": "GAP" if gap_codes else "PASS",
        "mode": configuration.mode.value,
        "order_mutation_enabled": configuration.order_mutation_enabled,
        "max_order_mutations": configuration.max_order_mutations,
        "requested_duration_seconds": configuration.duration_seconds,
        "observed_duration_seconds": observed_duration_seconds,
        "minimum_formal_duration_seconds": MINIMUM_FORMAL_SOAK_SECONDS,
        "sample_count": sample_count,
        "started_at": _format_utc(started_at),
        "ended_at": _format_utc(ended_at),
        "metrics_file": METRICS_FILE_NAME,
        "gap_codes": gap_codes,
    }  # GAP이 남으면 duration이 길어도 leak-free 또는 live-ready라고 표시하지 않는다.


def _write_json_line(
    file_object: BinaryIO,
    record: Mapping[str, object],
) -> None:
    """
    함수 이름: _write_json_line()
    기능: mapping 하나를 canonical UTF-8 JSON line으로 쓰고 즉시 fsync한다.
    인자: file_object -> binary append file, record -> secret-free JSON object
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    # 한 record를 canonical JSON line으로 만든 뒤 partial write 여부까지 확인한다.
    encoded_record = (
        json.dumps(
            record,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    written_length = file_object.write(encoded_record)
    if written_length != len(encoded_record):
        raise OSError("soak metric append was incomplete")
    file_object.flush()
    os.fsync(file_object.fileno())  # crash 뒤에도 마지막 완결 metric까지만 report 근거로 사용한다.


def _write_json_object(output_path: Path, record: Mapping[str, object]) -> None:
    """
    함수 이름: _write_json_object()
    기능: 기존 evidence를 덮어쓰지 않고 canonical JSON object를 새 file에 fsync한다.
    인자: output_path -> 새 report 경로, record -> secret-free JSON object
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    # Report를 canonical JSON 한 줄로 만들고 기존 evidence를 덮지 않는 exclusive create를 쓴다.
    encoded_record = (
        json.dumps(
            record,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    with output_path.open("xb") as output_file:
        written_length = output_file.write(encoded_record)
        if written_length != len(encoded_record):
            raise OSError("soak report write was incomplete")
        output_file.flush()
        os.fsync(output_file.fileno())


def _prepare_output_directory(output_directory: Path) -> None:
    """
    함수 이름: _prepare_output_directory()
    기능: symlink root와 기존 evidence overwrite를 거부하고 soak output directory를 준비한다.
    인자: output_directory -> metric/report를 생성할 명시 directory
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    # Output root의 type과 symlink 상태를 확인한 뒤 필요한 directory만 생성한다.
    if not isinstance(output_directory, Path):
        raise TypeError("output_directory must be a Path")
    if output_directory.is_symlink():
        raise SoakConfigurationError("output directory symlink is not allowed")
    output_directory.mkdir(parents=True, exist_ok=True)
    if not output_directory.is_dir() or output_directory.is_symlink():
        raise SoakConfigurationError("output directory is unavailable")

    # 두 canonical evidence 이름 중 하나라도 이미 있으면 새 run이 과거 근거를 덮지 못하게 한다.
    for evidence_name in (METRICS_FILE_NAME, REPORT_FILE_NAME):
        if (output_directory / evidence_name).exists():
            raise SoakConfigurationError("existing soak evidence cannot be overwritten")


def run_soak(
    configuration: SoakConfiguration,
    *,
    application_metric_provider: Callable[[], Mapping[str, object] | None] | None = None,
    wall_clock: Callable[[], datetime] = _utc_now,
    monotonic_clock: Callable[[], float] = monotonic,
    sleeper: Callable[[float], object] = sleep,
    sample_limit: int | None = None,
) -> dict[str, object]:
    """
    함수 이름: run_soak()
    기능: bounded interval로 metric JSONL을 fsync하고 종료 시 과장 없는 summary report를 기록한다.
    인자: configuration -> 검증된 no-order soak 설정
        application_metric_provider -> secret-free app snapshot provider 또는 None
        wall_clock -> UTC evidence clock
        monotonic_clock -> duration 판정 clock
        sleeper -> sample interval waiter
        sample_limit -> development/test에서 조기 종료할 양수 sample cap 또는 None
    반환값: 기록한 report object
    작성 날짜: 2026/08/24
    """
    # Configuration, provider, clock과 test seam을 evidence file 생성 전에 모두 검증한다.
    if not isinstance(configuration, SoakConfiguration):
        raise TypeError("configuration must be a SoakConfiguration")
    if configuration.output_directory is None:
        raise SoakConfigurationError("run mode requires an output directory")
    if application_metric_provider is not None and not callable(
        application_metric_provider
    ):
        raise TypeError("application_metric_provider must be callable or None")
    for callable_name, callable_value in (
        ("wall_clock", wall_clock),
        ("monotonic_clock", monotonic_clock),
        ("sleeper", sleeper),
    ):
        if not callable(callable_value):
            raise TypeError(f"{callable_name} must be callable")
    if sample_limit is not None and (
        type(sample_limit) is not int or sample_limit <= 0
    ):
        raise ValueError("sample_limit must be a positive integer or None")

    # 시작 시각과 누적 상태를 파일 open 전에 고정해 모든 metric이 같은 run을 공유하게 한다.
    _prepare_output_directory(configuration.output_directory)
    metrics_path = configuration.output_directory / METRICS_FILE_NAME
    report_path = configuration.output_directory / REPORT_FILE_NAME
    started_at = wall_clock()
    started_monotonic = monotonic_clock()
    sample_count = 0
    missing_application_metrics = False
    interrupted = False

    try:
        with metrics_path.open("xb") as metrics_file:
            while True:
                # 한 monotonic 관찰값으로 elapsed time과 app metric record를 순서대로 구성한다.
                current_monotonic = monotonic_clock()
                elapsed_seconds = max(0, int(current_monotonic - started_monotonic))
                application_metrics = (
                    None
                    if application_metric_provider is None
                    else application_metric_provider()
                )
                normalized_application = _normalize_application_metrics(
                    application_metrics
                )
                if any(value is None for value in normalized_application.values()):
                    missing_application_metrics = True
                sample_count += 1
                metric_record = build_metric_record(
                    configuration,
                    sequence=sample_count,
                    observed_at=wall_clock(),
                    elapsed_seconds=elapsed_seconds,
                    application_metrics=normalized_application,
                )
                _write_json_line(metrics_file, metric_record)

                # Development sample cap과 실제 requested duration 중 먼저 도달한 경계에서 종료한다.
                if (
                    (sample_limit is not None and sample_count >= sample_limit)
                    or elapsed_seconds >= configuration.duration_seconds
                ):
                    break
                remaining_seconds = configuration.duration_seconds - elapsed_seconds
                sleeper(min(configuration.sample_interval_seconds, remaining_seconds))
    except KeyboardInterrupt:
        interrupted = True  # Operator 중단도 완결 metric과 GAP report를 남기고 정식 성공으로 숨기지 않는다.

    # 마지막 monotonic duration으로 과장 없는 GAP report를 만들고 metric과 별도 fsync한다.
    observed_duration_seconds = max(
        0,
        int(monotonic_clock() - started_monotonic),
    )
    report = build_soak_report(
        configuration,
        started_at=started_at,
        ended_at=wall_clock(),
        observed_duration_seconds=observed_duration_seconds,
        sample_count=sample_count,
        missing_application_metrics=missing_application_metrics,
        interrupted=interrupted,
    )
    _write_json_object(report_path, report)
    return report


def parse_arguments(argument_values: Sequence[str] | None = None) -> argparse.Namespace:
    """
    함수 이름: parse_arguments()
    기능: no-order default와 별도 Testnet/order opt-in, duration과 output 인자를 읽는다.
    인자: argument_values -> 명시 CLI argument 또는 실제 argv를 뜻하는 None
    반환값: argparse Namespace
    작성 날짜: 2026/08/24
    """
    # Testnet과 주문은 별도 flag로 분리하고 기본 mode는 local no-order로 유지한다.
    parser = argparse.ArgumentParser(
        description="Record secret-free Phase 13 soak metrics without enabling orders by default.",
    )
    parser.add_argument("--testnet", action="store_true")
    parser.add_argument("--enable-testnet-orders", action="store_true")
    parser.add_argument("--max-order-mutations", type=int)
    parser.add_argument("--duration-hours", default=str(DEFAULT_SOAK_HOURS))
    parser.add_argument(
        "--sample-interval-seconds",
        type=int,
        default=DEFAULT_SAMPLE_INTERVAL_SECONDS,
    )
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--single-sample",
        action="store_true",
        help="개발 schema 확인용 metric 한 건만 기록하고 GAP report를 만든다.",
    )
    arguments = parser.parse_args(argument_values)

    # Validation-only와 evidence run의 경로·sample 조합을 parser 오류로 명확히 분리한다.
    if not arguments.validate_only and arguments.output_directory is None:
        parser.error("run mode requires --output-directory")
    if arguments.validate_only and arguments.single_sample:
        parser.error("--validate-only and --single-sample cannot be combined")
    return arguments


def main(argument_values: Sequence[str] | None = None) -> int:
    """
    함수 이름: main()
    기능: configuration을 검증하고 요청 시 local metric/report를 생성한다.
    인자: argument_values -> 명시 CLI argument 또는 실제 argv를 뜻하는 None
    반환값: readiness PASS 0, evidence GAP 1, fail-closed configuration/evidence 오류 2
    작성 날짜: 2026/08/24
    """
    # CLI와 hostile inherited environment를 한 configuration 경계에서 검증한다.
    arguments = parse_arguments(argument_values)
    try:
        configuration = build_soak_configuration(
            testnet_requested=arguments.testnet,
            testnet_orders_requested=arguments.enable_testnet_orders,
            duration_hours_text=arguments.duration_hours,
            sample_interval_seconds=arguments.sample_interval_seconds,
            output_directory=arguments.output_directory,
            max_order_mutations=arguments.max_order_mutations,
            environment=os.environ,
        )
        if arguments.validate_only:
            print(
                "phase13-soak: PASS: configuration validated; "
                f"mode={configuration.mode.value}; orders=disabled."
            )
            return 0

        report = run_soak(
            configuration,
            sample_limit=1 if arguments.single_sample else None,
        )
    except (OSError, SoakConfigurationError, ValueError):
        # Credential, policy 숫자와 output raw path를 CLI error에 반사하지 않는다.
        print("phase13-soak: ERROR: configuration or evidence rejected.", file=sys.stderr)
        return 2

    # Evidence file 생성 성공과 live-readiness 판정을 분리해 GAP report가 성공으로 오인되지 않게 한다.
    readiness_status = report["readiness_status"]
    print(
        f"phase13-soak: {readiness_status}: evidence written; "
        f"report_class={report['report_class']}; "
        f"readiness_status={readiness_status}."
    )
    return 0 if readiness_status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
