"""Account의 Decimal 잔액, 평가금액과 snapshot 적용 계약을 검증한다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from binance_auto_trader.domain.trading.account import (
    Account,
    AccountSnapshot,
    AssetBalance,
)


INITIAL_UPDATED_AT = datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc)
STREAM_UPDATED_AT = datetime(2026, 8, 21, 0, 1, tzinfo=timezone.utc)


def _balance(
    asset: str,
    free: str,
    locked: str,
) -> AssetBalance:
    """
    함수 이름: _balance()
    기능: 문자열 fixture로 Decimal 자산 잔액을 생성한다.
    인자: asset -> 자산 코드
        free -> 사용 가능 잔액 문자열
        locked -> 잠긴 잔액 문자열
    반환값: 불변 AssetBalance fixture
    작성 날짜: 2026/08/21
    """
    return AssetBalance(
        asset=asset,
        free=Decimal(free),
        locked=Decimal(locked),
    )


def _snapshot(
    *balances: AssetBalance,
    updated_at: datetime = INITIAL_UPDATED_AT,
    is_full_snapshot: bool = True,
) -> AccountSnapshot:
    """
    함수 이름: _snapshot()
    기능: Account 적용 시나리오에 사용할 불변 snapshot을 생성한다.
    인자: balances -> snapshot에 포함할 자산 잔액들
        updated_at -> Binance 계좌 갱신 UTC 시각
        is_full_snapshot -> 전체 REST snapshot 여부
    반환값: 불변 AccountSnapshot fixture
    작성 날짜: 2026/08/21
    """
    return AccountSnapshot(
        balances=tuple(balances),
        updated_at=updated_at,
        is_full_snapshot=is_full_snapshot,
    )


class AssetBalanceTests(unittest.TestCase):
    """
    클래스 이름: AssetBalanceTests
    기능: 자산 잔액 value object의 Decimal과 정규화 불변식을 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_preserves_exact_decimal_free_and_locked_balances(self) -> None:
        """
        함수 이름: test_preserves_exact_decimal_free_and_locked_balances()
        기능: free와 locked가 float 변환 없이 Decimal로 보존되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        balance = _balance("ETH", "1.25000000", "0.12500000")

        self.assertEqual(balance.asset, "ETH")
        self.assertEqual(balance.free, Decimal("1.25000000"))
        self.assertEqual(balance.locked, Decimal("0.12500000"))
        self.assertIsInstance(balance.free, Decimal)
        self.assertIsInstance(balance.locked, Decimal)

    def test_rejects_non_decimal_and_negative_balances(self) -> None:
        """
        함수 이름: test_rejects_non_decimal_and_negative_balances()
        기능: 금융 잔액의 float 유입과 음수 잔액을 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        invalid_arguments = (
            {"asset": "ETH", "free": 1.0, "locked": Decimal("0")},
            {"asset": "ETH", "free": Decimal("-1"), "locked": Decimal("0")},
            {"asset": "ETH", "free": Decimal("1"), "locked": Decimal("-1")},
        )

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises((TypeError, ValueError)):
                    AssetBalance(**arguments)


