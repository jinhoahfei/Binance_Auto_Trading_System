"""Production sidecar의 fixed FD credential configuration 경계를 검증한다."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from binance_auto_trader.bootstrap.sidecar import (
    MAX_SIDECAR_CONFIGURATION_BYTES,
    READY_DESCRIPTOR_FD,
    SESSION_TOKEN_FD,
    SIDECAR_CONFIGURATION_FD,
    STOP_SIGNAL_FD,
    read_sidecar_configuration_from_fd,
)
from binance_auto_trader.transport import SCHEMA_VERSION


class SidecarConfigurationTests(unittest.TestCase):
    """
    클래스 이름: SidecarConfigurationTests
    기능: FD6 exact JSON, read-only 권한과 credential redaction을 검증한다.
    작성 날짜: 2026/08/24
    """

    def _read_configuration(self, payload: bytes) -> object:
        """
        함수 이름: _read_configuration()
        기능: anonymous pipe에 payload를 기록하고 production parser 결과를 반환한다.
        인자: payload -> configuration FD에 기록할 raw bytes
        반환값: parser가 만든 SidecarConfiguration
        작성 날짜: 2026/08/24
        """
        read_fd, write_fd = os.pipe()
        os.write(write_fd, payload)
        os.close(write_fd)  # EOF가 credential frame의 명시적 끝을 표시한다.

        return read_sidecar_configuration_from_fd(
            read_fd,
            expected_schema_version=SCHEMA_VERSION,
        )

    def _valid_payload(self, history_path: Path) -> dict[str, object]:
        """
        함수 이름: _valid_payload()
        기능: 각 strict validation test가 한 필드만 바꿀 canonical FD6 object를 만든다.
        인자: history_path -> absolute per-user history test 경로
        반환값: exact seven-field configuration dictionary
        작성 날짜: 2026/08/24
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "allowed_origin": "tauri://localhost",
            "history_path": str(history_path),
            "api_key": "testnet-api-key",
            "api_secret": "testnet-api-secret",
            "allow_testnet_orders": False,
            "max_notional": None,
        }

    def test_fixed_file_descriptor_contract_and_read_only_mapping(self) -> None:
        """
        함수 이름: test_fixed_file_descriptor_contract_and_read_only_mapping()
        기능: child FD가 3/4/5/6이고 parser가 주문 권한 없는 injected mapping만 만드는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        self.assertEqual(
            (
                SESSION_TOKEN_FD,
                READY_DESCRIPTOR_FD,
                STOP_SIGNAL_FD,
                SIDECAR_CONFIGURATION_FD,
            ),
            (3, 4, 5, 6),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            payload_object = self._valid_payload(
                Path(temporary_directory) / "history.jsonl"
            )
            encoded_payload = json.dumps(payload_object).encode("utf-8")
            configuration = self._read_configuration(encoded_payload)

        # Mapping은 명시적 Testnet opt-in과 read-only order flag만 credential과 함께 주입한다.
        environment = configuration.to_testnet_environment()
        self.assertEqual(environment["BINANCE_RUN_TESTNET"], "1")
        self.assertEqual(environment["BINANCE_RUN_TESTNET_ORDERS"], "0")
        self.assertNotIn("BINANCE_TESTNET_MAX_NOTIONAL", environment)
        self.assertEqual(configuration.allowed_origin, "tauri://localhost")

    def test_configuration_repr_redacts_both_credentials(self) -> None:
        """
        함수 이름: test_configuration_repr_redacts_both_credentials()
        기능: configuration repr이 API key와 secret 원문 및 길이를 노출하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # 실제 parser를 통과한 configuration을 만들어 dataclass 기본 repr 우회 여부까지 확인한다.
        with tempfile.TemporaryDirectory() as temporary_directory:
            payload_object = self._valid_payload(
                Path(temporary_directory) / "history.jsonl"
            )
            configuration = self._read_configuration(
                json.dumps(payload_object).encode("utf-8")
            )

        representation = repr(configuration)  # 검증 실패 메시지에 representation 자체를 출력하지 않는다.
        self.assertNotIn("testnet-api-key", representation)
        self.assertNotIn("testnet-api-secret", representation)
        self.assertEqual(representation.count("<redacted>"), 2)

    def test_parser_rejects_extra_duplicate_and_privilege_fields(self) -> None:
        """
        함수 이름: test_parser_rejects_extra_duplicate_and_privilege_fields()
        기능: exact key 위반, duplicate, schema 불일치와 order opt-in을 모두 fail closed하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            history_path = Path(temporary_directory) / "history.jsonl"
            valid_payload = self._valid_payload(history_path)
            invalid_payloads = []

            extra_payload = dict(valid_payload)
            extra_payload["unexpected"] = "value"
            invalid_payloads.append(json.dumps(extra_payload).encode("utf-8"))

            privileged_payload = dict(valid_payload)
            privileged_payload["allow_testnet_orders"] = True
            invalid_payloads.append(
                json.dumps(privileged_payload).encode("utf-8")
            )

            notional_payload = dict(valid_payload)
            notional_payload["max_notional"] = "1"
            invalid_payloads.append(json.dumps(notional_payload).encode("utf-8"))

            stale_schema_payload = dict(valid_payload)
            stale_schema_payload["schema_version"] = SCHEMA_VERSION + 1
            invalid_payloads.append(
                json.dumps(stale_schema_payload).encode("utf-8")
            )

            duplicate_payload = json.dumps(valid_payload).encode("utf-8")
            duplicate_payload = duplicate_payload.replace(
                b'"schema_version": 2',
                b'"schema_version": 2, "schema_version": 2',
            )
            invalid_payloads.append(duplicate_payload)

            # 어느 invalid object도 permissive correction이나 read-only fallback으로 통과하지 않는다.
            for invalid_payload in invalid_payloads:
                with self.subTest(invalid_payload=invalid_payload[:80]):
                    with self.assertRaises(ValueError):
                        self._read_configuration(invalid_payload)

    def test_parser_closes_fd_when_payload_exceeds_bound(self) -> None:
        """
        함수 이름: test_parser_closes_fd_when_payload_exceeds_bound()
        기능: bounded read가 oversized credential payload를 거부하고 inherited FD를 항상 닫는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        oversized_payload = b"x" * (MAX_SIDECAR_CONFIGURATION_BYTES + 1)
        with tempfile.TemporaryFile() as configuration_file:
            configuration_file.write(oversized_payload)
            configuration_file.seek(0)
            read_fd = os.dup(
                configuration_file.fileno()
            )  # Regular FD는 pipe capacity와 무관하게 oversized 경계를 재현한다.

            with self.assertRaisesRegex(ValueError, "too large"):
                read_sidecar_configuration_from_fd(
                    read_fd,
                    expected_schema_version=SCHEMA_VERSION,
                )
        with self.assertRaises(OSError):
            os.read(read_fd, 1)  # 오류 경로도 credential FD ownership을 남기지 않는다.


if __name__ == "__main__":
    unittest.main()
