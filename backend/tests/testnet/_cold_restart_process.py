"""Phase 9 실제 Testnet cold-restart의 process A 주문 owner를 실행한다."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from binance_auto_trader.application import TradingSessionStatus
from binance_auto_trader.bootstrap import (
    ApplicationStatus,
    start_application,
)
from binance_auto_trader.bootstrap.testnet import (
    create_testnet_application_runtime,
    load_testnet_configuration,
    require_testnet_order_permission,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.history import Trade
from binance_auto_trader.domain.trading import SubmitOrder
from binance_auto_trader.domain.trading.action_requests import patch
from binance_auto_trader.domain.trading.states import (
    OrderAttemptKind,
    OrderSide,
    StrategyType,
    TradingPhase,
)


# Process A의 terminal 주문과 durable history 대기를 유한 시간으로 제한한다.
_ORDER_SETTLEMENT_TIMEOUT_SECONDS = 60
_POLL_INTERVAL_SECONDS = 0.25
_RECEIPT_SCHEMA_VERSION = 1
_ACCOUNTING_SUPPORTED_FEE_ASSETS = frozenset({"ETH", "USDT"})


def _drain_controller_work(runtime: object) -> None:
    """
    함수 이름: _drain_controller_work()
    기능: due same-ID 조회와 생성된 주문 outcome microstep을 모두 처리한다.
    인자: runtime -> production Testnet ApplicationRuntime
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    # Controller의 공개 reconciliation과 drain 경계만 호출해 신규 submit을 만들지 않는다.
    controller = runtime.trading_controller
    controller.trigger_order_reconciliation(
        occurred_at=datetime.now(timezone.utc),
    )
    asyncio.run(
        controller.drain_events()
    )  # 동기 unittest child에서 serial queue의 현재 microstep을 완료한다.


def _require_supported_commission_accounting(runtime: object) -> None:
    """
    함수 이름: _require_supported_commission_accounting()
    기능: signed commission 정책이 제3 자산 또는 수신 ETH 수수료를 만들 수 있으면 BUY 전에 차단한다.
    인자: runtime -> READY production Testnet ApplicationRuntime
    반환값: ETH 또는 USDT 수수료 회계만 가능한 정책이면 없음
    작성 날짜: 2026/08/24
    """
    # Raw commission payload 대신 APIGateway가 축약한 공식 discount 가능성만 읽는다.
    commission_policy = runtime.api_gateway.fetch_commission_discount_policy(
        "ETHUSDT"
    )
    if (
        commission_policy.can_charge_discount_asset
        and commission_policy.discount_asset
        not in _ACCOUNTING_SUPPORTED_FEE_ASSETS
    ):
        raise RuntimeError(
            "process A commission policy permits an unsupported fee asset"
        )  # BNB 등 제3 자산 fill이 생기기 전에 실제 BUY mutation을 fail closed한다.

    # 공식 FAQ상 MARKET BUY 수수료가 수신 ETH에서 빠지면 LOT_SIZE 밖 잔량이 생길 수 있다.
    if (
        commission_policy.market_buy_received_asset_commission_rate
        > Decimal("0")
    ):
        raise RuntimeError(
            "process A MARKET BUY commission can create unsupported base-asset dust"
        )  # Dust 회계와 전량 청산 정책을 갖추기 전에는 실제 BUY 자체를 허용하지 않는다.


def _client_order_id_for_intent(runtime: object, intent_id: str) -> str:
    """
    함수 이름: _client_order_id_for_intent()
    기능: 한 BUY intent가 사용한 유일한 Controller client order ID를 반환한다.
    인자: runtime -> production Testnet ApplicationRuntime
        intent_id -> process A가 생성한 BUY 의도 식별자
    반환값: Controller가 결정론적으로 만든 client order ID
    작성 날짜: 2026/08/24
    """
    # Trace에는 credential이나 raw Binance payload가 없으므로 same-ID 식별자만 추출한다.
    client_order_ids = {
        trace.client_order_id
        for trace in runtime.trading_controller.order_execution_trace
        if trace.intent_id == intent_id
    }
    if len(client_order_ids) != 1:
        raise RuntimeError("process A BUY must own exactly one client order ID")

    return next(iter(client_order_ids))  # 검증된 유일 식별자만 parent receipt에 전달한다.


