"""종료에만 사용하는 계좌·주문 검증과 기존 STOP 파이프라인 조정."""

import asyncio
from decimal import localcontext
from threading import get_ident
from time import monotonic, sleep

from binance_auto_trader.adapters.binance.spot_rest_client import BinanceAPIError
from binance_auto_trader.application.residual_settlement import ResidualSettlement
from binance_auto_trader.application.trade_history_controller import TradeHistoryPersistencePendingError
from binance_auto_trader.domain.trading.action_requests import CancelPendingOrder, patch
from binance_auto_trader.domain.trading.position import Position
from binance_auto_trader.domain.trading.states import RootState, TradingPhase


class ShutdownPreparationBlocked(RuntimeError):
    """
    클래스 이름: ShutdownPreparationBlocked
    기능: 종료 차단 단계의 안정적인 이유와 재시도 가능 여부를 전달한다.
    작성 날짜: 2026/09/16
    """
    def __init__(self, code: str, retryable: bool = False):
        """
        함수 이름: __init__()
        기능: 종료 차단 사유와 재시도 가능 여부를 초기화한다.
        인자: code, retryable -> 해당 작업에 필요한 입력 값
        반환값: 해당 단계의 처리 결과 또는 없음
        작성 날짜: 2026/09/16
        """
        super().__init__(code)
        self.code, self.retryable = code, retryable


