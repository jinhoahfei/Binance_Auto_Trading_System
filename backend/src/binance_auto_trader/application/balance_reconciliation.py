"""전략 원금, 잔여 원금과 Earn 보관·보상을 같은 ETH 대조 결과로 묶는다."""

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, localcontext
from threading import get_ident

from binance_auto_trader.adapters.binance.spot_rest_client import BinanceAPIError
from binance_auto_trader.domain.trading.residual import history_digest
from binance_auto_trader.domain.trading.account import SUPPORTED_VALUATION_ASSET


class BalanceObservationChangedError(RuntimeError):
    """조회 중 계좌·근거가 달라져 새 대조가 필요한 경우다."""


@dataclass(frozen=True, slots=True)
class BalanceReconciliation:
    """원본 장부를 고치지 않는, 특정 계좌·이력 버전의 대조 영수증이다."""

    status: str
    position_quantity: Decimal
    residual_principal_quantity: Decimal
    earn_rewards_quantity: Decimal
    earn_quantity: Decimal
    expected_spot_quantity: Decimal
    exchange_spot_quantity: Decimal
    difference_quantity: Decimal
    checked_at: datetime
    reason_code: str | None
    retryable: bool
    basis: tuple

    def snapshot(self, *, current: bool = True) -> dict:
        """Decimal을 문자열로 보존하며 내부 버전·이력 결속은 외부에 노출하지 않는다."""
        return {
            "asset": "ETH",
            "status": self.status if current else "stale",
            **{name: format(getattr(self, name), "f") for name in (
                "position_quantity", "residual_principal_quantity", "earn_rewards_quantity",
                "earn_quantity", "expected_spot_quantity", "exchange_spot_quantity", "difference_quantity",
            )},
            "checked_at": self.checked_at.isoformat(),
            "reason_code": self.reason_code if current else "BALANCE_OBSERVATION_CHANGED",
            "retryable": self.retryable if current else True,
        }


def balance_basis(c) -> tuple:
    """시세·화면 상태와 별개로 실제 잔고·포지션·장부·소유권 변경을 감지한다."""
    position = c._require_position()
    history = c._require_trade_history_controller().trade_history.trades
    transfers = () if c._residual_settlement is None else c._residual_settlement.transfers
    return (c._account.version, position.quantity, position.cost_basis, position.owner,
            history_digest(history), transfers, c._process_ownership_ambiguous,
            c._external_execution_reconciliation_required)


def current_balance_snapshot(c) -> dict | None:
    """지난 확인 이후 변경된 수량을 현재 검증 결과로 표시하지 않는다."""
    receipt = c._balance_reconciliation
    return None if receipt is None else receipt.snapshot(current=receipt.basis == balance_basis(c))


