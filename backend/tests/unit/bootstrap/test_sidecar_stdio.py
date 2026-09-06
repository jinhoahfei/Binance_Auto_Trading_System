"""Platform-neutral stdio frame과 strict bootstrap의 secret-safe 경계를 검증한다."""

from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import secrets
import struct
import unittest

from binance_auto_trader.sidecar_stdio import (
    MAX_STDIO_BOOTSTRAP_FRAME_BYTES,
    MAX_STDIO_CONFIGURATION_BYTES,
    read_stdio_bootstrap,
)
from binance_auto_trader.transport import SCHEMA_VERSION
from binance_auto_trader.transport.framing import (
    MAX_SIDECAR_FRAME_BYTES,
    read_json_frame,
    write_json_frame,
)


class FragmentedStream(BytesIO):
    """
    클래스 이름: FragmentedStream
    기능: 실제 anonymous pipe에서 허용되는 partial read와 write를 재현한다.
    작성 날짜: 2026/09/06
    """

    def read(self, size: int = -1) -> bytes:
        """
        함수 이름: read()
        기능: 한 번에 최대 두 byte만 반환해 prefix와 UTF-8 body를 분할한다.
        인자: size -> 호출자가 요청한 byte 수
        반환값: 최대 두 byte의 fragment
        작성 날짜: 2026/09/06
        """
        return super().read(min(size, 2))  # Prefix부터 body까지 같은 fragmentation을 적용한다.

    def write(self, payload: bytes) -> int:
        """
        함수 이름: write()
        기능: 한 write에서 최대 세 byte만 받아 partial-write 처리를 검증한다.
        인자: payload -> sender가 기록하려는 bytes
        반환값: 실제 수용한 byte 수
        작성 날짜: 2026/09/06
        """
        return super().write(payload[:3])  # Caller가 나머지 bytes를 잃지 않고 반복해야 한다.


