"""거래 동작과 독립된 진단 출력 경계와 비밀 없는 예외 위치를 제공한다."""

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import logging
from threading import RLock
from time import monotonic, monotonic_ns
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

    def __init__(self, sink: Callable[[Mapping[str, object]], None] | None = None) -> None:
        """
        함수 이름: __init__()
        기능: 파일 구현을 모르는 optional sink와 기록 실패 계수를 준비한다.
        인자: sink -> bootstrap이 연결한 출력 함수 또는 기록 비활성의 None
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        # 진단은 런타임마다 분리하며 전역 logger handler를 추가하지 않는다.
        self._sink = sink
        self.liveness = LivenessDiagnostics()
        self._lock = RLock()
        self._dropped_records = 0
        self._failure_count = 0
        self._next_heartbeat = 0.0  # monotonic 값은 금융 수치가 아닌 운영 heartbeat 시계다.

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
        기능: 사건을 동기 기록하고 저장 장애와 복구 시 누락 건수를 명시한다.
        인자: event -> 고정 사건 이름, level -> 심각도, details -> 비밀 없는 진단 값
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        self.liveness.observe(event, details)
        if self._sink is None:
            return

        # 저장 실패가 청산·동일 주문 조회를 막지 않도록 출력 오류만 이 경계에서 격리한다.
        with self._lock:
            try:
                self._sink({
                    "event": event,
                    "level": level,
                    "observed_at": datetime.now(timezone.utc),
                    "monotonic_ms": monotonic_ns() // 1_000_000,
                    "dropped_records_before": self._dropped_records,
                    "details": details,
                })
            except Exception:
                self._failure_count += 1
                self._dropped_records += 1
                if self._dropped_records == 1:
                    logging.getLogger(__name__).error(
                        "DIAGNOSTIC_LOG_WRITE_FAILED: log records are missing; order reconciliation continues"
                    )  # stderr에는 예외 원문이나 주문 payload를 전달하지 않는다.
            else:
                self._dropped_records = 0

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
