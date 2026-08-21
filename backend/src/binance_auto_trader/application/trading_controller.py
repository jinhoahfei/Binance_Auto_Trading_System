"""계좌 초기화와 REGIME별 TradingSTM 선택을 조정하는 Controller를 정의한다."""

from threading import RLock

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.websocket_gateway import (
    Subscription,
    WebSocketGateway,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.market import MarketSnapshot
from binance_auto_trader.domain.trading.account import (
    Account,
    SUPPORTED_VALUATION_ASSET,
)
from binance_auto_trader.domain.trading.stm import TradingSTM


class TradingController:
    """
    클래스 이름: TradingController
    기능: 계좌 startup 순서와 선택 REGIME의 TradingSTM factory 경계를 조정한다.
    작성 날짜: 2026/08/21
    """

    def __init__(
        self,
        api_gateway: APIGateway,
        web_socket_gateway: WebSocketGateway,
        account: Account,
        market_snapshot: MarketSnapshot,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 계좌 load에 필요한 Gateway, Account와 authoritative 시장 snapshot을 보존한다.
        인자: api_gateway -> Spot 계좌 REST snapshot을 조회할 Gateway
            web_socket_gateway -> account stream을 시작할 Gateway
            account -> REST와 stream 결과를 보존할 동일 수명의 Account
            market_snapshot -> ETH valuation 가격을 제공할 MarketSnapshot
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        if not isinstance(account, Account):
            raise TypeError("account must be an Account")
        if not isinstance(market_snapshot, MarketSnapshot):
            raise TypeError("market_snapshot must be a MarketSnapshot")

        self._api_gateway = api_gateway
        self._web_socket_gateway = web_socket_gateway
        self._account = account
        self._market_snapshot = market_snapshot
        self._account_subscription: Subscription | None = None
        self._account_load_lock = RLock()

    @property
    def account(self) -> Account:
        """
        함수 이름: account()
        기능: Controller가 초기화하고 stream callback이 갱신하는 Account를 반환한다.
        인자: 없음
        반환값: 동일 수명의 Account
        작성 날짜: 2026/08/21
        """
        return self._account

    @property
    def account_subscription(self) -> Subscription | None:
        """
        함수 이름: account_subscription()
        기능: 성공한 최신 account stream 구독 handle을 반환한다.
        인자: 없음
        반환값: 최신 구독 handle 또는 load 전 None
        작성 날짜: 2026/08/21
        """
        return self._account_subscription

    def fetch_selected_trading_logic(
        self,
        regime_type: RegimeType,
    ) -> TradingSTM:
        """
        함수 이름: fetch_selected_trading_logic()
        기능: fallback 없이 선택한 canonical REGIME에 대응하는 새 TradingSTM을 생성한다.
        인자: regime_type -> UI에서 명시적으로 선택한 canonical REGIME
        반환값: 지원된 거래 로직의 세션 전용 TradingSTM
        작성 날짜: 2026/08/21
        """
        # Session Context와 lifecycle은 Phase 7 책임이므로 factory 선택만 위임한다.
        return TradingSTM.get_stm_instance(regime_type)  # 미지원 오류를 그대로 전달한다.

    def load_account(
        self,
        asset: str = SUPPORTED_VALUATION_ASSET,
    ) -> Account:
        """
        함수 이름: load_account()
        기능: REST 전체 계좌를 가격과 적용한 뒤 변경 stream을 정확한 순서로 시작한다.
        인자: asset -> ETHUSDT 상품에서 평가할 기준 asset
        반환값: 주입 시 받은 것과 동일한 초기화된 Account
        작성 날짜: 2026/08/21
        """
        normalized_asset = self._normalize_asset(asset)

        with self._account_load_lock:
            account_snapshot = self._api_gateway.fetch_account_snapshot(
                normalized_asset
            )
            current_price = self._market_snapshot.get_current_eth_price()
            self._account.apply_initial_snapshot(
                account_snapshot,
                current_price,
            )
            self._account_subscription = None
            account_subscription = (
                self._web_socket_gateway.start_account_info_stream()
            )
            self._account_subscription = account_subscription

        return self._account

    def _normalize_asset(self, asset: object) -> str:
        """
        함수 이름: _normalize_asset()
        기능: Phase 4 Account valuation 입력을 canonical ETH로 정규화하고 제한한다.
        인자: asset -> 호출자가 전달한 기준 asset
        반환값: canonical ETH 문자열
        작성 날짜: 2026/08/21
        """
        if not isinstance(asset, str):
            raise TypeError("asset must be a string")

        normalized_asset = asset.strip().upper()
        if normalized_asset != SUPPORTED_VALUATION_ASSET:
            raise ValueError("TradingController account load supports only ETH")

        return normalized_asset
