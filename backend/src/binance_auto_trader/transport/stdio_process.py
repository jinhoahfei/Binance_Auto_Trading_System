"""Windows framed stdio process lifecycle을 기존 HTTP application과 연결한다."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from time import sleep
from typing import BinaryIO

from .app import (
    LoopbackTransportApplication,
    LoopbackTransportServer,
    _RuntimeOwnershipLock,
    _enforce_parent_stop_pipe_eof_safety,
    _get_runtime_process_identity,
    _runtime_is_closed,
    create_loopback_transport_application,
)
from .contracts import RuntimeSnapshotSource
from .framing import read_json_frame, write_json_frame


MAX_READY_FRAME_BYTES = 4 * 1024
MAX_CONTROL_FRAME_BYTES = 256


def _read_control_frames(
    input_stream: BinaryIO,
    runtime: RuntimeSnapshotSource,
    control_events: Queue[str],
) -> None:
    """
    함수 이름: _read_control_frames()
    기능: blocking Windows pipe read를 단일 daemon reader에 격리한다.
    인자: input_stream -> bootstrap 이후 같은 parent stdin reader
        runtime -> ACK 수신 시점 CLOSED 여부를 확인할 authoritative runtime
        control_events -> 단일 slot으로 backpressure를 주는 event queue
    반환값: parent EOF, malformed control 또는 post-CLOSED ACK 뒤 없음
    작성 날짜: 2026/09/06
    """
    # Windows select는 pipe를 지원하지 않으므로 bounded reader가 EOF를 직접 관찰한다.
    try:
        while True:
            control_frame = read_json_frame(input_stream, maximum_bytes=MAX_CONTROL_FRAME_BYTES)
            if control_frame is None:
                break
            if control_frame != {"type": "CLOSED_ACK"}:
                break
            if _runtime_is_closed(runtime):
                control_events.put("CLOSED_ACK")
                return
            # READY 중 ACK는 FD5의 early byte처럼 폐기해 나중 shutdown에 재사용하지 않는다.
    except (OSError, ValueError):
        pass  # Truncated frame도 parent-channel loss와 같은 orphan 안전 경계를 적용한다.
    control_events.put("PARENT_LOST")


def _wait_for_safe_framed_ack(
    runtime: RuntimeSnapshotSource,
    server: LoopbackTransportServer,
    input_stream: BinaryIO,
    *,
    runtime_ownership_lock: _RuntimeOwnershipLock,
) -> None:
    """
    함수 이름: _wait_for_safe_framed_ack()
    기능: CLOSED ACK와 HTTP flush를 결합하고 stdin EOF에는 FD5 orphan 안전 경계를 유지한다.
    인자: runtime -> lifecycle과 신규 BUY gate를 소유한 runtime
        server -> complete shutdown response flush를 제공할 HTTP server
        input_stream -> parent가 독점하는 framed control stream
        runtime_ownership_lock -> runtime 수명 동안 유지할 durable ownership lock
    반환값: CLOSED 이후 ACK와 response handler 완료 뒤 없음
    작성 날짜: 2026/09/06
    """
    # 한 reader와 한 queue slot만 사용해 연속 control 입력에도 메모리 사용량을 제한한다.
    control_events: Queue[str] = Queue(maxsize=1)
    reader_thread = Thread(
        target=_read_control_frames,
        args=(input_stream, runtime, control_events),
        daemon=True,
        name="sidecar-stdio-control",
    )
    reader_thread.start()
    parent_lost = False
    post_closed_ack_received = False
    ownership_artifact_orphaned = False
    ownership_ambiguity_marked = False
    durable_flush_completed = False

    while True:
        # HTTP response가 완전히 flush된 후에만 native ACK를 process 종료로 승격한다.
        if post_closed_ack_received and server.shutdown_response_flushed:
            return
        if parent_lost:
            if not ownership_artifact_orphaned:
                try:
                    runtime_ownership_lock.mark_orphaned()
                except Exception:
                    pass  # Artifact fsync 실패는 다음 iteration에서 재시도한다.
                else:
                    ownership_artifact_orphaned = True
            ownership_ambiguity_marked, durable_flush_completed = _enforce_parent_stop_pipe_eof_safety(
                runtime,
                ownership_ambiguity_marked=ownership_ambiguity_marked,
                durable_flush_completed=durable_flush_completed,
            )
            sleep(0.05)  # Orphan의 listener와 ownership lock은 operator recovery까지 유지한다.
            continue

        try:
            control_event = control_events.get(timeout=0.05)
        except Empty:
            continue
        if control_event == "CLOSED_ACK":
            post_closed_ack_received = True
        else:
            parent_lost = True  # 다음 iteration 시작에 delay 없이 두 orphan 장벽을 적용한다.


def run_framed_transport_process(
    runtime_factory: Callable[..., RuntimeSnapshotSource],
    *,
    session_token: str,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    allowed_origins: Sequence[str],
    start_runtime: Callable[[RuntimeSnapshotSource], object] | None = None,
    close_runtime: Callable[[RuntimeSnapshotSource], object] | None = None,
) -> None:
    """
    함수 이름: run_framed_transport_process()
    기능: Windows binary streams에 READY와 control을 연결해 production safe lifecycle을 실행한다.
    인자: runtime_factory -> 공통 observer를 받는 application factory
        session_token -> 최초 bootstrap frame에서 받은 per-launch token
        input_stream -> bootstrap 뒤 같은 parent stdin stream
        output_stream -> secret-free READY 전용 stdout stream
        allowed_origins -> exact renderer Origin allowlist
        start_runtime -> 명시적 test seam 또는 기본 startup
        close_runtime -> 명시적 test seam 또는 기본 cleanup
    반환값: 안전한 CLOSED ACK 뒤 모든 application resource가 닫히면 없음
    작성 날짜: 2026/09/06
    """
    transport_application: LoopbackTransportApplication | None = None
    runtime_ownership_lock: _RuntimeOwnershipLock | None = None
    try:
        # 공통 ownership adapter를 먼저 잠가 중복 runtime의 외부 startup을 차단한다.
        runtime_pid, process_start_id = _get_runtime_process_identity()
        runtime_ownership_lock = _RuntimeOwnershipLock.acquire(
            Path.cwd(),
            runtime_pid=runtime_pid,
            process_start_id=process_start_id,
        )
        transport_application = create_loopback_transport_application(
            runtime_factory,
            session_token,
            allowed_origins=allowed_origins,
            start_runtime=start_runtime,
            close_runtime=close_runtime,
        )
        descriptor = transport_application.start()
        write_json_frame(output_stream, descriptor.to_dto(), maximum_bytes=MAX_READY_FRAME_BYTES)
        _wait_for_safe_framed_ack(
            transport_application.runtime,
            transport_application.server,
            input_stream,
            runtime_ownership_lock=runtime_ownership_lock,
        )
    finally:
        try:
            if transport_application is not None:
                transport_application.stop()  # Runtime cleanup을 stream과 ownership release보다 먼저 완료한다.
        finally:
            if runtime_ownership_lock is not None:
                runtime_ownership_lock.release()
            session_token = ""  # 종료 경계에서 transport owner의 token 참조를 제거한다.
