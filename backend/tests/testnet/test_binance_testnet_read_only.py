"""명시적 opt-in에서 Binance Spot Testnet read-only parity를 검증한다."""

import os
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.binance import (
    AccountAssetFilter,
    AccountRelevantFilters,
    CommissionDiscountPolicy,
    ReferencePrice,
    SymbolTradingRules,
)
from binance_auto_trader.adapters.binance.mappers import (
    validate_account_relevant_filters,
)
from binance_auto_trader.bootstrap import (
    ApplicationStatus,
    ExecutionMode,
    close_application,
    start_application,
)
from binance_auto_trader.bootstrap.testnet import (
    BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV,
    BINANCE_RUN_TESTNET_ENV,
    BINANCE_RUN_TESTNET_ORDERS_ENV,
    BINANCE_TESTNET_API_KEY_ENV,
    BINANCE_TESTNET_API_SECRET_ENV,
    BINANCE_TESTNET_MAX_NOTIONAL_ENV,
    create_testnet_application_runtime,
    load_testnet_configuration,
)
from binance_auto_trader.domain.market import Kline, SUPPORTED_INTERVALS
from binance_auto_trader.domain.trading.states import OrderSide

from tests.testnet._support import (
    READ_ONLY_SKIP_REASON,
    READ_ONLY_TESTNET_REQUESTED,
    require_empty_all_client_open_orders,
    seed_verified_closed_history,
    verify_exact_recent_order_baseline,
)


_READ_ONLY_COMMISSION_FAILURE_MESSAGE = (
    "Read-only preflight requires zero MARKET BUY commission"
)
_READ_ONLY_REFERENCE_PRICE_FAILURE_MESSAGE = (
    "Read-only preflight requires a positive reference price"
)
_READ_ONLY_MAX_POSITION_FAILURE_MESSAGE = (
    "Read-only preflight requires MAX_POSITION to be absent"
)


def _build_read_only_testnet_environment() -> dict[str, str]:
    """
    함수 이름: _build_read_only_testnet_environment()
    기능: collection에서 확인한 credential만 read-only Testnet 설정으로 격리한다.
    인자: 없음
    반환값: 주문 권한과 notional cap이 없는 최소 Testnet 환경 mapping
    작성 날짜: 2026/08/24
    """
    # Order lifecycle opt-in과 같은 process에서 실행돼도 이 runtime에는 주문 권한을 전달하지 않는다.
    return {
        BINANCE_RUN_TESTNET_ENV: "1",
        BINANCE_TESTNET_API_KEY_ENV: os.environ[BINANCE_TESTNET_API_KEY_ENV],
        BINANCE_TESTNET_API_SECRET_ENV: os.environ[
            BINANCE_TESTNET_API_SECRET_ENV
        ],
        BINANCE_RUN_TESTNET_ORDERS_ENV: "0",
        BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV: "0",
    }  # Credential 값은 client 조립에만 사용하고 assertion이나 출력에는 포함하지 않는다.


def _require_zero_read_only_commission(
    commission_policy: CommissionDiscountPolicy,
) -> None:
    """
    함수 이름: _require_zero_read_only_commission()
    기능: 실제 account commission 값을 출력하지 않고 안전한 zero 정책인지 검증한다.
    인자: commission_policy -> signed account 응답에서 정규화한 수수료 정책
    반환값: MARKET BUY 비율·할인율이 zero이고 discount asset이 없으면 없음
    작성 날짜: 2026/08/31
    """
    # Exact policy의 모든 실제 rate와 discount asset을 한 고정 문장 뒤에서 함께 검사한다.
    if (
        type(commission_policy) is not CommissionDiscountPolicy
        or commission_policy.discount_asset is not None
        or any(
            commission_rate != Decimal("0")
            for commission_rate in (
                commission_policy.discount_rate,
                commission_policy.standard_market_buy_rate,
                commission_policy.special_market_buy_rate,
                commission_policy.tax_market_buy_rate,
                commission_policy.market_buy_received_asset_commission_rate,
            )
        )
    ):
        raise AssertionError(_READ_ONLY_COMMISSION_FAILURE_MESSAGE)

    return None  # 성공 여부만 남기고 account-specific rate는 unittest 출력에 복제하지 않는다.


