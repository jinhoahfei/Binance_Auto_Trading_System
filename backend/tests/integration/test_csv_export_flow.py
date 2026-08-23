"""Phase 11 durable history부터 실제 CSV publication까지의 종단 간 계약을 검증한다."""

import csv
from dataclasses import replace
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from binance_auto_trader.adapters.filesystem import CSVFileGateway
from binance_auto_trader.adapters.persistence import TradeHistoryRepository
from binance_auto_trader.application.trade_history_controller import (
    TradeHistoryController,
)
from binance_auto_trader.domain.history import (
    CSVExportOptions,
    CSVPeriod,
    DestinationExistsError,
    NoTradesToExportError,
    Trade,
)
from binance_auto_trader.domain.trading.states import OrderSide

from tests.unit.history.factories import make_trade


# Preset 여부와 무관하게 Controller가 읽는 현재 시각은 2026-08-23 KST로 고정한다.
EXPORT_CLOCK = datetime(2026, 8, 23, 3, 0, tzinfo=timezone.utc)
EXPORT_DATE = date(2026, 8, 23)


def fixed_export_clock() -> datetime:
    """
    함수 이름: fixed_export_clock()
    기능: Phase 11 통합 export의 현재 KST 날짜를 재현하는 UTC 시각을 반환한다.
    인자: 없음
    반환값: 2026-08-23 12:00:00 KST와 같은 고정 UTC datetime
    작성 날짜: 2026/08/23
    """
    return EXPORT_CLOCK  # Controller와 날짜 경계 test가 같은 결정적 clock을 공유한다.


def build_csv_export_fixture(
    history_path: Path,
) -> tuple[TradeHistoryRepository, TradeHistoryController]:
    """
    함수 이름: build_csv_export_fixture()
    기능: 실제 JSONL repository, Controller와 CSV filesystem gateway를 한 흐름으로 조립한다.
    인자: history_path -> durable Trade JSONL을 저장할 임시 경로
    반환값: 실제 TradeHistoryRepository와 TradeHistoryController tuple
    작성 날짜: 2026/08/23
    """
    # 동일 repository가 저장한 JSONL stream을 실제 gateway가 소비하도록 production port를 연결한다.
    repository = TradeHistoryRepository(history_path)
    controller = TradeHistoryController(
        repository,
        clock=fixed_export_clock,
        csv_export_writer=CSVFileGateway(),
    )

    return repository, controller  # Test는 저장 단계와 export receipt를 각각 관찰한다.


def save_trades(
    repository: TradeHistoryRepository,
    trades: tuple[Trade, ...],
) -> None:
    """
    함수 이름: save_trades()
    기능: 입력 순서대로 실제 JSONL repository에 Trade를 durable 저장한다.
    인자: repository -> Phase 11 stream source가 될 실제 repository
        trades -> 서로 다른 order ID를 가진 canonical Trade tuple
    반환값: 없음
    작성 날짜: 2026/08/23
    """
    # Repository public command를 사용해 test가 production JSONL encoding을 우회하지 않게 한다.
    for trade in trades:
        repository.save_this_trade_by_order_id(
            trade.order_id,
            trade,
        )  # 파일 순서가 CSV stream 순서의 근거가 되도록 한 건씩 완료한다.


