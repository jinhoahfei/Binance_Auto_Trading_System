"""주문 기능 없는 runtime fixture로 실제 운영 loopback transport와 heartbeat worker를 구동한다."""
from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import secrets
import sys
import threading
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/src")]
from binance_auto_trader.adapters.filesystem.diagnostic_log_writer import DiagnosticLogWriter
from binance_auto_trader.application.runtime_diagnostics import RuntimeDiagnostics
from binance_auto_trader.bootstrap.application import _TradingEventRuntimeWorker
from binance_auto_trader.transport import LoopbackTransportServer
from binance_auto_trader.transport.contracts import build_snapshot_dto, TransportContractError
from tests.unit.transport.test_contracts import _create_ready_runtime


def main():
    """
    함수 이름: main()
    기능: 비공개 부모 pipe로 descriptor를 전달하고 fault·idle·metrics 제어를 수용한다.
    인자: argv[1] -> 새로운 검증 출력 경로
    반환값: stdin EOF 뒤 worker와 테스트 서버를 정리한다.
    작성 날짜: 2026/09/13
    """
    runtime = _create_ready_runtime()  # 거래소 client·주문 명령 구현이 없는 고정 계좌 fixture다.
    runtime.state = SimpleNamespace(status="READY")
    runtime.diagnostics = RuntimeDiagnostics(DiagnosticLogWriter(Path(sys.argv[1]), "disabled"))
    token = secrets.token_urlsafe(32)
    server = LoopbackTransportServer(runtime, token, allowed_origins=("http://127.0.0.1:5173", "tauri://localhost"))
    controls = {"fail_snapshots": 0, "idle": False, "publications": 0}
    dispatch = server._dispatch_http_request

    def fault_dispatch(handler, request_path, request_id, **kwargs):
        """
        함수 이름: fault_dispatch()
        기능: 조회만 허용하고 지정된 snapshot 장애를 실제 HTTP 503 응답으로 재현한다.
        인자: handler/request_path/request_id -> 운영 HTTP 요청, kwargs -> 쿼리 인자
        반환값: 운영 route 응답 또는 주입한 503
        작성 날짜: 2026/09/13
        """
        if handler.command != "GET":
            raise TransportContractError("COMMAND_DISABLED", "Soak is read only", status=403)
        if request_path == "/v1/snapshot" and controls["fail_snapshots"] > 0:
            controls["fail_snapshots"] -= 1
            raise TransportContractError("BACKEND_UNREACHABLE", "Injected outage", status=503, retryable=True)
        return dispatch(handler, request_path, request_id, **kwargs)

    async def cycle():
        """
        함수 이름: cycle()
        기능: 활성 입력과 값 변화 없는 idle을 번갈아 실행한다.
        인자: 없음
        반환값: 활성 marker 또는 빈 tuple
        작성 날짜: 2026/09/13
        """
        return () if controls["idle"] else ("tick",)

    def publish():
        """
        함수 이름: publish()
        기능: 운영 DTO mapper로 유효한 계좌 event를 발행한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/13
        """
        snapshot = build_snapshot_dto(runtime, server.event_stream.session_id, 0)
        server.event_stream.publish("ACCOUNT_UPDATED", {"account": snapshot["account"]}, aggregate_version=snapshot["account"]["version"])
        controls["publications"] += 1

    server._dispatch_http_request = fault_dispatch
    worker = _TradingEventRuntimeWorker(cycle, lambda: None, lambda: True, lambda: 0,
        runtime.application_lock, state_update_observer=publish, poll_interval_seconds=2, diagnostics=runtime.diagnostics)
    server.start()
    worker.start()
    # Descriptor는 부모가 읽는 pipe에만 보낸다. 부모는 token을 어떤 산출물에도 기록하지 않는다.
    descriptor = server.descriptor
    print(json.dumps({"port": descriptor.port, "schema_version": descriptor.schema_version,
        "session_id": descriptor.session_id, "token": token}), flush=True)
    try:
        for line in sys.stdin:
            command = json.loads(line)
            if command["operation"] == "fault":
                controls["fail_snapshots"] = 2
            elif command["operation"] == "idle":
                controls["idle"] = command["enabled"] is True
            elif command["operation"] != "metrics":
                raise ValueError("Unknown soak command")
            print(json.dumps({"pid": os.getpid(), "threads": threading.active_count(),
                "max_rss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "publications": controls["publications"], "worker_failed": worker.failed,
                "diagnostic_failures": runtime.diagnostics.failure_count}), flush=True)
    finally:
        worker.close()
        server.stop()


if __name__ == "__main__":
    main()
