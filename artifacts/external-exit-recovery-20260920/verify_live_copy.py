"""원본 이력의 복사본과 주문 권한 없는 live graph로 두 번의 시작·종료를 검증한다."""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import resource
import shutil
from tempfile import TemporaryDirectory

from binance_auto_trader.bootstrap import close_application, start_application
from binance_auto_trader.bootstrap.live import create_live_application_runtime
from binance_auto_trader.bootstrap.live_configuration import LiveConfiguration
from binance_auto_trader.application import external_exit_recovery
from scripts.run_live_read_only_from_keychain import read_keychain_credential, zeroize_secret_buffer


def main():
    """비밀값을 출력하지 않고 원본 보존과 실제 startup 결과만 저장한다."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    source = Path.home() / "Library/Application Support/com.binance-auto.trader/com.binance-auto.trader.live"
    names = ("trade-history.jsonl", "trade-history.jsonl.pending-orders.jsonl", "residual-ledger.json")
    originals = {name: (source / name).read_bytes() for name in names}
    result = {"order_execution_enabled": False, "recovery_module": external_exit_recovery.__file__, "runs": [], "source_sha256": {
        name: hashlib.sha256(data).hexdigest() for name, data in originals.items()}}
    key = secret = bytearray()
    try:
        key = read_keychain_credential("api-key")
        secret = read_keychain_credential("api-secret")
        configuration = LiveConfiguration(key.decode("ascii"), secret.decode("ascii"), enabled=True, confirmation="LIVE")
        with TemporaryDirectory(prefix="external-exit-live-copy-") as directory:
            target = Path(directory).resolve() / source.name
            target.mkdir()
            for name in names:
                shutil.copyfile(source / name, target / name)
            for _ in range(2):
                runtime = create_live_application_runtime(configuration=configuration, history_path=target / names[0])
                run = {}
                result["runs"].append(run)
                try:
                    run["ready"] = start_application(runtime).ready
                    controller = runtime.trading_controller
                    run["session_status"] = controller.snapshot_session().status.value
                    run["position_quantity"] = str(controller._require_position().quantity)
                    run["residual_totals"] = tuple(map(str, controller.residual_totals))
                    run["balance"] = controller.balance_reconciliation_snapshot()
                    trades = runtime.trade_history_repository.get_trade_history()
                    run["history_count"] = len(trades)
                    run["last_trade"] = asdict(trades[-1])
                    run["pending_count"] = len(runtime.trade_history_repository.get_pending_orders())
                finally:
                    run["closed"] = close_application(runtime).status.value
            result["copy_ledger_unchanged"] = (target / "residual-ledger.json").read_bytes() == originals["residual-ledger.json"]
    except Exception as error:
        result["error_type"] = type(error).__name__
        result["cause_types"] = []
        cause = error.__cause__
        while cause is not None:
            result["cause_types"].append(type(cause).__name__)
            cause = cause.__cause__
    finally:
        zeroize_secret_buffer(key)
        zeroize_secret_buffer(secret)
        result["original_files_unchanged"] = all((source / name).read_bytes() == data for name, data in originals.items())
        output = Path(__file__).with_name("live-copy-verification.json")
        output.write_text(json.dumps(result, default=str, indent=2))
        print(json.dumps(result, default=str, indent=2))
    return 1 if "error_type" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
