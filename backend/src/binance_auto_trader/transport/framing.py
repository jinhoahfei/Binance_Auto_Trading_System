"""Windows anonymous stdio pipe의 bounded big-endian JSON frame codec을 제공한다."""

from __future__ import annotations

import json
import struct
from typing import BinaryIO


MAX_SIDECAR_FRAME_BYTES = 1024 * 1024


def _reject_duplicate_fields(object_pairs: list[tuple[str, object]]) -> dict[str, object]:
    """
    함수 이름: _reject_duplicate_fields()
    기능: 중첩 object까지 duplicate key overwrite 없이 strict JSON으로 변환한다.
    인자: object_pairs -> JSON decoder가 전달한 원래 key-value 순서
    반환값: 유일한 key의 object
    작성 날짜: 2026/09/06
    """
    # Payload의 credential이나 key를 오류에 반사하지 않는다.
    parsed_object: dict[str, object] = {}
    for field_name, field_value in object_pairs:
        if field_name in parsed_object:
            raise ValueError("sidecar frame contains duplicate fields")
        parsed_object[field_name] = field_value  # 유일성이 확인된 field만 추가한다.
    return parsed_object


def _reject_non_finite_constant(constant_text: str) -> object:
    """
    함수 이름: _reject_non_finite_constant()
    기능: JSON 표준 밖 NaN과 Infinity를 거부한다.
    인자: constant_text -> decoder의 비표준 numeric token
    반환값: 반환 없이 ValueError 발생
    작성 날짜: 2026/09/06
    """
    raise ValueError("sidecar frame contains a non-finite number")  # 원래 payload는 출력하지 않는다.


def _read_exact_bytes(stream: BinaryIO, byte_count: int, *, allow_eof: bool) -> bytes | None:
    """
    함수 이름: _read_exact_bytes()
    기능: pipe의 partial read를 이어 붙이고 frame 중간 EOF를 거부한다.
    인자: stream -> 단일 reader가 소유한 binary input
        byte_count -> 이미 상한 검증된 필요한 byte 수
        allow_eof -> frame 경계의 정상 EOF를 None으로 반환할지 여부
    반환값: 정확한 byte 수 또는 frame 경계 EOF의 None
    작성 날짜: 2026/09/06
    """
    # 요청한 frame 길이 이상의 메모리 allocation과 다음 frame read-ahead를 피한다.
    payload = bytearray()
    while len(payload) < byte_count:
        chunk = stream.read(byte_count - len(payload))
        if not chunk:
            if allow_eof and not payload:
                return None
            raise ValueError("sidecar frame was truncated")
        payload.extend(chunk)  # 임의 pipe fragmentation을 하나의 frame으로 복원한다.
    return bytes(payload)


def read_json_frame(
    stream: BinaryIO,
    *,
    maximum_bytes: int = MAX_SIDECAR_FRAME_BYTES,
) -> dict[str, object] | None:
    """
    함수 이름: read_json_frame()
    기능: 4-byte unsigned big-endian 길이와 bounded UTF-8 JSON object 한 개를 읽는다.
    인자: stream -> anonymous pipe binary reader
        maximum_bytes -> protocol 전역 상한 이내의 메시지별 byte 상한
    반환값: strict JSON object 또는 frame 경계의 EOF에 None
    작성 날짜: 2026/09/06
    """
    # 길이 prefix만 읽은 시점에 상한을 검사해 공격적인 크기만큼 body를 읽지 않는다.
    if type(maximum_bytes) is not int or not 0 < maximum_bytes <= MAX_SIDECAR_FRAME_BYTES:
        raise ValueError("sidecar frame limit is invalid")
    prefix = _read_exact_bytes(stream, 4, allow_eof=True)
    if prefix is None:
        return None
    payload_size = struct.unpack(">I", prefix)[0]
    if not 0 < payload_size <= maximum_bytes:
        raise ValueError("sidecar frame length is invalid")
    payload = _read_exact_bytes(stream, payload_size, allow_eof=False)

    # Decoder exception에도 raw bytes를 붙이지 않아 bootstrap secret이 진단에 섞이지 않는다.
    try:
        parsed_value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_fields,
            parse_constant=_reject_non_finite_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("sidecar frame is invalid JSON") from None
    if not isinstance(parsed_value, dict):
        raise ValueError("sidecar frame must be a JSON object")
    return parsed_value


def write_json_frame(
    stream: BinaryIO,
    payload: dict[str, object],
    *,
    maximum_bytes: int = MAX_SIDECAR_FRAME_BYTES,
) -> None:
    """
    함수 이름: write_json_frame()
    기능: bounded JSON frame을 partial write까지 완료한 뒤 명시적으로 flush한다.
    인자: stream -> 단일 writer가 소유한 anonymous pipe
        payload -> 직렬화할 JSON object
        maximum_bytes -> protocol 전역 상한 이내의 메시지별 byte 상한
    반환값: 없음
    작성 날짜: 2026/09/06
    """
    # 직렬화 단계에서 비표준 numeric 값과 잘못된 payload type을 먼저 거부한다.
    if not isinstance(payload, dict):
        raise ValueError("sidecar frame must be a JSON object")
    if type(maximum_bytes) is not int or not 0 < maximum_bytes <= MAX_SIDECAR_FRAME_BYTES:
        raise ValueError("sidecar frame limit is invalid")
    encoded_payload = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not 0 < len(encoded_payload) <= maximum_bytes:
        raise ValueError("sidecar frame length is invalid")

    # Binary CRT pipe는 개행이나 Ctrl-Z 변환 없이 prefix와 body를 한 순서로 보낸다.
    frame = struct.pack(">I", len(encoded_payload)) + encoded_payload
    frame_offset = 0
    while frame_offset < len(frame):
        written_count = stream.write(frame[frame_offset:])
        if written_count is None or written_count <= 0:
            raise OSError("sidecar frame write did not advance")
        frame_offset += written_count  # 부분 write도 framing 순서를 보존한다.
    stream.flush()
