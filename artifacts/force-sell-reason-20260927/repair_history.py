"""증거로 확인된 종료 매도 한 건의 사유와 연결된 장부 검증값만 교정한다."""

from dataclasses import asdict
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from tempfile import NamedTemporaryFile

from binance_auto_trader.adapters.persistence.residual_repository import ResidualRepository
from binance_auto_trader.adapters.persistence.trade_history_repository import TradeHistoryRepository
from binance_auto_trader.application.residual_settlement import ResidualSettlement
from binance_auto_trader.domain.history.performance import Performance
from binance_auto_trader.domain.history.trade import trade_from_json_object
from binance_auto_trader.domain.trading.position import Position
from binance_auto_trader.domain.trading.residual import history_digest


ARTIFACT = Path(__file__).resolve().parent
LIVE = Path.home() / "Library/Application Support/com.binance-auto.trader/com.binance-auto.trader.live"
CLIENT_ID = "bat-a5b87fd332130e5c496762b9-0"
ORDER_ID = "50138412055"
INTENT_ID = "force-sell:7ddfd416-00df-49e1-aa1e-b5049c8dbbc0"
FILES = ("trade-history.jsonl", "trade-history.jsonl.pending-orders.jsonl", "residual-ledger.json")


def sha256(data):
    """
    함수 이름: sha256()
    기능: 원본·후보 파일의 동일성을 검증할 해시를 계산한다.
    인자: data -> 파일 바이트
    반환값: SHA-256 문자열
    작성 날짜: 2026/09/27
    """
    return hashlib.sha256(data).hexdigest()


