"""거래 잠금·디스크·거래소 요청 없이 읽는 작은 생존 진단 snapshot."""

from collections.abc import Mapping
from datetime import datetime
from os import getpid
from threading import Lock
from time import monotonic_ns, time_ns
from uuid import uuid4


class LivenessDiagnostics:
    """클래스 이름: LivenessDiagnostics
    기능: 거래 데이터가 없는 시각·연결 관측만 별도 잠금으로 게시한다.
    작성 날짜: 2026/09/16
    """

    def __init__(self):
        """함수 이름: __init__()
        기능: 프로세스 수명과 미관측 상태를 초기화한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/16
        """
        self._lock = Lock()
        self._values = dict(diagnostic_schema_version=2, process_id=getpid(),
                            process_start_id=str(uuid4()), runtime_status="unknown",
                            last_runtime_cycle_at_ms=None, last_runtime_cycle_monotonic_ms=None,
                            last_market_input_at_ms=None, last_strategy_evaluation_at_ms=None,
                            last_ui_send_at_ms=None, last_ui_send_sequence=None,
                            last_ui_send_duration_ms=None, market_stream="unknown",
                            account_stream="unknown", api="unknown", api_checked_at_ms=None,
                            streams_checked_at_ms=None, last_exchange_error_code=None)

    def observe(self, event, details=None):
        """함수 이름: observe()
        기능: 허용된 사건의 scalar 값만 관측하며 원본 payload는 보관하지 않는다.
        인자: event -> 내부 고정 사건 이름, details -> 해당 사건의 입력
        반환값: 없음
        작성 날짜: 2026/09/16
        """
        if event == "runtime_cycle" and not isinstance(details, Mapping):
            # production snapshot은 dataclass이며 테스트 worker는 임의 marker를 사용할 수 있다.
            details = {"status": getattr(details, "status", None),
                       "recovery": getattr(details, "recovery", None)}
        details = details if isinstance(details, Mapping) else {}
        now = time_ns() // 1_000_000
        patch = {}
        if event == "runtime_cycle":
            patch = {"last_runtime_cycle_at_ms": now,
                     "last_runtime_cycle_monotonic_ms": monotonic_ns() // 1_000_000}
            status = details.get("status")
            if isinstance(status, str) and status in {"not_started", "running", "stopping", "reconciliation_required", "terminated"}:
                patch["runtime_status"] = status
            recovery = details.get("recovery")
            recovery = recovery if isinstance(recovery, Mapping) else {}
            for field in ("last_market_input_at", "last_strategy_evaluation_at"):
                value = recovery.get(field)
                if isinstance(value, datetime):
                    patch[field + "_ms"] = int(value.timestamp() * 1000)
        elif event in {"market_input_observed", "strategy_evaluated"}:
            patch["last_market_input_at_ms" if event == "market_input_observed" else "last_strategy_evaluation_at_ms"] = now
        elif event == "ui_send" and all(type(details.get(key)) is int and details[key] >= 0
                                        for key in ("sequence", "duration_ms")):
            patch = {"last_ui_send_at_ms": now, "last_ui_send_sequence": details["sequence"],
                     "last_ui_send_duration_ms": details["duration_ms"]}
        elif event == "exchange_observed":
            patch = {key: details[key] for key in ("market_stream", "account_stream")
                     if details.get(key) in {"online", "offline"}}
            patch["streams_checked_at_ms"] = now
        elif event == "api_observed" and details.get("api") in {"online", "offline"}:
            patch = {"api": details["api"], "api_checked_at_ms": now}
            if details["api"] == "offline":
                patch["last_exchange_error_code"] = "API_REQUEST_FAILED"
        elif event == "stream_unavailable" and details.get("stream") in {"market", "account"}:
            stream = details["stream"]
            patch = {stream + "_stream": "offline", "streams_checked_at_ms": now,
                     "last_exchange_error_code": stream.upper() + "_STREAM_UNAVAILABLE"}
        if patch:
            with self._lock:
                self._values.update(patch)

    def snapshot(self):
        """함수 이름: snapshot()
        기능: application lock 없이 고정 크기 snapshot을 복사한다.
        인자: 없음
        반환값: 계좌·인증 정보 없는 JSON 값
        작성 날짜: 2026/09/16
        """
        with self._lock:
            return {**self._values, "sampled_at_ms": time_ns() // 1_000_000,
                    "monotonic_ms": monotonic_ns() // 1_000_000}
