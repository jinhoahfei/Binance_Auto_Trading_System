"""Production runtime identity descriptor와 app-data lifetime lock을 검증한다."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from binance_auto_trader.transport.app import (
    SCHEMA_VERSION,
    ServerDescriptor,
    _get_runtime_process_identity,
    _RuntimeOwnershipLock,
    _read_runtime_ownership_artifact,
)
from binance_auto_trader.transport.contracts import TransportContractError
from binance_auto_trader.adapters.platform import windows_runtime
from binance_auto_trader.adapters.platform.runtime_lock import close_runtime_directory_handles


def _acquire_test_runtime_lock(directory: Path, *, runtime_pid: int, process_start_id: str):
    """
    함수 이름: _acquire_test_runtime_lock()
    기능: isolated temp directory에서 실제 OS lock primitive와 ownership lifecycle을 검증한다.
    인자: directory -> 테스트가 소유한 temporary directory
        runtime_pid -> synthetic owner의 positive PID
        process_start_id -> synthetic owner의 canonical UUID
    반환값: 실제 OS lock을 획득한 runtime ownership 객체
    작성 날짜: 2026/09/06
    """
    # Native Windows known-folder의 exact path 검증만 fixture root로 바꾸고 handle·lock은 실제 실행한다.
    with patch.object(windows_runtime, "get_local_app_data_directory", return_value=directory):
        return _RuntimeOwnershipLock.acquire(
            directory, runtime_pid=runtime_pid, process_start_id=process_start_id
        )


class ProcessIdentityTests(unittest.TestCase):
    """
    클래스 이름: ProcessIdentityTests
    기능: FD4 process identity의 strict type, canonical UUID와 secret-free shape를 검증한다.
    작성 날짜: 2026/08/25
    """

    def test_server_descriptor_has_exact_runtime_identity_shape(self) -> None:
        """
        함수 이름: test_server_descriptor_has_exact_runtime_identity_shape()
        기능: descriptor가 exact five-field shape와 canonical runtime identity를 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        session_id = str(uuid4())
        process_start_id = str(uuid4())
        descriptor = ServerDescriptor(
            port=41_000,
            session_id=session_id,
            runtime_pid=123,
            process_start_id=process_start_id,
        )

        self.assertEqual(
            descriptor.to_dto(),
            {
                "port": 41_000,
                "session_id": session_id,
                "runtime_pid": 123,
                "process_start_id": process_start_id,
                "schema_version": SCHEMA_VERSION,
            },
        )  # Session token, credential과 launcher PID는 Python READY wire에 포함하지 않는다.

    def test_server_descriptor_rejects_invalid_runtime_pid_types_and_values(
        self,
    ) -> None:
        """
        함수 이름: test_server_descriptor_rejects_invalid_runtime_pid_types_and_values()
        기능: bool, float, zero와 negative PID를 exact positive int로 오인하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 공통 valid descriptor에서 PID만 바꿔 exact type과 positive 범위를 분리 검증한다.
        valid_fields = {
            "port": 41_000,
            "session_id": str(uuid4()),
            "process_start_id": str(uuid4()),
        }

        for invalid_pid in (True, 1.0, "1", 0, -1):
            with self.subTest(invalid_pid=invalid_pid):
                expected_error = TypeError if type(invalid_pid) is not int else ValueError
                with self.assertRaises(expected_error):
                    ServerDescriptor(
                        **valid_fields,
                        runtime_pid=invalid_pid,
                    )

    def test_server_descriptor_rejects_noncanonical_process_start_uuid(
        self,
    ) -> None:
        """
        함수 이름: test_server_descriptor_rejects_noncanonical_process_start_uuid()
        기능: uppercase와 malformed process start UUID를 canonical UUID로 받지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # Parser가 해석 가능한 uppercase와 명백한 malformed UUID를 모두 canonical 입력에서 제외한다.
        canonical_uuid = str(uuid4())

        for invalid_start_id in (canonical_uuid.upper(), "not-a-uuid"):
            with self.subTest(invalid_start_id=invalid_start_id):
                with self.assertRaises(TransportContractError):
                    ServerDescriptor(
                        port=41_000,
                        session_id=str(uuid4()),
                        runtime_pid=123,
                        process_start_id=invalid_start_id,
                    )

    def test_interpreter_identity_is_stable_and_uses_actual_pid(self) -> None:
        """
        함수 이름: test_interpreter_identity_is_stable_and_uses_actual_pid()
        기능: 같은 interpreter lifetime에서 actual PID와 동일한 canonical start UUID를 반환하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 같은 interpreter에서 두 번 읽어 PID와 process-start identity의 안정성을 함께 확인한다.
        first_identity = _get_runtime_process_identity()
        second_identity = _get_runtime_process_identity()

        self.assertEqual(first_identity, second_identity)
        self.assertEqual(first_identity[0], os.getpid())
        self.assertEqual(str(UUID(first_identity[1])), first_identity[1])


class RuntimeOwnershipLockTests(unittest.TestCase):
    """
    클래스 이름: RuntimeOwnershipLockTests
    기능: app-data advisory lock의 contention, durable identity와 non-deletion 정책을 검증한다.
    작성 날짜: 2026/08/25
    """

    def test_lock_blocks_second_owner_and_persists_secret_free_identity(
        self,
    ) -> None:
        """
        함수 이름: test_lock_blocks_second_owner_and_persists_secret_free_identity()
        기능: active owner 중복 획득을 막고 owner-only exact identity artifact를 fsync하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        runtime_pid = os.getpid()
        process_start_id = str(uuid4())
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            ownership_lock = _acquire_test_runtime_lock(
                directory,
                runtime_pid=runtime_pid,
                process_start_id=process_start_id,
            )
            lock_path = directory / ".backend-runtime.lock"
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "runtime ownership lock is unavailable",
                ):
                    _acquire_test_runtime_lock(
                        directory,
                        runtime_pid=runtime_pid,
                        process_start_id=str(uuid4()),
                    )

                # Artifact는 credential 없이 current runtime identity와 ACTIVE 상태만 exact JSON으로 보존한다.
                artifact = _read_runtime_ownership_artifact(ownership_lock._descriptor)
                self.assertEqual(
                    artifact,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "runtime_pid": runtime_pid,
                        "process_start_id": process_start_id,
                        "owner_state": "ACTIVE",
                    },
                )
                if os.name == "posix":
                    self.assertEqual(
                        stat.S_IMODE(lock_path.stat().st_mode),
                        0o600,
                    )  # Windows owner 경계는 POSIX mode 대신 native known folder를 사용한다.
            finally:
                ownership_lock.release()

            self.assertTrue(lock_path.is_file())  # 정상 release도 stale 판정이나 파일 삭제를 수행하지 않는다.
            released_artifact = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(released_artifact["owner_state"], "RELEASED")

    def test_released_lock_can_be_reacquired_without_deleting_file(self) -> None:
        """
        함수 이름: test_released_lock_can_be_reacquired_without_deleting_file()
        기능: 이전 artifact를 삭제하지 않고 exclusive lock 획득 뒤 새 launch identity로 교체하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        # 첫 owner를 정상 release한 뒤 같은 inode를 삭제하지 않고 다음 identity로 갱신한다.
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            first_lock = _acquire_test_runtime_lock(
                directory,
                runtime_pid=os.getpid(),
                process_start_id=str(uuid4()),
            )
            first_lock.release()
            lock_path = directory / ".backend-runtime.lock"
            original_inode = lock_path.stat().st_ino
            next_start_id = str(uuid4())

            second_lock = _acquire_test_runtime_lock(
                directory,
                runtime_pid=os.getpid(),
                process_start_id=next_start_id,
            )
            try:
                refreshed_artifact = _read_runtime_ownership_artifact(second_lock._descriptor)
                self.assertEqual(refreshed_artifact["process_start_id"], next_start_id)
                self.assertEqual(lock_path.stat().st_ino, original_inode)
            finally:
                second_lock.release()

    def test_crash_active_artifact_blocks_relaunch_without_overwrite(self) -> None:
        """
        함수 이름: test_crash_active_artifact_blocks_relaunch_without_overwrite()
        기능: process crash로 OS lock만 풀린 ACTIVE artifact를 새 launch가 자동 덮어쓰지 못하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            process_start_id = str(uuid4())
            ownership_lock = _acquire_test_runtime_lock(
                directory,
                runtime_pid=os.getpid(),
                process_start_id=process_start_id,
            )
            lock_path = directory / ".backend-runtime.lock"
            active_artifact = _read_runtime_ownership_artifact(ownership_lock._descriptor)

            # 정상 release marker 없이 FD만 닫아 kernel이 crash process의 flock을 회수한 상태를 만든다.
            abandoned_descriptor = ownership_lock._descriptor
            self.assertIsNotNone(abandoned_descriptor)
            ownership_lock._descriptor = None
            os.close(abandoned_descriptor)
            close_runtime_directory_handles(ownership_lock._directory_handles)
            ownership_lock._directory_handles = ()  # Crash처럼 OS lock과 ancestor pin을 함께 반환한다.

            with self.assertRaisesRegex(
                RuntimeError,
                "runtime ownership lock is unavailable",
            ):
                _acquire_test_runtime_lock(
                    directory,
                    runtime_pid=os.getpid(),
                    process_start_id=str(uuid4()),
                )

            self.assertEqual(
                json.loads(lock_path.read_text(encoding="utf-8")),
                active_artifact,
            )  # 새 PID/UUID로 덮어쓰지 않아 operator reconciliation 근거를 남긴다.

    def test_orphan_marker_is_idempotent_and_never_becomes_released(self) -> None:
        """
        함수 이름: test_orphan_marker_is_idempotent_and_never_becomes_released()
        기능: parent-loss marker가 멱등 fsync되고 cleanup이 정상 release로 세탁하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/25
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            ownership_lock = _acquire_test_runtime_lock(
                directory,
                runtime_pid=os.getpid(),
                process_start_id=str(uuid4()),
            )
            lock_path = directory / ".backend-runtime.lock"

            # 반복 marker와 release를 순차 적용해도 ORPHANED 조정 근거는 남아야 한다.
            ownership_lock.mark_orphaned()
            ownership_lock.mark_orphaned()
            ownership_lock.release()
            artifact = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["owner_state"], "ORPHANED")

            with self.assertRaisesRegex(
                RuntimeError,
                "runtime ownership lock is unavailable",
            ):
                _acquire_test_runtime_lock(
                    directory,
                    runtime_pid=os.getpid(),
                    process_start_id=str(uuid4()),
                )


if __name__ == "__main__":
    unittest.main()