def _wait_for_buy_trade(
    runtime: object,
    client_order_id: str,
) -> Trade:
    """
    함수 이름: _wait_for_buy_trade()
    기능: 새 submit 없이 same-ID reconciliation으로 process A의 durable BUY를 기다린다.
    인자: runtime -> production Testnet ApplicationRuntime
        client_order_id -> 최초 BUY submit에 사용한 고정 client order ID
    반환값: history에 fsync된 terminal BUY Trade
    작성 날짜: 2026/08/24
    """
    deadline = time.monotonic() + _ORDER_SETTLEMENT_TIMEOUT_SECONDS

    # REST FULL 응답이 active 또는 UNKNOWN이어도 기존 주문 조회만 bounded polling한다.
    while time.monotonic() < deadline:
        matching_trade = next(
            (
                trade
                for trade in runtime.trade_history.trades
                if trade.client_order_id == client_order_id
            ),
            None,
        )
        if matching_trade is not None:
            return matching_trade

        _drain_controller_work(runtime)
        time.sleep(
            _POLL_INTERVAL_SECONDS
        )  # Controller가 기록한 실제 query due 시각까지 busy loop하지 않는다.

    raise TimeoutError("process A BUY did not become durable in time")


def _execute_capped_buy(runtime: object, max_notional: Decimal) -> Trade:
    """
    함수 이름: _execute_capped_buy()
    기능: production Controller pipeline으로 capped BUY 하나를 durable history에 남긴다.
    인자: runtime -> READY이고 RUNNING인 production Testnet ApplicationRuntime
        max_notional -> 사용자가 환경으로 승인한 양수 BUY quote 진입 상한
    반환값: terminal BUY Trade
    작성 날짜: 2026/08/24

    주의: 공개 market-context 입력이 없는 Phase 9에서 BUY 신호 생성만 기존 test-only
    action seam을 사용하며, journal 이후의 주문·Position·history 처리는 production 경계가 소유한다.
    """
    controller = runtime.trading_controller
    intent_id = f"phase9-cold-restart-buy-{uuid4().hex}"

    # 테스트 seam은 BUY 의도 예약과 SubmitOrder action 생성에만 한정한다.
    controller._execute_action(
        patch(
            pending_strategy=StrategyType.CASE_B,
            pending_order_side=OrderSide.BUY,
            pending_order_attempt_kind=OrderAttemptKind.INITIAL,
            pending_intent_id=intent_id,
            trading_phase=TradingPhase.ENTRY_ORDER_PENDING,
        )
    )
    outcomes = controller._execute_action(
        SubmitOrder(
            strategy=StrategyType.CASE_B,
            side=OrderSide.BUY,
            attempt_kind=OrderAttemptKind.INITIAL,
            idempotency_key=intent_id,
        )
    )

    # 동기 terminal 결과도 public serial intake로 되돌린 뒤 same-ID 조회만 허용한다.
    for outcome in outcomes:
        accepted_outcome = controller.enqueue_event(outcome)
        if accepted_outcome is None:
            raise RuntimeError("process A BUY outcome was rejected by the queue")
    asyncio.run(controller.drain_events())
    client_order_id = _client_order_id_for_intent(runtime, intent_id)
    buy_trade = _wait_for_buy_trade(runtime, client_order_id)

    # 승인 상한과 durable pending 제거를 process 종료 전에 canonical 값으로 검증한다.
    if buy_trade.side is not OrderSide.BUY:
        raise RuntimeError("process A durable trade must be a BUY")
    if (
        buy_trade.requested_quantity * buy_trade.market_price_at_decision
        > max_notional
    ):
        raise RuntimeError("process A BUY exceeded the approved notional cap")
    if runtime.trade_history_controller.get_pending_orders():
        raise RuntimeError("process A BUY still has a durable pending order")
    if controller.status is not TradingSessionStatus.RUNNING:
        raise RuntimeError("process A session stopped before crash handoff")

    return buy_trade  # parent는 이 durable execution만 새 runtime에서 인수한다.


