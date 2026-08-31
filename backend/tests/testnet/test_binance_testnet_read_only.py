"""명시적 opt-in에서 Binance Spot Testnet read-only parity를 검증한다."""

import os
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
import unittest
from unittest.mock import patch

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

from tests.testnet._support import (
    READ_ONLY_SKIP_REASON,
    READ_ONLY_TESTNET_REQUESTED,
    seed_verified_closed_history,
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
        seed_verified_closed_history(history_path)
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
        balance_assets = frozenset(
            balance.asset for balance in account_snapshot.balances
        )
        self.assertTrue(account_snapshot.is_full_snapshot)
        self.assertIn("ETH", balance_assets)
        self.assertIn("USDT", balance_assets)
        self.assertEqual(commission_policy.symbol, "ETHUSDT")
        # 실제 주문 전 Roadmap gate를 증명하도록 세 유형의 MARKET BUY 비율을 각각 0으로 요구한다.
        self.assertEqual(
            commission_policy.standard_market_buy_rate,
            Decimal("0"),
        )
        self.assertEqual(
            commission_policy.special_market_buy_rate,
            Decimal("0"),
        )
        self.assertEqual(
            commission_policy.tax_market_buy_rate,
            Decimal("0"),
        )
        self.assertIsNone(
            commission_policy.discount_asset,
        )  # Null policy는 parser가 세 group의 raw 12개 비율 전체 0을 확인한 뒤에만 생성한다.
        self.assertEqual(
            commission_policy.discount_rate,
            Decimal("0"),
        )
        self.assertFalse(
            commission_policy.can_charge_discount_asset
            and commission_policy.discount_asset not in {"ETH", "USDT"},
            "Unsupported third-asset commission discount must be disabled before orders",
        )  # BNB 등 제3 자산 수수료 가능성은 실제 BUY보다 먼저 credential-safe read-only로 차단한다.

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

        # 복구용 조회도 raw mapping 없이 tuple 결과만 application 경계에 공개해야 한다.
        open_results = self.runtime.api_gateway.list_open_order_results(
            "ETHUSDT"
        )
        recent_results = self.runtime.api_gateway.list_recent_order_results(
            "ETHUSDT",
            limit=10,
        )
        self.assertIsInstance(open_results, tuple)
        self.assertIsInstance(recent_results, tuple)
        self.assertTrue(
            all(
                result.symbol == "ETHUSDT"
                for result in (*open_results, *recent_results)
            )
        )

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
