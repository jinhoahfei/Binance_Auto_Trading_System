"""Windows sidecar bootstrap의 binary stdio와 strict configuration을 조립한다."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
import sys
from typing import BinaryIO

from binance_auto_trader.bootstrap.sidecar import (
    SidecarConfiguration,
    parse_sidecar_configuration_payload,
)
from binance_auto_trader.transport import SCHEMA_VERSION
from binance_auto_trader.transport.app import _validate_session_token
from binance_auto_trader.transport.framing import read_json_frame
from binance_auto_trader.transport.stdio_process import run_framed_transport_process


# Windows frame와 내부 설정은 native serializer의 서로 다른 byte 상한을 사용한다.
MAX_STDIO_BOOTSTRAP_FRAME_BYTES = 16 * 1024
MAX_STDIO_CONFIGURATION_BYTES = 8 * 1024
MAX_STDIO_CREDENTIAL_BYTES = 512


def read_stdio_bootstrap(input_stream: BinaryIO) -> tuple[str, SidecarConfiguration]:
    """
    함수 이름: read_stdio_bootstrap()
    기능: 최초 parent frame의 exact BOOTSTRAP, token과 read-only 설정을 검증한다.
    인자: input_stream -> anonymous binary stdin reader
    반환값: 메모리 안의 per-launch token과 strict SidecarConfiguration
    작성 날짜: 2026/09/06
    """
    # Frame decoder는 중첩 duplicate와 non-finite constant도 bootstrap 조립 전에 거부한다.
    bootstrap_frame = read_json_frame(input_stream, maximum_bytes=MAX_STDIO_BOOTSTRAP_FRAME_BYTES)
    if (
        bootstrap_frame is None
        or frozenset(bootstrap_frame) != {"type", "token", "configuration"}
        or bootstrap_frame["type"] != "BOOTSTRAP"
    ):
        raise ValueError("sidecar bootstrap fields do not match the contract")
    session_token = _validate_session_token(bootstrap_frame["token"])

    # serde_json과 같은 UTF-8 compact representation으로 내부 8 KiB byte 상한을 별도 강제한다.
    configuration_payload = json.dumps(
        bootstrap_frame["configuration"],
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(configuration_payload) > MAX_STDIO_CONFIGURATION_BYTES:
        raise ValueError("sidecar stdio configuration payload is too large")
    configuration = parse_sidecar_configuration_payload(
        configuration_payload,
        expected_schema_version=SCHEMA_VERSION,
    )

    # Windows credential blob은 native manager와 동일한 1~512 printable ASCII bytes만 허용한다.
    for credential in (configuration.api_key, configuration.api_secret):
        if len(credential) > MAX_STDIO_CREDENTIAL_BYTES or any(
            not 0x21 <= ord(character) <= 0x7E for character in credential
        ):
            raise ValueError("sidecar stdio credential does not match the contract")
    return session_token, configuration


def run_stdio_sidecar_process(runtime_factory_builder: Callable[[SidecarConfiguration], Callable]) -> None:
    """
    함수 이름: run_stdio_sidecar_process()
    기능: stdio를 protocol 전용으로 예약하고 Windows production bootstrap을 실행한다.
    인자: runtime_factory_builder -> 검증된 설정을 observer factory로 바꾸는 composition 함수
    반환값: post-CLOSED framed acknowledgement 뒤 없음
    작성 날짜: 2026/09/06
    """
    # Windows CRT text mode의 개행/Ctrl-Z 변환은 binary frame에 적용하지 않는다.
    if os.name == "nt":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

    # Protocol writer를 분리하고 이후 Python 일반 stdout 출력은 stderr로 보낸다.
    input_stream = sys.stdin.buffer
    previous_stdout = sys.stdout
    output_stream = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    sys.stdout = sys.stderr
    session_token = ""
    try:
        session_token, configuration = read_stdio_bootstrap(input_stream)
        configuration.history_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        runtime_factory = runtime_factory_builder(configuration)
        run_framed_transport_process(
            runtime_factory,
            session_token=session_token,
            input_stream=input_stream,
            output_stream=output_stream,
            allowed_origins=(configuration.allowed_origin,),
        )
    finally:
        output_stream.close()
        sys.stdout = previous_stdout
        session_token = ""  # Bootstrap owner도 정상 종료와 실패 때 token 참조를 제거한다.
