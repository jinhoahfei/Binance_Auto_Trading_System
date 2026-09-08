"""Live root의 Gateway에 필요한 read surface와 policy-bound mutation permission만 노출한다."""

from decimal import Decimal, localcontext
from datetime import datetime, timezone
from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.read_facade import BinanceReadOnlyRESTFacade
from binance_auto_trader.bootstrap.live_configuration import (
    LiveConfiguration, LiveConfigurationError, LIVE_NOTIONAL_CAP, LIVE_POLICY_VERSION,
)
from binance_auto_trader.domain.trading.order import Order, OrderResult
from binance_auto_trader.domain.trading.states import OrderSide, ExitReason

_ACCOUNTING_SUPPORTED_FEE_ASSETS = frozenset({"ETH", "USDT"})


class LiveOrderPermissionRESTClient(BinanceReadOnlyRESTFacade):
    """
    클래스 이름: LiveOrderPermissionRESTClient
    기능: live read-only Gateway와 정책 version·cap을 확인하는 별도 mutation 경계를 제공한다.
    작성 날짜: 2026/09/08
    """

    def __init__(self, delegate: object, configuration: LiveConfiguration, *, base_fee_residual_enabled: bool = False, bnb_fee_accounting_enabled: bool = False) -> None:
        """
        함수 이름: __init__()
        기능: live validator가 승인한 immutable 설정을 전용 REST delegate와 결속한다.
        인자: delegate -> fixed live REST client, configuration -> 검증된 live 설정,
            base_fee_residual_enabled -> root가 durable 잔여 정책을 조립했는지 여부
            bnb_fee_accounting_enabled -> v3 fill 회계와 WS 평가를 결속했는지 여부
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        # Testnet 설정은 모양이 같아도 live 권한으로 사용할 수 없다.
        if type(configuration) is not LiveConfiguration or not configuration.enabled:
            raise LiveConfigurationError("enabled live configuration required")
        if type(base_fee_residual_enabled) is not bool:
            raise TypeError("residual policy flag must be bool")
        if type(bnb_fee_accounting_enabled) is not bool:
            raise TypeError("BNB accounting flag must be bool")
        if bnb_fee_accounting_enabled and not callable(getattr(delegate, "resolve_bnb_fee", None)):
            raise TypeError("BNB accounting requires a valuation resolver")
        self._bnb_fee_accounting_enabled = bnb_fee_accounting_enabled
        self._base_fee_residual_enabled = base_fee_residual_enabled
        self._delegate = delegate
        self._allow_orders = configuration.allow_live_orders
        self._maximum_order_notional = configuration.max_notional

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

        # 공식 signed commission 설정을 매 attempt 전에 확인해 제3 자산 fill을 주문 전에 막는다.
        commission_policy = APIGateway(
            self._delegate
        ).fetch_commission_discount_policy(order.symbol)
        if (
            commission_policy.can_charge_discount_asset
            and commission_policy.discount_asset
            not in _ACCOUNTING_SUPPORTED_FEE_ASSETS
            and not (commission_policy.discount_asset == "BNB" and self._bnb_fee_accounting_enabled)
        ):
            raise LiveConfigurationError(
                "live commission policy permits an unsupported fee asset"
            )

        # BNB 정책은 가격 근거 조회도 제출 전에 검사하되 실제 비용은 체결 시각으로 다시 평가한다.
        if commission_policy.can_charge_discount_asset and commission_policy.discount_asset == "BNB":
            self._delegate.resolve_bnb_fee(datetime.now(timezone.utc))

        # ETH BUY fee는 live root의 durable sub-step 잔여 장부가 보존한다.
        # BNB 등 제3 자산 차단은 위에서 유지하며 Testnet 정책에는 적용하지 않는다.
        if order.side is OrderSide.BUY and commission_policy.market_buy_received_asset_commission_rate > 0 and not self._base_fee_residual_enabled:
            raise LiveConfigurationError("base fee requires durable residual settlement")

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
            raise LiveConfigurationError(
                "prepared live order exceeds the configured or absolute cap"
            )

        return prepared_order  # journal 이전 마지막 bootstrap 경계가 final 수량과 immutable 가격을 결속한다.

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: version·cap 재검증 뒤 준비 완료 주문만 REST에 전달한다.
        인자: order -> durable journal에 기록된 주문
        반환값: 정규화 주문 결과
        작성 날짜: 2026/09/08
        """
        # Prepare 이후 mutation이나 policy drift도 최종 submit 전에 거부한다.
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
