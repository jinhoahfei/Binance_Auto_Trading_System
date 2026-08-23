"""공식 Binance Spot Testnet REST를 HMAC 인증과 domain 주문 계약으로 연결한다."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import hmac
import json
import re
import socket
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from binance_auto_trader.domain.trading.order import (
    FeeAssetReconciliationRequiredError,
    Fill,
    Order,
    OrderResult,
    OrderResultFailureKind,
    OrderStatus,
)

from .mappers import (
    BinancePayloadError,
    SymbolFilterError,
    SymbolTradingRules,
    format_decimal_parameter,
    map_fill_payloads,
    map_order_result,
    parse_symbol_trading_rules,
    prepare_market_order,
)


SPOT_TESTNET_API_BASE_URL = "https://testnet.binance.vision/api"
SPOT_TESTNET_ALTERNATE_API_BASE_URL = "https://api1.testnet.binance.vision/api"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 12
DEFAULT_RECV_WINDOW_MILLISECONDS = 5_000
MAXIMUM_RETRY_AFTER_SECONDS = 30
APPLICATION_CLIENT_ORDER_ID_PREFIX = "bat-"
_OFFICIAL_TESTNET_BASE_URLS = frozenset(
    {
        SPOT_TESTNET_API_BASE_URL,
        SPOT_TESTNET_ALTERNATE_API_BASE_URL,
    }
)
_AMBIGUOUS_API_CODES = frozenset(
    {
        -1000,
        -1001,
        -1006,
        -1007,
        -1008,
        -1016,
    }
)
_RATE_LIMIT_API_CODE = -1003
_INVALID_TIMESTAMP_API_CODE = -1021
_NO_SUCH_ORDER_API_CODE = -2013
_CONFIRMED_NON_EXECUTED_SUBMISSION_API_CODES = frozenset(
    {
        -1013,
        -1021,
        -1022,
    }
)
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_CLIENT_ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,36}$")


@dataclass(frozen=True, slots=True)
class HTTPTransportResponse:
    """
    클래스 이름: HTTPTransportResponse
    기능: 주입 가능한 HTTP transport가 반환할 status·header·body를 불변으로 보존한다.
    작성 날짜: 2026/08/22
    """

    status_code: int
    headers: Mapping[str, str]
    body: bytes

    def __post_init__(self) -> None:
        """
        함수 이름: __post_init__()
        기능: transport 응답의 HTTP status, header mapping과 byte body를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # bool status와 비 HTTP 범위를 차단해 오류 분류가 흔들리지 않게 한다.
        if (
            isinstance(self.status_code, bool)
            or not isinstance(self.status_code, int)
            or not 100 <= self.status_code <= 599
        ):
            raise ValueError("status_code must be an HTTP status integer")
        if not isinstance(self.headers, Mapping):
            raise TypeError("headers must be a mapping")
        if not isinstance(self.body, bytes):
            raise TypeError("body must be bytes")


class HTTPTransport(Protocol):
    """
    클래스 이름: HTTPTransport
    기능: 실제 urllib과 unit fake가 공유할 최소 HTTP request 계약을 정의한다.
    작성 날짜: 2026/08/22
    """

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: int,
    ) -> HTTPTransportResponse:
        """
        함수 이름: request()
        기능: 한 HTTP 요청을 전송하고 오류 status도 구조화된 응답으로 반환한다.
        인자: method -> HTTP method
            url -> 절대 HTTPS URL
            headers -> 전송할 header mapping
            body -> form body 또는 None
            timeout_seconds -> 연결과 응답에 적용할 양수 제한
        반환값: body를 모두 읽은 HTTPTransportResponse
        작성 날짜: 2026/08/22
        """
        ...