def _require_positive_read_only_reference_price(reference_price: object) -> None:
    """
    함수 이름: _require_positive_read_only_reference_price()
    기능: 실제 public price를 출력하지 않고 양수 유한 Decimal인지 검증한다.
    인자: reference_price -> strict ReferencePrice DTO에서 읽은 가격
    반환값: 양수 유한 Decimal이면 없음
    작성 날짜: 2026/08/31
    """
    # Type drift·NaN·무한·0 이하 가격을 값 없는 동일 failure로 닫는다.
    if (
        type(reference_price) is not Decimal
        or not reference_price.is_finite()
        or reference_price <= Decimal("0")
    ):
        raise AssertionError(_READ_ONLY_REFERENCE_PRICE_FAILURE_MESSAGE)

    return None  # 검증한 market price 원문은 assertion message에 넣지 않는다.


def _require_absent_read_only_maximum_position(
    maximum_position: object,
) -> None:
    """
    함수 이름: _require_absent_read_only_maximum_position()
    기능: 실제 MAX_POSITION 값을 출력하지 않고 filter 부재를 검증한다.
    인자: maximum_position -> strict symbol rules의 optional account 상한
    반환값: filter가 없으면 없음
    작성 날짜: 2026/08/31
    """
    # Non-null account filter는 값과 무관하게 고정 문장으로 actual mutation 전 차단한다.
    if maximum_position is not None:
        raise AssertionError(_READ_ONLY_MAX_POSITION_FAILURE_MESSAGE)

    return None  # Optional filter value는 실패·성공 출력 어느 쪽에도 반사하지 않는다.