def prepare_shutdown_cycle(controller, operation) -> None:
    """
    함수 이름: prepare_shutdown_cycle()
    기능: 기존 주문과 장부를 대조하고 동의한 포지션만 청산해 종료를 검증한다.
    인자: controller, operation -> 해당 작업에 필요한 입력 값
    반환값: 해당 단계의 처리 결과 또는 없음
    작성 날짜: 2026/09/16
    """
    # Controller와 같은 application 계층에서만 종료 전용 상태를 다룬다.
    from .trading_controller import TradingSessionStatus
    c = controller
    if not operation.version_validated and c.context.version != operation.expected_version:
        raise ShutdownPreparationBlocked("STALE_CONTEXT_VERSION", True)
    if c._process_ownership_ambiguous:
        raise ShutdownPreparationBlocked("SHUTDOWN_OWNERSHIP_UNVERIFIED")
    if c._external_execution_reconciliation_required:
        raise ShutdownPreparationBlocked("SHUTDOWN_UNEXPLAINED_EXECUTION")
    c._market_resume_suppressed = True
    c._shutdown_verified_version = None
    c._preparation_retry_due_at = None
    c._preparation_retry_intent = None
    c._scheduler.clear()
    if c._event_queue is not None:
        c._event_queue.discard_market_work()
    c._shutdown_cleanup_owner = get_ident()
    def check_cleanup():
        operation.check()
        if c._process_ownership_ambiguous:
            raise ShutdownPreparationBlocked("SHUTDOWN_OWNERSHIP_UNVERIFIED")
        if c._external_execution_reconciliation_required:
            raise ShutdownPreparationBlocked("SHUTDOWN_UNEXPLAINED_EXECUTION")
    c._shutdown_cleanup_check = check_cleanup
    c._shutdown_liquidation_allowed = operation.liquidation_confirmed
    try:
        _verify_history(c._require_trade_history_controller())
        _clear_unsubmitted_intent(c)
        while True:
            operation.progress("settling_orders", "orders", c.context.version)
            operation.check()
            open_orders = c._api_gateway.list_all_open_order_results("ETHUSDT")
            if not open_orders and (c._api_gateway.has_any_exchange_open_orders() or c._api_gateway.has_any_exchange_open_order_lists()):
                raise ShutdownPreparationBlocked("SHUTDOWN_UNEXPLAINED_ORDER")
            for result in open_orders:
                state = c._order_states_by_client_id.get(result.client_order_id)
                if state is None or state.order.exchange_order_id not in (None, result.exchange_order_id):
                    raise ShutdownPreparationBlocked("SHUTDOWN_UNEXPLAINED_ORDER")
                operation.progress("settling_orders", "orders", c.context.version)
                operation.check()
                # 취소 결과는 확정으로 쓰지 않는다. 다음 REST 대조가 같은 ID의 체결을 확정한다.
                c._cancel_pending_order_action(CancelPendingOrder(
                    order_id=state.order.exchange_order_id or state.order.client_order_id,
                    reason="SHUTDOWN_PREPARATION",
                ))
            operation.progress("checking", "account", c.context.version)
            _retry_reconciliation(c, operation)
            if open_orders:
                _wait(operation)
                continue
            history = c._require_trade_history_controller()
            pending = history.get_pending_order_recovery_records() if history.supports_pending_order_recovery else ()
            unresolved = pending or c._persistence_states_by_order_id or any(
                not state.order.is_terminal or state.pending_recovery_pending or state.persistence_pending
                for state in c._order_states_by_client_id.values())
            if unresolved:
                raise ShutdownPreparationBlocked("SHUTDOWN_ORDER_UNRESOLVED", True)
            preparing_liquidation = (operation.liquidation_confirmed
                and c._context.initialized and c._context.runtime.pending_intent_id is not None
                and c._context.runtime.pending_intent_id == c._force_sell_intent_id
                and c._context.runtime.pending_intent_id == c._preparation_retry_intent)
            if (c._context.initialized and c._context.runtime.pending_intent_id is not None
                    and not preparing_liquidation
                    and not any(state.order.intent_id == c._context.runtime.pending_intent_id
                                and state.order.is_terminal for state in c._order_states_by_client_id.values())):
                if c._context.runtime.pending_intent_id == c._unsubmitted_preparation_intent:
                    raise ShutdownPreparationBlocked("SHUTDOWN_LIQUIDATION_PREPARATION_FAILED", True)
                raise ShutdownPreparationBlocked("SHUTDOWN_ORDER_UNRESOLVED")
            position = c._require_position()
            _verify_position_history(c, history, operation)
            with localcontext() as decimal_context:
                decimal_context.prec = 34
                expected_balance = position.quantity + c.residual_totals[0]
            if c._account.get_holdings("ETH") != expected_balance and not c._try_reconcile_earn_residual(c._account.get_holdings("ETH")):
                raise ShutdownPreparationBlocked("SHUTDOWN_BALANCE_MISMATCH")
            if position.quantity > 0 and not operation.liquidation_confirmed:
                raise ShutdownPreparationBlocked("SHUTDOWN_LIQUIDATION_CONFIRMATION_REQUIRED")
            operation.check()
            if position.quantity > 0:
                operation.progress("liquidating", "liquidation", c.context.version)
                # 종료 owner만 STOP을 진행한다. 일반 주문 gate는 끝까지 닫혀 있다.
                if c._active_stm is None or c._session_id is None:
                    c.liquidate_recovered_position(command_id=f"shutdown-recovery:{operation.operation_id}", expected_version=c.context.version)
                elif c._active_stm.current_state.root_state is RootState.STOPPING:
                    c._status = TradingSessionStatus.STOPPING
                    if c._force_sell_retry_due_at is None and c._force_sell_intent_id is not None:
                        c._force_sell_retry_due_at = c._clock()
                elif c.status is not TradingSessionStatus.STOPPING:
                    c._status = TradingSessionStatus.RUNNING
                    c.stop_trading(command_id=f"shutdown-prepare:{operation.operation_id}", expected_version=c.context.version)
                c.trigger_order_reconciliation()
                if c._event_processor is not None:
                    asyncio.run(c.drain_events(max_microsteps=1000))
                _wait(operation)
                continue
            # Flat은 메모리 bool만으로 판정하지 않는다. 대조된 장부와 잔고도 같아야 한다.
            operation.progress("checking", "history", c.context.version)
            if c._account.get_holdings("ETH") != c.residual_totals[0]:
                # 기존 Earn 증거 검증이 성공한 경우에는 거래소 현물과 보관 잔여의 차이가 설명된다.
                if not c._try_reconcile_earn_residual(c._account.get_holdings("ETH")):
                    raise ShutdownPreparationBlocked("SHUTDOWN_BALANCE_MISMATCH")
            operation.check()
            _verify_history(history)
            try:
                history.flush_durable_state()
            except Exception as error:
                raise ShutdownPreparationBlocked("SHUTDOWN_HISTORY_SAVE_FAILED", True) from error
            operation.check()
            c._cleanup_session_resources()
            if c.cleanup_failures:
                # 기존 session cleanup은 실패한 handle을 재사용하지 않는다. 자동 재시도를 약속하지 않는다.
                raise ShutdownPreparationBlocked("SHUTDOWN_RESOURCE_CLEANUP_FAILED")
            if c._context.initialized:
                c._context.update_pending_order(None)
                c._context.apply_runtime_patch(patch(pending_intent_id=None, pending_strategy=None,
                    pending_order_side=None, pending_order_attempt_kind=None, pending_exit_reason=None,
                    pending_exit_pct_b=None, trading_phase=TradingPhase.IDLE))
            c._status = TradingSessionStatus.TERMINATED
            check_cleanup()
            c._shutdown_verified_version = c.context.version
            c._shutdown_verified_account_version = c._account.version
            c._record_diagnostic("shutdown_state_verified", version=c.context.version)
            return
    except BinanceAPIError as error:
        raise ShutdownPreparationBlocked("SHUTDOWN_ACCOUNT_UNREACHABLE",
            error.status_code >= 500 or error.status_code in (418, 429)) from error
    except (OSError, TimeoutError) as error:
        raise ShutdownPreparationBlocked("SHUTDOWN_ACCOUNT_UNREACHABLE", True) from error
    finally:
        c._shutdown_cleanup_owner = None
        c._shutdown_cleanup_check = None
        c._shutdown_liquidation_allowed = False


