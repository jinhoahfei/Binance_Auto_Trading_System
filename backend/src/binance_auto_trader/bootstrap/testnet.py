"""명시적 환경 opt-in만으로 Binance Spot Testnet backend runtime을 조립한다."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import os

from binance_auto_trader.adapters.binance.api_gateway import (
    DEFAULT_KLINE_LIMIT,
)
from binance_auto_trader.bootstrap.application import (
    ApplicationRuntime,
    _TESTNET_ORDER_CAPABILITY,
    create_application_runtime,
)
from binance_auto_trader.domain.history import Performance, Trade
from binance_auto_trader.domain.trading import Account
from binance_auto_trader.domain.trading.order import Order, OrderResult


BINANCE_RUN_TESTNET_ENV = "BINANCE_RUN_TESTNET"
BINANCE_TESTNET_API_KEY_ENV = "BINANCE_TESTNET_API_KEY"
BINANCE_TESTNET_API_SECRET_ENV = "BINANCE_TESTNET_API_SECRET"
BINANCE_RUN_TESTNET_ORDERS_ENV = "BINANCE_RUN_TESTNET_ORDERS"
BINANCE_TESTNET_MAX_NOTIONAL_ENV = "BINANCE_TESTNET_MAX_NOTIONAL"
BINANCE_SPOT_TESTNET_REST_ORIGIN = "https://testnet.binance.vision"
BINANCE_SPOT_TESTNET_STREAM_ORIGIN = "wss://stream.testnet.binance.vision"
BINANCE_SPOT_TESTNET_WEBSOCKET_API_URL = (
    "wss://ws-api.testnet.binance.vision/ws-api/v3"
)


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

    __slots__ = ("_allow_orders", "_delegate")

    def __init__(self, delegate: object, *, allow_orders: bool) -> None:
        """
        함수 이름: __init__()
        기능: 고정 testnet REST client와 검증된 주문 권한을 보존한다.
        인자: delegate -> 실제 read/order REST operation을 가진 testnet client
            allow_orders -> 환경 이중 opt-in 완료 여부
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # delegate가 최소 read operation을 가져야 runtime Gateway 조립이 의미를 갖는다.
        if delegate is None or not any(
            callable(getattr(delegate, operation_name, None))
            for operation_name in ("get_account", "get_klines", "submit_order")
        ):
            raise TypeError("delegate must provide a Binance REST operation")
        if type(allow_orders) is not bool:
            raise TypeError("allow_orders must be a bool")
        self._delegate = delegate
        self._allow_orders = allow_orders  # upward 권한 변경 operation은 제공하지 않는다.

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

    def prepare_order(self, *, order: Order) -> Order:
        """
        함수 이름: prepare_order()
        기능: 주문 권한 확인 후 testnet symbol filter 준비를 delegate에 전달한다.
        인자: order -> filter 전 canonical Order
        반환값: filter와 notional cap을 통과한 Order
        작성 날짜: 2026/08/22
        """
        self._require_order_permission()
        prepare_order = getattr(self._delegate, "prepare_order", None)
        if not callable(prepare_order):
            raise TypeError("delegate must provide prepare_order")

        return prepare_order(order=order)  # cap이 설정된 실제 client만 준비를 완료한다.

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: 주문 권한 확인 후 준비 완료 Order의 단일 submit을 delegate에 전달한다.
        인자: order -> durable journal에 기록한 준비 완료 Order
        반환값: normalized OrderResult
        작성 날짜: 2026/08/22
        """
        self._require_order_permission()
        submit_order = getattr(self._delegate, "submit_order", None)
        if not callable(submit_order):
            raise TypeError("delegate must provide submit_order")

        return submit_order(order=order)  # proxy는 timeout 재제출이나 결과 변환을 추가하지 않는다.

    def cancel_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: cancel_order()
        기능: 주문 권한 확인 후 같은 Order의 cancel mutation을 delegate에 전달한다.
        인자: order -> 취소할 기존 Order
        반환값: normalized cancel OrderResult
        작성 날짜: 2026/08/22
        """
        self._require_order_permission()
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
    기능: 주문 opt-in에 필수인 유한 양수 quote notional 상한을 Decimal로 읽는다.
    인자: environment -> 읽기 전용 backend 환경 mapping
    반환값: 양수 Decimal 상한
    작성 날짜: 2026/08/22
    """
    # 금융 상한은 JSON number나 float 변환 없이 환경 문자열에서 바로 Decimal로 만든다.
    raw_max_notional = environment.get(BINANCE_TESTNET_MAX_NOTIONAL_ENV)
    if not isinstance(raw_max_notional, str) or not raw_max_notional:
        raise TestnetConfigurationError(
            f"{BINANCE_TESTNET_MAX_NOTIONAL_ENV} is required when orders are enabled"
        )
    try:
        max_notional = Decimal(raw_max_notional)
    except InvalidOperation:
        raise TestnetConfigurationError(
            f"{BINANCE_TESTNET_MAX_NOTIONAL_ENV} must be a positive decimal"
        ) from None
    if not max_notional.is_finite() or max_notional <= Decimal("0"):
        raise TestnetConfigurationError(
            f"{BINANCE_TESTNET_MAX_NOTIONAL_ENV} must be a positive decimal"
        )

    return max_notional  # caller는 이 값을 넘는 test order 수량을 만들 수 없다.


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
    max_notional = (
        _read_positive_max_notional(selected_environment)
        if allow_testnet_orders
        else None
    )

    return TestnetConfiguration(
        api_key=api_key,
        api_secret=api_secret,
        allow_testnet_orders=allow_testnet_orders,
        max_notional=max_notional,
    )  # 알 수 없는 order flag 값은 권한 없는 read-only 상태로 수렴한다.


def require_testnet_order_permission(
    configuration: TestnetConfiguration,
) -> Decimal:
    """
    함수 이름: require_testnet_order_permission()
    기능: 주문 이중 opt-in이 완성된 configuration에서 notional 상한을 반환한다.
    인자: configuration -> load_testnet_configuration() 결과
    반환값: test order가 넘지 않아야 할 양수 Decimal 상한
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
    *,
    history_path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] | None = None,
    kline_limit: int = DEFAULT_KLINE_LIMIT,
) -> ApplicationRuntime:
    """
    함수 이름: create_testnet_application_runtime()
    기능: 고정 Spot Testnet client와 execution_mode testnet runtime을 opt-in 설정으로 조립한다.
    인자: account_update_observer -> 실제 Account 변경 뒤 호출할 optional observer
        trade_history_update_observer -> durable Trade와 전체 Performance 게시 후 호출할 observer
        history_path -> test run별 local JSONL history 경로
        environment -> 주입 환경 mapping 또는 실제 os.environ이면 None
        clock -> runtime 전체가 공유할 optional UTC clock
        kline_limit -> interval별 REST 초기 조회 개수
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

    # 모든 설정을 client 생성 전에 검증해 잘못된 opt-in에서 객체나 network가 만들어지지 않게 한다.
    configuration = load_testnet_configuration(environment)
    rest_client_type, web_socket_client_type = _load_testnet_client_types()
    rest_client = rest_client_type(
        api_key=configuration.api_key,
        secret_key=configuration.api_secret,
        maximum_order_notional=configuration.max_notional,
    )
    web_socket_client = web_socket_client_type(
        api_key=configuration.api_key,
        api_secret=configuration.api_secret,
        timestamp_provider=rest_client.get_server_timestamp_milliseconds,
    )
    permission_checked_rest_client = _TestnetOrderPermissionRESTClient(
        rest_client,
        allow_orders=configuration.allow_testnet_orders,
    )

    return create_application_runtime(
        permission_checked_rest_client,
        web_socket_client,
        history_path=history_path,
        execution_mode="testnet",
        allow_testnet_orders=configuration.allow_testnet_orders,
        testnet_maximum_order_notional=configuration.max_notional,
        _testnet_order_capability=(
            _TESTNET_ORDER_CAPABILITY
            if configuration.allow_testnet_orders
            else None
        ),
        account_update_observer=account_update_observer,
        trade_history_update_observer=trade_history_update_observer,
        clock=clock,
        kline_limit=kline_limit,
    )  # live mode와 base URL 환경변수를 전달할 surface를 의도적으로 제공하지 않는다.
