"""명시적 opt-in에서 Binance Spot Testnet read-only parity를 검증한다."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from binance_auto_trader.bootstrap.application import ExecutionMode
from binance_auto_trader.bootstrap.testnet import (
    create_testnet_application_runtime,
    load_testnet_configuration,
)
from binance_auto_trader.domain.market import SUPPORTED_INTERVALS

from tests.testnet._support import (
    READ_ONLY_SKIP_REASON,
    READ_ONLY_TESTNET_REQUESTED,
)


@unittest.skipUnless(
    READ_ONLY_TESTNET_REQUESTED,
    READ_ONLY_SKIP_REASON,
)
class BinanceTestnetReadOnlyTests(unittest.TestCase):
    """
    클래스 이름: BinanceTestnetReadOnlyTests
    기능: 고정 testnet endpoint의 Kline, account와 order 조회 정규화를 실제 검증한다.
    작성 날짜: 2026/08/22
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: opt-in 설정을 검증하고 network 미연결 runtime을 임시 history로 조립한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # flag만 켜고 credential을 빠뜨린 실행은 skip으로 숨기지 않고 설정 오류로 실패한다.
        self.configuration = load_testnet_configuration()
        self.temporary_directory = TemporaryDirectory()
        history_path = Path(self.temporary_directory.name) / "history.jsonl"
        self.runtime = create_testnet_application_runtime(
            history_path=history_path,
            kline_limit=2,
        )  # 실제 I/O는 각 test operation이 호출될 때만 시작한다.

    def tearDown(self) -> None:
        """
        함수 이름: tearDown()
        기능: read-only test가 만든 local history directory를 정리한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        self.temporary_directory.cleanup()  # credential과 network session은 파일에 저장되지 않는다.

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

        # signed account와 public Kline은 주문 operation 없이 같은 testnet REST client를 사용한다.
        account_snapshot = self.runtime.api_gateway.fetch_account_snapshot()
        balance_assets = frozenset(
            balance.asset for balance in account_snapshot.balances
        )
        self.assertTrue(account_snapshot.is_full_snapshot)
        self.assertIn("ETH", balance_assets)
        self.assertIn("USDT", balance_assets)

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
            all(result.symbol == "ETHUSDT" for result in (*open_results, *recent_results))
        )


if __name__ == "__main__":
    unittest.main()
