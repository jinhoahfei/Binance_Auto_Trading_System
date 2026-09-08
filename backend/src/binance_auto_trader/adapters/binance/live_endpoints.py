"""공식 Spot live 주소와 내부 protocol 선택 표식을 한 곳에 고정한다."""

LIVE_REST_BASE_URL = "https://api.binance.com/api"
LIVE_MARKET_STREAM_URL = "wss://stream.binance.com:443/stream?streams="
LIVE_WEBSOCKET_API_URL = "wss://ws-api.binance.com:443/ws-api/v3"
_LIVE_ENDPOINT_CAPABILITY = object()  # 환경 문자열은 내부 endpoint 선택 권한이 될 수 없다.
