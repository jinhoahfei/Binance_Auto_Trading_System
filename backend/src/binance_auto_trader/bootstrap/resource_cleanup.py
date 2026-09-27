"""종료 요청과 실제 자원 정리 완료를 구분하는 내부 기록."""

from collections.abc import Callable

from binance_auto_trader.adapters.binance.request_deadline import deadline_lock


def close_runtime_resource(runtime, resource_name: str, operation: Callable[[], object]) -> None:
    """
    함수 이름: close_runtime_resource()
    기능: 성공한 자원은 재사용하고 실패한 정리는 같은 소유자가 다시 확인하게 한다.
    인자: runtime -> 종료 기록 소유 runtime, resource_name -> 고정 자원 이름
        operation -> 완료 시 None 또는 True, 미완료 시 False를 반환하는 정리 함수
    반환값: 실제 정리를 확인하면 없음, 실패 또는 미완료이면 원인 예외
    작성 날짜: 2026/09/27
    """
    store = runtime._shutdown_store
    # 종료 준비와 최종 종료가 같은 자원을 동시에 정리하지 않도록 소유권을 직렬화한다.
    with deadline_lock(store.cleanup_lock):
        if resource_name in store.cleanup_completed:
            return
        if resource_name in store.cleanup_active:
            raise RuntimeError("resource cleanup is already running")
        store.cleanup_requested.add(resource_name)
        store.cleanup_active.add(resource_name)
        preparation = store.preparation
        operation_id = None if preparation is None else preparation.operation_id
        try:
            if operation() is False:
                raise TimeoutError("resource cleanup is not complete")
        except BaseException as error:
            store.cleanup_finished = False
            store.cleanup_errors[resource_name] = error
            runtime.diagnostics.record_exception(
                "shutdown_resource_cleanup", error, resource=resource_name, operation_id=operation_id,
            )
            raise
        else:
            store.cleanup_completed.add(resource_name)
            store.cleanup_errors.pop(resource_name, None)  # 실제 완료가 확인된 실패 기록만 지운다.
            runtime.diagnostics.record(
                "shutdown_resource_cleaned", resource=resource_name, operation_id=operation_id,
            )
        finally:
            store.cleanup_active.remove(resource_name)