def reconcile_balance(c, *, strict: bool = False, use_cached: bool = False) -> BalanceReconciliation | None:
    """시작·재연결·종료가 동일한 공식 수량과 Earn 근거를 검증한다."""
    from .trading_controller import ResidualBalanceMismatchError

    position = c._require_position()
    principal = c.residual_totals[0]
    exchange = c._account.get_holdings("ETH")
    # 잔여 장부가 없는 기존 비전용/testnet 계좌의 시작 정책은 보존한다.
    if not strict and principal == 0 and (c._residual_settlement is None or not c._residual_settlement.transfers):
        return None
    basis = balance_basis(c)
    prior = c._balance_reconciliation
    if use_cached and prior is not None and prior.status == "verified" and prior.basis == basis:
        _check_authority(c)
        return prior
    with localcontext() as context:
        context.prec = 34
        expected = position.quantity + principal
        receipt = BalanceReconciliation("mismatch", position.quantity, principal, Decimal("0"), Decimal("0"),
            expected, exchange, exchange - expected, c._clock(), "EARN_EVIDENCE_MISSING", False, basis)

    def publish(value):
        c._balance_reconciliation = value
        c._record_diagnostic("balance_reconciliation_checked", **value.snapshot())
        return value

    def mismatch(code):
        publish(replace(receipt, status="mismatch", reason_code=code))
        c._record_diagnostic("residual_balance_mismatch", level="ERROR", position_quantity=position.quantity,
            residual_quantity=principal, expected_quantity=receipt.expected_spot_quantity, exchange_quantity=exchange)
        raise ResidualBalanceMismatchError("residual ledger differs from exchange ETH balance")

    try:
        _check_authority(c)
        if position.quantity > exchange:
            mismatch("POSITION_EXCEEDS_BALANCE")
        if exchange != expected:
            settlement = c._residual_settlement
            if settlement is None or not settlement.transfers:
                mismatch("EARN_EVIDENCE_MISSING")
            history = c._require_trade_history_controller().trade_history.trades
            since = history[settlement.transfers[0].history_count - 1].executed_at
            evidence = c._api_gateway.fetch_earn_residual_evidence(since)
            _check_authority(c)
            if evidence is None or not settlement.matches_earn_custody(evidence, history):
                mismatch("EARN_EVIDENCE_MISSING")
            with localcontext() as context:
                context.prec = 34
                expected = position.quantity + principal + evidence.rewards - evidence.quantity
                receipt = replace(receipt, earn_rewards_quantity=evidence.rewards, earn_quantity=evidence.quantity,
                    expected_spot_quantity=expected, difference_quantity=exchange - expected)
            if exchange != expected:
                mismatch("BALANCE_UNEXPLAINED")
            first = c._api_gateway.fetch_account_snapshot(SUPPORTED_VALUATION_ASSET)
            _check_authority(c)
            repeated = c._api_gateway.fetch_earn_residual_evidence(since)
            _check_authority(c)
            confirmed = c._api_gateway.fetch_account_snapshot(SUPPORTED_VALUATION_ASSET)
            _check_authority(c)
            first_eth = tuple(balance for balance in first.balances if balance.asset == "ETH")
            confirmed_eth = tuple(balance for balance in confirmed.balances if balance.asset == "ETH")
            if (repeated != evidence or len(confirmed_eth) != 1 or first_eth != confirmed_eth
                    or confirmed_eth[0].total != exchange or balance_basis(c) != basis):
                raise BalanceObservationChangedError("balance evidence changed during verification")
            if not _shutdown_owner(c) and (not c._web_socket_gateway.account_ready or c._event_runtime_failed):
                raise BalanceObservationChangedError("account stream is not ready")
            c._record_diagnostic("residual_custody_reconciled", custody="spot" if evidence.quantity == 0 else "simple_earn",
                residual_quantity=principal, earn_quantity=evidence.quantity, earn_rewards=evidence.rewards, spot_quantity=exchange)
        _check_authority(c)
        if balance_basis(c) != basis:
            raise BalanceObservationChangedError("account changed during verification")
        return publish(replace(receipt, status="verified", reason_code=None, checked_at=c._clock()))
    except BalanceObservationChangedError:
        publish(replace(receipt, status="stale", reason_code="BALANCE_OBSERVATION_CHANGED", retryable=True))
        raise
    except (OSError, TimeoutError, BinanceAPIError) as error:
        retryable = not isinstance(error, BinanceAPIError) or error.status_code >= 500 or error.status_code in (418, 429)
        publish(replace(receipt, status="unavailable", reason_code="EARN_ACCOUNT_UNREACHABLE", retryable=retryable))
        raise  # 조회 실패를 영구적인 수량 불일치로 바꾸지 않는다.
    except ValueError as error:
        c._diagnostics.record_exception("residual_earn_reconciliation", error)
        mismatch("EARN_EVIDENCE_INVALID")


def _shutdown_owner(c) -> bool:
    """종료 중 연결 조건 예외는 해당 종료 담당 스레드에만 적용한다."""
    return c._shutdown_preparing and c._shutdown_cleanup_owner == get_ident()


def _check_authority(c) -> None:
    """각 조회 뒤 기한과 소유권을 다시 확인해 늦은 응답의 승인을 막는다."""
    if c._shutdown_cleanup_check is not None:
        c._shutdown_cleanup_check()
    if c._process_ownership_ambiguous or c._external_execution_reconciliation_required:
        raise BalanceObservationChangedError("account ownership requires reconciliation")
