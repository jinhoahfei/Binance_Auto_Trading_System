"""ADR-004 CSV schema를 streaming no-replace 파일로 기록한다."""

from collections.abc import Iterable, Iterator
import ctypes
import csv
from decimal import Decimal
import errno
import os
from pathlib import Path
import stat
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo

from binance_auto_trader.domain.history import (
    CSVExportIOError,
    CSVExportOptions,
    CSVExportResult,
    CSVExportValidationError,
    DestinationExistsError,
    NoTradesToExportError,
    Trade,
)


# CSV schema version과 column 순서는 ADR-004 5.1의 외부 파일 계약으로 고정한다.
CSV_SCHEMA_VERSION = 1
CSV_HEADER = (
    "schema_version",
    "trade_id",
    "order_id",
    "client_order_id",
    "executed_at_utc",
    "executed_at_kst",
    "symbol",
    "side",
    "regime_type",
    "strategy",
    "average_fill_price",
    "market_price_at_decision",
    "executed_quantity",
    "executed_amount",
    "fee_amount",
    "fee_asset",
    "fee_quote_amount",
    "allocated_cost_basis",
    "realized_pnl",
    "realized_return_rate",
    "exit_reason",
)
_KOREA_TIME_ZONE = ZoneInfo("Asia/Seoul")
_DARWIN_RENAME_EXCL = 0x00000004
_LINUX_AT_FDCWD = -100
_LINUX_RENAME_NOREPLACE = 1


def _plain_decimal(value: Decimal | None) -> str:
    """
    함수 이름: _plain_decimal()
    기능: nullable Decimal을 exponent 없는 CSV field 또는 빈 field로 변환한다.
    인자: value -> Trade의 Decimal 또는 None 값
    반환값: locale과 exponent가 없는 plain 문자열 또는 빈 문자열
    작성 날짜: 2026/08/23
    """
    return (
        "" if value is None else format(value, "f")
    )  # Trade가 검증한 Decimal scale을 locale 변환 없이 그대로 보존한다.


def _trade_to_csv_row(trade: Trade) -> tuple[str, ...]:
    """
    함수 이름: _trade_to_csv_row()
    기능: canonical Trade 하나를 ADR-004 CSV schema version 1의 field 순서로 투영한다.
    인자: trade -> 직렬화할 검증 완료 Trade
    반환값: csv.writer에 전달할 21개 문자열 field tuple
    작성 날짜: 2026/08/23
    """
    # duck typing 객체가 금융 필드를 임의 문자열로 주입하지 못하게 canonical Trade만 받는다.
    if not isinstance(trade, Trade):
        raise TypeError("CSV export stream must contain only Trade values")

    # UTC는 microsecond Z, 사용자 시각은 같은 instant의 KST microsecond offset으로 표현한다.
    executed_at_utc = trade.executed_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    executed_at_kst = trade.executed_at.astimezone(_KOREA_TIME_ZONE).isoformat(
        timespec="microseconds"
    )

    # 첫 field는 Trade JSONL 회계 version이 아니라 고정된 CSV schema version을 기록한다.
    return (
        str(CSV_SCHEMA_VERSION),
        trade.trade_id,
        trade.order_id,
        trade.client_order_id,
        executed_at_utc,
        executed_at_kst,
        trade.symbol,
        trade.side.value,
        trade.regime_type.value,
        trade.strategy.value,
        _plain_decimal(trade.average_fill_price),
        _plain_decimal(trade.market_price_at_decision),
        _plain_decimal(trade.executed_quantity),
        _plain_decimal(trade.executed_amount),
        _plain_decimal(trade.fee_amount),
        trade.fee_asset,
        _plain_decimal(trade.fee_quote_amount),
        _plain_decimal(trade.allocated_cost_basis),
        _plain_decimal(trade.realized_pnl),
        _plain_decimal(trade.realized_return_rate),
        "" if trade.exit_reason is None else trade.exit_reason.value,
    )


