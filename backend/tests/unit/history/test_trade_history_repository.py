"""TradeHistoryRepository의 JSONL streaming 복원과 crash recovery를 검증한다."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from binance_auto_trader.adapters.persistence import (
    HistoryCorruptedError,
    TradeHistoryRepository,
)
from binance_auto_trader.domain.history import (
    FeeAssetConversionRequiredError,
    OrderHistoryConflictError,
)
from binance_auto_trader.domain.trading.states import OrderSide

from tests.unit.history.factories import make_trade, make_trade_record


BACKUP_TIME = datetime(2026, 8, 21, 0, 1, 2, 345678, tzinfo=timezone.utc)


def fixed_backup_clock() -> datetime:
    """
    함수 이름: fixed_backup_clock()
    기능: corrupt tail backup 이름의 UTC timestamp를 고정한다.
    인자: 없음
    반환값: 고정 UTC datetime
    작성 날짜: 2026/08/21
    """
    return BACKUP_TIME


def encode_record(record: dict[str, object]) -> bytes:
    """
    함수 이름: encode_record()
    기능: JSONL unit test record를 UTF-8 JSON bytes로 변환한다.
    인자: record -> encoding할 JSON object
    반환값: LF를 포함하지 않는 compact UTF-8 bytes
    작성 날짜: 2026/08/21
    """
    return json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


class TradeHistoryRepositoryTests(unittest.TestCase):
    """
    클래스 이름: TradeHistoryRepositoryTests
    기능: 파일 상태별 startup load, 중복 index와 손상 정책을 테스트한다.
    작성 날짜: 2026/08/21
    """

    def setUp(self) -> None:
        """
        함수 이름: setUp()
        기능: 각 test가 독립적으로 사용할 temporary history 경로를 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.temporary_directory = TemporaryDirectory()
        self.history_path = Path(self.temporary_directory.name) / "trades.jsonl"
        self.repository = TradeHistoryRepository(
            self.history_path,
            clock=fixed_backup_clock,
        )

    def tearDown(self) -> None:
        """
        함수 이름: tearDown()
        기능: test가 생성한 temporary directory와 파일을 제거한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.temporary_directory.cleanup()

    def test_missing_and_empty_files_restore_empty_history(self) -> None:
        """
        함수 이름: test_missing_and_empty_files_restore_empty_history()
        기능: 파일 없음과 0-byte 파일을 모두 정상 빈 tuple로 복원하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.assertEqual(self.repository.get_trade_history(), ())

        self.history_path.write_bytes(b"")

        self.assertEqual(self.repository.get_trade_history(), ())
        self.assertEqual(self.repository.loaded_order_ids, frozenset())

    def test_streams_multiple_records_and_rebuilds_order_index(self) -> None:
        """
        함수 이름: test_streams_multiple_records_and_rebuilds_order_index()
        기능: 여러 JSONL 거래의 Decimal 정밀도, 순서와 order index를 복원하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        buy_trade = make_trade(trade_id="buy", order_id="101")
        sell_trade = make_trade(
            trade_id="sell",
            order_id="102",
            side=OrderSide.SELL,
        )
        buy_record = make_trade_record(buy_trade)
        buy_record["market_price_at_decision"] = "99.1234567890123456789"
        raw_history = b"\n".join(
            (
                encode_record(buy_record),
                encode_record(make_trade_record(sell_trade)),
            )
        ) + b"\n"
        self.history_path.write_bytes(raw_history)

        loaded_trades = self.repository.get_trade_history()

        self.assertEqual(tuple(trade.order_id for trade in loaded_trades), ("101", "102"))
        self.assertEqual(
            str(loaded_trades[0].market_price_at_decision),
            "99.1234567890123456789",
        )
        self.assertEqual(
            self.repository.loaded_order_ids,
            frozenset({"101", "102"}),
        )

    def test_same_content_duplicate_is_no_op_and_conflict_is_fatal(self) -> None:
        """
        함수 이름: test_same_content_duplicate_is_no_op_and_conflict_is_fatal()
        기능: 같은 order의 동일 line은 dedup하고 다른 내용은 typed conflict로 실패한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        record = make_trade_record()
        encoded_record = encode_record(record)
        self.history_path.write_bytes(encoded_record + b"\n" + encoded_record + b"\n")

        loaded_trades = self.repository.get_trade_history()

        self.assertEqual(len(loaded_trades), 1)
        self.assertEqual(self.repository.loaded_order_ids, frozenset({"1"}))

        conflicting_record = dict(record)
        conflicting_record["trade_id"] = "different-trade"
        self.history_path.write_bytes(
            encoded_record + b"\n" + encode_record(conflicting_record) + b"\n"
        )

        with self.assertRaises(OrderHistoryConflictError) as captured_error:
            self.repository.get_trade_history()

        self.assertEqual(captured_error.exception.code, "ORDER_HISTORY_CONFLICT")
        self.assertEqual(self.repository.loaded_order_ids, frozenset({"1"}))

    def test_malformed_middle_line_is_fatal_and_never_changes_file(self) -> None:
        """
        함수 이름: test_malformed_middle_line_is_fatal_and_never_changes_file()
        기능: LF로 완결된 중간 손상 줄이 HISTORY_CORRUPTED이고 원본을 유지하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        first_line = encode_record(make_trade_record()) + b"\n"
        second_trade = make_trade(trade_id="trade-2", order_id="2")
        original_bytes = (
            first_line
            + b'{"schema_version":1,"record_type":"trade"}\n'
            + encode_record(make_trade_record(second_trade))
            + b"\n"
        )
        self.history_path.write_bytes(original_bytes)

        with self.assertRaises(HistoryCorruptedError) as captured_error:
            self.repository.get_trade_history()

        self.assertEqual(captured_error.exception.code, "HISTORY_CORRUPTED")
        self.assertEqual(captured_error.exception.line_number, 2)
        self.assertEqual(self.history_path.read_bytes(), original_bytes)
        self.assertEqual(list(self.history_path.parent.glob("*.corrupt-*")), [])

    def test_malformed_lf_terminated_last_line_is_fatal(self) -> None:
        """
        함수 이름: test_malformed_lf_terminated_last_line_is_fatal()
        기능: LF가 있는 마지막 손상 줄도 자동 복구하지 않고 startup을 중단하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        valid_prefix = encode_record(make_trade_record()) + b"\n"
        original_bytes = valid_prefix + b"{not-json}\n"
        self.history_path.write_bytes(original_bytes)

        with self.assertRaises(HistoryCorruptedError) as captured_error:
            self.repository.get_trade_history()

        self.assertEqual(captured_error.exception.line_number, 2)
        self.assertEqual(self.history_path.read_bytes(), original_bytes)

    def test_malformed_non_lf_tail_is_backed_up_then_truncated(self) -> None:
        """
        함수 이름: test_malformed_non_lf_tail_is_backed_up_then_truncated()
        기능: partial 마지막 bytes를 timestamp backup에 보존한 뒤 정상 LF까지 자르는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        valid_prefix = encode_record(make_trade_record()) + b"\n"
        corrupt_tail = b'{"schema_version":1,"record_type":"tra'
        self.history_path.write_bytes(valid_prefix + corrupt_tail)

        loaded_trades = self.repository.get_trade_history()

        backup_path = self.history_path.with_name(
            "trades.jsonl.corrupt-20260821T000102345678Z"
        )
        self.assertEqual(len(loaded_trades), 1)
        self.assertEqual(self.history_path.read_bytes(), valid_prefix)
        self.assertEqual(backup_path.read_bytes(), corrupt_tail)
        self.assertEqual(self.repository.loaded_order_ids, frozenset({"1"}))
        self.assertEqual(self.repository.get_trade_history(), loaded_trades)

    def test_directory_fsync_failure_preserves_original_tail(self) -> None:
        """
        함수 이름: test_directory_fsync_failure_preserves_original_tail()
        기능: backup directory fsync 실패 전에 원본 tail을 절단하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        valid_prefix = encode_record(make_trade_record()) + b"\n"
        corrupt_tail = b'{"schema_version":1,"record_type":"tra'
        original_bytes = valid_prefix + corrupt_tail
        self.history_path.write_bytes(original_bytes)

        with patch.object(
            os,
            "fsync",
            side_effect=(None, OSError("directory fsync failed")),
        ) as fsync_mock:
            with patch.object(os, "close", wraps=os.close) as close_mock:
                with self.assertRaisesRegex(OSError, "directory fsync failed"):
                    self.repository.get_trade_history()

        backup_path = self.history_path.with_name(
            "trades.jsonl.corrupt-20260821T000102345678Z"
        )
        self.assertEqual(fsync_mock.call_count, 2)
        self.assertEqual(close_mock.call_count, 1)
        self.assertEqual(self.history_path.read_bytes(), original_bytes)
        self.assertEqual(backup_path.read_bytes(), corrupt_tail)
        self.assertEqual(self.repository.loaded_order_ids, frozenset())

    def test_non_lf_schema_and_domain_failures_are_fatal_not_recovered(self) -> None:
        """
        함수 이름: test_non_lf_schema_and_domain_failures_are_fatal_not_recovered()
        기능: JSON decode 후 schema/domain 실패는 LF가 없어도 backup·truncate하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        missing_field_record = make_trade_record()
        del missing_field_record["trade_id"]
        invalid_domain_record = make_trade_record()
        invalid_domain_record["executed_amount"] = "0"

        for invalid_record in (missing_field_record, invalid_domain_record):
            with self.subTest(keys=tuple(invalid_record)):
                original_bytes = encode_record(invalid_record)
                self.history_path.write_bytes(original_bytes)

                with self.assertRaises(HistoryCorruptedError):
                    self.repository.get_trade_history()

                self.assertEqual(self.history_path.read_bytes(), original_bytes)
                self.assertEqual(
                    list(self.history_path.parent.glob("*.corrupt-*")),
                    [],
                )

    def test_valid_non_lf_record_loads_without_recovery(self) -> None:
        """
        함수 이름: test_valid_non_lf_record_loads_without_recovery()
        기능: JSON과 Trade 검증이 성공한 non-LF 마지막 record는 손상 복구하지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        original_bytes = encode_record(make_trade_record())
        self.history_path.write_bytes(original_bytes)

        loaded_trades = self.repository.get_trade_history()

        self.assertEqual(len(loaded_trades), 1)
        self.assertEqual(self.history_path.read_bytes(), original_bytes)
        self.assertEqual(list(self.history_path.parent.glob("*.corrupt-*")), [])

    def test_duplicate_json_key_is_fatal_even_without_lf(self) -> None:
        """
        함수 이름: test_duplicate_json_key_is_fatal_even_without_lf()
        기능: canonical object를 만들 수 없는 중복 JSON key를 partial tail로 숨기지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        original_record = encode_record(make_trade_record())
        duplicate_key_bytes = original_record.replace(
            b'{"schema_version":1,',
            b'{"schema_version":1,"schema_version":1,',
            1,
        )
        self.history_path.write_bytes(duplicate_key_bytes)

        with self.assertRaises(HistoryCorruptedError):
            self.repository.get_trade_history()

        self.assertEqual(self.history_path.read_bytes(), duplicate_key_bytes)
        self.assertEqual(list(self.history_path.parent.glob("*.corrupt-*")), [])

    def test_third_asset_fee_requires_reconciliation_without_mutation(self) -> None:
        """
        함수 이름: test_third_asset_fee_requires_reconciliation_without_mutation()
        기능: 제3 fee asset을 typed failure로 전파하고 파일과 이전 index를 유지한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        valid_bytes = encode_record(make_trade_record()) + b"\n"
        self.history_path.write_bytes(valid_bytes)
        self.repository.get_trade_history()

        third_asset_record = make_trade_record(
            make_trade(trade_id="trade-2", order_id="2")
        )
        third_asset_record["fee_asset"] = "BNB"
        third_asset_record["fee_quote_amount"] = "0.01"
        original_bytes = encode_record(third_asset_record) + b"\n"
        self.history_path.write_bytes(original_bytes)

        with self.assertRaises(FeeAssetConversionRequiredError) as context:
            self.repository.get_trade_history()

        self.assertEqual(
            context.exception.code,
            "FEE_ASSET_CONVERSION_REQUIRED",
        )
        self.assertEqual(self.history_path.read_bytes(), original_bytes)
        self.assertEqual(self.repository.loaded_order_ids, frozenset({"1"}))
        self.assertEqual(list(self.history_path.parent.glob("*.corrupt-*")), [])

    def test_decoding_and_permission_errors_are_not_hidden_as_empty(self) -> None:
        """
        함수 이름: test_decoding_and_permission_errors_are_not_hidden_as_empty()
        기능: 완결 UTF-8 오류와 파일 접근 권한 오류가 빈 history로 바뀌지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.history_path.write_bytes(b"\xff\n")

        with self.assertRaises(HistoryCorruptedError):
            self.repository.get_trade_history()

        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                self.repository.get_trade_history()


if __name__ == "__main__":
    unittest.main()
