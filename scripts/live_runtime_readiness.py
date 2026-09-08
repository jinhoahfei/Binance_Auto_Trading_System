"""주문 권한 없이 실제 live graph의 startup·잔여 상태·정상 종료를 검증한다."""

import argparse
import json
import os
from pathlib import Path
import resource
from uuid import uuid4

from binance_auto_trader.bootstrap import close_application, start_application
from binance_auto_trader.bootstrap.application import ApplicationStatus
from binance_auto_trader.bootstrap.live import create_live_application_runtime
from binance_auto_trader.bootstrap.live_configuration import LiveConfiguration, validate_live_history_path
from binance_auto_trader.transport.app import _RuntimeOwnershipLock


def run_runtime_readiness(
    configuration: LiveConfiguration,
    history_path: Path,
) -> dict[str, bool]:
    """
    함수 이름: run_runtime_readiness()
    기능: native와 같은 owner lock과 live history로 읽기 전용 graph 한 개를 검증한다.
    인자: configuration -> live 읽기 전용 설정, history_path -> native live history 경로
    반환값: secret·잔액·주문 ID 없는 readiness와 종료 검증 boolean
    작성 날짜: 2026/09/08
    """
    if not configuration.enabled or configuration.allow_live_orders:
        raise ValueError("runtime readiness requires read-only live configuration")
    history_path = validate_live_history_path(history_path)

    # 실행 중 native app 또는 미조정 owner가 있으면 graph를 만들기 전에 실패한다.
    ownership = _RuntimeOwnershipLock.acquire(
        history_path.parent,
        runtime_pid=os.getpid(),
        process_start_id=str(uuid4()),
    )
    runtime = None
    checks = {}
    try:
        runtime = create_live_application_runtime(
            configuration=configuration,
            history_path=history_path,
        )
        checks["ready"] = start_application(runtime).ready

        # Controller가 authoritative history와 exchange 사실을 조정한 뒤 상태를 같은 lock에서 읽는다.
        with runtime.application_lock:
            controller = runtime.trading_controller
            repository = runtime.trade_history_repository
            checks.update({
                "orders_disabled": not runtime.order_execution_enabled,
                "startup_reconciled": controller.startup_reconciliation_complete,
                "position_zero": not controller.snapshot_session().has_open_position,
                "residual_assets_zero": controller.residual_totals[0] == 0,
                "pending_zero": not repository.get_pending_orders(),
                "pending_queries_zero": controller.pending_order_query_count == 0,
                "unknown_execution_zero": not controller.external_execution_reconciliation_required,
                "reconciliation_clear": not controller.reconciliation_required,
                "history_zero": not repository.get_trade_history(),
            })

        # Local empty state만으로 계좌 전체의 미체결 상태까지 추정하지 않는다.
        checks["exchange_open_orders_zero"] = not runtime.api_gateway.has_any_exchange_open_orders()
        checks["exchange_open_lists_zero"] = not runtime.api_gateway.has_any_exchange_open_order_lists()
        return checks
    finally:
        try:
            if runtime is not None:
                checks["closed"] = close_application(runtime).status is ApplicationStatus.CLOSED
                if not checks["closed"]:
                    raise RuntimeError("runtime did not close")
        except BaseException:
            # 종료가 불명확하면 다음 native 실행이 정상 RELEASED로 오인하지 않도록 보존한다.
            ownership.mark_orphaned()
            ownership.release()  # ORPHANED fsync 실패 때는 lock을 유지하고 process 종료에 맡긴다.
            raise
        else:
            ownership.release()


def main() -> int:
    """
    함수 이름: main()
    기능: 명시적 LIVE 확인 뒤 native와 같은 경로에서 실제 읽기 전용 lifecycle을 검사한다.
    인자: 없음; CLI는 --confirm-live LIVE를 요구한다.
    반환값: 모든 readiness 검사 성공 0 또는 실패 1
    작성 날짜: 2026/09/08
    """
    from run_live_read_only_from_keychain import read_keychain_credential, zeroize_secret_buffer

    parser = argparse.ArgumentParser(description="Live read-only runtime readiness; close native app first")
    parser.add_argument("--confirm-live", required=True, choices=["LIVE"])
    parser.parse_args()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    key = bytearray()
    secret = bytearray()
    try:
        # Secret은 기존 bounded reader로만 읽고 CLI·환경·파일에는 기록하지 않는다.
        key = read_keychain_credential("api-key")
        secret = read_keychain_credential("api-secret")
        configuration = LiveConfiguration(key.decode("ascii"), secret.decode("ascii"), enabled=True, confirmation="LIVE")
        history_path = Path.home() / "Library/Application Support/com.binance-auto.trader/com.binance-auto.trader.live/trade-history.jsonl"
        checks = run_runtime_readiness(configuration, history_path)
        passed = bool(checks) and all(checks.values())
        print(json.dumps({"runtime_readiness": "PASS" if passed else "FAILED", "checks": checks, "order_mutations": 0}, sort_keys=True))
        return 0 if passed else 1
    except Exception as error:
        print(json.dumps({"runtime_readiness": "FAILED", "error_type": type(error).__name__, "order_mutations": 0}))
        return 1  # Traceback은 credential-bearing runtime을 표현할 수 있어 출력하지 않는다.
    finally:
        zeroize_secret_buffer(key)
        zeroize_secret_buffer(secret)


if __name__ == "__main__":
    raise SystemExit(main())