def _clear_unsubmitted_intent(c) -> None:
    """
    함수 이름: _clear_unsubmitted_intent()
    기능: 제출 증거가 전혀 없는 준비 단계의 주문 의도만 해제한다.
    인자: c -> 해당 작업에 필요한 입력 값
    반환값: 해당 단계의 처리 결과 또는 없음
    작성 날짜: 2026/09/16
    """
    if not c._context.initialized or c._context.runtime.pending_intent_id is None:
        return
    intent = c._context.runtime.pending_intent_id
    if intent != c._unsubmitted_preparation_intent:
        return  # 모르는 의도는 임의로 지우지 않는다.
    history = c._require_trade_history_controller()
    records = history.get_pending_order_recovery_records() if history.supports_pending_order_recovery else ()
    client_id = c._create_client_order_id(intent, c._submission_attempts_by_intent.get(intent, 0))
    if (any(record.order.intent_id == intent for record in records)
            or c._find_active_state_for_intent(intent) is not None
            or c._api_gateway.get_order_submission_attempt_evidence(client_id) is not None):
        raise ShutdownPreparationBlocked("SHUTDOWN_ORDER_UNRESOLVED")
    c._context.update_pending_order(None)
    c._context.apply_runtime_patch(patch(pending_intent_id=None, pending_strategy=None,
        pending_order_side=None, pending_order_attempt_kind=None, pending_exit_reason=None,
        pending_exit_pct_b=None, trading_phase=TradingPhase.IDLE))
    c._unsubmitted_preparation_intent = None


def _reconcile(c, operation) -> None:
    """
    함수 이름: _reconcile()
    기능: 종료 중 신규 구독 없이 거래소의 계좌와 주문을 직접 대조한다.
    인자: c, operation -> 해당 작업에 필요한 입력 값
    반환값: 해당 단계의 처리 결과 또는 없음
    작성 날짜: 2026/09/16
    """
    operation.check()
    try:
        previous = c.account_subscription
        if previous is not None:
            previous.close()
            c._account_subscription = None
        c.reconnect_account_stream_after_reconciliation()
    except (OSError, TimeoutError) as error:
        raise ShutdownPreparationBlocked("SHUTDOWN_ACCOUNT_UNREACHABLE", True) from error
    except BinanceAPIError as error:
        raise ShutdownPreparationBlocked("SHUTDOWN_ACCOUNT_UNREACHABLE", error.status_code >= 500 or error.status_code in (418, 429)) from error
    except Exception as error:
        c._diagnostics.record_exception("shutdown_account_reconciliation", error)
        code = {
            "stream reconnect found an unexplained open order": "SHUTDOWN_UNEXPLAINED_ORDER",
            "stream reconnect found an unexplained recent execution": "SHUTDOWN_UNEXPLAINED_EXECUTION",
            "terminal execution is not exact durable history": "SHUTDOWN_HISTORY_MISMATCH",
            "account stream recovery Position exceeds Binance balance": "SHUTDOWN_BALANCE_MISMATCH",
            "account stream recovery gap Position exceeds Binance balance": "SHUTDOWN_BALANCE_MISMATCH",
            "residual ledger differs from exchange ETH balance": "SHUTDOWN_BALANCE_MISMATCH",
        }.get(str(error), "SHUTDOWN_ACCOUNT_RECONCILIATION_FAILED")
        raise ShutdownPreparationBlocked(code) from error
    operation.check()


