"""Gateway가 사용하는 명시적 REST 조회 surface만 공유하는 기술 facade이다."""

from binance_auto_trader.adapters.binance.mappers import (
    OrderPreparationFilterEvidence, OrderSubmissionAttemptEvidence, ReferencePrice, SymbolTradingRules,
)
from binance_auto_trader.domain.trading.order import Fill, Order, OrderResult
from binance_auto_trader.domain.trading.account_execution import AccountExecution


class BinanceReadOnlyRESTFacade:
    """
    클래스 이름: BinanceReadOnlyRESTFacade
    기능: mode별 permission adapter가 공유하는 명시적 조회 operation만 전달한다.
    작성 날짜: 2026/09/08
    """

    __slots__ = ()  # Credential과 delegate 저장 책임은 mode별 adapter에 남긴다.

    def restore_historical_fee_fills(
        self,
        *,
        symbol: str,
        client_order_id: str,
        exchange_order_id: str,
        fills: tuple[Fill, ...],
    ) -> None:
        """
        함수 이름: restore_historical_fee_fills()
        기능: 검증된 과거 체결 근거를 delegate의 동일 주문 읽기 복구에 전달한다.
        인자: symbol -> 저장 거래 symbol
            client_order_id -> 원 client 주문 ID
            exchange_order_id -> 거래소 주문 ID
            fills -> 검증된 v3/v4 저장 체결 tuple
        반환값: 없음
        작성 날짜: 2026/09/22
        """
        self._delegate.restore_historical_fee_fills(
            symbol=symbol,
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            fills=fills,
        )

    def preview_cached_buy_quantity(self, *, symbol, quantity, price):
        """
        함수 이름: preview_cached_buy_quantity()
        기능: 네트워크 없는 보류 판단만 mode별 delegate에 전달한다.
        인자: symbol, quantity, price -> 예산 계산 입력
        반환값: 로컬 예상 수량 또는 미지원 None
        작성 날짜: 2026/09/18
        """
        preview = getattr(self._delegate, "preview_cached_buy_quantity", None)
        return preview(symbol=symbol, quantity=quantity, price=price) if callable(preview) else None

    def discard_unsubmitted_preparation(self, *, order):
        """
        함수 이름: discard_unsubmitted_preparation()
        기능: 외부 주문 없이 포기한 준비 자료만 delegate에서 제거한다.
        인자: order -> journal 이전 주문
        반환값: 정리 여부 또는 미지원 False
        작성 날짜: 2026/09/18
        """
        discard = getattr(self._delegate, "discard_unsubmitted_preparation", None)
        return discard(order=order) if callable(discard) else False

    def fetch_earn_residual_evidence(self, *, since):
        """
        함수 이름: fetch_earn_residual_evidence()
        기능: 예치 조회를 지원하는 live delegate에만 읽기 전용 근거 조회를 전달한다.
        인자: since -> 잔여 발생 시각
        반환값: 예치 근거 또는 미지원 None
        작성 날짜: 2026/09/15
        """
        reader = getattr(self._delegate, "fetch_earn_residual_evidence", None)
        return reader(since=since) if callable(reader) else None

    def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> object:
        """
        함수 이름: get_klines()
        기능: 허용된 공개 market 조회만 고정 Spot delegate에 전달한다.
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
        기능: 허용된 signed account 조회를 고정 Spot delegate에 전달한다.
        인자: 없음
        반환값: 공식 Spot account payload
        작성 날짜: 2026/08/23
        """
        # Account 조회는 주문 mutation이 아니며 APIGateway의 정규화 경계를 그대로 통과한다.
        return self._delegate.get_account()

    def get_account_commission(self, *, symbol: str) -> object:
        """
        함수 이름: get_account_commission()
        기능: 허용된 signed account commission 조회를 고정 Spot delegate에 전달한다.
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
        기능: 허용된 signed myFilters 계정 조회를 고정 Spot delegate에 전달한다.
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
        기능: 허용된 open-order 조회를 고정 Spot delegate에 전달한다.
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
        기능: 허용된 recent-order 조회를 고정 Spot delegate에 전달한다.
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

    def list_account_executions_since(self, *, symbol: str, order_id: str) -> tuple[AccountExecution, ...] | None:
        """
        함수 이름: list_account_executions_since()
        기능: 주문 권한 없이 동일 delegate의 외부 체결 복구 조회를 전달한다.
        인자: symbol -> 상품, order_id -> durable 주문 ID
        반환값: 완전한 체결 tuple 또는 미지원 None
        작성 날짜: 2026/09/10
        """
        reader = getattr(self._delegate, "list_account_executions_since", None)
        return reader(symbol=symbol, order_id=order_id) if callable(reader) else None

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