class ReadOnlyTestnetEnvironmentIsolationTests(unittest.TestCase):
    """
    클래스 이름: ReadOnlyTestnetEnvironmentIsolationTests
    기능: 외부 network 없이 read-only preflight 환경 격리 계약을 검증한다.
    작성 날짜: 2026/08/24
    """

    def test_builder_discards_order_permission_and_notional_cap(self) -> None:
        """
        함수 이름: test_builder_discards_order_permission_and_notional_cap()
        기능: 상위 order opt-in이 최소 read-only mapping으로 전파되지 않음을 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Fixture credential은 실제 값 없이 loader 경계와 환경 key 선택만 검증한다.
        parent_environment = {
            BINANCE_RUN_TESTNET_ENV: "1",
            BINANCE_TESTNET_API_KEY_ENV: "fixture-api-key",
            BINANCE_TESTNET_API_SECRET_ENV: "fixture-api-secret",
            BINANCE_RUN_TESTNET_ORDERS_ENV: "1",
            BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV: "1",
            BINANCE_TESTNET_MAX_NOTIONAL_ENV: "20",
        }
        with patch.dict(os.environ, parent_environment, clear=True):
            read_only_environment = _build_read_only_testnet_environment()
        configuration = load_testnet_configuration(read_only_environment)

        # Assertion은 credential 값 대신 공개 가능한 variable 이름과 permission만 비교한다.
        self.assertEqual(
            frozenset(read_only_environment),
            frozenset(
                {
                    BINANCE_RUN_TESTNET_ENV,
                    BINANCE_TESTNET_API_KEY_ENV,
                    BINANCE_TESTNET_API_SECRET_ENV,
                    BINANCE_RUN_TESTNET_ORDERS_ENV,
                    BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV,
                }
            ),
        )
        self.assertFalse(configuration.allow_testnet_orders)
        self.assertFalse(configuration.allow_phase13_public_case2)
        self.assertIsNone(configuration.max_notional)

    def test_sensitive_read_only_preflight_uses_fixed_failure_output(
        self,
    ) -> None:
        """
        함수 이름: test_sensitive_read_only_preflight_uses_fixed_failure_output()
        기능: Commission·price·MAX_POSITION 실패 출력에 실제 Decimal 값이 없는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/31
        """
        nonzero_commission_policy = CommissionDiscountPolicy(
            symbol="ETHUSDT",
            enabled_for_account=False,
            enabled_for_symbol=False,
            discount_asset="USDT",
            discount_rate=Decimal("0"),
            standard_market_buy_rate=Decimal("0.12345678"),
            special_market_buy_rate=Decimal("0"),
            tax_market_buy_rate=Decimal("0"),
        )

        # 세 external numeric canary를 독립 unittest failure로 실행해 stderr redaction을 고정한다.
        guarded_cases = (
            (
                lambda: _require_zero_read_only_commission(
                    nonzero_commission_policy
                ),
                _READ_ONLY_COMMISSION_FAILURE_MESSAGE,
                "0.12345678",
            ),
            (
                lambda: _require_positive_read_only_reference_price(
                    Decimal("-8765.4321")
                ),
                _READ_ONLY_REFERENCE_PRICE_FAILURE_MESSAGE,
                "-8765.4321",
            ),
            (
                lambda: _require_absent_read_only_maximum_position(
                    Decimal("9876.54321")
                ),
                _READ_ONLY_MAX_POSITION_FAILURE_MESSAGE,
                "9876.54321",
            ),
        )
        for guarded_check, fixed_message, forbidden_value in guarded_cases:
            with self.subTest(fixed_message=fixed_message):

                def run_guarded_check() -> None:
                    """
                    함수 이름: run_guarded_check()
                    기능: Read-only 민감 수치 guard 하나를 독립 failure 경계에서 호출한다.
                    인자: 없음
                    반환값: 없음
                    작성 날짜: 2026/08/31
                    """
                    guarded_check()  # Actual preflight와 같은 fixed-message helper를 호출한다.

                failure_output = StringIO()
                failure_result = unittest.TextTestRunner(
                    stream=failure_output,
                    verbosity=2,
                    failfast=True,
                ).run(unittest.FunctionTestCase(run_guarded_check))
                captured_output = failure_output.getvalue()

                # Generic blocker만 확인하고 synthetic account value는 출력 전체에서 금지한다.
                self.assertFalse(failure_result.wasSuccessful())
                self.assertIn(fixed_message, captured_output)
                self.assertNotIn(
                    forbidden_value,
                    captured_output,
                )  # Raw commission·price·filter canary를 stderr에 남기지 않는다.


@unittest.skipUnless(
    READ_ONLY_TESTNET_REQUESTED,
    READ_ONLY_SKIP_REASON,
)
class BinanceTestnetReadOnlyTests(unittest.TestCase):
    """
    클래스 이름: BinanceTestnetReadOnlyTests
    기능: 고정 Testnet의 authenticated REST와 signed account stream lifecycle을 검증한다.
    작성 날짜: 2026/08/22
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: opt-in 설정과 optional closed baseline을 검증하고 network 미연결 runtime을 조립한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 별도 read-only mapping은 상위 shell의 주문 opt-in이나 cap을 runtime에 전파하지 않는다.
        self.testnet_environment = _build_read_only_testnet_environment()
        self.configuration = load_testnet_configuration(
            self.testnet_environment
        )
        self.temporary_directory = TemporaryDirectory()
        history_path = Path(self.temporary_directory.name) / "history.jsonl"

        # 이전 actual run이 있으면 pending 0·Position 0으로 검증된 closed history만 startup에 복제한다.
        self.baseline_trades = seed_verified_closed_history(history_path)
        self.runtime = create_testnet_application_runtime(
            history_path=history_path,
            environment=self.testnet_environment,
        )  # 실제 I/O는 각 test operation이 호출될 때만 시작한다.

    def tearDown(self) -> None:
        """
        함수 이름: tearDown()
        기능: read-only test의 network 자원과 local history directory를 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # Startup assertion이 실패해도 공개 close 경계로 모든 WebSocket과 worker를 먼저 회수한다.
        try:
            close_application(self.runtime)
        finally:
            self.temporary_directory.cleanup()  # Credential과 network session은 파일에 저장되지 않는다.

    def test_account_kline_and_order_queries_use_normalized_contracts(
        self,
    ) -> None:
        """
        함수 이름: test_account_kline_and_order_queries_use_normalized_contracts()
        기능: 실제 account, 네 Kline interval과 open/recent order 조회의 domain shape를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.assertIs(self.runtime.execution_mode, ExecutionMode.TESTNET)
        self.assertFalse(self.configuration.allow_testnet_orders)
        self.assertIsNone(self.configuration.max_notional)

        # signed account와 public Kline은 주문 operation 없이 같은 testnet REST client를 사용한다.
        account_snapshot = self.runtime.api_gateway.fetch_account_snapshot()
        commission_policy = (
            self.runtime.api_gateway.fetch_commission_discount_policy(
                "ETHUSDT"
            )
        )
        account_filters = (
            self.runtime.api_gateway.fetch_account_relevant_filters(
                "ETHUSDT"
            )
        )
        reference_price = self.runtime.api_gateway.fetch_reference_price(
            "ETHUSDT"
        )
        symbol_rules = self.runtime.api_gateway.fetch_symbol_trading_rules(
            "ETHUSDT"
        )
        balance_assets = frozenset(
            balance.asset for balance in account_snapshot.balances
        )
        self.assertTrue(account_snapshot.is_full_snapshot)
        self.assertIn("ETH", balance_assets)
        self.assertIn("USDT", balance_assets)
        self.assertEqual(commission_policy.symbol, "ETHUSDT")
        # 실제 주문 전 Roadmap gate는 값 repr 없이 MARKET BUY와 discount 정책 전체를 zero로 요구한다.
        _require_zero_read_only_commission(commission_policy)
        self.assertFalse(
            commission_policy.can_charge_discount_asset
            and commission_policy.discount_asset not in {"ETH", "USDT"},
            "Unsupported third-asset commission discount must be disabled before orders",
        )  # BNB 등 제3 자산 수수료 가능성은 실제 BUY보다 먼저 credential-safe read-only로 차단한다.
        self.assertIs(type(account_filters), AccountRelevantFilters)
        self.assertEqual(account_filters.symbol, "ETHUSDT")
        self.assertTrue(
            all(
                type(account_filter) is AccountAssetFilter
                for account_filter in account_filters.asset_filters
            )
        )
        self.assertIs(type(reference_price), ReferencePrice)
        self.assertEqual("ETHUSDT", reference_price.symbol)
        _require_positive_read_only_reference_price(reference_price.price)
        self.assertIs(type(symbol_rules), SymbolTradingRules)
        self.assertEqual("ETHUSDT", symbol_rules.symbol)
        _require_absent_read_only_maximum_position(
            symbol_rules.maximum_position
        )  # Non-null filter value는 고정 문장 밖으로 나오지 않는다.

        # Exchange-wide count filter는 symbol 생략 openOrders와 전용 openOrderList의 exact zero를 요구한다.
        self.assertFalse(
            self.runtime.api_gateway.has_any_exchange_open_orders(),
            "Read-only preflight requires zero exchange-wide open orders",
        )
        self.assertFalse(
            self.runtime.api_gateway.has_any_exchange_open_order_lists(),
            "Read-only preflight requires zero exchange-wide open order lists",
        )
        minimum_candidate_quantity = max(
            Decimal("1").scaleb(-symbol_rules.base_asset_precision),
            symbol_rules.lot_size.minimum_quantity,
            symbol_rules.market_lot_size.minimum_quantity,
            symbol_rules.lot_size.step_size,
            symbol_rules.market_lot_size.step_size,
        )
        validate_account_relevant_filters(
            minimum_candidate_quantity,
            reference_price.price,
            symbol_rules,
            account_filters,
            side=OrderSide.BUY,
            account_open_state_verified_empty=True,
        )

        klines_by_interval = self.runtime.api_gateway.load_all_klines(
            "ETHUSDT",
            limit=2,
        )
        self.assertEqual(
            tuple(klines_by_interval),
            SUPPORTED_INTERVALS,
        )
        self.assertTrue(
            all(
                klines
                and all(kline.symbol == "ETHUSDT" for kline in klines)
                for klines in klines_by_interval.values()
            )
        )

        # 격리용 조회는 manual client ID까지 포함하되 raw mapping 없이 tuple 결과만 공개한다.
        open_results = self.runtime.api_gateway.list_all_open_order_results(
            "ETHUSDT"
        )
        recent_results = self.runtime.api_gateway.list_all_recent_order_results(
            "ETHUSDT",
            limit=1000,
        )
        require_empty_all_client_open_orders(open_results)
        verify_exact_recent_order_baseline(
            self.baseline_trades,
            recent_results,
        )
        self.assertTrue(
            all(
                result.symbol == "ETHUSDT"
                for result in (*open_results, *recent_results)
            )
        )
        print(
            "PHASE13_READ_ONLY_BASELINE "
            f"all_open_order_count={len(open_results)} "
            f"all_recent_order_count={len(recent_results)}"
        )  # Credential, balance, order ID·length은 출력하지 않고 baseline 판정 개수만 남긴다.

    def test_application_ready_opens_signed_account_stream_and_closes(
        self,
    ) -> None:
        """
        함수 이름: test_application_ready_opens_signed_account_stream_and_closes()
        기능: production startup으로 서명 User Data Stream ACK와 READY, close를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/24
        """
        # Read-only configuration을 먼저 확인해 startup 중 mutation 가능한 REST surface를 차단한다.
        self.assertFalse(self.configuration.allow_testnet_orders)
        self.assertIsNone(self.configuration.max_notional)

        # Public lifecycle은 REST snapshot 뒤 서명 account stream이 연결된 경우에만 READY를 반환한다.
        ready_state = start_application(self.runtime)
        self.assertIs(ready_state.status, ApplicationStatus.READY)
        self.assertTrue(self.runtime.account.ready)
        self.assertTrue(self.runtime.web_socket_gateway.account_connected)
        self.assertTrue(self.runtime.web_socket_gateway.account_ready)
        self.assertIsNotNone(
            self.runtime.trading_controller.account_subscription
        )  # Controller도 같은 authenticated subscription handle을 소유해야 한다.

        # 명시적 public close가 stream 상태와 application lifecycle을 함께 닫는지 확인한다.
        closed_state = close_application(self.runtime)
        self.assertIs(closed_state.status, ApplicationStatus.CLOSED)
        self.assertFalse(self.runtime.web_socket_gateway.account_connected)
        self.assertFalse(self.runtime.web_socket_gateway.account_ready)

    def test_public_kline_subscription_promotes_same_generation_live_observer(
        self,
    ) -> None:
        """
        함수 이름: test_public_kline_subscription_promotes_same_generation_live_observer()
        기능: 실제 Testnet Kline 연결이 재구독 없이 같은 세대의 live observer로 승격되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/28
        """
        observed_intervals = set()
        observation_lock = Lock()

        def observe_kline(kline: Kline) -> None:
            """
            함수 이름: observe_kline()
            기능: credential 없는 실제 Kline의 interval을 모아 모든 구독 stream 수신을 알린다.
            인자: kline -> WebSocketGateway가 정규화한 Kline
            반환값: 없음
            작성 날짜: 2026/08/28
            """
            # Callback thread가 같은 set을 갱신하므로 lock 안에서 완료 조건까지 함께 판정한다.
            with observation_lock:
                observed_intervals.add(kline.interval)

        # 네 public stream을 한 세대로 열고 buffer replay 직후 동일 handle을 live로 승격한다.
        subscription = self.runtime.web_socket_gateway.start_all_kline_buffering(
            "ETHUSDT",
            SUPPORTED_INTERVALS,
        )
        try:
            promoted_subscription = (
                self.runtime.web_socket_gateway.promote_kline_buffer_to_live(
                    observe_kline,
                    subscription,
                )
            )
            self.assertIs(promoted_subscription, subscription)
            self.assertTrue(self.runtime.web_socket_gateway.kline_live_ready)
            with observation_lock:
                self.assertTrue(
                    observed_intervals.issubset(set(SUPPORTED_INTERVALS))
                )  # Testnet 무거래 구간에는 frame이 없을 수 있어 수신된 값의 계약만 검증한다.
        finally:
            subscription.close()  # 실패 시에도 public socket과 receive worker를 즉시 회수한다.

        self.assertFalse(self.runtime.web_socket_gateway.kline_live_ready)


if __name__ == "__main__":
    unittest.main()