class UrllibHTTPTransport:
    """
    클래스 이름: UrllibHTTPTransport
    기능: Python 표준 urllib로 Binance HTTPS 요청을 수행한다.
    작성 날짜: 2026/08/22
    """

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: int,
    ) -> HTTPTransportResponse:
        """
        함수 이름: request()
        기능: urllib request를 보내고 HTTPError도 JSON 해석 가능한 응답으로 변환한다.
        인자: method -> HTTP method
            url -> 절대 HTTPS URL
            headers -> 전송할 header mapping
            body -> form body 또는 None
            timeout_seconds -> 연결과 응답에 적용할 양수 제한
        반환값: 성공 또는 HTTP 오류의 구조화된 응답
        작성 날짜: 2026/08/22
        """
        # Request에는 비밀키가 아니라 client가 만든 URL·header·body만 전달한다.
        request_value = Request(
            url=url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        try:
            with urlopen(request_value, timeout=timeout_seconds) as response:
                response_headers = {
                    header_name: header_value
                    for header_name, header_value in response.headers.items()
                }
                return HTTPTransportResponse(
                    status_code=response.status,
                    headers=response_headers,
                    body=response.read(),
                )
        except HTTPError as error:
            # urllib의 HTTPError는 정상 HTTP 응답이므로 body와 Retry-After를 보존한다.
            error_headers = {
                header_name: header_value
                for header_name, header_value in error.headers.items()
            }
            return HTTPTransportResponse(
                status_code=error.code,
                headers=error_headers,
                body=error.read(),
            )


class BinanceAPIError(RuntimeError):
    """
    클래스 이름: BinanceAPIError
    기능: credential이나 원문 요청을 노출하지 않는 Binance HTTP/API 오류를 나타낸다.
    작성 날짜: 2026/08/22
    """

    def __init__(
        self,
        *,
        status_code: int,
        api_code: int | None,
        retry_after: timedelta | None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: 안전한 status·code·대기 시간만 오류 객체에 보존한다.
        인자: status_code -> HTTP status
            api_code -> Binance 정수 오류 코드 또는 None
            retry_after -> 0~30초로 제한한 대기 시간 또는 None
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Exception 문자열에는 API key, secret, URL, body와 서버 원문 message를 넣지 않는다.
        super().__init__(
            f"Binance API request failed: status={status_code}, code={api_code}"
        )
        self.status_code = status_code
        self.api_code = api_code
        self.retry_after = retry_after


class OrderPreparationRequiredError(RuntimeError):
    """
    클래스 이름: OrderPreparationRequiredError
    기능: durable journal에 기록한 주문과 REST 제출 대상이 같음을 증명할 준비 표식이 없음을 나타낸다.
    작성 날짜: 2026/08/22
    """


@dataclass(frozen=True, slots=True)
class _PreparedOrderFingerprint:
    """
    클래스 이름: _PreparedOrderFingerprint
    기능: 준비된 Order 객체 identity와 journal 대상 입력 metadata 전체를 불변으로 고정한다.
    작성 날짜: 2026/08/22
    """

    object_identity: int
    intent_id: str
    client_order_id: str
    submission_attempt: int
    symbol: str
    side: str
    strategy: str
    regime_type: str
    requested_quantity: Decimal
    submitted_quantity: Decimal
    market_price_at_decision: Decimal
    exit_reason: str | None


@dataclass(frozen=True, slots=True)
class _DecodedHTTPResponse:
    """
    클래스 이름: _DecodedHTTPResponse
    기능: transport 응답과 검증된 JSON payload를 내부 요청 pipeline에서 함께 전달한다.
    작성 날짜: 2026/08/22
    """

    transport_response: HTTPTransportResponse
    payload: object


def _utc_now() -> datetime:
    """
    함수 이름: _utc_now()
    기능: OrderResult 처리 시각에 사용할 현재 timezone-aware UTC 시각을 반환한다.
    인자: 없음
    반환값: 현재 UTC datetime
    작성 날짜: 2026/08/22
    """
    return datetime.now(timezone.utc)  # 모든 domain 처리 시각을 UTC로 생성한다.


def _system_utc_now() -> datetime:
    """
    함수 이름: _system_utc_now()
    기능: server time offset 계산에 사용할 현재 timezone-aware UTC 시각을 반환한다.
    인자: 없음
    반환값: 현재 UTC datetime
    작성 날짜: 2026/08/22
    """
    return datetime.now(timezone.utc)  # 외부 테스트는 constructor clock으로 이 경계를 대체한다.


def _normalize_base_url(base_url: object) -> str:
    """
    함수 이름: _normalize_base_url()
    기능: transport 주입 여부와 무관하게 공식 Spot Testnet REST URL만 허용한다.
    인자: base_url -> 검증할 API base URL
    반환값: 뒤 slash를 제거한 base URL
    작성 날짜: 2026/08/22
    """
    if not isinstance(base_url, str):
        raise TypeError("base_url must be a string")
    normalized_base_url = base_url.rstrip("/")
    parsed_url = urlsplit(normalized_base_url)

    # Built-in transport를 명시 주입해도 production endpoint capability가 생기지 않게 한다.
    if normalized_base_url not in _OFFICIAL_TESTNET_BASE_URLS:
        raise ValueError("BinanceSpotRESTClient supports only official Spot Testnet")
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise ValueError("base_url must be an absolute HTTPS URL")
    if parsed_url.query or parsed_url.fragment:
        raise ValueError("base_url must not contain query or fragment")

    return normalized_base_url  # endpoint path를 안전하게 덧붙일 canonical URL을 반환한다.


def _normalize_secret_text(value: object, field_name: str) -> str:
    """
    함수 이름: _normalize_secret_text()
    기능: 공백 보정 없이 비어 있지 않은 credential 문자열을 검증한다.
    인자: value -> 검증할 credential 후보
        field_name -> 오류에 사용할 안전한 필드 이름
    반환값: 원래 credential 문자열
    작성 날짜: 2026/08/22
    """
    # 앞뒤 공백을 자동 제거하면 실제 signing key와 다른 값이 될 수 있으므로 거부한다.
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty and trimmed")

    return value  # credential 내용은 변형하지 않는다.


def _clock_to_milliseconds(clock_value: object) -> int:
    """
    함수 이름: _clock_to_milliseconds()
    기능: 주입 clock의 UTC datetime을 epoch milliseconds로 변환한다.
    인자: clock_value -> timezone-aware datetime
    반환값: epoch milliseconds 정수
    작성 날짜: 2026/08/22
    """
    if isinstance(clock_value, datetime):
        if clock_value.tzinfo is None or clock_value.utcoffset() is None:
            raise ValueError("clock datetime must be timezone-aware")
        elapsed_time = clock_value.astimezone(timezone.utc) - _UNIX_EPOCH
        return (
            elapsed_time.days * 86_400_000
            + elapsed_time.seconds * 1_000
            + elapsed_time.microseconds // 1_000
        )  # 부동소수 timestamp를 사용하지 않고 millisecond를 계산한다.

    raise TypeError("clock must return a timezone-aware datetime")


def _encode_parameter_value(value: object) -> str:
    """
    함수 이름: _encode_parameter_value()
    기능: REST parameter 값을 signing과 전송이 공유할 문자열로 변환한다.
    인자: value -> str, int, bool 또는 Decimal parameter
    반환값: percent-encoding 전 문자열
    작성 날짜: 2026/08/22
    """
    # bool은 Binance JSON 관례와 같은 소문자 문자열로 직렬화한다.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format_decimal_parameter(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str) and value:
        return value

    raise TypeError("request parameters must be non-empty scalar values")


def _encode_parameters(parameters: Mapping[str, object]) -> str:
    """
    함수 이름: _encode_parameters()
    기능: parameter를 결정론적 순서로 직렬화하고 UTF-8 percent-encoding한다.
    인자: parameters -> 전송할 parameter mapping
    반환값: HMAC 입력과 HTTP query/body가 공유할 form 문자열
    작성 날짜: 2026/08/22
    """
    if not isinstance(parameters, Mapping):
        raise TypeError("parameters must be a mapping")

    # 키를 정렬한 동일 pair sequence를 signing과 HTTP 전송에 재사용한다.
    encoded_pairs = tuple(
        (parameter_name, _encode_parameter_value(parameters[parameter_name]))
        for parameter_name in sorted(parameters)
    )

    return urlencode(
        encoded_pairs,
        doseq=False,
        safe="",
        encoding="utf-8",
        errors="strict",
        quote_via=quote,
    )  # 공백도 '+'가 아닌 명시적 percent-encoding으로 고정한다.


def _decode_json_body(body: bytes) -> object:
    """
    함수 이름: _decode_json_body()
    기능: UTF-8 HTTP body를 비표준 NaN/Infinity 없는 JSON 값으로 해석한다.
    인자: body -> transport가 읽은 response bytes
    반환값: JSON object, array 또는 scalar
    작성 날짜: 2026/08/22
    """
    if not isinstance(body, bytes):
        raise TypeError("response body must be bytes")
    try:
        decoded_text = body.decode("utf-8")
        return json.loads(
            decoded_text,
            parse_constant=_reject_non_standard_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BinancePayloadError("Binance response must be valid UTF-8 JSON") from error


def _reject_non_standard_json_constant(value: str) -> None:
    """
    함수 이름: _reject_non_standard_json_constant()
    기능: JSON 표준에 없는 NaN·Infinity 상수를 payload 경계에서 거부한다.
    인자: value -> json module이 발견한 비표준 상수 이름
    반환값: 없음
    작성 날짜: 2026/08/22
    """
    raise BinancePayloadError(f"non-standard JSON constant is not allowed: {value}")


def _read_api_code(payload: object) -> int | None:
    """
    함수 이름: _read_api_code()
    기능: Binance 오류 object의 정수 code를 선택적으로 읽는다.
    인자: payload -> 해석된 JSON payload
    반환값: 정수 API code 또는 code가 없으면 None
    작성 날짜: 2026/08/22
    """
    if not isinstance(payload, Mapping) or "code" not in payload:
        return None
    api_code = payload.get("code")
    if isinstance(api_code, bool) or not isinstance(api_code, int):
        raise BinancePayloadError("Binance error code must be an integer")

    return api_code  # 서버 원문 message는 credential-safe 오류 분류에 사용하지 않는다.


def _read_retry_after(headers: Mapping[str, str]) -> timedelta | None:
    """
    함수 이름: _read_retry_after()
    기능: REST Retry-After seconds를 domain 허용 범위인 0~30초로 제한한다.
    인자: headers -> HTTP response header mapping
    반환값: 제한한 timedelta 또는 header가 없으면 None
    작성 날짜: 2026/08/22
    """
    retry_after_text = next(
        (
            header_value
            for header_name, header_value in headers.items()
            if header_name.lower() == "retry-after"
        ),
        None,
    )
    if retry_after_text is None:
        return None
    try:
        retry_after_seconds = Decimal(retry_after_text)
    except Exception as error:
        raise BinancePayloadError("Retry-After must contain seconds") from error
    if not retry_after_seconds.is_finite() or retry_after_seconds < Decimal("0"):
        raise BinancePayloadError("Retry-After must be finite and non-negative")

    # ADR/domain은 최대 30초만 표현하므로 더 긴 ban은 30초 뒤에도 재조회로만 이어진다.
    bounded_seconds = min(
        retry_after_seconds,
        Decimal(MAXIMUM_RETRY_AFTER_SECONDS),
    )
    retry_after_microseconds = int(bounded_seconds * Decimal("1000000"))

    return timedelta(microseconds=retry_after_microseconds)  # Decimal seconds를 정수 microseconds로 옮긴다.


def _safe_failure_reason(prefix: str, api_code: int | None = None) -> str:
    """
    함수 이름: _safe_failure_reason()
    기능: 서버 message·URL·credential 없이 안정된 주문 failure reason을 만든다.
    인자: prefix -> 내부 오류 분류 이름
        api_code -> 선택 Binance 정수 오류 코드
    반환값: 공백 없는 안전한 failure reason
    작성 날짜: 2026/08/22
    """
    if api_code is None:
        return prefix  # code가 없는 transport/HTTP 오류는 고정 분류만 사용한다.

    return f"{prefix}_{api_code}"  # 숫자 code만 붙여 서버 원문 반사를 막는다.


class BinanceSpotRESTClient:
    """
    클래스 이름: BinanceSpotRESTClient
    기능: Spot Testnet REST 인증·시간·filter·fill을 기존 BinanceRESTClient 계약으로 제공한다.
    작성 날짜: 2026/08/22
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        base_url: str = SPOT_TESTNET_API_BASE_URL,
        transport: HTTPTransport | None = None,
        clock: Callable[[], object] = _system_utc_now,
        result_clock: Callable[[], datetime] = _utc_now,
        request_timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        recv_window_milliseconds: int = DEFAULT_RECV_WINDOW_MILLISECONDS,
        maximum_order_notional: Decimal | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: Testnet endpoint, credential, transport와 시간 정책을 검증해 초기화한다.
        인자: api_key -> X-MBX-APIKEY에만 보낼 API key
            secret_key -> HMAC-SHA256 signing에만 사용할 secret
            base_url -> 공식 Spot Testnet API base URL
            transport -> 주입 HTTP transport 또는 None
            clock -> server offset용 datetime/Unix seconds clock
            result_clock -> fallback OrderResult UTC clock
            request_timeout_seconds -> 양수 HTTP timeout
            recv_window_milliseconds -> 1~60000ms signed request window
            maximum_order_notional -> filter 외에 적용할 선택 Testnet 주문 금액 상한
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        selected_transport = transport or UrllibHTTPTransport()
        if not callable(getattr(selected_transport, "request", None)):
            raise TypeError("transport must provide request")
        if not callable(clock) or not callable(result_clock):
            raise TypeError("clock and result_clock must be callable")
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, int)
            or request_timeout_seconds <= 0
        ):
            raise ValueError("request_timeout_seconds must be a positive integer")
        if (
            isinstance(recv_window_milliseconds, bool)
            or not isinstance(recv_window_milliseconds, int)
            or not 1 <= recv_window_milliseconds <= 60_000
        ):
            raise ValueError("recv_window_milliseconds must be between 1 and 60000")
        if maximum_order_notional is not None and (
            not isinstance(maximum_order_notional, Decimal)
            or not maximum_order_notional.is_finite()
            or maximum_order_notional <= Decimal("0")
        ):
            raise ValueError("maximum_order_notional must be a positive finite Decimal")

        # Credentials는 공개 property나 repr에 노출하지 않고 private signing state로만 둔다.
        self._api_key = _normalize_secret_text(api_key, "api_key")
        self._secret_key_bytes = _normalize_secret_text(
            secret_key,
            "secret_key",
        ).encode("utf-8")
        self._base_url = _normalize_base_url(base_url)
        self._transport = selected_transport
        self._clock = clock
        self._result_clock = result_clock
        self._request_timeout_seconds = request_timeout_seconds
        self._recv_window_milliseconds = recv_window_milliseconds
        self._maximum_order_notional = maximum_order_notional
        self._server_time_offset_milliseconds: int | None = None
        self._symbol_rules_by_symbol: dict[str, SymbolTradingRules] = {}
        self._prepared_orders_by_client_id: dict[
            str,
            _PreparedOrderFingerprint,
        ] = {}

    def __repr__(self) -> str:
        """
        함수 이름: __repr__()
        기능: API key·secret을 제외한 endpoint와 timeout만 진단 문자열로 반환한다.
        인자: 없음
        반환값: credential-safe client 표현
        작성 날짜: 2026/08/22
        """
        return (
            "BinanceSpotRESTClient("
            f"base_url={self._base_url!r}, "
            f"request_timeout_seconds={self._request_timeout_seconds!r}"
            ")"
        )  # credential과 signature는 어떤 repr 필드에도 포함하지 않는다.

    def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> object:
        """
        함수 이름: get_klines()
        기능: 공식 GET /api/v3/klines의 JSON array를 반환한다.
        인자: symbol -> 조회할 Spot symbol
            interval -> 공식 Kline interval
            limit -> 1~1000 반환 개수
        반환값: 해석된 Binance Kline JSON array
        작성 날짜: 2026/08/22
        """
        normalized_symbol = self._normalize_symbol(symbol)
        if not isinstance(interval, str) or not interval or interval != interval.strip():
            raise ValueError("interval must be a non-empty trimmed string")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")

        # Public market data는 credential header나 signed parameter 없이 조회한다.
        response = self._request_json(
            method="GET",
            endpoint="/v3/klines",
            parameters={
                "symbol": normalized_symbol,
                "interval": interval,
                "limit": limit,
            },
            signed=False,
        )
        if not isinstance(response.payload, list):
            raise BinancePayloadError("klines response must be an array")

        return response.payload  # APIGateway가 공식 12-field row를 domain Kline으로 변환한다.

    def get_account(self) -> object:
        """
        함수 이름: get_account()
        기능: 공식 signed GET /api/v3/account의 전체 balance JSON을 반환한다.
        인자: 없음
        반환값: 해석된 Binance account JSON object
        작성 날짜: 2026/08/22
        """
        # omitZeroBalances=false로 ETH/USDT 0 잔액도 전체 snapshot schema에 남긴다.
        response = self._request_json(
            method="GET",
            endpoint="/v3/account",
            parameters={"omitZeroBalances": False},
            signed=True,
        )
        if not isinstance(response.payload, Mapping):
            raise BinancePayloadError("account response must be an object")

        return response.payload  # APIGateway가 raw account를 AccountSnapshot으로 정규화한다.

    def get_server_timestamp_milliseconds(self) -> int:
        """
        함수 이름: get_server_timestamp_milliseconds()
        기능: REST time sync를 공유해 signed WebSocket subscription용 server timestamp를 반환한다.
        인자: 없음
        반환값: 현재 추정 Binance server epoch milliseconds
        작성 날짜: 2026/08/22
        """
        # 첫 사용 전에는 공개 /time을 동기화하고 이후에는 credential 없는 offset 계산만 수행한다.
        if self._server_time_offset_milliseconds is None:
            self._synchronize_server_time()
        if self._server_time_offset_milliseconds is None:
            raise RuntimeError("server time synchronization did not produce an offset")

        return (
            self._local_time_milliseconds()
            + self._server_time_offset_milliseconds
        )  # API key·secret·signature 없이 정수 timestamp만 외부에 제공한다.

    def prepare_order(self, order: Order) -> Order:
        """
        함수 이름: prepare_order()
        기능: 최신 MARKET rule을 적용하고 durable 제출에 쓸 객체·ID·수량 fingerprint를 고정한다.
        인자: order -> 제출할 ETHUSDT Order aggregate
        반환값: requested_quantity를 보존하고 submitted_quantity만 내린 동일 Order
        작성 날짜: 2026/08/22
        """
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        if _CLIENT_ORDER_ID_PATTERN.fullmatch(order.client_order_id) is None:
            raise ValueError("client_order_id must match Binance's 1-36 character format")

        # 같은 객체와 정확한 수량을 다시 준비하면 exchangeInfo나 aggregate를 건드리지 않는다.
        existing_fingerprint = self._prepared_orders_by_client_id.get(
            order.client_order_id
        )
        current_fingerprint = self._order_preparation_fingerprint(order)
        if existing_fingerprint is not None:
            if existing_fingerprint != current_fingerprint:
                raise OrderPreparationRequiredError(
                    "prepared order identity or quantity changed"
                )
            return order  # journal 이전의 중복 prepare도 최초 준비 결과를 그대로 사용한다.

        # 매 제출 직전 최신 Testnet 상태·filter를 조회해 reset 또는 설정 변경을 반영한다.
        response = self._request_json(
            method="GET",
            endpoint="/v3/exchangeInfo",
            parameters={"symbol": order.symbol},
            signed=False,
        )
        rules = parse_symbol_trading_rules(response.payload, order.symbol)
        self._symbol_rules_by_symbol[order.symbol] = rules  # 같은 submit의 fill 자산 mapping에 재사용한다.

        prepared_order = prepare_market_order(order, rules)

        # Bootstrap opt-in cap은 거래소 filter보다 작을 수 있으므로 실제 내림 수량에 추가 적용한다.
        if self._maximum_order_notional is not None:
            with localcontext() as decimal_context:
                decimal_context.prec = 34
                prepared_notional = (
                    prepared_order.submitted_quantity
                    * prepared_order.market_price_at_decision
                )
            if prepared_notional > self._maximum_order_notional:
                raise SymbolFilterError("FILTER_CONFIGURED_MAXIMUM_NOTIONAL")

        # 모든 filter와 local cap을 통과한 뒤에만 journal/submit 사이의 불변 표식을 만든다.
        self._prepared_orders_by_client_id[prepared_order.client_order_id] = (
            self._order_preparation_fingerprint(prepared_order)
        )

        return prepared_order  # 원 aggregate identity와 requested_quantity를 그대로 유지한다.

    def submit_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: submit_order()
        기능: prepare_order가 고정한 exact MARKET 주문을 재보정 없이 FULL 응답으로 한 번 제출한다.
        인자: order -> durable journal에 기록한 준비 완료 Order
        반환값: terminal rejection 또는 normalized exchange OrderResult
        작성 날짜: 2026/08/22
        """
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")

        # 표식을 먼저 소모해 동일 client ID의 중복 제출과 journal 이후 수량 변경을 막는다.
        self._consume_prepared_order(order)

        # MARKET은 base quantity를 사용해 BUY/SELL 모두 domain requested quantity 의미를 유지한다.
        parameters = {
            "symbol": order.symbol,
            "side": order.side.value,
            "type": "MARKET",
            "quantity": order.submitted_quantity,
            "newClientOrderId": order.client_order_id,
            "newOrderRespType": "FULL",
        }
        try:
            response = self._request_json(
                method="POST",
                endpoint="/v3/order",
                parameters=parameters,
                signed=True,
            )
            return self._map_order_payload(order, response.payload)
        except BinanceAPIError as error:
            return self._submission_error_result(order, error)
        except (OSError, TimeoutError, socket.timeout):
            return self._unknown_result(
                order,
                "BINANCE_TRANSPORT_UNKNOWN",
            )
        except (
            BinancePayloadError,
            FeeAssetReconciliationRequiredError,
        ):
            # 성공 응답을 회계상 확정할 수 없으면 같은 client ID 조회만 허용한다.
            return self._unknown_result(
                order,
                "BINANCE_PAYLOAD_RECONCILIATION_REQUIRED",
            )

    def query_order_result(self, *, order: Order) -> OrderResult:
        """
        함수 이름: query_order_result()
        기능: 신규 제출 없이 같은 exchange/client order ID를 조회하고 myTrades fill을 병합한다.
        인자: order -> 이전 제출 식별자를 가진 기존 Order
        반환값: 누적 fill을 포함한 최신 OrderResult 또는 UNKNOWN
        작성 날짜: 2026/08/22
        """
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        parameters = self._order_identifier_parameters(order)

        try:
            response = self._request_json(
                method="GET",
                endpoint="/v3/order",
                parameters=parameters,
                signed=True,
            )
            return self._map_order_payload(order, response.payload, hydrate_fills=True)
        except BinanceAPIError as error:
            # Memory=>Database 지연의 -2013은 즉시 rejection 증거가 아니므로 UNKNOWN을 유지한다.
            failure_prefix = (
                "BINANCE_ORDER_NOT_VISIBLE"
                if error.api_code == _NO_SUCH_ORDER_API_CODE
                else "BINANCE_QUERY_UNKNOWN"
            )
            return self._unknown_result(
                order,
                _safe_failure_reason(failure_prefix, error.api_code),
                failure_kind=(
                    OrderResultFailureKind.ORDER_NOT_VISIBLE
                    if error.api_code == _NO_SUCH_ORDER_API_CODE
                    else None
                ),
                retry_after=error.retry_after,
            )
        except (OSError, TimeoutError, socket.timeout):
            return self._unknown_result(order, "BINANCE_QUERY_TRANSPORT_UNKNOWN")
        except (
            BinancePayloadError,
            FeeAssetReconciliationRequiredError,
        ):
            return self._unknown_result(
                order,
                "BINANCE_QUERY_RECONCILIATION_REQUIRED",
            )

    def cancel_order(self, *, order: Order) -> OrderResult:
        """
        함수 이름: cancel_order()
        기능: 같은 order ID를 취소하고 취소 시점까지의 myTrades fill을 함께 반환한다.
        인자: order -> 취소할 기존 Order
        반환값: 누적 fill을 포함한 CANCELED/terminal 결과 또는 UNKNOWN
        작성 날짜: 2026/08/22
        """
        if not isinstance(order, Order):
            raise TypeError("order must be an Order")
        parameters = self._order_identifier_parameters(order)

        try:
            response = self._request_json(
                method="DELETE",
                endpoint="/v3/order",
                parameters=parameters,
                signed=True,
            )
            return self._map_order_payload(order, response.payload, hydrate_fills=True)
        except BinanceAPIError as error:
            # 취소 실패는 원 주문의 terminal 상태를 뜻하지 않으므로 조회 가능한 UNKNOWN으로 보존한다.
            return self._unknown_result(
                order,
                _safe_failure_reason("BINANCE_CANCEL_UNKNOWN", error.api_code),
                retry_after=error.retry_after,
            )
        except (OSError, TimeoutError, socket.timeout):
            return self._unknown_result(order, "BINANCE_CANCEL_TRANSPORT_UNKNOWN")
        except (
            BinancePayloadError,
            FeeAssetReconciliationRequiredError,
        ):
            return self._unknown_result(
                order,
                "BINANCE_CANCEL_RECONCILIATION_REQUIRED",
            )

    def list_open_order_results(
        self,
        *,
        symbol: str = "ETHUSDT",
        client_order_id_prefix: str = APPLICATION_CLIENT_ORDER_ID_PREFIX,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_open_order_results()
        기능: 한 symbol의 open orders를 조회해 application client ID 주문만 fill과 함께 반환한다.
        인자: symbol -> 조회할 Spot symbol
            client_order_id_prefix -> 로컬 application 주문 ID prefix
        반환값: 최신 open OrderResult tuple
        작성 날짜: 2026/08/22
        """
        normalized_symbol = self._normalize_symbol(symbol)
        normalized_prefix = self._normalize_client_order_id_prefix(
            client_order_id_prefix
        )

        # Symbol을 항상 보내 weight 80인 전체-symbol 조회 대신 공식 weight 6 경로를 사용한다.
        response = self._request_json(
            method="GET",
            endpoint="/v3/openOrders",
            parameters={"symbol": normalized_symbol},
            signed=True,
        )

        return self._map_order_collection(
            response.payload,
            expected_symbol=normalized_symbol,
            client_order_id_prefix=normalized_prefix,
        )  # 호출자는 이 결과로 restart open-order reconciliation을 수행한다.

    def list_recent_order_results(
        self,
        *,
        symbol: str = "ETHUSDT",
        client_order_id_prefix: str = APPLICATION_CLIENT_ORDER_ID_PREFIX,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 500,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: list_recent_order_results()
        기능: allOrders 최근 구간에서 application 주문을 골라 누적 myTrades fill과 반환한다.
        인자: symbol -> 조회할 Spot symbol
            client_order_id_prefix -> 로컬 application 주문 ID prefix
            start_time -> 선택 조회 시작 UTC 시각
            end_time -> 선택 조회 종료 UTC 시각
            limit -> 1~1000 주문 개수
        반환값: 최근 OrderResult tuple
        작성 날짜: 2026/08/22
        """
        normalized_symbol = self._normalize_symbol(symbol)
        normalized_prefix = self._normalize_client_order_id_prefix(
            client_order_id_prefix
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")

        # 공식 allOrders의 최대 24시간 time range를 입력 단계에서 검증한다.
        parameters: dict[str, object] = {
            "symbol": normalized_symbol,
            "limit": limit,
        }
        if start_time is not None:
            parameters["startTime"] = self._datetime_to_milliseconds(
                start_time,
                "start_time",
            )
        if end_time is not None:
            parameters["endTime"] = self._datetime_to_milliseconds(
                end_time,
                "end_time",
            )
        if start_time is not None and end_time is not None:
            if end_time < start_time:
                raise ValueError("end_time must not be before start_time")
            if end_time - start_time > timedelta(hours=24):
                raise ValueError("allOrders time range must not exceed 24 hours")

        response = self._request_json(
            method="GET",
            endpoint="/v3/allOrders",
            parameters=parameters,
            signed=True,
        )

        return self._map_order_collection(
            response.payload,
            expected_symbol=normalized_symbol,
            client_order_id_prefix=normalized_prefix,
        )  # client prefix filtering은 allOrders가 지원하지 않아 로컬에서 수행한다.

    def _order_preparation_fingerprint(
        self,
        order: Order,
    ) -> _PreparedOrderFingerprint:
        """
        함수 이름: _order_preparation_fingerprint()
        기능: 준비와 제출 사이에 동일해야 할 Order 객체 identity와 입력 metadata를 읽는다.
        인자: order -> fingerprint를 만들 준비 또는 제출 Order
        반환값: 불변 준비 fingerprint
        작성 날짜: 2026/08/22
        """
        for field_name, field_value in (
            ("requested_quantity", order.requested_quantity),
            ("submitted_quantity", order.submitted_quantity),
            ("market_price_at_decision", order.market_price_at_decision),
        ):
            if not isinstance(field_value, Decimal):
                raise TypeError(f"{field_name} must be a Decimal")

        return _PreparedOrderFingerprint(
            object_identity=id(order),
            intent_id=order.intent_id,
            client_order_id=order.client_order_id,
            submission_attempt=order.submission_attempt,
            symbol=order.symbol,
            side=order.side.value,
            strategy=order.strategy.value,
            regime_type=order.regime_type.value,
            requested_quantity=order.requested_quantity,
            submitted_quantity=order.submitted_quantity,
            market_price_at_decision=order.market_price_at_decision,
            exit_reason=(
                None if order.exit_reason is None else order.exit_reason.value
            ),
        )  # Enum·Decimal은 변환 손실 없이 journal과 HTTP 의미를 같은 snapshot으로 고정한다.

    def _consume_prepared_order(self, order: Order) -> None:
        """
        함수 이름: _consume_prepared_order()
        기능: 준비 표식을 한 번 소모하고 journal 이후 객체 또는 수량 변경을 HTTP 전에 거부한다.
        인자: order -> durable journal에 기록된 제출 대상 Order
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # pop은 ambiguous 응답 뒤 같은 client ID를 재전송하지 못하게 한 번만 제출하게 한다.
        prepared_fingerprint = self._prepared_orders_by_client_id.pop(
            order.client_order_id,
            None,
        )
        if prepared_fingerprint is None:
            raise OrderPreparationRequiredError(
                "order must be prepared before submission"
            )

        # 객체 교체나 입력 metadata 변경은 durable record와 전송 payload 불일치다.
        if prepared_fingerprint != self._order_preparation_fingerprint(order):
            raise OrderPreparationRequiredError(
                "prepared order identity or quantity changed"
            )

    def _request_json(
        self,
        *,
        method: str,
        endpoint: str,
        parameters: Mapping[str, object],
        signed: bool,
    ) -> _DecodedHTTPResponse:
        """
        함수 이름: _request_json()
        기능: 요청을 서명·전송·JSON 해석하고 -1021에서 time sync 후 한 번만 재시도한다.
        인자: method -> GET, POST 또는 DELETE
            endpoint -> /v3로 시작하는 API path
            parameters -> timestamp/signature 전 parameter
            signed -> HMAC 인증 필요 여부
        반환값: 성공한 decoded HTTP response
        작성 날짜: 2026/08/22
        """
        if signed and self._server_time_offset_milliseconds is None:
            self._synchronize_server_time()

        # -1021은 Matching Engine 도달 전 rejection이므로 같은 요청을 새 timestamp로 한 번만 보낸다.
        maximum_attempts = 2 if signed else 1
        for attempt_index in range(maximum_attempts):
            response = self._perform_request(
                method=method,
                endpoint=endpoint,
                parameters=parameters,
                signed=signed,
            )
            api_code = _read_api_code(response.payload)
            if (
                signed
                and api_code == _INVALID_TIMESTAMP_API_CODE
                and attempt_index == 0
            ):
                self._synchronize_server_time()
                continue
            if response.transport_response.status_code >= 400 or api_code is not None:
                raise BinanceAPIError(
                    status_code=response.transport_response.status_code,
                    api_code=api_code,
                    retry_after=_read_retry_after(
                        response.transport_response.headers
                    ),
                )

            return response  # HTTP 성공과 error code 부재를 모두 확인한 응답만 공개한다.

        raise RuntimeError("signed request retry loop terminated unexpectedly")

    def _perform_request(
        self,
        *,
        method: str,
        endpoint: str,
        parameters: Mapping[str, object],
        signed: bool,
    ) -> _DecodedHTTPResponse:
        """
        함수 이름: _perform_request()
        기능: 한 REST attempt의 parameter를 percent-encode·HMAC sign하고 transport로 전송한다.
        인자: method -> GET, POST 또는 DELETE
            endpoint -> /v3로 시작하는 API path
            parameters -> timestamp/signature 전 parameter
            signed -> HMAC 인증 필요 여부
        반환값: JSON을 해석한 단일 attempt 응답
        작성 날짜: 2026/08/22
        """
        if method not in {"GET", "POST", "DELETE"}:
            raise ValueError("unsupported HTTP method")
        if not isinstance(endpoint, str) or not endpoint.startswith("/v3/"):
            raise ValueError("endpoint must start with /v3/")

        # Signed parameter에는 server offset timestamp와 작은 recvWindow를 attempt마다 새로 넣는다.
        request_parameters = dict(parameters)
        headers = {"Accept": "application/json"}
        if signed:
            if self._server_time_offset_milliseconds is None:
                raise RuntimeError("server time must be synchronized before signing")
            request_parameters["recvWindow"] = self._recv_window_milliseconds
            request_parameters["timestamp"] = (
                self._local_time_milliseconds()
                + self._server_time_offset_milliseconds
            )
            headers["X-MBX-APIKEY"] = self._api_key

        # 공식 HMAC 계약대로 percent-encoded payload 자체를 sign하고 같은 bytes를 전송한다.
        encoded_parameters = _encode_parameters(request_parameters)
        if signed:
            signature = hmac.new(
                self._secret_key_bytes,
                encoded_parameters.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            encoded_parameters = f"{encoded_parameters}&signature={signature}"

        # GET은 query string, POST/DELETE는 form body를 사용해 공식 method별 규칙을 따른다.
        request_url = f"{self._base_url}{endpoint}"
        request_body: bytes | None = None
        if method == "GET" and encoded_parameters:
            request_url = f"{request_url}?{encoded_parameters}"
        elif method in {"POST", "DELETE"}:
            request_body = encoded_parameters.encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        transport_response = self._transport.request(
            method=method,
            url=request_url,
            headers=headers,
            body=request_body,
            timeout_seconds=self._request_timeout_seconds,
        )
        if not isinstance(transport_response, HTTPTransportResponse):
            raise TypeError("transport must return HTTPTransportResponse")

        return _DecodedHTTPResponse(
            transport_response=transport_response,
            payload=_decode_json_body(transport_response.body),
        )  # URL·body는 저장하거나 logging하지 않고 즉시 해석 결과만 넘긴다.

    def _synchronize_server_time(self) -> None:
        """
        함수 이름: _synchronize_server_time()
        기능: GET /api/v3/time 왕복 중간 시각을 기준으로 server offset을 갱신한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 요청 전후 local clock을 읽어 네트워크 왕복 시간의 중간값을 offset 기준으로 사용한다.
        local_before_milliseconds = self._local_time_milliseconds()
        response = self._perform_request(
            method="GET",
            endpoint="/v3/time",
            parameters={},
            signed=False,
        )
        local_after_milliseconds = self._local_time_milliseconds()
        if response.transport_response.status_code >= 400:
            raise BinanceAPIError(
                status_code=response.transport_response.status_code,
                api_code=_read_api_code(response.payload),
                retry_after=_read_retry_after(response.transport_response.headers),
            )
        if not isinstance(response.payload, Mapping):
            raise BinancePayloadError("time response must be an object")
        server_time = response.payload.get("serverTime")
        if isinstance(server_time, bool) or not isinstance(server_time, int) or server_time < 0:
            raise BinancePayloadError("serverTime must be a non-negative integer")

        # 중간 시각 offset은 단방향 지연을 알 수 없는 조건에서 편향을 줄이는 보수적 추정이다.
        local_midpoint = (
            local_before_milliseconds + local_after_milliseconds
        ) // 2
        self._server_time_offset_milliseconds = server_time - local_midpoint

    def _local_time_milliseconds(self) -> int:
        """
        함수 이름: _local_time_milliseconds()
        기능: 주입 clock을 호출해 현재 local epoch milliseconds를 반환한다.
        인자: 없음
        반환값: epoch milliseconds
        작성 날짜: 2026/08/22
        """
        return _clock_to_milliseconds(self._clock())  # 모든 signed attempt가 새 시각을 얻는다.

    def _result_time(self) -> datetime:
        """
        함수 이름: _result_time()
        기능: 주입 result clock을 timezone-aware UTC datetime으로 검증해 반환한다.
        인자: 없음
        반환값: UTC datetime
        작성 날짜: 2026/08/22
        """
        result_time = self._result_clock()
        if (
            not isinstance(result_time, datetime)
            or result_time.tzinfo is None
            or result_time.utcoffset() is None
        ):
            raise ValueError("result_clock must return timezone-aware datetime")

        return result_time.astimezone(timezone.utc)  # domain에는 UTC만 전달한다.

    def _map_order_payload(
        self,
        order: Order,
        payload: object,
        *,
        hydrate_fills: bool = False,
    ) -> OrderResult:
        """
        함수 이름: _map_order_payload()
        기능: 한 order payload의 FULL fills 또는 myTrades를 병합해 OrderResult로 변환한다.
        인자: order -> 결과가 가리켜야 할 기존 Order
            payload -> 공식 order response JSON
            hydrate_fills -> executedQty가 있으면 myTrades를 조회할지 여부
        반환값: 누적 fill이 검증된 OrderResult
        작성 날짜: 2026/08/22
        """
        if not isinstance(payload, Mapping):
            raise BinancePayloadError("order response must be an object")
        order_id = payload.get("orderId")
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
            raise BinancePayloadError("orderId must be a positive integer")
        processed_at = self._result_time()

        # FULL response fills를 우선 사용하고 query/cancel/list 응답은 myTrades로 보강한다.
        direct_fill_payloads = payload.get("fills")
        if direct_fill_payloads is not None:
            rules = self._rules_from_order_symbol(order.symbol)
            fills = map_fill_payloads(
                direct_fill_payloads,
                symbol=order.symbol,
                exchange_order_id=str(order_id),
                base_asset=rules.base_asset,
                quote_asset=rules.quote_asset,
                fallback_executed_at=self._payload_processed_time(
                    payload,
                    processed_at,
                ),
            )
        elif hydrate_fills and self._payload_has_executions(payload):
            fills = self._load_order_fills(
                symbol=order.symbol,
                exchange_order_id=order_id,
                fallback_executed_at=processed_at,
            )
        else:
            fills = ()

        return map_order_result(
            payload,
            expected_symbol=order.symbol,
            expected_client_order_id=order.client_order_id,
            fills=fills,
            fallback_processed_at=processed_at,
        )  # mapper가 executedQty와 fill 합계를 마지막으로 검증한다.

    def _map_order_collection(
        self,
        payload: object,
        *,
        expected_symbol: str,
        client_order_id_prefix: str,
    ) -> tuple[OrderResult, ...]:
        """
        함수 이름: _map_order_collection()
        기능: openOrders/allOrders 배열에서 application 주문을 골라 fill과 OrderResult로 변환한다.
        인자: payload -> 공식 order JSON array
            expected_symbol -> 요청한 symbol
            client_order_id_prefix -> 선택할 client ID prefix
        반환값: 응답 순서를 유지한 OrderResult tuple
        작성 날짜: 2026/08/22
        """
        if not isinstance(payload, list):
            raise BinancePayloadError("order collection must be an array")
        results: list[OrderResult] = []

        # Binance endpoint는 prefix 검색을 제공하지 않으므로 clientOrderId를 로컬에서 필터링한다.
        for order_payload in payload:
            if not isinstance(order_payload, Mapping):
                raise BinancePayloadError("each order collection item must be an object")
            client_order_id = order_payload.get("clientOrderId")
            if not isinstance(client_order_id, str):
                raise BinancePayloadError("clientOrderId must be a string")
            if not client_order_id.startswith(client_order_id_prefix):
                continue
            if order_payload.get("symbol") != expected_symbol:
                raise BinancePayloadError("order collection symbol does not match request")
            order_id = order_payload.get("orderId")
            if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
                raise BinancePayloadError("orderId must be a positive integer")

            # Query/list 응답은 fill 배열이 없으므로 executedQty 양수일 때 myTrades를 조회한다.
            processed_at = self._result_time()
            fills = (
                self._load_order_fills(
                    symbol=expected_symbol,
                    exchange_order_id=order_id,
                    fallback_executed_at=processed_at,
                )
                if self._payload_has_executions(order_payload)
                else ()
            )
            results.append(
                map_order_result(
                    order_payload,
                    expected_symbol=expected_symbol,
                    expected_client_order_id=client_order_id,
                    fills=fills,
                    fallback_processed_at=processed_at,
                )
            )

        return tuple(results)  # 공식 chronological 응답 순서를 그대로 유지한다.

    def _load_order_fills(
        self,
        *,
        symbol: str,
        exchange_order_id: int,
        fallback_executed_at: datetime,
    ) -> tuple[Fill, ...]:
        """
        함수 이름: _load_order_fills()
        기능: GET /api/v3/myTrades를 orderId 조건으로 조회해 해당 order의 Fill tuple을 만든다.
        인자: symbol -> 주문 symbol
            exchange_order_id -> Binance orderId
            fallback_executed_at -> trade time 누락 시 사용할 UTC 시각
        반환값: 중복 제거한 Fill tuple
        작성 날짜: 2026/08/22
        """
        # orderId를 보내 weight 20 대신 공식 weight 5 경로를 사용한다.
        response = self._request_json(
            method="GET",
            endpoint="/v3/myTrades",
            parameters={
                "symbol": symbol,
                "orderId": exchange_order_id,
            },
            signed=True,
        )
        rules = self._rules_from_order_symbol(symbol)

        return map_fill_payloads(
            response.payload,
            symbol=symbol,
            exchange_order_id=str(exchange_order_id),
            base_asset=rules.base_asset,
            quote_asset=rules.quote_asset,
            fallback_executed_at=fallback_executed_at,
        )  # trade ID와 order ID를 domain fill 멱등 key로 유지한다.

    def _rules_from_order_symbol(self, symbol: str) -> SymbolTradingRules:
        """
        함수 이름: _rules_from_order_symbol()
        기능: fill fee의 base/quote 자산 확인에 사용할 최신 symbol rules를 조회한다.
        인자: symbol -> 조회할 Spot symbol
        반환값: exchangeInfo에서 해석한 SymbolTradingRules
        작성 날짜: 2026/08/22
        """
        cached_rules = self._symbol_rules_by_symbol.get(symbol)
        if cached_rules is not None:
            return cached_rules  # 같은 process에서 이미 검증한 symbol 자산명을 재사용한다.

        # Reconciliation이 submit보다 먼저 실행된 경우에만 exchangeInfo를 추가 조회한다.
        response = self._request_json(
            method="GET",
            endpoint="/v3/exchangeInfo",
            parameters={"symbol": symbol},
            signed=False,
        )
        selected_rules = parse_symbol_trading_rules(response.payload, symbol)
        self._symbol_rules_by_symbol[symbol] = selected_rules

        return selected_rules  # 자산 이름을 문자열 분해로 추측하지 않는다.

    def _payload_has_executions(self, payload: Mapping[object, object]) -> bool:
        """
        함수 이름: _payload_has_executions()
        기능: order payload의 executedQty가 양수인지 Decimal 문자열로 판정한다.
        인자: payload -> 공식 order response object
        반환값: 체결 수량이 양수이면 True
        작성 날짜: 2026/08/22
        """
        executed_quantity = payload.get("executedQty")
        if not isinstance(executed_quantity, str):
            raise BinancePayloadError("executedQty must be a decimal string")
        try:
            parsed_quantity = Decimal(executed_quantity)
        except Exception as error:
            raise BinancePayloadError("executedQty must be a decimal string") from error
        if not parsed_quantity.is_finite() or parsed_quantity < Decimal("0"):
            raise BinancePayloadError("executedQty must be finite and non-negative")

        return parsed_quantity > Decimal("0")  # 0이면 불필요한 myTrades weight를 사용하지 않는다.

    def _payload_processed_time(
        self,
        payload: Mapping[object, object],
        fallback_time: datetime,
    ) -> datetime:
        """
        함수 이름: _payload_processed_time()
        기능: FULL fill fallback에 사용할 order transact/update/time을 UTC로 변환한다.
        인자: payload -> 공식 order response object
            fallback_time -> 공식 timestamp가 없을 때 사용할 UTC 시각
        반환값: UTC 처리 시각
        작성 날짜: 2026/08/22
        """
        for field_name in ("transactTime", "updateTime", "time"):
            if field_name not in payload:
                continue
            timestamp_value = payload[field_name]
            if (
                isinstance(timestamp_value, bool)
                or not isinstance(timestamp_value, int)
                or timestamp_value < 0
            ):
                raise BinancePayloadError(f"{field_name} must be a non-negative integer")
            return _UNIX_EPOCH + timedelta(milliseconds=timestamp_value)

        return fallback_time.astimezone(timezone.utc)  # timestamp 없는 payload만 주입 시각을 사용한다.

    def _rules_for_fee_assets(self, symbol: str) -> tuple[str, str]:
        """
        함수 이름: _rules_for_fee_assets()
        기능: symbol rules에서 base와 quote asset 이름만 반환한다.
        인자: symbol -> 조회할 Spot symbol
        반환값: base asset과 quote asset tuple
        작성 날짜: 2026/08/22
        """
        rules = self._rules_from_order_symbol(symbol)

        return (rules.base_asset, rules.quote_asset)  # fee mapper에 공식 자산명을 전달한다.

    def _order_identifier_parameters(self, order: Order) -> dict[str, object]:
        """
        함수 이름: _order_identifier_parameters()
        기능: query/cancel에 같은 exchange ID와 client ID를 사용하는 signed parameter를 만든다.
        인자: order -> 식별자를 가진 기존 Order
        반환값: symbol과 하나 또는 두 order 식별자 mapping
        작성 날짜: 2026/08/22
        """
        parameters: dict[str, object] = {
            "symbol": order.symbol,
            "origClientOrderId": order.client_order_id,
        }
        if order.exchange_order_id is not None:
            parameters["orderId"] = int(order.exchange_order_id)

        return parameters  # 두 ID가 있으면 Binance가 orderId 검색 후 client ID를 교차 검증한다.

    def _submission_error_result(
        self,
        order: Order,
        error: BinanceAPIError,
    ) -> OrderResult:
        """
        함수 이름: _submission_error_result()
        기능: 제출 오류를 UNKNOWN 또는 거래소 미도달이 확정된 REJECTED로 분류한다.
        인자: order -> 제출한 Order
            error -> 안전한 HTTP/API 오류
        반환값: normalized OrderResult
        작성 날짜: 2026/08/22
        """
        # 5xx, -1007 계열과 rate limit은 실행 여부를 단정하지 않고 같은 ID 조회로 전환한다.
        if (
            error.status_code >= 500
            or error.status_code in {418, 429}
            or error.api_code in _AMBIGUOUS_API_CODES
            or error.api_code == _RATE_LIMIT_API_CODE
        ):
            return self._unknown_result(
                order,
                _safe_failure_reason("BINANCE_SUBMISSION_UNKNOWN", error.api_code),
                retry_after=error.retry_after,
            )

        # 공식적으로 Matching Engine 미도달이 확정된 code만 terminal rejection으로 좁힌다.
        if error.api_code in _CONFIRMED_NON_EXECUTED_SUBMISSION_API_CODES:
            return self._rejected_result(
                order,
                _safe_failure_reason(
                    "BINANCE_SUBMISSION_REJECTED",
                    error.api_code,
                ),
                failure_kind=OrderResultFailureKind.SUBMISSION_REJECTED,
                retry_after=error.retry_after,
            )

        # -2010은 duplicate-order 등 이미 존재할 수 있는 Matching Engine 사유를 함께 쓰므로 조회만 허용한다.
        return self._unknown_result(
            order,
            _safe_failure_reason("BINANCE_SUBMISSION_UNKNOWN", error.api_code),
            retry_after=error.retry_after,
        )

    def _unknown_result(
        self,
        order: Order,
        failure_reason: str,
        *,
        failure_kind: OrderResultFailureKind | None = None,
        retry_after: timedelta | None = None,
    ) -> OrderResult:
        """
        함수 이름: _unknown_result()
        기능: 같은 주문의 조회만 허용하는 credential-safe UNKNOWN 결과를 만든다.
        인자: order -> 결과가 속한 Order
            failure_reason -> 안전한 내부 분류 문자열
            failure_kind -> 문자열과 분리한 선택 normalized 실패 사실
            retry_after -> 0~30초 대기 또는 None
        반환값: UNKNOWN OrderResult
        작성 날짜: 2026/08/22
        """
        return OrderResult(
            symbol=order.symbol,
            client_order_id=order.client_order_id,
            status=OrderStatus.UNKNOWN,
            processed_at=self._result_time(),
            exchange_order_id=order.exchange_order_id,
            failure_reason=failure_reason,
            failure_kind=failure_kind,
            retry_after=retry_after,
        )  # 신규 client ID를 만들 정보는 결과에 포함하지 않는다.

    def _rejected_result(
        self,
        order: Order,
        failure_reason: str,
        *,
        failure_kind: OrderResultFailureKind | None = None,
        retry_after: timedelta | None = None,
    ) -> OrderResult:
        """
        함수 이름: _rejected_result()
        기능: Matching Engine 실행이 없는 known-terminal 제출 거부 결과를 만든다.
        인자: order -> 거부된 Order
            failure_reason -> 안전한 filter/API 분류 문자열
            failure_kind -> 문자열과 분리한 선택 normalized 실패 사실
            retry_after -> 선택 제한 대기 시간
        반환값: fill 없는 REJECTED OrderResult
        작성 날짜: 2026/08/22
        """
        return OrderResult(
            symbol=order.symbol,
            client_order_id=order.client_order_id,
            status=OrderStatus.REJECTED,
            processed_at=self._result_time(),
            failure_reason=failure_reason,
            failure_kind=failure_kind,
            retry_after=retry_after,
        )  # terminal rejection에는 존재하지 않는 exchange order ID를 만들지 않는다.

    @staticmethod
    def _normalize_symbol(symbol: object) -> str:
        """
        함수 이름: _normalize_symbol()
        기능: Spot REST symbol을 공백 없는 ASCII 대문자 영숫자로 검증한다.
        인자: symbol -> 검증할 symbol 후보
        반환값: canonical symbol
        작성 날짜: 2026/08/22
        """
        if not isinstance(symbol, str):
            raise TypeError("symbol must be a string")
        normalized_symbol = symbol.strip().upper()
        if (
            not normalized_symbol
            or not normalized_symbol.isascii()
            or not normalized_symbol.isalnum()
        ):
            raise ValueError("symbol must contain only ASCII letters and digits")

        return normalized_symbol  # 공식 REST query에는 canonical 대문자 symbol을 보낸다.

    @staticmethod
    def _normalize_client_order_id_prefix(prefix: object) -> str:
        """
        함수 이름: _normalize_client_order_id_prefix()
        기능: reconciliation local filter에 사용할 공백 없는 ASCII prefix를 검증한다.
        인자: prefix -> 검증할 client order ID prefix
        반환값: 검증된 prefix
        작성 날짜: 2026/08/22
        """
        if not isinstance(prefix, str):
            raise TypeError("client_order_id_prefix must be a string")
        if not prefix or prefix != prefix.strip() or not prefix.isascii():
            raise ValueError("client_order_id_prefix must be non-empty trimmed ASCII")

        return prefix  # allOrders/openOrders local filtering에만 사용한다.

    @staticmethod
    def _datetime_to_milliseconds(value: object, field_name: str) -> int:
        """
        함수 이름: _datetime_to_milliseconds()
        기능: timezone-aware datetime을 부동소수 오차 없는 epoch milliseconds로 변환한다.
        인자: value -> 변환할 datetime
            field_name -> 오류에 사용할 필드 이름
        반환값: epoch milliseconds
        작성 날짜: 2026/08/22
        """
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ValueError(f"{field_name} must be timezone-aware datetime")
        elapsed_time = value.astimezone(timezone.utc) - _UNIX_EPOCH

        return (
            elapsed_time.days * 86_400_000
            + elapsed_time.seconds * 1_000
            + elapsed_time.microseconds // 1_000
        )  # Binance timestamp 단위에 맞춰 microseconds 아래를 버린다.