class AccountSnapshotTests(unittest.TestCase):
    """
    클래스 이름: AccountSnapshotTests
    기능: AccountSnapshot의 tuple 잔액과 UTC 시각 계약을 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_preserves_tuple_balances_utc_time_and_snapshot_kind(self) -> None:
        """
        함수 이름: test_preserves_tuple_balances_utc_time_and_snapshot_kind()
        기능: snapshot이 자산 tuple과 canonical UTC, full flag를 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        balances = (
            _balance("ETH", "1", "0.1"),
            _balance("USDT", "20", "3"),
        )

        snapshot = AccountSnapshot(
            balances=balances,
            updated_at=INITIAL_UPDATED_AT,
            is_full_snapshot=True,
        )

        self.assertIsInstance(snapshot.balances, tuple)
        self.assertEqual(snapshot.balances, balances)
        self.assertEqual(snapshot.updated_at, INITIAL_UPDATED_AT)
        self.assertIs(snapshot.updated_at.tzinfo, timezone.utc)
        self.assertTrue(snapshot.is_full_snapshot)

    def test_rejects_naive_or_non_utc_updated_at(self) -> None:
        """
        함수 이름: test_rejects_naive_or_non_utc_updated_at()
        기능: domain snapshot에 naive 또는 UTC가 아닌 시각이 들어오지 못하게 한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        invalid_times = (
            datetime(2026, 8, 21, 0, 0),
            datetime(
                2026,
                8,
                21,
                9,
                0,
                tzinfo=timezone(timedelta(hours=9)),
            ),
        )

        for invalid_time in invalid_times:
            with self.subTest(invalid_time=invalid_time):
                with self.assertRaises((TypeError, ValueError)):
                    _snapshot(
                        _balance("ETH", "1", "0"),
                        updated_at=invalid_time,
                    )


class AccountTests(unittest.TestCase):
    """
    클래스 이름: AccountTests
    기능: Account의 full/partial snapshot 적용과 ETH 평가 상태를 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_starts_empty_not_ready_at_version_zero(self) -> None:
        """
        함수 이름: test_starts_empty_not_ready_at_version_zero()
        기능: 초기 Account가 빈 잔액과 version 0의 fail-closed 상태인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        account = Account()

        self.assertEqual(dict(account.balances), {})
        self.assertIsNone(account.current_price)
        self.assertIsNone(account.valuation)
        self.assertIsNone(account.updated_at)
        self.assertEqual(account.version, 0)
        self.assertFalse(account.ready)
        self.assertEqual(account.get_holdings("ETH"), Decimal("0"))

    def test_initial_snapshot_values_free_and_locked_with_market_price(self) -> None:
        """
        함수 이름: test_initial_snapshot_values_free_and_locked_with_market_price()
        기능: REST full snapshot의 ETH free+locked와 시장가로 평가금액을 계산하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        account = Account(valuation_asset="ETH")
        initial_snapshot = _snapshot(
            _balance("ETH", "1.25000000", "0.25000000"),
            _balance("USDT", "125.50", "4.50"),
        )

        account.apply_initial_snapshot(
            initial_snapshot,
            current_price=Decimal("2000.125"),
        )

        self.assertEqual(
            account.balances["ETH"],
            _balance("ETH", "1.25000000", "0.25000000"),
        )
        self.assertEqual(
            account.balances["USDT"],
            _balance("USDT", "125.50", "4.50"),
        )
        self.assertEqual(account.get_holdings("ETH"), Decimal("1.50000000"))
        self.assertEqual(account.current_price, Decimal("2000.125"))
        self.assertEqual(account.valuation, Decimal("3000.18750000000"))
        self.assertEqual(account.updated_at, INITIAL_UPDATED_AT)
        self.assertEqual(account.version, 1)
        self.assertTrue(account.ready)

    def test_partial_stream_snapshot_patches_assets_and_preserves_others(self) -> None:
        """
        함수 이름: test_partial_stream_snapshot_patches_assets_and_preserves_others()
        기능: account stream의 partial absolute balance patch가 누락 자산을 보존하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        account = Account()
        account.apply_initial_snapshot(
            _snapshot(
                _balance("ETH", "1", "0.2"),
                _balance("USDT", "50", "5"),
            ),
            current_price=Decimal("2500"),
        )

        applied = account.apply_stream_snapshot(
            _snapshot(
                _balance("ETH", "0.8", "0.1"),
                updated_at=STREAM_UPDATED_AT,
                is_full_snapshot=False,
            )
        )

        self.assertTrue(applied)
        self.assertEqual(account.get_holdings("ETH"), Decimal("0.9"))
        self.assertEqual(
            account.balances["USDT"],
            _balance("USDT", "50", "5"),
        )
        self.assertEqual(account.current_price, Decimal("2500"))
        self.assertEqual(account.valuation, Decimal("2250.0"))
        self.assertEqual(account.updated_at, STREAM_UPDATED_AT)
        self.assertEqual(account.version, 2)

    def test_stream_snapshot_rejects_stale_and_exact_duplicate_but_accepts_patch(self) -> None:
        """
        함수 이름: test_stream_snapshot_rejects_stale_and_exact_duplicate_but_accepts_patch()
        기능: stale와 동일 patch는 no-op이고 같은 시각의 다른 patch는 적용하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        account = Account()
        account.apply_initial_snapshot(
            _snapshot(_balance("ETH", "1", "0")),
            current_price=Decimal("1000"),
        )
        first_patch = _snapshot(
            _balance("ETH", "0.9", "0.1"),
            updated_at=STREAM_UPDATED_AT,
            is_full_snapshot=False,
        )

        self.assertTrue(account.apply_stream_snapshot(first_patch))
        state_after_first_patch = (
            dict(account.balances),
            account.valuation,
            account.updated_at,
            account.version,
        )

        stale_patch = _snapshot(
            _balance("ETH", "9", "0"),
            updated_at=INITIAL_UPDATED_AT,
            is_full_snapshot=False,
        )
        self.assertFalse(account.apply_stream_snapshot(stale_patch))
        self.assertFalse(account.apply_stream_snapshot(first_patch))
        self.assertEqual(
            (
                dict(account.balances),
                account.valuation,
                account.updated_at,
                account.version,
            ),
            state_after_first_patch,
        )

        same_time_different_patch = _snapshot(
            _balance("ETH", "0.7", "0.2"),
            updated_at=STREAM_UPDATED_AT,
            is_full_snapshot=False,
        )
        self.assertTrue(
            account.apply_stream_snapshot(same_time_different_patch)
        )
        self.assertEqual(account.get_holdings("ETH"), Decimal("0.9"))
        self.assertEqual(account.version, 3)

    def test_initial_snapshot_requires_full_data_and_decimal_price(self) -> None:
        """
        함수 이름: test_initial_snapshot_requires_full_data_and_decimal_price()
        기능: initial commit이 partial snapshot과 float 가격을 거부하고 원자성을 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        account = Account()
        partial_snapshot = _snapshot(
            _balance("ETH", "1", "0"),
            is_full_snapshot=False,
        )

        with self.assertRaises((TypeError, ValueError)):
            account.apply_initial_snapshot(
                partial_snapshot,
                current_price=Decimal("1000"),
            )
        with self.assertRaises((TypeError, ValueError)):
            account.apply_initial_snapshot(
                _snapshot(_balance("ETH", "1", "0")),
                current_price=1000.0,
            )

        self.assertFalse(account.ready)
        self.assertEqual(account.version, 0)
        self.assertEqual(dict(account.balances), {})


if __name__ == "__main__":
    unittest.main()