def _resolve_output_directory(save_location: str) -> Path:
    """
    함수 이름: _resolve_output_directory()
    기능: 저장 위치를 존재하는 absolute directory Path로 filesystem에서 확인한다.
    인자: save_location -> domain 구조 검증을 통과한 저장 위치 문자열
    반환값: symlink를 해소한 존재하는 absolute directory Path
    작성 날짜: 2026/08/23
    """
    # native picker 계약과 absolute 성공 receipt를 위해 상대 경로는 validation에서 거부한다.
    selected_path = Path(save_location)
    if not selected_path.is_absolute():
        raise CSVExportValidationError(
            "save_location",
            "must_be_an_absolute_directory",
        )

    # 없음·파일 선택은 수정 가능한 입력 오류이고 permission·symlink loop는 I/O 실패다.
    try:
        output_directory = selected_path.resolve(strict=True)
        directory_status = output_directory.stat()
    except (FileNotFoundError, NotADirectoryError):
        raise CSVExportValidationError(
            "save_location",
            "must_identify_an_existing_directory",
        ) from None
    except (OSError, RuntimeError):
        raise CSVExportIOError("directory") from None
    if not stat.S_ISDIR(directory_status.st_mode):
        raise CSVExportValidationError(
            "save_location",
            "must_identify_an_existing_directory",
        )

    return output_directory  # resolve 결과는 성공 receipt에 사용할 absolute 경로다.


def _destination_exists(destination_path: Path) -> bool:
    """
    함수 이름: _destination_exists()
    기능: dangling symlink를 포함한 기존 destination entry를 덮어쓰기 전에 확인한다.
    인자: destination_path -> 확인할 최종 CSV Path
    반환값: 같은 이름의 filesystem entry가 있으면 True
    작성 날짜: 2026/08/23
    """
    # lstat은 symlink target이 없어도 이름이 이미 점유됐음을 정확히 감지한다.
    try:
        os.lstat(destination_path)
    except FileNotFoundError:
        return False  # 존재하지 않는 이름만 temporary write 단계로 진행한다.
    except OSError:
        raise CSVExportIOError("directory") from None

    return True


def _write_csv_content(
    temporary_path: Path,
    trades: Iterator[Trade],
) -> int:
    """
    함수 이름: _write_csv_content()
    기능: exclusive temporary 파일에 BOM, header와 Trade stream을 쓰고 file fsync한다.
    인자: temporary_path -> 최종 파일과 같은 directory의 충돌 없는 temporary Path
        trades -> 비어 있지 않음을 확인한 뒤 첫 Trade를 되붙인 streaming iterator
    반환값: 실제로 기록하고 fsync한 양수 data row 수
    작성 날짜: 2026/08/23
    """
    try:
        # newline 변환을 끄고 csv.writer가 모든 record separator를 CRLF로 직접 기록한다.
        with temporary_path.open(
            "x",
            encoding="utf-8-sig",
            newline="",
        ) as csv_file:
            csv_writer = csv.writer(
                csv_file,
                dialect="excel",
                lineterminator="\r\n",
                quoting=csv.QUOTE_MINIMAL,
            )
            csv_writer.writerow(CSV_HEADER)  # BOM 다음에 정확한 schema header를 한 번 기록한다.

            # 각 Trade 참조를 다음 next 전에 해제해 큰 history에서도 한 행 단위 메모리를 유지한다.
            row_count = 0
            while True:
                try:
                    trade = next(trades)
                except StopIteration:
                    break
                csv_writer.writerow(_trade_to_csv_row(trade))
                row_count += 1
                del trade  # 직렬화를 마친 Trade는 다음 repository row를 요청하기 전에 놓아준다.

            # 성공 반환 전에 Python buffer, OS buffer 순서로 같은 temporary inode를 내린다.
            csv_file.flush()
            try:
                os.fsync(csv_file.fileno())
            except OSError:
                raise CSVExportIOError("fsync") from None
    except CSVExportIOError:
        raise
    except (OSError, UnicodeError, csv.Error):
        raise CSVExportIOError("write") from None

    return row_count  # first_trade를 먼저 받았으므로 성공 count는 항상 양수다.


def _prepend_first_trade(
    first_trade: Trade,
    remaining_trades: Iterator[Trade],
) -> Iterator[Trade]:
    """
    함수 이름: _prepend_first_trade()
    기능: empty probe에서 꺼낸 첫 Trade를 원래 iterator 앞에 복제 없이 한 번 되붙인다.
    인자: first_trade -> temporary 생성 전에 확인한 첫 Trade
        remaining_trades -> 첫 Trade 다음 위치의 원래 streaming iterator
    반환값: 첫 Trade와 나머지를 순서대로 한 번씩 내는 generator
    작성 날짜: 2026/08/24
    """
    yield first_trade  # 첫 next는 이미 수행했지만 CSV row 순서에서는 첫 위치를 유지한다.
    del first_trade
    yield from remaining_trades  # 이후 row는 원래 repository generator에서 직접 전달한다.