def _write_process_receipt(
    receipt_path: Path,
    *,
    status: str,
    client_order_id: str | None = None,
    order_id: str | None = None,
    failure_type: str | None = None,
) -> None:
    """
    함수 이름: _write_process_receipt()
    기능: secret과 raw payload가 없는 process A 결과를 원자·durable JSON으로 저장한다.
    인자: receipt_path -> parent가 읽을 receipt 파일 경로
        status -> 성공 또는 실패 상태 코드
        client_order_id -> 성공한 BUY application 식별자 또는 None
        order_id -> 성공한 BUY exchange 식별자 또는 None
        failure_type -> 실패 예외의 class 이름 또는 None
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    # Receipt schema는 normalized ID와 예외 class만 허용해 credential과 응답 원문을 배제한다.
    receipt_payload: dict[str, object] = {
        "schema_version": _RECEIPT_SCHEMA_VERSION,
        "status": status,
    }
    if client_order_id is not None:
        receipt_payload["client_order_id"] = client_order_id
    if order_id is not None:
        receipt_payload["order_id"] = order_id
    if failure_type is not None:
        receipt_payload["failure_type"] = failure_type

    receipt_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary_path = receipt_path.with_name(f"{receipt_path.name}.tmp")
    encoded_receipt = (
        json.dumps(
            receipt_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")

    # File과 rename directory entry를 모두 fsync해 abrupt process exit 뒤에도 receipt를 남긴다.
    file_descriptor = os.open(
        temporary_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(file_descriptor, "wb", closefd=False) as receipt_file:
            receipt_file.write(encoded_receipt)
            receipt_file.flush()
            os.fsync(receipt_file.fileno())
    finally:
        os.close(file_descriptor)  # write 실패에도 descriptor를 process 종료 전에 회수한다.
    os.replace(temporary_path, receipt_path)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor = os.open(receipt_path.parent, directory_flags)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(
            directory_descriptor
        )  # atomic receipt 이름도 cold restart에서 관찰 가능하게 만든다.


def run_process_a(history_path: Path, receipt_path: Path) -> int:
    """
    함수 이름: run_process_a()
    기능: READY→RUNNING→durable capped BUY까지 실행하고 normal close 없이 종료 코드를 반환한다.
    인자: history_path -> parent와 공유할 durable history JSONL 경로
        receipt_path -> normalized handoff receipt 경로
    반환값: 성공이면 0, secret-safe 실패 receipt를 남기면 1
    작성 날짜: 2026/08/24
    """
    try:
        # 모든 opt-in과 cap을 production loader에서 다시 검증한 뒤 고정 Testnet runtime을 만든다.
        configuration = load_testnet_configuration()
        max_notional = require_testnet_order_permission(configuration)
        runtime = create_testnet_application_runtime(
            history_path=history_path,
        )
        application_state = start_application(runtime)
        if application_state.status is not ApplicationStatus.READY:
            raise RuntimeError("process A application did not become READY")

        # REGIME 선택과 session 시작은 production public command boundary를 그대로 사용한다.
        controller = runtime.trading_controller
        selection = runtime.regime_controller.set_regime_type(
            RegimeType.TYPE_0,
            command_id=f"phase9-cold-select-{uuid4().hex}",
            expected_version=controller.context.version,
        )
        split_result = controller.update_split_ratios(
            command_id=f"phase9-cold-split-{uuid4().hex}",
            expected_version=selection.version,
            scale_in=Decimal("1"),
            scale_out=Decimal("1"),
        )
        session_result = controller.start_trading(
            command_id=f"phase9-cold-start-{uuid4().hex}",
            expected_version=split_result.version,
        )
        if session_result.status is not TradingSessionStatus.RUNNING:
            raise RuntimeError("process A trading session did not start")

        # Signed commission 정책을 effect 직전에 확인하고 durable BUY 뒤 normal close는 호출하지 않는다.
        _require_supported_commission_accounting(runtime)
        buy_trade = _execute_capped_buy(runtime, max_notional)
        _write_process_receipt(
            receipt_path,
            status="BUY_HISTORY_DURABLE",
            client_order_id=buy_trade.client_order_id,
            order_id=buy_trade.order_id,
        )
        return 0  # caller는 이 값을 받은 즉시 interpreter cleanup 없는 process exit를 수행한다.
    except BaseException as error:
        # 예외 메시지에는 외부 payload가 섞일 수 있으므로 class 이름만 durable 진단에 기록한다.
        try:
            _write_process_receipt(
                receipt_path,
                status="PROCESS_A_FAILED",
                failure_type=type(error).__name__,
            )
        except BaseException:
            pass  # receipt 실패 자체도 stdout/stderr로 노출하지 않고 exit code로만 전달한다.
        return 1


def main(arguments: list[str] | None = None) -> int:
    """
    함수 이름: main()
    기능: parent가 전달한 두 artifact 경로를 검증하고 process A를 실행한다.
    인자: arguments -> history와 receipt 경로 두 개 또는 None
    반환값: process A 성공 또는 실패 종료 코드
    작성 날짜: 2026/08/24
    """
    selected_arguments = sys.argv[1:] if arguments is None else arguments
    if len(selected_arguments) != 2:
        return 2  # 경로 외 CLI surface를 허용하지 않아 credential argument 사용을 막는다.

    history_path = Path(selected_arguments[0])
    receipt_path = Path(selected_arguments[1])
    return run_process_a(history_path, receipt_path)


if __name__ == "__main__":
    # Python shutdown hook이 runtime 자원을 정상 close하지 못하게 성공·실패 모두 즉시 종료한다.
    os._exit(main())
