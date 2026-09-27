"""거래 동작과 독립된 진단 출력 경계와 비밀 없는 예외 위치를 제공한다."""

from collections.abc import Callable, Mapping
from collections import deque
from datetime import datetime, timezone
import logging
from math import isfinite
from threading import Condition, RLock, Thread, current_thread
from time import monotonic, monotonic_ns

from .diagnostic_values import encode_diagnostic_value
from .liveness_diagnostics import LivenessDiagnostics


def describe_exception(error: BaseException) -> tuple[dict[str, object], ...]:
    """
    함수 이름: describe_exception()
    기능: 예외 원문·지역변수 없이 원인별 타입과 내부 파일의 함수·행 위치를 추출한다.
    인자: error -> 포착한 원인 예외
    반환값: 최대 다섯 원인의 안전한 진단 목록
    작성 날짜: 2026/09/09
    """
    causes = []
    visited = set()

    # Credential을 포함할 수 있는 str(error), 소스 문장과 frame locals는 읽지 않는다.
    while error is not None and id(error) not in visited and len(causes) < 5:
        visited.add(id(error))
        exception_type = type(error).__name__
        if not exception_type.isascii() or not exception_type.isidentifier() or len(exception_type) > 128:
            exception_type = "UNSAFE_EXCEPTION_TYPE"
        frames = []
        traceback_cursor = error.__traceback__
        while traceback_cursor is not None:
            module_name = traceback_cursor.tb_frame.f_globals.get("__name__", "")
            function_name = traceback_cursor.tb_frame.f_code.co_name
            if (
                isinstance(module_name, str)
                and module_name.startswith("binance_auto_trader.")
                and all(part.isascii() and part.isidentifier() for part in module_name.split("."))
                and function_name.isascii()
                and function_name.isidentifier()
            ):
                frames.append({"module": module_name, "function": function_name, "line": traceback_cursor.tb_lineno})
            traceback_cursor = traceback_cursor.tb_next
        causes.append({"exception_type": exception_type, "frames": frames[-20:]})
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return tuple(causes)  # 외부 예외도 타입은 보존하지만 외부 frame 내용은 복사하지 않는다.