def _best_effort_unlink(file_path: Path) -> None:
    """
    함수 이름: _best_effort_unlink()
    기능: 실패 또는 commit 뒤 남은 temporary 이름을 원래 오류를 가리지 않고 제거한다.
    인자: file_path -> 제거할 same-directory temporary Path
    반환값: 없음
    작성 날짜: 2026/08/23
    """
    try:
        file_path.unlink()
    except FileNotFoundError:
        return  # 생성 전 실패하거나 이미 제거된 temporary는 정상 cleanup 상태다.
    except OSError:
        return  # cleanup 실패는 기존 destination이나 원래 typed 오류를 바꾸지 않는다.


def _best_effort_close_iterator(trade_iterator: Iterator[Trade]) -> None:
    """
    함수 이름: _best_effort_close_iterator()
    기능: 조기 종료와 실패에서도 repository generator의 입력 descriptor close를 요청한다.
    인자: trade_iterator -> write_csv가 소유권을 넘겨받아 한 번 소비한 iterator
    반환값: 없음
    작성 날짜: 2026/08/23
    """
    # generator와 custom stream만 제공하는 optional close를 다른 iterator에도 안전하게 적용한다.
    close_iterator = getattr(trade_iterator, "close", None)
    if not callable(close_iterator):
        return
    try:
        close_iterator()
    except Exception:
        return  # descriptor cleanup 오류는 원래 export 결과나 typed 실패를 덮어쓰지 않는다.


def _fallback_link_rename_without_replace(
    temporary_path: Path,
    destination_path: Path,
) -> None:
    """
    함수 이름: _fallback_link_rename_without_replace()
    기능: native exclusive rename이 없는 POSIX에서 hard-link 이름 이동을 no-replace로 수행한다.
    인자: temporary_path -> fsync를 마친 temporary Path
        destination_path -> 절대 덮어쓰지 않을 최종 CSV Path
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    # Link 생성은 destination 존재 확인과 새 이름 생성을 하나의 원자 연산으로 묶는다.
    os.link(temporary_path, destination_path)
    _best_effort_unlink(
        temporary_path
    )  # Final link가 생긴 뒤 temporary unlink 실패는 committed 성공을 되돌리지 않는다.


def _atomic_rename_without_replace(
    temporary_path: Path,
    destination_path: Path,
) -> None:
    """
    함수 이름: _atomic_rename_without_replace()
    기능: 현재 OS의 exclusive rename primitive로 같은-directory temporary 이름을 원자 이동한다.
    인자: temporary_path -> fsync를 마친 temporary Path
        destination_path -> 존재하면 절대 교체하지 않을 최종 CSV Path
    반환값: rename 성공 시 없음
    작성 날짜: 2026/08/24
    """
    encoded_temporary_path = os.fsencode(temporary_path)
    encoded_destination_path = os.fsencode(destination_path)

    if sys.platform == "darwin":
        # macOS 10.12+ renamex_np(RENAME_EXCL)는 외부 volume에서도 가능한 atomic rename을 요청한다.
        system_library = ctypes.CDLL(None, use_errno=True)
        rename_exclusive = system_library.renamex_np
        rename_exclusive.argtypes = (
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename_exclusive.restype = ctypes.c_int
        rename_result = rename_exclusive(
            encoded_temporary_path,
            encoded_destination_path,
            _DARWIN_RENAME_EXCL,
        )
    elif sys.platform.startswith("linux"):
        # Linux renameat2(RENAME_NOREPLACE)는 사전 exists 검사와 rename 사이의 race를 제거한다.
        system_library = ctypes.CDLL(None, use_errno=True)
        rename_no_replace = getattr(system_library, "renameat2", None)
        if rename_no_replace is None:
            _fallback_link_rename_without_replace(
                temporary_path,
                destination_path,
            )
            return
        rename_no_replace.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename_no_replace.restype = ctypes.c_int
        rename_result = rename_no_replace(
            _LINUX_AT_FDCWD,
            encoded_temporary_path,
            _LINUX_AT_FDCWD,
            encoded_destination_path,
            _LINUX_RENAME_NOREPLACE,
        )
    elif os.name == "nt":
        # Windows os.rename은 destination이 존재하면 교체하지 않고 FileExistsError를 낸다.
        os.rename(temporary_path, destination_path)
        return
    else:
        _fallback_link_rename_without_replace(
            temporary_path,
            destination_path,
        )
        return

    if rename_result == 0:
        return

    # Native errno는 path 없이 상위 typed failure로 매핑할 수 있도록 exception 종류만 보존한다.
    rename_error_number = ctypes.get_errno()
    if rename_error_number in (errno.EEXIST, errno.ENOTEMPTY):
        raise FileExistsError("CSV destination already exists")
    raise OSError(rename_error_number, "exclusive CSV rename failed")


def _best_effort_fsync_directory(directory_path: Path) -> None:
    """
    함수 이름: _best_effort_fsync_directory()
    기능: commit된 directory entry를 지원 filesystem에서 동기화하되 완료 결과를 뒤집지 않는다.
    인자: directory_path -> 새 destination 이름을 포함하는 output directory
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    # Rename 성공 뒤 fsync 실패는 안전하게 rollback할 수 없으므로 지원되는 platform에서만 보강한다.
    if os.name == "nt":
        return
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_descriptor = os.open(directory_path, directory_flags)
    except OSError:
        return
    try:
        os.fsync(directory_descriptor)
    except OSError:
        return
    finally:
        try:
            os.close(directory_descriptor)
        except OSError:
            pass  # Directory durability 보강의 close 실패도 committed 결과를 뒤집지 않는다.


