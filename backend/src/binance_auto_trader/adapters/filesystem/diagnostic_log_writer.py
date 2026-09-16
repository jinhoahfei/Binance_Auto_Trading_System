"""날짜·실행·분할 번호별 UTF-8 JSON Lines 진단 파일을 저장한다."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import json
import os
from pathlib import Path
from threading import RLock
from uuid import uuid4


# KST는 고정 UTC+9이며 파일 분할과 사람이 읽는 시각에 같은 기준을 사용한다.
KST = timezone(timedelta(hours=9), name="KST")
_PRIVATE_FIELDS = frozenset({
    "api_key", "api_secret", "secret", "token", "session_token", "signature",
    "authorization", "headers", "body", "url", "payload", "failure_reason", "message",
})


def encode_diagnostic_value(value: object) -> object:
    """
    함수 이름: encode_diagnostic_value()
    기능: 선택된 진단 값을 Decimal 정밀도를 보존하는 JSON 값으로 변환한다.
    인자: value -> 명시적으로 기록 대상으로 선택한 값
    반환값: JSON 호환 값 또는 비공개 타입의 고정 표식
    작성 날짜: 2026/09/09
    """
    # Domain enum·시각·경과 시간은 문자열 또는 초 단위 Decimal 문자열로 표현한다.
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    if isinstance(value, timedelta):
        return str(Decimal(value // timedelta(microseconds=1)) / Decimal("1000000"))
    if value is None or isinstance(value, (str, bool, int)):
        return value

    # 재귀 변환에서도 credential·raw payload·예외 메시지와 private aggregate cache를 제외한다.
    if is_dataclass(value) and not isinstance(value, type):
        value = {field.name: getattr(value, field.name) for field in fields(value) if not field.name.startswith("_")}
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]" if key.lower() in _PRIVATE_FIELDS else encode_diagnostic_value(item)
            for key, item in value.items()
            if isinstance(key, str) and not key.startswith("_")
        }
    if isinstance(value, (tuple, list)):
        return [encode_diagnostic_value(item) for item in value]
    return "[UNSUPPORTED]"  # 임의 객체의 repr에는 credential이 있을 수 있으므로 호출하지 않는다.


class DiagnosticLogWriter:
    """
    클래스 이름: DiagnosticLogWriter
    기능: 런타임별 순서를 보존하며 날짜·크기별로 파일을 분할하고 과거 로그를 유지한다.
    작성 날짜: 2026/09/09
    """

    def __init__(self, directory: Path, execution_mode: str, *, maximum_bytes: int = 16 * 1024 * 1024) -> None:
        """
        함수 이름: __init__()
        기능: 비밀 없는 실행 ID와 독점 파일을 만들어 시작 전에 저장 가능 여부를 확인한다.
        인자: directory -> 로그 디렉터리, execution_mode -> 실행 환경, maximum_bytes -> 분할 크기
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        if execution_mode not in ("live", "testnet", "fake", "disabled"):
            raise ValueError("unsupported diagnostic execution mode")
        if type(maximum_bytes) is not int or maximum_bytes < 1:
            raise ValueError("maximum_bytes must be positive")

        # 프로세스와 각 runtime을 분리해 동시 실행이나 재시작 시 파일을 덮어쓰지 않는다.
        self.directory = Path(directory).resolve()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.run_id = uuid4().hex
        self._execution_mode = execution_mode
        self._maximum_bytes = maximum_bytes
        self._sequence = 0
        self._part = 0
        self._size = 0
        self._write_uncertain = False
        self._lock = RLock()
        self._rotate(datetime.now(timezone.utc))  # 실패하면 실제 주문 runtime 조립 전에 예외를 알린다.

    def _rotate(self, timestamp: datetime) -> None:
        """
        함수 이름: _rotate()
        기능: KST 시각과 실행·분할 번호를 가진 새 파일을 독점 생성한다.
        인자: timestamp -> 새 파일의 기록 기준 시각
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        local_time = timestamp.astimezone(KST)
        next_part = self._part + 1
        next_path = self.directory / (
            f"{local_time:%Y-%m-%d_%H-%M-%S-%f}_KST_{self._execution_mode}_"
            f"{os.getpid()}_{self.run_id}_part{next_part:04d}.log"
        )

        # 새 파일 생성 성공 뒤에만 현재 파일 참조를 교체해 분할 실패 시 이전 위치를 보존한다.
        descriptor = os.open(next_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        self.path = next_path
        self._day = local_time.date()
        self._part = next_part
        self._size = 0

    def __call__(self, record: Mapping[str, object]) -> None:
        """
        함수 이름: __call__()
        기능: 한 사건을 JSON 한 줄로 append하고 즉시 flush하여 실행 중에도 읽을 수 있게 한다.
        인자: record -> application이 선택한 사건 envelope와 상세 값
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        with self._lock:
            timestamp = record["observed_at"]
            if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
                raise ValueError("diagnostic observed_at must be timezone-aware")

            # 수신 순서와 실제 기록 시각을 시장 evaluated_at과 분리해 지연·재연결도 추적한다.
            next_sequence = self._sequence + 1
            envelope = {
                "schema_version": 2,
                "timestamp_kst": timestamp.astimezone(KST),
                "timestamp_utc": timestamp.astimezone(timezone.utc),
                "run_id": self.run_id,
                "execution_mode": self._execution_mode,
                "pid": os.getpid(),
                "sequence": next_sequence,
                **record,
            }
            encoded_line = (json.dumps(encode_diagnostic_value(envelope), ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")

            # 날짜·크기 분할 전에 이전 파일의 불완전한 append를 복구해 손상된 과거 파일을 남기지 않는다.
            if self._write_uncertain:
                with self.path.open("r+b") as recovery_stream:
                    recovery_stream.truncate(self._size)
                self._write_uncertain = False
            if timestamp.astimezone(KST).date() != self._day or (self._size > 0 and self._size + len(encoded_line) > self._maximum_bytes):
                self._rotate(timestamp)

            # 매 append마다 파일을 닫아 shutdown 예외나 외부 tail 조회에도 buffered record를 남기지 않는다.
            try:
                with self.path.open("r+b") as output_stream:
                    output_stream.seek(self._size)
                    output_stream.write(encoded_line)
                    output_stream.flush()
            except Exception:
                self._write_uncertain = True
                raise  # 누락 집계와 stderr 알림은 application 진단 경계가 담당한다.
            self._sequence = next_sequence
            self._size += len(encoded_line)  # 파일 크기 한도는 UTF-8 byte 수로 판정한다.