class CSVExportFlowTests(unittest.TestCase):
    """
    클래스 이름: CSVExportFlowTests
    기능: 실제 repository, Controller와 CSV gateway의 Phase 11 filesystem 결과를 검증한다.
    작성 날짜: 2026/08/23
    """

    def test_custom_kst_boundaries_export_all_sides_as_exact_golden_bytes(
        self,
    ) -> None:
        """
        함수 이름: test_custom_kst_boundaries_export_all_sides_as_exact_golden_bytes()
        기능: KST 양끝 포함, ALL side와 ADR-004 CSV byte 계약을 실제 파일에서 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        with TemporaryDirectory() as temporary_directory:
            # KST 2026-08-23의 직전·시작·끝·직후 UTC instant를 durable 순서로 준비한다.
            output_directory = Path(temporary_directory).resolve()
            repository, controller = build_csv_export_fixture(
                output_directory / "trades.jsonl"
            )
            outside_before = make_trade(
                trade_id="outside-before",
                order_id="100",
                executed_at=datetime(
                    2026,
                    8,
                    22,
                    14,
                    59,
                    59,
                    999999,
                    tzinfo=timezone.utc,
                ),
            )
            inside_start = replace(
                make_trade(
                    trade_id="매수-시작",
                    order_id="101",
                    executed_at=datetime(
                        2026,
                        8,
                        22,
                        15,
                        0,
                        tzinfo=timezone.utc,
                    ),
                ),
                client_order_id='주문, "한국어"',
            )  # 한 field에 한국어·쉼표·따옴표를 넣어 RFC 4180 escaping을 동시에 확인한다.
            inside_end = make_trade(
                trade_id="매도-끝",
                order_id="102",
                executed_at=datetime(
                    2026,
                    8,
                    23,
                    14,
                    59,
                    59,
                    999999,
                    tzinfo=timezone.utc,
                ),
                side=OrderSide.SELL,
            )
            outside_after = make_trade(
                trade_id="outside-after",
                order_id="103",
                executed_at=datetime(
                    2026,
                    8,
                    23,
                    15,
                    0,
                    tzinfo=timezone.utc,
                ),
            )
            save_trades(
                repository,
                (
                    outside_before,
                    inside_start,
                    inside_end,
                    outside_after,
                ),
            )

            # CUSTOM LocalDate 양끝과 확장자가 없는 안전한 Korean basename으로 export한다.
            options = CSVExportOptions(
                save_location=str(output_directory),
                period=CSVPeriod.CUSTOM,
                start_date=EXPORT_DATE,
                end_date=EXPORT_DATE,
                file_name="거래 내역",
            )
            result = controller.export_csv(options)
            destination_path = output_directory / "거래 내역.csv"
            actual_bytes = destination_path.read_bytes()

            # Production schema 상수를 재사용하지 않은 독립 golden으로 column 순서와 값을 고정한다.
            expected_text = (
                "schema_version,trade_id,order_id,client_order_id,"
                "executed_at_utc,executed_at_kst,symbol,side,regime_type,"
                "strategy,average_fill_price,market_price_at_decision,"
                "executed_quantity,executed_amount,fee_amount,fee_asset,"
                "fee_quote_amount,allocated_cost_basis,realized_pnl,"
                "realized_return_rate,exit_reason\r\n"
                '1,매수-시작,101,"주문, ""한국어""",'
                "2026-08-22T15:00:00.000000Z,"
                "2026-08-23T00:00:00.000000+09:00,"
                "ETHUSDT,BUY,TYPE_0,CASE_B,100,100,2,200,"
                "0.002,ETH,0.20,,,,\r\n"
                "1,매도-끝,102,client-102,"
                "2026-08-23T14:59:59.999999Z,"
                "2026-08-23T23:59:59.999999+09:00,"
                "ETHUSDT,SELL,TYPE_0,CASE_B,110,110,1,110,"
                "0.11,USDT,0.11,100.10,9.79,9.78021978,TAKE_PROFIT\r\n"
            )
            expected_bytes = expected_text.encode("utf-8-sig")

            # Golden byte equality가 UTF-8 BOM, CRLF, Decimal plain format과 null 빈 칸을 함께 검증한다.
            self.assertEqual(actual_bytes, expected_bytes)
            self.assertTrue(actual_bytes.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(
                b"\n",
                actual_bytes.replace(b"\r\n", b""),
            )

            # 표준 CSV parser로 escaped field의 논리 값과 BUY·SELL 두 row를 다시 확인한다.
            decoded_csv = actual_bytes.decode("utf-8-sig")
            parsed_rows = list(csv.reader(StringIO(decoded_csv, newline="")))
            self.assertEqual(len(parsed_rows), 3)
            self.assertEqual(parsed_rows[1][3], '주문, "한국어"')
            self.assertEqual(
                tuple(row[7] for row in parsed_rows[1:]),
                ("BUY", "SELL"),
            )

            # 성공 receipt는 게시된 absolute path와 실제 data row count만 반환해야 한다.
            self.assertTrue(Path(result.file_path).is_absolute())
            self.assertEqual(result.file_path, str(destination_path))
            self.assertEqual(result.exported_row_count, 2)
            self.assertNotIn("outside-before", decoded_csv)
            self.assertNotIn("outside-after", decoded_csv)

    def test_empty_range_creates_neither_final_nor_temporary_file(self) -> None:
        """
        함수 이름: test_empty_range_creates_neither_final_nor_temporary_file()
        기능: 조회 결과가 빈 durable stream이 typed 실패하고 CSV entry를 만들지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        with TemporaryDirectory() as temporary_directory:
            # Durable history는 존재하지만 선택한 KST LocalDate 밖에 있는 Trade만 저장한다.
            output_directory = Path(temporary_directory).resolve()
            history_path = output_directory / "trades.jsonl"
            repository, controller = build_csv_export_fixture(history_path)
            outside_trade = make_trade(
                trade_id="empty-range-outside",
                order_id="150",
                executed_at=datetime(
                    2026,
                    8,
                    22,
                    14,
                    59,
                    59,
                    999999,
                    tzinfo=timezone.utc,
                ),
            )
            save_trades(repository, (outside_trade,))
            original_history_bytes = history_path.read_bytes()
            options = CSVExportOptions(
                save_location=str(output_directory),
                period=CSVPeriod.CUSTOM,
                start_date=EXPORT_DATE,
                end_date=EXPORT_DATE,
                file_name="empty.csv",
            )

            # 첫 row 확인이 temporary 생성보다 앞서므로 typed empty 실패 뒤 source만 남아야 한다.
            with self.assertRaises(NoTradesToExportError):
                controller.export_csv(options)

            self.assertFalse((output_directory / "empty.csv").exists())
            self.assertEqual(
                tuple(output_directory.glob(".empty.csv.*.tmp")),
                (),
            )
            self.assertEqual(history_path.read_bytes(), original_history_bytes)
            self.assertEqual(
                tuple(file_path.name for file_path in output_directory.iterdir()),
                ("trades.jsonl",),
            )  # Export 실패는 source JSONL 외에 final이나 hidden temporary를 남기지 않는다.

    def test_existing_destination_is_not_overwritten_or_shadowed_by_temp(
        self,
    ) -> None:
        """
        함수 이름: test_existing_destination_is_not_overwritten_or_shadowed_by_temp()
        기능: 기존 destination 충돌이 원문을 보존하고 temporary residue를 남기지 않는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/23
        """
        with TemporaryDirectory() as temporary_directory:
            # 범위 안 Trade가 있는 실제 repository와 미리 존재하는 최종 파일을 준비한다.
            output_directory = Path(temporary_directory).resolve()
            repository, controller = build_csv_export_fixture(
                output_directory / "trades.jsonl"
            )
            trade = make_trade(
                trade_id="existing-destination-trade",
                order_id="201",
                executed_at=datetime(
                    2026,
                    8,
                    23,
                    4,
                    0,
                    tzinfo=timezone.utc,
                ),
            )
            save_trades(repository, (trade,))
            destination_path = output_directory / "existing.csv"
            original_bytes = "기존 파일은 보존한다.".encode("utf-8")
            destination_path.write_bytes(original_bytes)
            options = CSVExportOptions(
                save_location=str(output_directory),
                period=CSVPeriod.CUSTOM,
                start_date=EXPORT_DATE,
                end_date=EXPORT_DATE,
                file_name="existing.csv",
            )

            # No-replace 정책은 writer 진입 시점의 destination도 typed conflict로 반환한다.
            with self.assertRaises(DestinationExistsError):
                controller.export_csv(options)

            self.assertEqual(destination_path.read_bytes(), original_bytes)
            self.assertEqual(
                tuple(output_directory.glob(".existing.csv.*.tmp")),
                (),
            )  # 충돌은 temporary 파일을 생성하기 전에 종료되어 residue가 없어야 한다.
