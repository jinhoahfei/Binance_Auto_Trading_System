"""환경 URL·Testnet credential fallback 없이 live application graph를 별도로 조립한다."""

from binance_auto_trader.application.residual_settlement import ResidualSettlement
from binance_auto_trader.adapters.persistence.residual_repository import ResidualRepository
from collections.abc import Callable
from pathlib import Path

from binance_auto_trader.adapters.binance.live_clients import (
    BinanceLiveRESTClient, BinanceLiveWebSocketClient, _LIVE_REST_ORDER_CAPABILITY,
)
from binance_auto_trader.bootstrap.application import (
    ApplicationRuntime, _LIVE_ORDER_CAPABILITY, create_application_runtime,
)
from binance_auto_trader.bootstrap.live_configuration import (
    LiveConfiguration, LiveConfigurationError, create_live_risk_policy, validate_live_history_path,
)
from binance_auto_trader.bootstrap.live_permission import LiveOrderPermissionRESTClient


def create_live_application_runtime(
    account_update_observer: Callable | None = None,
    trade_history_update_observer: Callable | None = None,
    trading_session_update_observer: Callable | None = None,
    *, configuration: LiveConfiguration, history_path: str | Path,
) -> ApplicationRuntime:
    """
    함수 이름: create_live_application_runtime()
    기능: 검증된 native live 설정으로 fixed REST·WS와 기존 Controller graph를 조립한다.
    인자: 세 observer -> transport publication callback,
        configuration -> memory-only live 설정, history_path -> live namespace 전용 history
    반환값: read-only 기본 또는 별도 capability가 결속된 live runtime
    작성 날짜: 2026/09/08
    """
    # Disabled와 Testnet 설정은 어떤 network client나 history owner를 만들기 전에 거부한다.
    if type(configuration) is not LiveConfiguration or not configuration.enabled:
        raise LiveConfigurationError("live profile disabled")
    selected_history_path = validate_live_history_path(history_path)
    for observer in (account_update_observer, trade_history_update_observer, trading_session_update_observer):
        if observer is not None and not callable(observer):
            raise TypeError("live observer must be callable")

    # 동일 cap과 opt-in을 REST transport·permission·Controller에 각각 결속한다.
    rest_client = BinanceLiveRESTClient(
        configuration.api_key, configuration.api_secret,
        order_capability=_LIVE_REST_ORDER_CAPABILITY if configuration.allow_live_orders else None,
        maximum_order_notional=configuration.max_notional,
    )
    websocket_client = BinanceLiveWebSocketClient(
        configuration.api_key, configuration.api_secret,
        timestamp_provider=rest_client.get_server_timestamp_milliseconds,
    )
    return create_application_runtime(
        LiveOrderPermissionRESTClient(rest_client, configuration, base_fee_residual_enabled=True), websocket_client,
        history_path=selected_history_path, execution_mode="live", market_data_environment="mainnet",
        risk_policy_state=create_live_risk_policy(),
        _live_order_capability=_LIVE_ORDER_CAPABILITY if configuration.allow_live_orders else None,
        live_maximum_order_notional=configuration.max_notional,
        maximum_order_submissions_per_intent=1,
        residual_settlement=ResidualSettlement(ResidualRepository(selected_history_path.with_name("residual-ledger.json"))),
        account_update_observer=account_update_observer,
        trade_history_update_observer=trade_history_update_observer,
        trading_session_update_observer=trading_session_update_observer,
    )  # Session 7 native 실행은 allow_live_orders=False로 이 graph를 사용한다.