class RuntimeDiagnostics:
    """
    클래스 이름: RuntimeDiagnostics
    기능: 주입된 진단 sink로 사건을 보내며 저장 장애가 주문 조정을 중단하지 않게 격리한다.
    작성 날짜: 2026/09/09
    """

    def __init__(
        self,
        sink: Callable[[Mapping[str, object]], None] | None = None,
        *,
        asynchronous: bool = False,
        queue_capacity: int = 1024,
    ) -> None:
        """
        함수 이름: __init__()
        기능: optional sink와 bounded 단일 writer 또는 동기 테스트 관측 경계를 준비한다.
        인자: sink -> bootstrap이 연결한 출력 함수 또는 기록 비활성의 None
            asynchronous -> 파일 I/O를 호출 thread에서 분리할지 여부
            queue_capacity -> 실행 중 record를 제외한 대기 record 상한
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        # 진단은 런타임마다 분리하며 전역 logger handler를 추가하지 않는다.
        if type(asynchronous) is not bool:
            raise TypeError("asynchronous must be a bool")
        if type(queue_capacity) is not int or queue_capacity < 1:
            raise ValueError("queue_capacity must be a positive integer")
        self._sink = sink
        self.liveness = LivenessDiagnostics()
        self._lock = RLock()
        self._dropped_records = 0
        self._failure_count = 0
        self._overflow_count = 0
        self._closed_rejection_count = 0
        self._first_failure_event: str | None = None
        self._record_sequence = 0
        self._queue_capacity = queue_capacity
        self._records: deque[dict[str, object]] = deque()
        self._condition = Condition(self._lock)
        self._active_record = False
        self._closed = False
        self._close_unwritten_records = 0
        self._writer_thread: Thread | None = None
        self._next_heartbeat = 0.0  # monotonic 값은 금융 수치가 아닌 운영 heartbeat 시계다.
        if asynchronous and sink is not None:
            self._writer_thread = Thread(target=self._run_writer, name="binance-diagnostic-writer", daemon=True)
            self._writer_thread.start()

    @property
    def enabled(self) -> bool:
        """
        함수 이름: enabled()
        기능: 비활성 테스트에서 진단 snapshot 구성 비용까지 생략하도록 연결 여부를 반환한다.
        인자: 없음
        반환값: sink가 연결되어 있으면 True
        작성 날짜: 2026/09/09
        """
        return self._sink is not None  # Domain은 이 출력 여부와 관계없이 같은 판단을 수행한다.

    @property
    def failure_count(self) -> int:
        """
        함수 이름: failure_count()
        기능: 운영 점검과 검증에서 누적 진단 저장 실패 횟수를 조회한다.
        인자: 없음
        반환값: 이 runtime의 누적 실패 횟수
        작성 날짜: 2026/09/09
        """
        with self._lock:
            return self._failure_count  # 정상 복구 뒤에도 장애 발생 사실은 유지한다.

    def record(self, event: str, *, level: str = "INFO", **details: object) -> None:
        """
        함수 이름: record()
        기능: 생존 관측을 즉시 갱신하고 운영 진단은 원본과 분리해 기다리지 않고 예약한다.
        인자: event -> 고정 사건 이름, level -> 심각도, details -> 비밀 없는 진단 값
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        self.liveness.observe(event, details)
        if self._sink is None:
            return

        observed_at = datetime.now(timezone.utc)
        observed_monotonic_ms = monotonic_ns() // 1_000_000
        if self._writer_thread is not None:
            try:
                frozen_details = encode_diagnostic_value(details)
            except Exception:
                with self._lock:
                    self._note_failure(event)
                return  # 손상된 진단 객체가 거래나 socket callback으로 예외를 전파하지 않는다.
            with self._condition:
                self._record_sequence += 1
                if self._closed:
                    self._note_failure(event)
                    self._closed_rejection_count += 1
                    return
                if len(self._records) >= self._queue_capacity:
                    self._note_failure(event)
                    self._overflow_count += 1
                    return  # 생산자는 디스크나 stderr로 동기 우회하지 않는다.
                self._records.append({
                    "event": event, "level": level, "observed_at": observed_at,
                    "monotonic_ms": observed_monotonic_ms,
                    "diagnostic_sequence": self._record_sequence, "details": frozen_details,
                    "queue_depth_at_enqueue": len(self._records) + 1,
                })
                self._condition.notify_all()
            return

        # 명시적으로 주입한 동기 in-memory observer는 기존 관측 순서를 보존한다.
        with self._lock:
            if self._closed:
                self._note_failure(event)
                return
            try:
                self._sink({
                    "event": event,
                    "level": level,
                    "observed_at": observed_at,
                    "monotonic_ms": observed_monotonic_ms,
                    "dropped_records_before": self._dropped_records,
                    "details": details,
                })
            except Exception:
                self._note_failure(event)
                if self._dropped_records == 1:
                    logging.getLogger(__name__).error(
                        "DIAGNOSTIC_LOG_WRITE_FAILED: log records are missing; order reconciliation continues"
                    )  # stderr에는 예외 원문이나 주문 payload를 전달하지 않는다.
            else:
                self._dropped_records = 0

    def _note_failure(self, event: str) -> None:
        """
        함수 이름: _note_failure()
        기능: 상태 잠금 아래 저장·enqueue 실패의 최초 사건과 누락 수를 보존한다.
        인자: event -> 실패한 고정 사건 이름
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        self._failure_count += 1
        self._dropped_records += 1
        if self._first_failure_event is None:
            self._first_failure_event = event

    def _run_writer(self) -> None:
        """
        함수 이름: _run_writer()
        기능: 단일 daemon에서만 sink를 호출하고 종료 전 수락한 진단을 FIFO로 회수한다.
        인자: 없음
        반환값: 종료 요청 뒤 queue가 비면 없음
        작성 날짜: 2026/09/22
        """
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or bool(self._records))
                if not self._records:
                    return
                record = self._records.popleft()
                self._active_record = True
                reported_drops = self._dropped_records
                record["dropped_records_before"] = reported_drops
                record["first_failure_event"] = self._first_failure_event
                record["queue_overflow_count"] = self._overflow_count
                record["close_unwritten_records"] = self._close_unwritten_records
            try:
                self._sink(record)
            except Exception:
                with self._condition:
                    self._note_failure(record["event"])
                    report_error = self._dropped_records == 1
                if report_error:
                    try:
                        logging.getLogger(__name__).error(
                            "DIAGNOSTIC_LOG_WRITE_FAILED: log records are missing; order reconciliation continues"
                        )  # stderr 지연도 이 단일 writer에만 남긴다.
                    except Exception:
                        pass  # 외부 logging handler 실패로 queue owner를 잃지 않는다.
            else:
                with self._condition:
                    self._dropped_records -= reported_drops
            finally:
                with self._condition:
                    self._active_record = False
                    self._condition.notify_all()

    def queue_snapshot(self) -> dict[str, object]:
        """
        함수 이름: queue_snapshot()
        기능: 디스크를 읽지 않고 bounded queue와 최초 실패·종료 미기록 수를 반환한다.
        인자: 없음
        반환값: 민감정보 없는 진단 writer 상태
        작성 날짜: 2026/09/22
        """
        with self._lock:
            return {
                "capacity": self._queue_capacity, "queued_records": len(self._records),
                "active_record": self._active_record, "failure_count": self._failure_count,
                "overflow_count": self._overflow_count, "dropped_records": self._dropped_records,
                "closed_rejection_count": self._closed_rejection_count,
                "first_failure_event": self._first_failure_event,
                "close_unwritten_records": self._close_unwritten_records,
                "closed": self._closed,
            }

    def flush(self, timeout_seconds: float = 1.0) -> bool:
        """
        함수 이름: flush()
        기능: sink가 멈춰도 제한 시간까지만 수락한 queue의 완료를 기다린다.
        인자: timeout_seconds -> 최대 대기 시간
        반환값: 모든 수락 record가 처리됐으면 True
        작성 날짜: 2026/09/22
        """
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not isfinite(timeout_seconds) or timeout_seconds < 0):
            raise ValueError("timeout_seconds must be finite and non-negative")
        with self._condition:
            return self._condition.wait_for(
                lambda: not self._records and not self._active_record, timeout=timeout_seconds,
            )

    def close(self, timeout_seconds: float = 1.0) -> bool:
        """
        함수 이름: close()
        기능: 신규 기록을 닫고 단일 writer를 제한 시간만 회수하며 미기록 수를 보존한다.
        인자: timeout_seconds -> 종료의 최대 대기 시간
        반환값: writer 회수가 완료됐으면 True
        작성 날짜: 2026/09/22
        """
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not isfinite(timeout_seconds) or timeout_seconds < 0):
            raise ValueError("timeout_seconds must be finite and non-negative")
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        writer_thread = self._writer_thread
        if writer_thread is not None and writer_thread is not current_thread():
            writer_thread.join(timeout=timeout_seconds)
        with self._lock:
            self._close_unwritten_records = len(self._records) + int(self._active_record)
            return writer_thread is None or not writer_thread.is_alive()

    def record_exception(self, stage: str, error: BaseException, **details: object) -> None:
        """
        함수 이름: record_exception()
        기능: 실패 단계·상관관계와 원인별 안전한 stack 위치를 함께 기록한다.
        인자: stage -> 실패 작업, error -> 원인 예외, details -> 관련 식별자와 상태
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        if self.enabled:
            self.record("operation_failed", level="ERROR", stage=stage, causes=describe_exception(error), **details)

    def heartbeat_due(self) -> bool:
        """
        함수 이름: heartbeat_due()
        기능: 무전이·대기 중에도 runtime 생존을 확인하도록 분당 한 번 기록을 허용한다.
        인자: 없음
        반환값: 이번 호출에서 heartbeat를 남길 차례이면 True
        작성 날짜: 2026/09/09
        """
        with self._lock:
            current_time = monotonic()
            if not self.enabled or current_time < self._next_heartbeat:
                return False
            self._next_heartbeat = current_time + 60
            return True  # 전략의 5초·3분 timer에는 이 시계를 사용하지 않는다.
