"""Binance REST와 WebSocket payload 정규화 Gateway를 공개한다."""

from .api_gateway import (
    APIGateway,
    BinanceRESTClient,
    CommissionDiscountPolicy,
    Phase13OrderSubmissionAttempt,
    Phase13OrderSubmissionGuardSnapshot,
)
from .mappers import (
    AccountAssetFilter,
    AccountOrderCountFilter,
    AccountRelevantFilters,
    BinancePayloadError,
    OrderPreparationFilterEvidence,
    OrderSubmissionAttemptEvidence,
    ReferencePrice,
    SymbolFilterError,
    SymbolTradingRules,
)
from .spot_rest_client import BinanceAPIError, BinanceSpotRESTClient
from .spot_websocket_client import BinanceSpotWebSocketClient
from .websocket_gateway import (
    AccountStreamStateError,
    BinanceWebSocketClient,
    KlineBufferStateError,
    Subscription,
    WebSocketGateway,
)


# Package 소비자가 의존할 REST·WebSocket DTO와 Gateway 공개 surface를 한 목록으로 고정한다.
__all__ = [
    "AccountAssetFilter",
    "AccountOrderCountFilter",
    "AccountRelevantFilters",
    "AccountStreamStateError",
    "APIGateway",
    "BinanceAPIError",
    "BinancePayloadError",
    "BinanceRESTClient",
    "BinanceSpotRESTClient",
    "BinanceSpotWebSocketClient",
    "BinanceWebSocketClient",
    "CommissionDiscountPolicy",
    "KlineBufferStateError",
    "OrderPreparationFilterEvidence",
    "OrderSubmissionAttemptEvidence",
    "Phase13OrderSubmissionAttempt",
    "Phase13OrderSubmissionGuardSnapshot",
    "ReferencePrice",
    "Subscription",
    "SymbolFilterError",
    "SymbolTradingRules",
    "WebSocketGateway",
]  # 내부 parser/helper가 우연히 wildcard import로 노출되지 않게 한다.
