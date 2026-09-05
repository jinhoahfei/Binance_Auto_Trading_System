"""명시적 환경 opt-in만으로 Binance Spot Testnet backend runtime을 조립한다."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import os
from threading import RLock

from binance_auto_trader.adapters.binance.api_gateway import (
    APIGateway,
    DEFAULT_KLINE_LIMIT,
    Phase13OrderSubmissionAttempt,
    Phase13OrderSubmissionGuardSnapshot,
)
from binance_auto_trader.adapters.binance.mappers import (
    OrderPreparationFilterEvidence,
    OrderSubmissionAttemptEvidence,
    ReferencePrice,
    SymbolTradingRules,
)
from binance_auto_trader.application import TradingController
from binance_auto_trader.bootstrap.application import (
    ApplicationRuntime,
    ExecutionMode,
    _TESTNET_ORDER_CAPABILITY,
    create_application_runtime,
)
from binance_auto_trader.domain.history import Performance, Trade
from binance_auto_trader.domain.trading import (
    Account,
    RiskPolicy,
    RiskPolicyUnavailable,
)
from binance_auto_trader.domain.trading.order import Order, OrderResult
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderSide,
    StrategyType,
)


BINANCE_RUN_TESTNET_ENV = "BINANCE_RUN_TESTNET"
BINANCE_TESTNET_API_KEY_ENV = "BINANCE_TESTNET_API_KEY"
BINANCE_TESTNET_API_SECRET_ENV = "BINANCE_TESTNET_API_SECRET"
BINANCE_RUN_TESTNET_ORDERS_ENV = "BINANCE_RUN_TESTNET_ORDERS"
BINANCE_TESTNET_MAX_NOTIONAL_ENV = "BINANCE_TESTNET_MAX_NOTIONAL"
BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV = "BINANCE_RUN_PHASE13_PUBLIC_CASE2"
BINANCE_RUN_PHASE13_RECOVERY_ONLY_ENV = (
    "BINANCE_RUN_PHASE13_RECOVERY_ONLY"
)
BINANCE_TESTNET_ABSOLUTE_MAX_NOTIONAL = Decimal("100")
BINANCE_PHASE13_PUBLIC_CASE2_MAX_NOTIONAL = Decimal("10")
BINANCE_SPOT_TESTNET_REST_ORIGIN = "https://testnet.binance.vision"
BINANCE_SPOT_TESTNET_STREAM_ORIGIN = "wss://stream.testnet.binance.vision"
BINANCE_SPOT_TESTNET_WEBSOCKET_API_URL = (
    "wss://ws-api.testnet.binance.vision/ws-api/v3"
)
_ACCOUNTING_SUPPORTED_FEE_ASSETS = frozenset({"ETH", "USDT"})


class TestnetConfigurationError(RuntimeError):
    """
    클래스 이름: TestnetConfigurationError
    기능: testnet opt-in이나 credential·주문 상한 설정이 fail-closed 계약을 위반함을 나타낸다.
    작성 날짜: 2026/08/22
    """

    code = "TESTNET_CONFIGURATION_INVALID"


class _TestnetOrderPermissionRESTClient:
    """
    클래스 이름: _TestnetOrderPermissionRESTClient
    기능: read-only runtime의 공개 Gateway에서도 주문 prepare·submit·cancel을 network 전에 차단한다.
    작성 날짜: 2026/08/22
    """

    __slots__ = (
        "_allow_orders",
        "_clock",
        "_delegate",
        "_maximum_order_notional",
        "_phase13_attempts",
        "_phase13_guard_lock",
        "_phase13_public_case2",
        "_phase13_recovery_only",
        "_phase13_submission_in_progress",
        "_phase13_submissions_blocked",
    )

    def __init__(
        self,
        delegate: object,
        *,
        allow_orders: bool,
        maximum_order_notional: Decimal | None,
        phase13_public_case2: bool = False,
        phase13_recovery_only: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 고정 testnet REST client와 검증된 주문 권한을 보존한다.
        인자: delegate -> 실제 read/order REST operation을 가진 testnet client
            allow_orders -> 환경 이중 opt-in 완료 여부
            maximum_order_notional -> 주문 권한에 결속된 quote cap 또는 read-only None
            phase13_public_case2 -> 전용 BUY 1회와 STOP SELL 1회 guard 활성 여부
            phase13_recovery_only -> 기존 open BUY의 STOP SELL 1회만 허용할지 여부
            clock -> secret-free logical submit UTC 시각 provider 또는 None
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        # delegate가 최소 read operation을 가져야 runtime Gateway 조립이 의미를 갖는다.
        if delegate is None or not any(
            callable(getattr(delegate, operation_name, None))
            for operation_name in ("get_account", "get_klines", "submit_order")
        ):
            raise TypeError("delegate must provide a Binance REST operation")
        if type(allow_orders) is not bool:
            raise TypeError("allow_orders must be a bool")
        if type(phase13_public_case2) is not bool:
            raise TypeError("phase13_public_case2 must be a bool")
        if type(phase13_recovery_only) is not bool:
            raise TypeError("phase13_recovery_only must be a bool")
        if phase13_public_case2 and phase13_recovery_only:
            raise TestnetConfigurationError(
                "Phase 13 mutation targets must be mutually exclusive"
            )
        if (phase13_public_case2 or phase13_recovery_only) and not allow_orders:
            raise TestnetConfigurationError(
                "Phase 13 submission guards require enabled testnet orders"
            )
        selected_clock = (
            (lambda: datetime.now(timezone.utc))
            if clock is None
            else clock
        )
        if not callable(selected_clock):
            raise TypeError("clock must be callable or None")
        if allow_orders and (
            not isinstance(maximum_order_notional, Decimal)
            or not maximum_order_notional.is_finite()
            or maximum_order_notional <= Decimal("0")
            or maximum_order_notional
            > BINANCE_TESTNET_ABSOLUTE_MAX_NOTIONAL
        ):
            raise TestnetConfigurationError(
                "enabled testnet orders require a finite cap no greater than 100"
            )
        if (
            phase13_public_case2
            and maximum_order_notional is not None
            and maximum_order_notional
            > BINANCE_PHASE13_PUBLIC_CASE2_MAX_NOTIONAL
        ):
            raise TestnetConfigurationError(
                "Phase 13 public Case 2 max notional must be no greater than 10"
            )
        if not allow_orders and maximum_order_notional is not None:
            raise TestnetConfigurationError(
                "read-only testnet configuration must not carry an order cap"
            )
        self._delegate = delegate
        self._maximum_order_notional = maximum_order_notional
        self._allow_orders = allow_orders  # upward 권한 변경 operation은 제공하지 않는다.

        # Phase 13 전용 clock, lock과 permit state는 legacy proxy 동작과 분리해 초기화한다.
        self._clock = selected_clock
        self._phase13_public_case2 = phase13_public_case2
        self._phase13_recovery_only = phase13_recovery_only
        self._phase13_guard_lock = RLock()
        self._phase13_attempts: list[Phase13OrderSubmissionAttempt] = []
        self._phase13_submission_in_progress = False
        self._phase13_submissions_blocked = False

    def __repr__(self) -> str:
        """
        함수 이름: __repr__()
        기능: delegate credential 없이 주문 gate 상태만 반환한다.
        인자: 없음
        반환값: secret-safe proxy 표현
        작성 날짜: 2026/08/22
        """
        return (
            "_TestnetOrderPermissionRESTClient("
            f"allow_orders={self._allow_orders!r})"
        )  # delegate repr도 포함하지 않아 credential 구현 변화와 무관하게 안전하다.

    def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> object:
        """
        함수 이름: get_klines()
        기능: 허용된 공개 market 조회만 고정 testnet delegate에 전달한다.
        인자: symbol -> 조회할 Spot symbol
            interval -> 공식 Kline interval
            limit -> 반환할 최대 Kline 개수
        반환값: 공식 Kline payload
        작성 날짜: 2026/08/23
        """
        # Read-only proxy는 명시한 operation만 노출해 delegate의 raw request와 credential을 숨긴다.
        return self._delegate.get_klines(
            symbol=symbol,
            interval=interval,
            limit=limit,
        )

    def get_account(self) -> object:
        """
        함수 이름: get_account()
        기능: 허용된 signed account 조회를 고정 testnet delegate에 전달한다.
        인자: 없음
        반환값: 공식 Spot account payload
        작성 날짜: 2026/08/23
        """
        # Account 조회는 주문 mutation이 아니며 APIGateway의 정규화 경계를 그대로 통과한다.
        return self._delegate.get_account()

    def get_account_commission(self, *, symbol: str) -> object:
        """
        함수 이름: get_account_commission()
        기능: 허용된 signed account commission 조회를 고정 testnet delegate에 전달한다.
        인자: symbol -> 수수료 정책을 조회할 canonical Spot symbol
        반환값: 공식 Spot account commission payload
        작성 날짜: 2026/08/24
        """
        # 수수료 자산 preflight는 주문 mutation 전에 제3 자산 가능성을 차단하는 read-only 조회다.
        get_account_commission = getattr(
            self._delegate,
            "get_account_commission",
            None,
        )
        if not callable(get_account_commission):
            raise TypeError("delegate must provide get_account_commission")

        return get_account_commission(symbol=symbol)  # Raw 응답은 APIGateway만 정규화한다.

    def get_account_filters(self, *, symbol: str) -> object:
        """
        함수 이름: get_account_filters()
        기능: 허용된 signed myFilters 계정 조회를 고정 testnet delegate에 전달한다.
        인자: symbol -> 계정 MAX_ASSET를 조회할 canonical Spot symbol
        반환값: 공식 Spot myFilters payload
        작성 날짜: 2026/08/31
        """
        get_account_filters = getattr(
            self._delegate,
            "get_account_filters",
            None,
        )
        if not callable(get_account_filters):
            raise TypeError("delegate must provide get_account_filters")

        # USER_DATA read는 주문 mutation gate와 무관하며 raw 응답은 APIGateway에서만 해석한다.
        return get_account_filters(symbol=symbol)  # Proxy는 signed payload를 저장하거나 출력하지 않는다.

    def has_any_exchange_open_orders(self) -> bool:
        """
        함수 이름: has_any_exchange_open_orders()
        기능: all-symbol signed openOrders의 non-empty 여부를 read-only로 전달한다.
        인자: 없음
        반환값: account 전체 open order가 하나라도 있으면 True
        작성 날짜: 2026/08/31
        """
        # Delegate capability와 exact bool 반환을 모두 확인해 누락된 reader를 empty로 간주하지 않는다.
        read_open_state = getattr(
            self._delegate,
            "has_any_exchange_open_orders",
            None,
        )
        if not callable(read_open_state):
            raise TypeError(
                "delegate must provide has_any_exchange_open_orders"
            )
        has_open_orders = read_open_state()
        if type(has_open_orders) is not bool:
            raise TypeError("exchange open order state must be a bool")

        return has_open_orders  # Permission proxy는 ID나 raw response를 새로 노출하지 않는다.

    def has_any_exchange_open_order_lists(self) -> bool:
        """
        함수 이름: has_any_exchange_open_order_lists()
        기능: all-symbol signed openOrderList의 non-empty 여부를 read-only로 전달한다.
        인자: 없음
        반환값: account 전체 open order list가 하나라도 있으면 True
        작성 날짜: 2026/08/31
        """
        # Order-list 전용 reader의 부재나 type drift는 read-only 성공으로 완화하지 않는다.
        read_open_list_state = getattr(
            self._delegate,
            "has_any_exchange_open_order_lists",
            None,
        )
        if not callable(read_open_list_state):
            raise TypeError(
                "delegate must provide has_any_exchange_open_order_lists"
            )
        has_open_order_lists = read_open_list_state()
        if type(has_open_order_lists) is not bool:
            raise TypeError("exchange open order list state must be a bool")

        return has_open_order_lists  # Order-list payload는 memory bool로만 축약한다.

    def fetch_reference_price(
        self,
        *,
        symbol: str,
    ) -> ReferencePrice:
        """
        함수 이름: fetch_reference_price()
        기능: 주문 권한과 무관하게 최신 public reference price 조회를 delegate에 전달한다.
        인자: symbol -> 조회할 canonical Spot symbol
        반환값: delegate가 엄격 해석한 ReferencePrice
        작성 날짜: 2026/08/31
        """
        fetch_reference_price = getattr(
            self._delegate,
            "fetch_reference_price",
            None,
        )
        if not callable(fetch_reference_price):
            raise TypeError("delegate must provide fetch_reference_price")

        # Public GET은 mutation 권한을 열지 않으며 strict DTO 외의 반환을 proxy에서 차단한다.
        reference_price = fetch_reference_price(symbol=symbol)
        if type(reference_price) is not ReferencePrice:
            raise TypeError(
                "fetch_reference_price must return ReferencePrice"
            )

        return reference_price  # 검증된 public DTO만 mutation 권한과 무관하게 전달한다.

    def fetch_symbol_trading_rules(
        self,
        *,
        symbol: str,
    ) -> SymbolTradingRules:
        """
        함수 이름: fetch_symbol_trading_rules()
        기능: 주문 권한과 무관하게 최신 public Spot symbol rule 조회를 delegate에 전달한다.
        인자: symbol -> 조회할 canonical Spot symbol
        반환값: delegate가 새 exchangeInfo로 해석한 SymbolTradingRules
        작성 날짜: 2026/08/31
        """
        fetch_symbol_trading_rules = getattr(
            self._delegate,
            "fetch_symbol_trading_rules",
            None,
        )
        if not callable(fetch_symbol_trading_rules):
            raise TypeError(
                "delegate must provide fetch_symbol_trading_rules"
            )

        # Public GET은 mutation gate를 거치지 않지만 raw private state를 대신 노출하지 않는다.
        rules = fetch_symbol_trading_rules(symbol=symbol)
        if type(rules) is not SymbolTradingRules:
            raise TypeError(
                "fetch_symbol_trading_rules must return SymbolTradingRules"
            )

        return rules

    def get_order_preparation_filter_evidence(
        self,
        *,
        client_order_id: str,
    ) -> OrderPreparationFilterEvidence | None:
        """
        함수 이름: get_order_preparation_filter_evidence()
        기능: 이미 성공한 prepare의 public filter provenance를 mutation 권한 없이 전달한다.
        인자: client_order_id -> 준비된 application Order identity
        반환값: immutable filter evidence 또는 해당 prepare가 없으면 None
        작성 날짜: 2026/08/31
        """
        if (
            not isinstance(client_order_id, str)
            or not client_order_id
            or client_order_id != client_order_id.strip()
        ):
            raise ValueError("client_order_id must be non-empty canonical text")
        get_filter_evidence = getattr(
            self._delegate,
            "get_order_preparation_filter_evidence",
            None,
        )
        if not callable(get_filter_evidence):
            raise TypeError(
                "delegate must provide get_order_preparation_filter_evidence"
            )

        # 과거 prepare 증거 조회는 POST 권한을 열지 않으며 raw client cache도 반환하지 않는다.
        evidence = get_filter_evidence(client_order_id=client_order_id)
        if evidence is None:
            return None
        if type(evidence) is not OrderPreparationFilterEvidence:
            raise TypeError(
                "filter evidence must be an OrderPreparationFilterEvidence"
            )
        if evidence.client_order_id != client_order_id:
            raise ValueError("filter evidence does not match requested order")

        return evidence

    def get_order_submission_attempt_evidence(
        self,
        *,
        client_order_id: str,
    ) -> OrderSubmissionAttemptEvidence | None:
        """
        함수 이름: get_order_submission_attempt_evidence()
        기능: 이미 시작한 REST submission의 safe timestamp evidence를 mutation 권한 없이 전달한다.
        인자: client_order_id -> 제출을 시작한 application Order identity
        반환값: immutable submission evidence 또는 POST 시작 전이면 None
        작성 날짜: 2026/08/31
        """
        if (
            not isinstance(client_order_id, str)
            or not client_order_id
            or client_order_id != client_order_id.strip()
        ):
            raise ValueError("client_order_id must be non-empty canonical text")
        get_submission_evidence = getattr(
            self._delegate,
            "get_order_submission_attempt_evidence",
            None,
        )
        if not callable(get_submission_evidence):
            raise TypeError(
                "delegate must provide get_order_submission_attempt_evidence"
            )

        # 주문 권한은 새로운 POST에만 적용하고 완료된 submission의 safe provenance 조회는 read-only다.
        evidence = get_submission_evidence(client_order_id=client_order_id)
        if evidence is None:
            return None
        if type(evidence) is not OrderSubmissionAttemptEvidence:
            raise TypeError(
                "submission evidence must be an OrderSubmissionAttemptEvidence"
            )
        if evidence.client_order_id != client_order_id:
            raise ValueError("submission evidence does not match requested order")

        return evidence

    def query_order_result(self, *, order: Order) -> OrderResult:
        """
        함수 이름: query_order_result()
        기능: 신규 제출 없이 같은 주문의 signed 상태 조회를 delegate에 전달한다.
        인자: order -> 조회할 기존 canonical Order
        반환값: 정규화된 같은 주문 OrderResult
        작성 날짜: 2026/08/23
        """
        # Reconciliation 조회는 read-only mode에서도 필요하지만 새 client ID나 POST를 만들지 않는다.
        return self._delegate.query_order_result(order=order)

    def list_open_order_results(
        self,
        *,
        symbol: str,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_open_order_results()
        기능: 허용된 open-order 조회를 고정 testnet delegate에 전달한다.
        인자: symbol -> 조회할 Spot symbol
        반환값: 정규화된 open OrderResult tuple
        작성 날짜: 2026/08/23
        """
        # Startup과 reconnect가 외부 주문을 설명하는 데 필요한 조회 surface만 공개한다.
        return self._delegate.list_open_order_results(symbol=symbol)

    def list_all_open_order_results(
        self,
        *,
        symbol: str,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_all_open_order_results()
        기능: 새 주문 없이 account isolation용 전체 client ID open-order 조회를 전달한다.
        인자: symbol -> 조회할 Spot symbol
        반환값: 정규화된 전체 open OrderResult tuple
        작성 날짜: 2026/08/31
        """
        list_all_open_results = getattr(
            self._delegate,
            "list_all_open_order_results",
            None,
        )
        if not callable(list_all_open_results):
            raise TypeError("delegate must provide all-open-order query")

        # Read-only/actual permission은 POST만 나누며 격리 확인 GET은 두 mode에서 동일하게 허용한다.
        return list_all_open_results(symbol=symbol)  # Prefix 없는 결과를 변경 없이 Gateway 검증에 넘긴다.

    def list_recent_order_results(
        self,
        *,
        symbol: str,
        limit: int = 100,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_recent_order_results()
        기능: 허용된 recent-order 조회를 고정 testnet delegate에 전달한다.
        인자: symbol -> 조회할 Spot symbol
            limit -> 반환할 최근 주문 최대 개수
        반환값: 정규화된 recent OrderResult tuple
        작성 날짜: 2026/08/23
        """
        # Durable history 이후 누락 execution 탐지에 필요한 bounded 조회만 위임한다.
        return self._delegate.list_recent_order_results(
            symbol=symbol,
            limit=limit,
        )

    def list_all_recent_order_results(
        self,
        *,
        symbol: str,
        limit: int = 100,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_all_recent_order_results()
        기능: 새 주문 없이 account isolation용 전체 client ID recent-order 조회를 전달한다.
        인자: symbol -> 조회할 Spot symbol
            limit -> 반환할 최근 주문 최대 개수
        반환값: 정규화된 최근 전체 OrderResult tuple
        작성 날짜: 2026/08/31
        """
        list_all_recent_results = getattr(
            self._delegate,
            "list_all_recent_order_results",
            None,
        )
        if not callable(list_all_recent_results):
            raise TypeError("delegate must provide all-recent-order query")

        # Full-account delta GET은 permission flag를 바꾸지 않고 exact symbol/limit만 위임한다.
        return list_all_recent_results(
            symbol=symbol,
            limit=limit,
        )  # Exact symbol·limit의 read-only 결과만 호출자에게 전달한다.

    def _require_order_permission(self) -> None:
        """
        함수 이름: _require_order_permission()
        기능: read-only runtime의 mutation 시도를 typed 설정 오류로 차단한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        if not self._allow_orders:
            raise TestnetConfigurationError(
                "testnet order operation requires the separate order opt-in"
            )  # delegate method를 찾기 전에 막아 network side effect를 만들지 않는다.

    def block_phase13_order_submissions(self) -> None:
        """
        함수 이름: block_phase13_order_submissions()
        기능: Phase 13 failure finalizer가 모든 후속 logical submit permit을 원자 폐쇄한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        if not (
            self._phase13_public_case2 or self._phase13_recovery_only
        ):
            raise TestnetConfigurationError(
                "Phase 13 submission blocking requires a dedicated opt-in"
            )

        # 이미 시작한 delegate 호출은 취소하지 않되 이 lock 이후 새 호출은 하나도 승인하지 않는다.
        with self._phase13_guard_lock:
            self._phase13_submissions_blocked = True

    def get_phase13_order_submission_guard_snapshot(
        self,
    ) -> Phase13OrderSubmissionGuardSnapshot:
        """
        함수 이름: get_phase13_order_submission_guard_snapshot()
        기능: Phase 13 mutation 시작·차단과 permit을 소비한 최소 주문 identity를 반환한다.
        인자: 없음
        반환값: credential과 raw parameter가 없는 frozen guard snapshot
        작성 날짜: 2026/08/31
        """
        if not (
            self._phase13_public_case2 or self._phase13_recovery_only
        ):
            raise TestnetConfigurationError(
                "Phase 13 submission snapshot requires a dedicated opt-in"
            )

        # Mutable 내부 목록을 lock 아래 tuple로 복제해 다른 thread의 submit과 혼합되지 않게 한다.
        with self._phase13_guard_lock:
            attempts = tuple(self._phase13_attempts)
            submissions_blocked = self._phase13_submissions_blocked

        return Phase13OrderSubmissionGuardSnapshot(
            mutation_started=bool(attempts),
            submissions_blocked=submissions_blocked,
            attempts=attempts,
        )

    def _begin_phase13_order_submission(self, order: Order) -> bool:
        """
        함수 이름: _begin_phase13_order_submission()
        기능: 선택된 Phase 13 target의 exact 주문 shape와 단일 submit permit을 원자 검증한다.
        인자: order -> delegate 호출 직전의 durable canonical Order
        반환값: Phase 13 guard가 활성화되어 permit을 소비했으면 True
        작성 날짜: 2026/08/31
        """
        if not (
            self._phase13_public_case2 or self._phase13_recovery_only
        ):
            return False  # Legacy Phase 9 Testnet은 기존 다섯 attempt 정책을 그대로 사용한다.

        # Shape와 순서는 하나의 lock에서 읽고 써 concurrent submit이 같은 permit을 공유하지 못하게 한다.
        with self._phase13_guard_lock:
            if self._phase13_submissions_blocked:
                raise TestnetConfigurationError(
                    "Phase 13 order submissions are permanently blocked"
                )
            if self._phase13_submission_in_progress:
                raise TestnetConfigurationError(
                    "Phase 13 permits only one in-progress order submission"
                )

            attempt_index = len(self._phase13_attempts)
            maximum_attempt_count = (
                1 if self._phase13_recovery_only else 2
            )
            if attempt_index >= maximum_attempt_count:
                self._phase13_submissions_blocked = True
                raise TestnetConfigurationError(
                    "Phase 13 recovery STOP SELL permit is exhausted"
                    if self._phase13_recovery_only
                    else "Phase 13 permits exactly one BUY and one STOP SELL"
                )
            expected_side = (
                OrderSide.SELL
                if self._phase13_recovery_only
                else OrderSide.BUY
                if attempt_index == 0
                else OrderSide.SELL
            )
            expected_exit_reason = (
                ExitReason.STOP
                if self._phase13_recovery_only or attempt_index == 1
                else None
            )
            if (
                order.symbol != "ETHUSDT"
                or order.strategy is not StrategyType.CASE_C
                or order.submission_attempt != 0
                or order.side is not expected_side
                or order.exit_reason is not expected_exit_reason
            ):
                raise TestnetConfigurationError(
                    "Phase 13 recovery requires ETHUSDT CASE_C initial STOP SELL"
                    if self._phase13_recovery_only
                    else "Phase 13 requires ETHUSDT CASE_C initial BUY then STOP SELL"
                )
            if any(
                attempt.client_order_id == order.client_order_id
                for attempt in self._phase13_attempts
            ):
                raise TestnetConfigurationError(
                    "Phase 13 submissions require distinct client order IDs"
                )

            # Permit 소비 시각은 delegate 호출 시작보다 늦지 않은 aware UTC 값으로만 기록한다.
            attempted_at = self._clock()
            if (
                not isinstance(attempted_at, datetime)
                or attempted_at.tzinfo is None
                or attempted_at.utcoffset() is None
                or attempted_at.utcoffset() != timedelta(0)
            ):
                raise ValueError("clock must return a timezone-aware UTC datetime")
            self._phase13_attempts.append(
                Phase13OrderSubmissionAttempt(
                    symbol=order.symbol,
                    side=order.side,
                    order_type="MARKET",
                    intent_id=order.intent_id,
                    submission_attempt=order.submission_attempt,
                    attempted_at=attempted_at,
                    client_order_id=order.client_order_id,
                )
            )
            self._phase13_submission_in_progress = True
            if len(self._phase13_attempts) == maximum_attempt_count:
                self._phase13_submissions_blocked = True

        return True  # Delegate 실패나 UNKNOWN이어도 이미 소비한 permit은 되돌리지 않는다.

    def _finish_phase13_order_submission(self, guard_consumed: bool) -> None:
        """
        함수 이름: _finish_phase13_order_submission()
        기능: delegate 호출 종료 뒤 in-progress latch만 해제하고 소비 permit은 유지한다.
        인자: guard_consumed -> 이번 호출이 Phase 13 permit을 소비했는지 여부
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        if not guard_consumed:
            return  # Legacy 제출에는 Phase 13 lock 상태가 존재하지 않는다.

        # 차단 latch와 attempt 기록은 보존하고 동시 호출 방지 표식만 정리한다.
        with self._phase13_guard_lock:
            self._phase13_submission_in_progress = False

    def prepare_order(self, *, order: Order) -> Order:
        """
        함수 이름: prepare_order()
        기능: 주문 권한 확인 후 testnet symbol filter 준비를 delegate에 전달한다.
        인자: order -> filter 전 canonical Order
        반환값: filter와 notional cap을 통과한 Order
        작성 날짜: 2026/08/31
        """
        self._require_order_permission()
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        if self._phase13_recovery_only and (
            order.symbol != "ETHUSDT"
            or order.strategy is not StrategyType.CASE_C
            or order.submission_attempt != 0
            or order.side is not OrderSide.SELL
            or order.exit_reason is not ExitReason.STOP
        ):
            raise TestnetConfigurationError(
                "Phase 13 recovery permits one exact STOP SELL only"
            )  # Filter REST도 호출하기 전에 recovery-only target의 BUY·cancel·retry shape를 차단한다.
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
        ):
            raise TestnetConfigurationError(
                "testnet commission policy permits an unsupported fee asset"
            )

        # 공식 FAQ상 MARKET BUY 수수료는 수신 ETH에서 빠질 수 있어 LOT_SIZE dust를 만든다.
        if (
            order.side is OrderSide.BUY
            and commission_policy.market_buy_received_asset_commission_rate
            > Decimal("0")
        ):
            raise TestnetConfigurationError(
                "testnet MARKET BUY commission can create unsupported base-asset dust"
            )

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
            raise TestnetConfigurationError(
                "prepared testnet order changed immutable decision provenance"
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
            raise TestnetConfigurationError(
                "prepared testnet order requires a positive finite quantity"
            )

        # Decimal128 정밀도로 filter 후 최종 notional을 설정 cap과 절대 100 USDT 모두에 대조한다.
        with localcontext() as decimal_context:
            decimal_context.prec = 34
            final_notional = final_quantity * decision_price
        configured_cap = self._maximum_order_notional
        if configured_cap is None:
            raise TestnetConfigurationError(
                "enabled testnet orders require a configured notional cap"
            )
        if (
            final_notional > configured_cap
            or final_notional > BINANCE_TESTNET_ABSOLUTE_MAX_NOTIONAL
        ):
            raise TestnetConfigurationError(
                "prepared testnet order exceeds the configured or absolute cap"
            )

        return prepared_order  # journal 이전 마지막 bootstrap 경계가 final 수량과 immutable 가격을 결속한다.

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: 주문 권한 확인 후 준비 완료 Order의 단일 submit을 delegate에 전달한다.
        인자: order -> durable journal에 기록한 준비 완료 Order
        반환값: normalized OrderResult
        작성 날짜: 2026/08/22
        """
        self._require_order_permission()
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        submit_order = getattr(self._delegate, "submit_order", None)
        if not callable(submit_order):
            raise TypeError("delegate must provide submit_order")

        # Phase 13 permit은 실제 delegate 진입 직전에 소비하고 예외에서도 in-progress latch만 해제한다.
        guard_consumed = self._begin_phase13_order_submission(order)
        try:
            return submit_order(order=order)
        finally:
            self._finish_phase13_order_submission(guard_consumed)

    def cancel_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: cancel_order()
        기능: 주문 권한 확인 후 같은 Order의 cancel mutation을 delegate에 전달한다.
        인자: order -> 취소할 기존 Order
        반환값: normalized cancel OrderResult
        작성 날짜: 2026/08/22
        """
        self._require_order_permission()
        if self._phase13_public_case2:
            raise TestnetConfigurationError(
                "Phase 13 public Case 2 does not permit order cancellation"
            )  # 전용 target의 유일한 mutation은 최초 BUY와 exact STOP SELL 제출이다.
        if self._phase13_recovery_only:
            raise TestnetConfigurationError(
                "Phase 13 recovery does not permit order cancellation"
            )  # Recovery-only target은 exact STOP SELL 외 mutation을 허용하지 않는다.
        cancel_order = getattr(self._delegate, "cancel_order", None)
        if not callable(cancel_order):
            raise TypeError("delegate must provide cancel_order")

        return cancel_order(order=order)  # read-only mode에는 cancel도 외부 mutation이므로 허용하지 않는다.


@dataclass(frozen=True, slots=True, repr=False)
class TestnetConfiguration:
    """
    클래스 이름: TestnetConfiguration
    기능: memory 안의 testnet credential과 별도 주문 권한·notional 상한을 보존한다.
    작성 날짜: 2026/08/22
    """

    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    allow_testnet_orders: bool
    allow_phase13_public_case2: bool
    allow_phase13_recovery_only: bool
    max_notional: Decimal | None

    def __repr__(self) -> str:
        """
        함수 이름: __repr__()
        기능: credential 값을 항상 고정 redaction한 configuration 표현을 반환한다.
        인자: 없음
        반환값: secret이 없는 진단용 문자열
        작성 날짜: 2026/08/22
        """
        # 실제 길이도 credential 단서가 될 수 있으므로 값과 길이를 모두 고정 문구로 숨긴다.
        return (
            "TestnetConfiguration("
            "api_key=<redacted>, api_secret=<redacted>, "
            f"allow_testnet_orders={self.allow_testnet_orders!r}, "
            "allow_phase13_public_case2="
            f"{self.allow_phase13_public_case2!r}, "
            "allow_phase13_recovery_only="
            f"{self.allow_phase13_recovery_only!r}, "
            f"max_notional={self.max_notional!r})"
        )  # 주문 gate와 공개 가능한 cap만 운영 진단에 남긴다.


def _read_required_secret(
    environment: Mapping[str, str],
    variable_name: str,
) -> str:
    """
    함수 이름: _read_required_secret()
    기능: 환경 credential을 값 노출 없이 비어 있지 않은 원문 문자열로 검증한다.
    인자: environment -> 읽기 전용 backend 환경 mapping
        variable_name -> 읽을 credential 환경변수 이름
    반환값: 검증을 마친 credential 원문
    작성 날짜: 2026/08/22
    """
    # trim이나 자동 문자열 변환은 사용자가 발급받은 key identity를 바꾸므로 허용하지 않는다.
    credential = environment.get(variable_name)
    if not isinstance(credential, str):
        raise TestnetConfigurationError(
            f"{variable_name} is required for testnet"
        )
    if not credential or credential != credential.strip():
        raise TestnetConfigurationError(
            f"{variable_name} must be non-empty without outer whitespace"
        )

    return credential  # 값은 client 생성에만 전달하고 오류나 repr에는 포함하지 않는다.


def _read_positive_max_notional(
    environment: Mapping[str, str],
) -> Decimal:
    """
    함수 이름: _read_positive_max_notional()
    기능: 주문 opt-in에 필수인 100 USDT 이하 유한 양수 quote notional 상한을 Decimal로 읽는다.
    인자: environment -> 읽기 전용 backend 환경 mapping
    반환값: 0 초과 100 이하 Decimal 상한
    작성 날짜: 2026/08/31
    """
    # 금융 상한은 JSON number나 float 변환 없이 환경 문자열에서 바로 Decimal로 만든다.
    raw_max_notional = environment.get(BINANCE_TESTNET_MAX_NOTIONAL_ENV)
    if (
        not isinstance(raw_max_notional, str)
        or not raw_max_notional
        or raw_max_notional != raw_max_notional.strip()
    ):
        raise TestnetConfigurationError(
            f"{BINANCE_TESTNET_MAX_NOTIONAL_ENV} is required when orders are enabled"
        )
    try:
        max_notional = Decimal(raw_max_notional)
    except InvalidOperation:
        raise TestnetConfigurationError(
            f"{BINANCE_TESTNET_MAX_NOTIONAL_ENV} must be a positive decimal"
        ) from None
    if (
        not max_notional.is_finite()
        or max_notional <= Decimal("0")
        or max_notional > BINANCE_TESTNET_ABSOLUTE_MAX_NOTIONAL
    ):
        raise TestnetConfigurationError(
            f"{BINANCE_TESTNET_MAX_NOTIONAL_ENV} must be greater than 0 and no greater than 100"
        )

    return max_notional  # Caller는 BUY 진입과 일반 주문 cap에 쓰고 STOP cleanup만 예외로 둔다.


def load_testnet_configuration(
    environment: Mapping[str, str] | None = None,
) -> TestnetConfiguration:
    """
    함수 이름: load_testnet_configuration()
    기능: backend 환경에서 read-only 및 주문 이중 opt-in을 fail-closed 방식으로 읽는다.
    인자: environment -> 주입 환경 mapping 또는 실제 os.environ이면 None
    반환값: secret-safe TestnetConfiguration
    작성 날짜: 2026/08/22
    """
    # module import는 환경이나 network를 읽지 않고 이 operation을 호출할 때만 설정을 조회한다.
    selected_environment = os.environ if environment is None else environment
    if not isinstance(selected_environment, Mapping):
        raise TypeError("environment must be a mapping or None")
    if selected_environment.get(BINANCE_RUN_TESTNET_ENV) != "1":
        raise TestnetConfigurationError(
            f"{BINANCE_RUN_TESTNET_ENV}=1 is required for testnet access"
        )

    # read-only testnet조차 사용자 계정 endpoint를 쓰므로 testnet 전용 key pair를 모두 요구한다.
    api_key = _read_required_secret(
        selected_environment,
        BINANCE_TESTNET_API_KEY_ENV,
    )
    api_secret = _read_required_secret(
        selected_environment,
        BINANCE_TESTNET_API_SECRET_ENV,
    )
    allow_testnet_orders = (
        selected_environment.get(BINANCE_RUN_TESTNET_ORDERS_ENV) == "1"
    )
    allow_phase13_public_case2 = (
        allow_testnet_orders
        and selected_environment.get(BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV)
        == "1"
    )  # 세 번째 flag만 켠 read-only runtime이 Phase 13 mutation mode로 승격되지 않게 한다.
    allow_phase13_recovery_only = (
        allow_testnet_orders
        and selected_environment.get(BINANCE_RUN_PHASE13_RECOVERY_ONLY_ENV)
        == "1"
    )
    if allow_phase13_public_case2 and allow_phase13_recovery_only:
        raise TestnetConfigurationError(
            "Phase 13 mutation targets must be mutually exclusive"
        )
    max_notional = (
        _read_positive_max_notional(selected_environment)
        if allow_testnet_orders
        else None
    )

    return TestnetConfiguration(
        api_key=api_key,
        api_secret=api_secret,
        allow_testnet_orders=allow_testnet_orders,
        allow_phase13_public_case2=allow_phase13_public_case2,
        allow_phase13_recovery_only=allow_phase13_recovery_only,
        max_notional=max_notional,
    )  # 알 수 없는 order flag 값은 권한 없는 read-only 상태로 수렴한다.


def require_testnet_order_permission(
    configuration: TestnetConfiguration,
) -> Decimal:
    """
    함수 이름: require_testnet_order_permission()
    기능: 주문 이중 opt-in이 완성된 configuration에서 BUY 진입 notional 상한을 반환한다.
    인자: configuration -> load_testnet_configuration() 결과
    반환값: BUY 진입과 일반 주문 방어에 사용할 양수 Decimal 상한
    작성 날짜: 2026/08/22
    """
    # test helper가 read-only configuration으로 주문 객체를 만들기 전에 명시적으로 차단한다.
    if not isinstance(configuration, TestnetConfiguration):
        raise TypeError("configuration must be a TestnetConfiguration")
    if (
        not configuration.allow_testnet_orders
        or configuration.max_notional is None
    ):
        raise TestnetConfigurationError(
            "testnet orders require the separate order opt-in and max notional"
        )

    return configuration.max_notional  # loader가 finite 양수 검증을 이미 완료했다.


def require_phase13_public_case2_permission(
    configuration: TestnetConfiguration,
) -> Decimal:
    """
    함수 이름: require_phase13_public_case2_permission()
    기능: 세 번째 전용 opt-in과 Session 3의 10 USDT 상한을 함께 확인한다.
    인자: configuration -> load_testnet_configuration() 결과
    반환값: 10 USDT 이하의 승인된 Decimal cap
    작성 날짜: 2026/09/05
    """
    # Broad Testnet order suite와 구분된 exact flag가 없으면 public-path actual target을 실행하지 않는다.
    if not isinstance(configuration, TestnetConfiguration):
        raise TypeError("configuration must be a TestnetConfiguration")
    if not configuration.allow_phase13_public_case2:
        raise TestnetConfigurationError(
            "Phase 13 public Case 2 requires its dedicated opt-in"
        )

    # 일반 Testnet의 100 USDT ceiling보다 좁은 Session 3 실행 승인을 별도 경계에서 고정한다.
    maximum_notional = require_testnet_order_permission(configuration)
    if maximum_notional > BINANCE_PHASE13_PUBLIC_CASE2_MAX_NOTIONAL:
        raise TestnetConfigurationError(
            "Phase 13 public Case 2 max notional must be no greater than 10"
        )

    return maximum_notional  # 승인 범위 안의 원본 Decimal cap을 adapter guard에 전달한다.


def require_phase13_recovery_only_permission(
    configuration: TestnetConfiguration,
) -> Decimal:
    """
    함수 이름: require_phase13_recovery_only_permission()
    기능: 기존 open BUY를 exact STOP SELL 한 번으로 닫는 전용 opt-in과 상한을 검증한다.
    인자: configuration -> load_testnet_configuration() 결과
    반환값: 기존 이중 gate가 검증한 양수 Decimal 상한
    작성 날짜: 2026/09/04
    """
    if not isinstance(configuration, TestnetConfiguration):
        raise TypeError("configuration must be a TestnetConfiguration")
    if not configuration.allow_phase13_recovery_only:
        raise TestnetConfigurationError(
            "Phase 13 recovery requires its dedicated opt-in"
        )

    return require_testnet_order_permission(configuration)  # STOP은 cap 예외지만 broad order gate도 함께 요구한다.


def _load_testnet_client_types() -> tuple[type[object], type[object]]:
    """
    함수 이름: _load_testnet_client_types()
    기능: 고정 endpoint Binance client 타입을 runtime 생성 시점에만 import한다.
    인자: 없음
    반환값: REST와 WebSocket client class tuple
    작성 날짜: 2026/08/22
    """
    # 지연 import는 testnet module import 자체에서 dependency load나 network 접속을 막는다.
    from binance_auto_trader.adapters.binance import (
        BinanceSpotRESTClient,
        BinanceSpotWebSocketClient,
    )

    return (
        BinanceSpotRESTClient,
        BinanceSpotWebSocketClient,
    )  # testnet 전용 class만 반환해 live client fallback을 허용하지 않는다.


def create_testnet_application_runtime(
    account_update_observer: Callable[[Account], object] | None = None,
    trade_history_update_observer: Callable[[Trade, Performance], object]
    | None = None,
    trading_session_update_observer: Callable[
        [TradingController, ExecutionMode],
        object,
    ]
    | None = None,
    *,
    history_path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
    risk_policy_state: RiskPolicy | RiskPolicyUnavailable | None = None,
    clock: Callable[[], datetime] | None = None,
    monotonic_clock: Callable[[], int] | None = None,
    kline_limit: int = DEFAULT_KLINE_LIMIT,
    use_mainnet_market_data: bool = False,
) -> ApplicationRuntime:
    """
    함수 이름: create_testnet_application_runtime()
    기능: 고정 Spot Testnet client와 execution_mode testnet runtime을 opt-in 설정으로 조립한다.
    인자: account_update_observer -> 실제 Account 변경 뒤 호출할 optional observer
        trade_history_update_observer -> durable Trade와 전체 Performance 게시 후 호출할 observer
        trading_session_update_observer -> event cycle 뒤 session snapshot을 게시할 observer
        history_path -> test run별 local JSONL history 경로
        environment -> 주입 환경 mapping 또는 실제 os.environ이면 None
        risk_policy_state -> 승인값이 없으면 None인 versioned Testnet 위험 정책 상태
        clock -> runtime 전체가 공유할 optional UTC clock
        monotonic_clock -> 30분 연속 조건 전용 optional nanosecond monotonic clock
        kline_limit -> interval별 REST 초기 조회 개수
        use_mainnet_market_data -> 주문 비활성 runtime에서 실제 시장의 REST·WS 시세를 함께 사용할지 여부
    반환값: live endpoint와 분리된 ApplicationRuntime
    작성 날짜: 2026/08/22
    """
    # Transport가 주입하는 observer를 client 생성 전에 검증해 잘못된 조립을 fail fast한다.
    if account_update_observer is not None and not callable(
        account_update_observer
    ):
        raise TypeError("account_update_observer must be callable")
    if trade_history_update_observer is not None and not callable(
        trade_history_update_observer
    ):
        raise TypeError(
            "trade_history_update_observer must be callable"
        )  # Durable publication 후에 실행할 호출 경계만 허용한다.
    if trading_session_update_observer is not None and not callable(
        trading_session_update_observer
    ):
        raise TypeError("trading_session_update_observer must be callable")
    if clock is not None and not callable(clock):
        raise TypeError("clock must be callable or None")
    if monotonic_clock is not None and not callable(monotonic_clock):
        raise TypeError("monotonic_clock must be callable or None")
    if risk_policy_state is not None and not isinstance(
        risk_policy_state,
        (RiskPolicy, RiskPolicyUnavailable),
    ):
        raise TypeError(
            "risk_policy_state must be a RiskPolicy, "
            "RiskPolicyUnavailable, or None"
        )

    # 모든 설정을 client 생성 전에 검증해 잘못된 opt-in에서 객체나 network가 만들어지지 않게 한다.
    configuration = load_testnet_configuration(environment)
    # 실제 시장 분석과 Testnet 주문 실행의 혼합을 막고 데스크톱 조회 용도로만 허용한다.
    if type(use_mainnet_market_data) is not bool:
        raise TypeError("use_mainnet_market_data must be a bool")
    if use_mainnet_market_data and configuration.allow_testnet_orders:
        raise TestnetConfigurationError(
            "mainnet market data requires disabled Testnet orders"
        )
    if configuration.allow_phase13_public_case2:
        require_phase13_public_case2_permission(
            configuration
        )  # Session 3 cap은 credential을 transport client에 전달하기 전에 확정한다.
    rest_client_type, web_socket_client_type = _load_testnet_client_types()
    rest_client_arguments: dict[str, object] = {
        "api_key": configuration.api_key,
        "secret_key": configuration.api_secret,
        "maximum_order_notional": configuration.max_notional,
    }
    if use_mainnet_market_data:
        rest_client_arguments["use_mainnet_market_data"] = True  # 기존 Testnet 실행의 기본 시세를 보존한다.
    if (
        configuration.allow_phase13_public_case2
        or configuration.allow_phase13_recovery_only
    ):
        rest_client_arguments[
            "allow_order_timestamp_retry"
        ] = False  # 한 logical Phase 13 주문은 -1021에서도 추가 HTTP POST permit을 얻지 못한다.
    if clock is not None:
        # REST offset·filter·attempt와 fallback result도 runtime과 같은 UTC 축을 사용해 wall 역행을 격리한다.
        rest_client_arguments["clock"] = clock
        rest_client_arguments["result_clock"] = clock
    rest_client = rest_client_type(**rest_client_arguments)
    web_socket_client = web_socket_client_type(
        api_key=configuration.api_key,
        api_secret=configuration.api_secret,
        timestamp_provider=rest_client.get_server_timestamp_milliseconds,
        **({"use_mainnet_market_data": True} if use_mainnet_market_data else {}),
    )
    permission_checked_rest_client = _TestnetOrderPermissionRESTClient(
        rest_client,
        allow_orders=configuration.allow_testnet_orders,
        maximum_order_notional=configuration.max_notional,
        phase13_public_case2=configuration.allow_phase13_public_case2,
        phase13_recovery_only=configuration.allow_phase13_recovery_only,
        clock=clock,
    )

    return create_application_runtime(
        permission_checked_rest_client,
        web_socket_client,
        history_path=history_path,
        execution_mode="testnet",
        market_data_environment="mainnet" if use_mainnet_market_data else "testnet",
        allow_testnet_orders=configuration.allow_testnet_orders,
        testnet_maximum_order_notional=configuration.max_notional,
        maximum_order_submissions_per_intent=(
            1
            if (
                configuration.allow_phase13_public_case2
                or configuration.allow_phase13_recovery_only
            )
            else 5
        ),
        risk_policy_state=risk_policy_state,
        _testnet_order_capability=(
            _TESTNET_ORDER_CAPABILITY
            if configuration.allow_testnet_orders
            else None
        ),
        account_update_observer=account_update_observer,
        trade_history_update_observer=trade_history_update_observer,
        trading_session_update_observer=trading_session_update_observer,
        clock=clock,
        monotonic_clock=monotonic_clock,
        kline_limit=kline_limit,
    )  # live mode와 base URL 환경변수를 전달할 surface를 의도적으로 제공하지 않는다.