def encode(value):
    """
    함수 이름: encode()
    기능: 교정한 JSON을 UTF-8과 실제 개행으로 직렬화한다.
    인자: value -> JSON 값
    반환값: 파일 바이트
    작성 날짜: 2026/09/27
    """
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def replace_atomically(path, data, mode):
    """
    함수 이름: replace_atomically()
    기능: 같은 디렉터리의 임시 파일을 fsync한 뒤 원자 교체하고 디렉터리까지 반영한다.
    인자: path -> 대상 경로, data -> 새 바이트, mode -> 보존할 권한
    반환값: 없음
    작성 날짜: 2026/09/27
    """
    with NamedTemporaryFile(dir=path.parent, prefix=".force-sell-repair-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)


def restore_financial_state(directory):
    """
    함수 이름: restore_financial_state()
    기능: 실제 저장소와 재시작 로직으로 체결·열린 주문·잔여·성과를 복원한다.
    인자: directory -> 검증할 저장소 디렉터리
    반환값: 청산 사유를 제외한 회계 상태
    작성 날짜: 2026/09/27
    """
    repository = TradeHistoryRepository(directory / FILES[0])
    trades = repository.get_trade_history()
    if repository.get_pending_orders():
        raise RuntimeError("Repair requires no pending orders")
    position = Position()
    settlement = ResidualSettlement(ResidualRepository(directory / FILES[2]))
    settlement.restore(position, trades, current_step_size=Decimal("0.00010000"))
    snapshot = asdict(position.get_snapshot())
    snapshot.pop("exit_reason", None)
    performance = Performance(trades)
    metrics = {name: getattr(performance, name) for name in (
        "realized_pnl", "total_fee", "total_profit", "completed_sell_count",
        "winning_sell_count", "losing_sell_count", "cumulative_return_rate",
    )}
    return {"position": snapshot, "residual": settlement.totals, "performance": metrics}


def main():
    """
    함수 이름: main()
    기능: 백업으로 후보를 만들고 회계 동일성을 검증하며 --apply일 때만 중지된 앱 데이터를 교정한다.
    인자: 명령행의 선택적 --apply
    반환값: 없음
    작성 날짜: 2026/09/27
    """
    if sys.argv[1:] not in ([], ["--apply"]):
        raise SystemExit("Use no arguments for verification or --apply for repair")
    backup = ARTIFACT / "data-before"
    candidate = ARTIFACT / "data-corrected"
    candidate.mkdir(exist_ok=True)
    manifest = json.loads((ARTIFACT / "data-before-manifest.json").read_bytes())
    original = {name: (backup / name).read_bytes() for name in FILES}
    for name, data in original.items():
        if sha256(data) != manifest[name]["sha256"]:
            raise RuntimeError("Backup changed")
    evidence = json.loads((ARTIFACT / "evidence.json").read_bytes())
    if not any(row["force_sell"] is True and row["client_order_id"] == CLIENT_ID
               and row["intent_id"] == INTENT_ID for row in evidence):
        raise RuntimeError("Missing exact force-sell evidence")

    rows = [json.loads(line) for line in original[FILES[0]].splitlines()]
    target = [index for index, row in enumerate(rows) if row["order_id"] == ORDER_ID]
    if len(target) != 1:
        raise RuntimeError("Target must occur exactly once")
    index = target[0]
    trade = rows[index]
    if (trade["client_order_id"], trade["side"], trade["strategy"], trade["exit_reason"]) != (
        CLIENT_ID, "SELL", "CASE_B", "TAKE_PROFIT"
    ):
        raise RuntimeError("Trade provenance mismatch")
    before_trades = tuple(trade_from_json_object(row) for row in rows)
    trade["exit_reason"] = "FORCE_SELL"
    after_trades = tuple(trade_from_json_object(row) for row in rows)
    history_lines = original[FILES[0]].splitlines(keepends=True)
    history_lines[index] = encode(trade)
    corrected = {FILES[0]: b"".join(history_lines)}

    journal_lines = original[FILES[1]].splitlines(keepends=True)
    matches = 0
    for line_index, line in enumerate(journal_lines):
        record = json.loads(line)
        order = record.get("order", {})
        if order.get("client_order_id") != CLIENT_ID:
            continue
        if (record["operation"], order["intent_id"], order["side"], order["exit_reason"]) != (
            "UPSERT", INTENT_ID, "SELL", "TAKE_PROFIT"
        ):
            raise RuntimeError("Order journal provenance mismatch")
        order["exit_reason"] = "FORCE_SELL"
        journal_lines[line_index] = encode(record)
        matches += 1
    if matches != 1:
        raise RuntimeError("Expected one matching prepared order")
    corrected[FILES[1]] = b"".join(journal_lines)

    ledger = json.loads(original[FILES[2]])
    for transfer in ledger["transfers"]:
        count = transfer["history_count"]
        if transfer["history_sha256"] != history_digest(before_trades[:count]):
            raise RuntimeError("Original residual provenance mismatch")
        transfer["history_sha256"] = history_digest(after_trades[:count])
    corrected[FILES[2]] = encode(ledger)
    for name, data in corrected.items():
        replace_atomically(candidate / name, data, 0o600)
    financial_state = restore_financial_state(backup)
    if restore_financial_state(candidate) != financial_state:
        raise RuntimeError("Repair changed financial state")
    report = {
        "order_id": ORDER_ID, "client_order_id": CLIENT_ID,
        "before_exit_reason": "TAKE_PROFIT", "after_exit_reason": "FORCE_SELL",
        "financial_state_unchanged": True, "financial_state": financial_state,
        "files": {name: {"before": sha256(original[name]), "after": sha256(corrected[name])} for name in FILES},
        "applied": False,
    }

    if sys.argv[1:] == ["--apply"]:
        # 새 청산 사유를 읽을 수 있는 검증된 앱을 설치한 뒤에만 이력을 교정한다.
        installed = Path("/Applications/Binance Auto Trader.app/Contents/MacOS/binance-auto-sidecar")
        bundle = json.loads((ARTIFACT / "bundle-verification.json").read_bytes())
        if not bundle["successful"] or sha256(installed.read_bytes()) != bundle["sha256"]:
            raise RuntimeError("Verified application must be installed first")
        # 앱의 동일한 OS 잠금을 잡은 동안에만 검증한 세 파일을 교체한다.
        with (LIVE / ".backend-runtime.lock").open("rb") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if json.load(lock)["owner_state"] != "RELEASED":
                raise RuntimeError("Runtime is not released")
            modes = {}
            for name in FILES:
                path = LIVE / name
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != os.getuid():
                    raise RuntimeError("Unsafe storage file")
                modes[name] = stat.S_IMODE(metadata.st_mode)
                if path.read_bytes() != original[name]:
                    raise RuntimeError("Live data changed since backup")
            changed = []
            try:
                for name in FILES:
                    changed.append(name)
                    replace_atomically(LIVE / name, corrected[name], modes[name])
                if restore_financial_state(LIVE) != financial_state:
                    raise RuntimeError("Live verification mismatch")
                report["applied"] = True
            except BaseException:
                for name in reversed(changed):
                    replace_atomically(LIVE / name, original[name], modes[name])
                raise
    report_path = ARTIFACT / ("repair-applied.json" if report["applied"] else "repair-verified.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