class SidecarStdioTests(unittest.TestCase):
    """
    클래스 이름: SidecarStdioTests
    기능: frame 상한, strict JSON과 read-only bootstrap 공통 계약을 고정한다.
    작성 날짜: 2026/09/06
    """

    def _bootstrap_payload(self) -> dict[str, object]:
        """
        함수 이름: _bootstrap_payload()
        기능: 외부 credential 없이 유효한 BOOTSTRAP test object를 만든다.
        인자: 없음
        반환값: 정확한 token과 configuration field를 가진 object
        작성 날짜: 2026/09/06
        """
        # Host의 absolute path 문법을 써 Windows/macOS 모두 같은 contract test를 실행한다.
        return {
            "type": "BOOTSTRAP",
            "token": secrets.token_urlsafe(32),
            "configuration": {
                "schema_version": SCHEMA_VERSION,
                "allowed_origin": "http://localhost:5173",
                "history_path": str(Path.cwd() / "history.jsonl"),
                "api_key": "fixture-key",
                "api_secret": "fixture-secret",
                "allow_testnet_orders": False,
                "max_notional": None,
            },
        }

    def test_fragmented_unicode_and_consecutive_frames_round_trip(self) -> None:
        """
        함수 이름: test_fragmented_unicode_and_consecutive_frames_round_trip()
        기능: partial prefix/body/write와 붙어 있는 두 frame을 정확히 복원한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        # Unicode와 escaped newline은 text-line 경계에 의존하지 않아야 한다.
        output_stream = FragmentedStream()
        first_payload = {"message": "한국어\nframe"}
        write_json_frame(output_stream, first_payload)
        write_json_frame(output_stream, {"type": "CLOSED_ACK"})
        output_stream.seek(0)

        self.assertEqual(read_json_frame(output_stream), first_payload)
        self.assertEqual(read_json_frame(output_stream), {"type": "CLOSED_ACK"})
        self.assertIsNone(read_json_frame(output_stream))  # 정상 EOF는 frame 경계에서만 허용한다.

    def test_invalid_prefix_rejected_before_body_is_read(self) -> None:
        """
        함수 이름: test_invalid_prefix_rejected_before_body_is_read()
        기능: 0과 oversized 길이는 body allocation/read 이전에 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        for payload_length in (0, MAX_SIDECAR_FRAME_BYTES + 1, 0xFFFFFFFF):
            with self.subTest(payload_length=payload_length):
                stream = BytesIO(struct.pack(">I", payload_length) + b"unread-body")
                with self.assertRaises(ValueError):
                    read_json_frame(stream)
                self.assertEqual(stream.tell(), 4)  # 큰 길이만으로 payload를 먼저 읽지 않는다.

    def test_truncated_prefix_and_body_are_not_clean_eof(self) -> None:
        """
        함수 이름: test_truncated_prefix_and_body_are_not_clean_eof()
        기능: frame 중간 EOF를 부모의 정상 메시지 완료로 오인하지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        # Prefix와 body 어느 쪽이 끊겨도 lifecycle에는 channel loss로 전달해야 한다.
        for payload in (b"\x00", b"\x00\x00\x00", struct.pack(">I", 4) + b"{}"):
            with self.subTest(payload_length=len(payload)):
                with self.assertRaises(ValueError):
                    read_json_frame(BytesIO(payload))

    def test_invalid_json_and_nested_duplicates_are_secret_safe(self) -> None:
        """
        함수 이름: test_invalid_json_and_nested_duplicates_are_secret_safe()
        기능: malformed JSON, UTF-8, duplicate, NaN과 non-object를 raw payload 없이 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        invalid_payloads = (
            b'{"secret":"fixture-secret",}',
            b'{"configuration":{"token":1,"token":2}}',
            b'{"number":NaN}',
            b'{"number":Infinity}',
            b'[]',
            b'\xff',
        )
        for payload in invalid_payloads:
            with self.subTest(payload_length=len(payload)):
                with self.assertRaises(ValueError) as caught:
                    read_json_frame(BytesIO(struct.pack(">I", len(payload)) + payload))
                self.assertNotIn("fixture-secret", str(caught.exception))

    def test_bootstrap_reuses_exact_read_only_configuration_contract(self) -> None:
        """
        함수 이름: test_bootstrap_reuses_exact_read_only_configuration_contract()
        기능: 최초 token/config frame 뒤 ACK를 소비하지 않고 strict read-only 설정을 반환한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        payload = self._bootstrap_payload()
        stream = BytesIO()
        write_json_frame(stream, payload)
        write_json_frame(stream, {"type": "CLOSED_ACK"})
        stream.seek(0)
        token, configuration = read_stdio_bootstrap(stream)

        # Bootstrap과 control이 같은 stream을 공유하되 메시지 순서를 유지한다.
        self.assertEqual(token, payload["token"])
        self.assertIs(configuration.allow_testnet_orders, False)
        self.assertNotIn("fixture-secret", repr(configuration))
        self.assertEqual(read_json_frame(stream), {"type": "CLOSED_ACK"})

    def test_bootstrap_rejects_extra_fields_bad_token_and_order_permission(self) -> None:
        """
        함수 이름: test_bootstrap_rejects_extra_fields_bad_token_and_order_permission()
        기능: bootstrap envelope와 내부 configuration 모두 permissive fallback 없이 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        payload = self._bootstrap_payload()
        privileged_payload = self._bootstrap_payload()
        privileged_payload["configuration"]["allow_testnet_orders"] = True
        invalid_payloads = (
            dict(payload, type="CLOSED_ACK"),
            dict(payload, extra=True),
            dict(payload, token="bad-token"),
            dict(payload, configuration=[]),
            privileged_payload,
        )
        for invalid_payload in invalid_payloads:
            stream = BytesIO()
            write_json_frame(stream, invalid_payload)
            stream.seek(0)
            with self.assertRaises((ValueError, TypeError)):
                read_stdio_bootstrap(stream)

    def test_bootstrap_specific_limit_rejects_oversized_frame(self) -> None:
        """
        함수 이름: test_bootstrap_specific_limit_rejects_oversized_frame()
        기능: 일반 1MiB frame보다 작은 16KiB bootstrap 상한을 강제한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        stream = BytesIO(struct.pack(">I", 16 * 1024 + 1))
        with self.assertRaises(ValueError):
            read_stdio_bootstrap(stream)  # Body가 없어도 prefix만으로 상한 위반을 알아야 한다.

    def test_bootstrap_outer_limit_is_independent_of_inner_configuration(self) -> None:
        """
        함수 이름: test_bootstrap_outer_limit_is_independent_of_inner_configuration()
        기능: 작은 configuration을 포함한 16 KiB frame은 허용하고 외부 상한과 내부 상한을 분리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        # JSON의 허용 whitespace로 외부 frame만 키워 내부 설정의 byte 수를 바꾸지 않는다.
        payload = json.dumps(self._bootstrap_payload()).encode("utf-8")
        payload += b" " * (MAX_STDIO_BOOTSTRAP_FRAME_BYTES - len(payload))
        stream = BytesIO(struct.pack(">I", len(payload)) + payload)
        _, configuration = read_stdio_bootstrap(stream)

        self.assertIs(configuration.allow_testnet_orders, False)
        self.assertEqual(stream.tell(), MAX_STDIO_BOOTSTRAP_FRAME_BYTES + 4)

    def test_inner_configuration_enforces_exact_utf8_byte_boundary(self) -> None:
        """
        함수 이름: test_inner_configuration_enforces_exact_utf8_byte_boundary()
        기능: 내부 UTF-8 설정 8 KiB는 허용하고 외부 16 KiB 이내라도 한 byte 초과를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        for extra_bytes in (0, 1):
            bootstrap_payload = self._bootstrap_payload()
            configuration = bootstrap_payload["configuration"]
            original_size = len(json.dumps(
                configuration, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8"))

            # Unicode path는 lexical 길이 상한 이내에서 UTF-8 byte 상한을 정확히 재현한다.
            filler_size = MAX_STDIO_CONFIGURATION_BYTES + extra_bytes - original_size
            configuration["history_path"] += "가" * (filler_size // 3) + "x" * (filler_size % 3)
            payload = json.dumps(
                bootstrap_payload, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")
            stream = BytesIO(struct.pack(">I", len(payload)) + payload)
            with self.subTest(extra_bytes=extra_bytes):
                if extra_bytes:
                    with self.assertRaisesRegex(ValueError, "configuration payload is too large"):
                        read_stdio_bootstrap(stream)
                else:
                    _, parsed_configuration = read_stdio_bootstrap(stream)
                    self.assertIs(parsed_configuration.allow_testnet_orders, False)

    def test_windows_credentials_match_native_ascii_blob_boundary(self) -> None:
        """
        함수 이름: test_windows_credentials_match_native_ascii_blob_boundary()
        기능: Windows native와 같은 512 ASCII bytes만 허용하고 Unicode와 513 bytes를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/06
        """
        for credential in ("x" * 512, "x" * 513, "é"):
            payload = self._bootstrap_payload()
            payload["configuration"]["api_key"] = credential
            stream = BytesIO()
            write_json_frame(stream, payload)
            stream.seek(0)
            if credential == "x" * 512:
                _, configuration = read_stdio_bootstrap(stream)
                self.assertEqual(len(configuration.api_key), 512)
            else:
                with self.assertRaisesRegex(ValueError, "credential does not match the contract"):
                    read_stdio_bootstrap(stream)


if __name__ == "__main__":
    unittest.main()
