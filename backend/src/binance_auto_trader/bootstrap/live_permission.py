"""Live root의 Gateway에 필요한 read surface와 policy-bound mutation permission만 노출한다."""

from decimal import Decimal, localcontext

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.mappers import SymbolFilterError
from binance_auto_trader.adapters.binance.read_facade import BinanceReadOnlyRESTFacade
from binance_auto_trader.bootstrap.live_configuration import (
    LiveConfiguration, LiveConfigurationError, LIVE_NOTIONAL_CAP, LIVE_POLICY_VERSION,
)
from binance_auto_trader.domain.trading.order import Order, OrderResult
from binance_auto_trader.domain.trading.states import OrderSide, ExitReason


class _LiveOrderLimitError(LiveConfigurationError, SymbolFilterError):
    """
    클래스 이름: _LiveOrderLimitError
    기능: 설정 오류와 실제 준비 주문의 금액 한도 위반을 구분한다.
    작성 날짜: 2026/09/16
    """


class LiveOrderPermissionRESTClient(BinanceReadOnlyRESTFacade):
    """
    클래스 이름: LiveOrderPermissionRESTClient
    기능: live read-only Gateway와 정책 version·cap을 확인하는 별도 mutation 경계를 제공한다.
    작성 날짜: 2026/09/08
    """

    def __init__(
        self,
        delegate: object,
        configuration: LiveConfiguration,
        *,
        base_fee_residual_enabled: bool = False,
    ) -> None:
        """
        함수 이름: __init__()
        기능: live validator가 승인한 immutable 설정을 전용 REST delegate와 결속한다.
        인자: delegate -> fixed live REST client, configuration -> 검증된 live 설정,
            base_fee_residual_enabled -> root가 durable 잔여 정책을 조립했는지 여부
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        # Testnet 설정은 모양이 같아도 live 권한으로 사용할 수 없다.
        if type(configuration) is not LiveConfiguration or not configuration.enabled:
            raise LiveConfigurationError("enabled live configuration required")
        if type(base_fee_residual_enabled) is not bool:
            raise TypeError("residual policy flag must be bool")
        self._base_fee_residual_enabled = base_fee_residual_enabled
        self._delegate = delegate
        self._allow_orders = configuration.allow_live_orders
        self._maximum_order_notional = configuration.max_notional

    def _require_spot_fee_policy(self, order: Order) -> None:
        """
        함수 이름: _require_spot_fee_policy()
        기능: 할인 자산 미사용과 편도 0.1% 및 ETH 수수료 잔여 회계를 검증한다.
        인자: order -> 준비 또는 제출할 canonical 주문
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        # 실제 계정 설정이 달라졌다면 추정 수수료로 주문하지 않고 사전에 차단한다.
        commission_policy = APIGateway(
            self._delegate
        ).fetch_commission_discount_policy(order.symbol)
        if commission_policy.can_charge_discount_asset:
            raise LiveConfigurationError(
                "live spot fee policy requires discount asset payment disabled"
            )
        if not commission_policy.matches_spot_fee_policy:
            raise LiveConfigurationError(
                "live spot fee policy requires 0.1% commission on BUY and SELL"
            )
        if order.side is OrderSide.BUY and not self._base_fee_residual_enabled:
            raise LiveConfigurationError(
                "base fee requires durable residual settlement"
            )

    def _require_order_permission(self, order: Order) -> None:
        """
        함수 이름: _require_order_permission()
        기능: order version과 decision cap을 delegate 호출 전에 검증한다.
        인자: order -> canonical 주문
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        # REST와 Controller 중 한쪽의 권한만 열려 있어도 다른 gate를 우회할 수 없다.
        if not self._allow_orders or self._maximum_order_notional != LIVE_NOTIONAL_CAP:
            raise LiveConfigurationError("live orders disabled")
        if (
            not isinstance(order, Order)
            or type(order.risk_policy_version) is not int
            or order.risk_policy_version != LIVE_POLICY_VERSION
        ):
            raise LiveConfigurationError("live order policy version mismatch")
        if order.symbol != "ETHUSDT":
            raise LiveConfigurationError("live pilot symbol mismatch")
        if order.side is OrderSide.SELL and order.exit_reason is ExitReason.STOP:
            return  # 기존 STOP은 Controller가 소유한 정확한 잔여 Position만 정리한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            notional = order.submitted_quantity * order.market_price_at_decision
        if not notional.is_finite() or not Decimal("0") < notional <= LIVE_NOTIONAL_CAP:
            raise LiveConfigurationError("live order decision cap exceeded")

    def prepare_order(self, *, order: Order) -> Order:
        """
        함수 이름: prepare_order()
        기능: 주문 권한 확인 후 live symbol filter 준비를 delegate에 전달한다.
        인자: order -> filter 전 canonical Order
        반환값: filter와 notional cap을 통과한 Order
        작성 날짜: 2026/08/31
        """
        self._require_order_permission(order)
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        prepare_order = getattr(self._delegate, "prepare_order", None)
        if not callable(prepare_order):
            raise TypeError("delegate must provide prepare_order")

        # Filter adapter가 mutable Order를 돌려주더라도 evaluation 가격과 intent provenance를 원본으로 고정한다.
        immutable_provenance_fields = (
            "intent_id",
            "client_order_id",
            "submission_attempt",
            "symbol",
            "side",
            "strategy",
            "regime_type",
            "requested_quantity",
            "market_price_at_decision",
            "risk_policy_version",
            "exit_reason",
            "exit_pct_b_at_intent",
        )
        immutable_provenance = tuple(
            getattr(order, field_name)
            for field_name in immutable_provenance_fields
        )
        decision_price = order.market_price_at_decision  # cap 계산은 이후 mutable 객체가 아닌 event claim을 쓴다.

        # BNB 가격이나 잔액 대신 공식 비할인 현물 수수료 정책만 검사한다.
        self._require_spot_fee_policy(order)

        prepared_order = prepare_order(order=order)
        if not isinstance(prepared_order, Order):
            raise TypeError("prepare_order must return an Order")
        if any(
            getattr(prepared_order, field_name) != original_value
            for field_name, original_value in zip(
                immutable_provenance_fields,
                immutable_provenance,
                strict=True,
            )
        ):
            raise LiveConfigurationError(
                "prepared live order changed immutable decision provenance"
            )

        # STOP/recovery SELL은 이번 run의 authoritative Position을 닫는 경로이므로 BUY cap에서만 제외한다.
        if (
            prepared_order.side is OrderSide.SELL
            and prepared_order.exit_reason is ExitReason.STOP
        ):
            self._require_spot_fee_policy(prepared_order)
            return prepared_order

        final_quantity = prepared_order.submitted_quantity
        if (
            not isinstance(final_quantity, Decimal)
            or not final_quantity.is_finite()
            or final_quantity <= Decimal("0")
        ):
            raise LiveConfigurationError(
                "prepared live order requires a positive finite quantity"
            )

        # Decimal128 정밀도로 filter 후 최종 notional을 설정 cap과 절대 10 USDT 모두에 대조한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            final_notional = final_quantity * decision_price
        configured_cap = self._maximum_order_notional
        if configured_cap is None:
            raise LiveConfigurationError(
                "enabled live orders require a configured notional cap"
            )
        if (
            final_notional > configured_cap
            or final_notional > LIVE_NOTIONAL_CAP
        ):
            raise _LiveOrderLimitError(
                "prepared live order exceeds the configured or absolute cap"
            )

        # 마지막 읽기 검증도 journal 전에 끝내 조회 실패·종료 요청을 미제출 상태로 처리한다.
        self._require_spot_fee_policy(prepared_order)
        return prepared_order  # journal 이전 마지막 bootstrap 경계가 final 수량과 immutable 가격을 결속한다.

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: 추가 조회 없이 version·cap을 재검증하고 준비 완료 주문만 REST에 전달한다.
        인자: order -> durable journal에 기록된 주문
        반환값: 정규화 주문 결과
        작성 날짜: 2026/09/08
        """
        # 수수료 조회는 journal 전 prepare에서 끝내고 여기서는 로컬 권한만 재검증한다.
        self._require_order_permission(order)
        return self._delegate.submit_order(order=order)

    def cancel_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: cancel_order()
        기능: 별도 live 권한이 있을 때만 기존 주문 취소를 전달한다.
        인자: order -> 기존 pending 주문
        반환값: 정규화 취소 결과
        작성 날짜: 2026/09/08
        """
        # Read-only에서는 cancel도 금지하며 신규 주문 identity를 만들지 않는다.
        self._require_order_permission(order)
        return self._delegate.cancel_order(order=order)