def _commit_without_replace(
    temporary_path: Path,
    destination_path: Path,
) -> None:
    """
    함수 이름: _commit_without_replace()
    기능: same-directory native no-replace rename으로 final을 게시하고 directory를 동기화한다.
    인자: temporary_path -> fsync를 마친 temporary Path
        destination_path -> 절대 덮어쓰지 않을 최종 CSV Path
    반환값: 없음
    작성 날짜: 2026/08/24
    """
    try:
        _atomic_rename_without_replace(temporary_path, destination_path)
    except FileExistsError:
        raise DestinationExistsError from None
    except OSError:
        raise CSVExportIOError("commit") from None

    _best_effort_fsync_directory(
        destination_path.parent
    )  # Rename 뒤 지원 filesystem의 directory entry durability를 best-effort로 보강한다.


class CSVFileGateway:
    """
    클래스 이름: CSVFileGateway
    기능: Trade iterable을 ADR-004 UTF-8 BOM CSV로 streaming 기록하고 no-replace commit한다.
    작성 날짜: 2026/08/23
    """

    def write_csv(
        self,
        trades: Iterable[Trade],
        options: CSVExportOptions,
    ) -> CSVExportResult:
        """
        함수 이름: write_csv()
        기능: directory와 첫 Trade를 검증한 뒤 same-directory temporary CSV를 원자 생성한다.
        인자: trades -> 파일 순서로 한 번만 소비할 Trade iterable
            options -> backend 구조 검증과 파일명 정규화를 마친 CSV option
        반환값: absolute 생성 경로와 양수 data row 수를 가진 불변 결과
        작성 날짜: 2026/08/23
        """
        # Gateway 경계에서 검증되지 않은 option 유사 객체의 파일 접근을 차단한다.
        if not isinstance(options, CSVExportOptions):
            raise TypeError("options must be CSVExportOptions")
        try:
            trade_iterator = iter(trades)
        except TypeError:
            raise TypeError("trades must be an iterable of Trade values") from None

        # 모든 성공·조기 실패 branch는 finally에서 repository stream close를 요청한다.
        try:
            # Read-only directory 검증은 빈 조회에서도 backend 최종 검증 순서를 우회하지 않는다.
            output_directory = _resolve_output_directory(options.save_location)

            # 빈 조회는 destination 확인이나 temporary 생성 전에 typed 실패로 종료한다.
            try:
                first_trade = next(trade_iterator)
            except StopIteration:
                raise NoTradesToExportError from None
            if not isinstance(first_trade, Trade):
                raise TypeError("CSV export stream must contain only Trade values")

            # 첫 거래가 존재할 때만 기존 destination 이름과 temporary write를 확인한다.
            destination_path = output_directory / options.file_name
            if _destination_exists(destination_path):
                raise DestinationExistsError

            # UUID temporary는 final과 같은 directory에 exclusive 생성되고 오류마다 제거된다.
            temporary_path = output_directory / (
                f".{options.file_name}.{uuid4()}.tmp"
            )
            csv_trades = _prepend_first_trade(first_trade, trade_iterator)
            del first_trade  # 첫 Trade의 유일한 소유권을 prepend generator로 넘긴다.
            try:
                row_count = _write_csv_content(
                    temporary_path,
                    csv_trades,
                )
                _commit_without_replace(temporary_path, destination_path)
            except BaseException:
                _best_effort_unlink(temporary_path)
                raise

            return CSVExportResult(
                file_path=str(destination_path),
                exported_row_count=row_count,
            )  # resolved parent와 검증된 basename을 결합한 absolute 성공 receipt다.
        finally:
            _best_effort_close_iterator(trade_iterator)