def _wait(operation) -> None:
    """
    함수 이름: _wait()
    기능: 기한을 확인하며 다음 주문 확인까지 짧게 기다린다.
    인자: operation -> 해당 작업에 필요한 입력 값
    반환값: 해당 단계의 처리 결과 또는 없음
    작성 날짜: 2026/09/16
    """
    operation.check()
    sleep(0.25)
    operation.check()


def _verify_position_history(c, history, operation) -> None:
    """
    함수 이름: _verify_position_history()
    기능: 저장 거래와 잔여 장부를 별도 포지션에 재생해 메모리 상태와 대조한다.
    인자: c -> 거래 담당자, history -> 거래 기록, operation -> 종료 기한
    반환값: 기록과 포지션이 같으면 없음
    작성 날짜: 2026/09/16
    """
    operation.check()
    try:
        _verify_history(history)
        replayed = Position()
        if c._residual_settlement is not None:
            residual = ResidualSettlement(c._residual_settlement.storage)
            transfers = residual.storage.load()
            step = c._api_gateway.fetch_symbol_trading_rules("ETHUSDT").lot_size.step_size if transfers else None
            residual.restore(replayed, history.trade_history.trades, step)
            if residual.totals != c.residual_totals:
                raise ShutdownPreparationBlocked("SHUTDOWN_HISTORY_MISMATCH")
        else:
            for trade in history.trade_history.trades:
                replayed.apply_historical_trade(trade)
            replayed.require_history_accounting_compatibility()
        current = c._require_position()
        if (replayed.quantity, replayed.cost_basis, replayed.owner) != (current.quantity, current.cost_basis, current.owner):
            raise ShutdownPreparationBlocked("SHUTDOWN_HISTORY_MISMATCH")
    except ShutdownPreparationBlocked:
        raise
    except (OSError, TimeoutError, BinanceAPIError):
        raise
    except Exception as error:
        raise ShutdownPreparationBlocked("SHUTDOWN_HISTORY_MISMATCH") from error


def _retry_reconciliation(c, operation) -> None:
    """
    함수 이름: _retry_reconciliation()
    기능: 일시적인 계좌 조회 실패를 같은 종료 기한 안에서 제한된 간격으로 재확인한다.
    인자: c -> 거래 담당자, operation -> 종료 작업
    반환값: 주문과 잔고 확인이 완료되면 없음
    작성 날짜: 2026/09/16
    """
    attempt = 0
    while True:
        try:
            _reconcile(c, operation)
            return
        except ShutdownPreparationBlocked as error:
            if error.code != "SHUTDOWN_ACCOUNT_UNREACHABLE" or not error.retryable:
                raise
            c._diagnostics.record("shutdown_account_retry", attempt=attempt + 1, reason_code=error.code)
            delay = (1, 2, 5, 10, 30)[min(attempt, 4)]
            attempt += 1
            until = monotonic() + delay
            while monotonic() < until:
                _wait(operation)


def _verify_history(history) -> None:
    """
    함수 이름: _verify_history()
    기능: 기록 손상과 완료되지 않은 저장을 별도 종료 차단 사유로 구분한다.
    인자: history -> 거래 이력 담당자
    반환값: 파일과 게시된 기록이 일치하면 없음
    작성 날짜: 2026/09/16
    """
    try:
        if not history.verify_durable_history():
            raise ShutdownPreparationBlocked("SHUTDOWN_HISTORY_MISMATCH")
    except ShutdownPreparationBlocked:
        raise
    except (OSError, TradeHistoryPersistencePendingError) as error:
        raise ShutdownPreparationBlocked("SHUTDOWN_HISTORY_SAVE_FAILED", True) from error
    except Exception as error:
        raise ShutdownPreparationBlocked("SHUTDOWN_HISTORY_MISMATCH") from error
