"""Tauri가 상속한 anonymous FD만으로 production Testnet sidecar를 조립한다."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re

from binance_auto_trader.bootstrap.application import ApplicationRuntime
from binance_auto_trader.bootstrap.testnet import (
    BINANCE_RUN_TESTNET_ENV,
    BINANCE_RUN_TESTNET_ORDERS_ENV,
    BINANCE_TESTNET_API_KEY_ENV,
    BINANCE_TESTNET_API_SECRET_ENV,
    create_testnet_application_runtime,
)
SESSION_TOKEN_FD = 3
READY_DESCRIPTOR_FD = 4
STOP_SIGNAL_FD = 5
SIDECAR_CONFIGURATION_FD = 6
MAX_SIDECAR_CONFIGURATION_BYTES = 16 * 1024
_MAX_ORIGIN_LENGTH = 256
_MAX_HISTORY_PATH_LENGTH = 4_096
_MAX_CREDENTIAL_LENGTH = 1_024
_ALLOWED_ORIGIN_PATTERN = re.compile(
    r"^(?:tauri://[A-Za-z0-9.-]+|https?://(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?)$"
)
_CONFIGURATION_FIELDS = frozenset(
    {
        "schema_version",
        "allowed_origin",
        "history_path",
        "api_key",
        "api_secret",
        "allow_testnet_orders",
        "max_notional",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class SidecarConfiguration:
    """
    클래스 이름: SidecarConfiguration
    기능: renderer 밖 FD에서 받은 exact Origin, durable 경로와 read-only Testnet credential을 보존한다.
    작성 날짜: 2026/08/24
    """

    schema_version: int
    allowed_origin: str
    history_path: Path
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    allow_testnet_orders: bool = False
    max_notional: None = None

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: sidecar 설정이 현재 schema와 read-only Testnet 권한만 표현하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Schema와 두 권한 필드는 bool의 int 상속이나 truthy 값을 허용하지 않는다.
        if type(self.schema_version) is not int:
            raise TypeError("schema_version must be an int")
        if self.schema_version <= 0:
            raise ValueError("schema_version must be positive")
        if self.allow_testnet_orders is not False:
            raise ValueError("packaged sidecar must disable testnet orders")
        if self.max_notional is not None:
            raise ValueError("read-only sidecar max_notional must be null")

        # Origin과 path는 보정하지 않고 Tauri가 보낸 canonical identity를 그대로 요구한다.
        if (
            not isinstance(self.allowed_origin, str)
            or not self.allowed_origin
            or self.allowed_origin != self.allowed_origin.strip()
            or len(self.allowed_origin) > _MAX_ORIGIN_LENGTH
            or _ALLOWED_ORIGIN_PATTERN.fullmatch(self.allowed_origin) is None
        ):
            raise ValueError("allowed_origin must be a bounded canonical string")
        if not isinstance(self.history_path, Path):
            raise TypeError("history_path must be a Path")
        if not self.history_path.is_absolute() or ".." in self.history_path.parts:
            raise ValueError("history_path must be an absolute canonical path")
        if self.history_path.name in {"", ".", ".."}:
            raise ValueError("history_path must identify a file")

        # Credential은 client에 전달할 원문 identity만 허용하고 값이나 길이를 repr에 남기지 않는다.
        for field_name in ("api_key", "api_secret"):
            credential = getattr(self, field_name)
            if (
                not isinstance(credential, str)
                or not credential
                or credential != credential.strip()
                or len(credential) > _MAX_CREDENTIAL_LENGTH
                or any(
                    ord(character) < 33 or ord(character) == 127
                    for character in credential
                )
            ):
                raise ValueError(
                    f"{field_name} must be a bounded non-empty credential"
                )

    def __repr__(self) -> str:
        """
        함수 이름: __repr__()
        기능: credential 값과 길이를 노출하지 않는 고정 redaction 표현을 반환한다.
        인자: 없음
        반환값: secret-safe configuration 문자열
        작성 날짜: 2026/08/24
        """
        return (
            "SidecarConfiguration("
            f"schema_version={self.schema_version!r}, "
            f"allowed_origin={self.allowed_origin!r}, "
            f"history_path={self.history_path!r}, "
            "api_key=<redacted>, api_secret=<redacted>, "
            "allow_testnet_orders=False, max_notional=None)"
        )  # Credential의 실제 문자열과 길이는 어떤 진단 표현에도 포함하지 않는다.

    def to_testnet_environment(self) -> Mapping[str, str]:
        """
        함수 이름: to_testnet_environment()
        기능: 기존 Testnet bootstrap에 직접 주입할 read-only 설정 mapping을 만든다.
        인자: 없음
        반환값: os.environ을 읽지 않는 최소 Testnet configuration mapping
        작성 날짜: 2026/08/24
        """
        # Order flag는 명시적 0으로 고정하고 max notional key 자체를 만들지 않는다.
        return {
            BINANCE_RUN_TESTNET_ENV: "1",
            BINANCE_TESTNET_API_KEY_ENV: self.api_key,
            BINANCE_TESTNET_API_SECRET_ENV: self.api_secret,
            BINANCE_RUN_TESTNET_ORDERS_ENV: "0",
        }


def _reject_duplicate_object_pairs(
    object_pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """
    함수 이름: _reject_duplicate_object_pairs()
    기능: JSON object의 duplicate key가 마지막 값으로 덮이는 parsing을 거부한다.
    인자: object_pairs -> decoder가 원본 순서로 전달한 key-value pair
    반환값: key가 유일한 dictionary
    작성 날짜: 2026/08/24
    """
    parsed_object: dict[str, object] = {}
    for key, value in object_pairs:
        if key in parsed_object:
            raise ValueError("sidecar configuration contains a duplicate field")
        parsed_object[key] = value  # 유일성이 확인된 pair만 strict object에 추가한다.

    return parsed_object


def _reject_non_finite_json_constant(constant_text: str) -> object:
    """
    함수 이름: _reject_non_finite_json_constant()
    기능: 표준 JSON이 아닌 NaN과 Infinity 상수를 sidecar 설정에서 거부한다.
    인자: constant_text -> decoder가 발견한 비표준 숫자 token
    반환값: 정상 반환 없이 ValueError 발생
    작성 날짜: 2026/08/24
    """
    raise ValueError(
        "sidecar configuration contains a non-finite number"
    )  # 원래 token은 오류에 포함하지 않아 payload 반사를 피한다.


def _read_bounded_fd_payload(file_descriptor: int) -> bytes:
    """
    함수 이름: _read_bounded_fd_payload()
    기능: inherited configuration FD를 EOF까지 제한 크기로 읽고 항상 닫는다.
    인자: file_descriptor -> configuration 전용 anonymous pipe read FD
    반환값: 상한 안의 non-empty JSON bytes
    작성 날짜: 2026/08/24
    """
    if type(file_descriptor) is not int or file_descriptor < 0:
        raise ValueError("file_descriptor must be a non-negative int")

    payload_chunks: list[bytes] = []
    payload_size = 0
    try:
        # 작은 고정 chunk로 읽어 한 번의 oversized allocation도 설정 상한을 넘지 않게 한다.
        while True:
            payload_chunk = os.read(file_descriptor, 4_096)
            if not payload_chunk:
                break
            payload_size += len(payload_chunk)
            if payload_size > MAX_SIDECAR_CONFIGURATION_BYTES:
                raise ValueError("sidecar configuration payload is too large")
            payload_chunks.append(payload_chunk)
    finally:
        os.close(file_descriptor)

    payload = b"".join(payload_chunks)
    if not payload:
        raise ValueError("sidecar configuration payload is empty")

    return payload


def read_sidecar_configuration_from_fd(
    configuration_fd: int,
    *,
    expected_schema_version: int,
) -> SidecarConfiguration:
    """
    함수 이름: read_sidecar_configuration_from_fd()
    기능: FD JSON을 duplicate·필드·타입·권한 상한으로 엄격 검증해 설정 객체를 만든다.
    인자: configuration_fd -> Tauri가 credential과 nonsecret 설정을 기록한 inherited FD
        expected_schema_version -> transport composition root가 주입한 현재 schema version
    반환값: read-only Testnet SidecarConfiguration
    작성 날짜: 2026/08/24
    """
    if type(expected_schema_version) is not int or expected_schema_version <= 0:
        raise ValueError("expected_schema_version must be a positive int")

    payload = _read_bounded_fd_payload(configuration_fd)
    try:
        payload_text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("sidecar configuration must use UTF-8") from error

    # 표준 JSON, unique key와 top-level object를 한 decoder 경계에서 강제한다.
    try:
        parsed_value = json.loads(
            payload_text,
            object_pairs_hook=_reject_duplicate_object_pairs,
            parse_constant=_reject_non_finite_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("sidecar configuration is invalid JSON") from error
    if not isinstance(parsed_value, dict):
        raise ValueError("sidecar configuration must be a JSON object")

    actual_fields = frozenset(parsed_value)
    if actual_fields != _CONFIGURATION_FIELDS:
        raise ValueError("sidecar configuration fields do not match the contract")
    if parsed_value["schema_version"] != expected_schema_version:
        raise ValueError("sidecar schema_version is not supported")

    history_path_value = parsed_value["history_path"]
    if (
        not isinstance(history_path_value, str)
        or not history_path_value
        or history_path_value != history_path_value.strip()
        or "\x00" in history_path_value
        or len(history_path_value) > _MAX_HISTORY_PATH_LENGTH
    ):
        raise ValueError("history_path must be a bounded canonical string")

    normalized_history_path = Path(history_path_value)
    if str(normalized_history_path) != history_path_value:
        raise ValueError("history_path must not require lexical normalization")

    # Dataclass가 나머지 exact scalar types와 read-only 권한 조합을 최종 검증한다.
    return SidecarConfiguration(
        schema_version=parsed_value["schema_version"],
        allowed_origin=parsed_value["allowed_origin"],
        history_path=normalized_history_path,
        api_key=parsed_value["api_key"],
        api_secret=parsed_value["api_secret"],
        allow_testnet_orders=parsed_value["allow_testnet_orders"],
        max_notional=parsed_value["max_notional"],
    )


def _create_sidecar_runtime_factory(
    configuration: SidecarConfiguration,
) -> Callable[
    [Callable[[object], object], Callable[[object, object], object]],
    ApplicationRuntime,
]:
    """
    함수 이름: _create_sidecar_runtime_factory()
    기능: secure FD 설정을 기존 Testnet runtime factory의 주입 mapping으로 닫아 보존한다.
    인자: configuration -> 검증된 read-only sidecar 설정
    반환값: transport observer 두 개를 받는 application runtime factory
    작성 날짜: 2026/08/24
    """
    if not isinstance(configuration, SidecarConfiguration):
        raise TypeError("configuration must be a SidecarConfiguration")

    def runtime_factory(
        account_update_observer: Callable[[object], object],
        trade_history_update_observer: Callable[[object, object], object],
    ) -> ApplicationRuntime:
        """
        함수 이름: runtime_factory()
        기능: shared transport observer와 FD 설정으로 고정 Testnet application runtime을 조립한다.
        인자: account_update_observer -> Account publication 뒤 호출할 transport observer
            trade_history_update_observer -> durable Trade publication 뒤 호출할 transport observer
        반환값: read-only Testnet ApplicationRuntime
        작성 날짜: 2026/08/24
        """
        # Mapping은 os.environ에 게시하지 않고 runtime 생성 호출의 명시 인자로만 전달한다.
        return create_testnet_application_runtime(
            account_update_observer,
            trade_history_update_observer,
            history_path=configuration.history_path,
            environment=configuration.to_testnet_environment(),
        )

    return runtime_factory
