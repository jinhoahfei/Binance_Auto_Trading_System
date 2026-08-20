"""ADR-004 JSONL v1 거래 이력을 streaming 방식으로 복원한다."""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from threading import RLock
from typing import BinaryIO

from binance_auto_trader.domain.history import (
    FeeAssetConversionRequiredError,
    OrderHistoryConflictError,
    Trade,
    trade_from_json_object,
)


class HistoryCorruptedError(ValueError):
    """
    클래스 이름: HistoryCorruptedError
    기능: 자동 복구할 수 없는 완결 JSONL record 손상을 나타낸다.
    작성 날짜: 2026/08/21
    """

    code = "HISTORY_CORRUPTED"

    def __init__(self, line_number: int, reason: str) -> None:
        """
        함수 이름: __init__()
        기능: 손상된 line 번호와 사용자 secret을 포함하지 않는 원인을 보존한다.
        인자: line_number -> 1부터 시작하는 손상 JSONL line 번호
            reason -> 손상 유형을 설명하는 안전한 문자열
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        self.line_number = line_number
        self.reason = reason
        super().__init__(f"history line {line_number} is corrupted: {reason}")


class _NonStandardJsonConstantError(ValueError):
    """
    클래스 이름: _NonStandardJsonConstantError
    기능: strict JSON parser가 거부한 NaN·Infinity 상수를 구문 해석 실패로 구분한다.
    작성 날짜: 2026/08/21
    """


class _DuplicateJsonKeyError(ValueError):
    """
    클래스 이름: _DuplicateJsonKeyError
    기능: decode된 JSON object의 중복 key로 인한 canonical schema 손상을 나타낸다.
    작성 날짜: 2026/08/21
    """


def _utc_now() -> datetime:
    """
    함수 이름: _utc_now()
    기능: corrupt tail backup 이름에 사용할 현재 UTC 시각을 반환한다.
    인자: 없음
    반환값: 현재 UTC datetime
    작성 날짜: 2026/08/21
    """
    return datetime.now(timezone.utc)


def _normalize_utc_datetime(value: object, field_name: str) -> datetime:
    """
    함수 이름: _normalize_utc_datetime()
    기능: 주입 clock 결과가 timezone-aware UTC인지 검증하고 정규화한다.
    인자: value -> 검증할 datetime
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: timezone.utc datetime
    작성 날짜: 2026/08/21
    """
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")

    return value.astimezone(timezone.utc)


def _reject_json_constant(constant_name: str) -> object:
    """
    함수 이름: _reject_json_constant()
    기능: Python JSON decoder가 허용하는 NaN과 Infinity 비표준 상수를 거부한다.
    인자: constant_name -> decoder가 발견한 비표준 상수 이름
    반환값: 정상 반환 없이 ValueError 발생
    작성 날짜: 2026/08/21
    """
    raise _NonStandardJsonConstantError(
        f"non-standard JSON constant is forbidden: {constant_name}"
    )


def _build_unique_json_object(
    object_pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """
    함수 이름: _build_unique_json_object()
    기능: JSON object pair에서 중복 key를 거부하고 dictionary를 생성한다.
    인자: object_pairs -> decoder가 key 순서대로 전달한 pair 목록
    반환값: 중복 key가 없는 JSON dictionary
    작성 날짜: 2026/08/21
    """
    decoded_object: dict[str, object] = {}
    for key, value in object_pairs:
        if key in decoded_object:
            raise _DuplicateJsonKeyError("duplicate JSON object key is forbidden")
        decoded_object[key] = value

    return decoded_object


def _decode_json_line(raw_line: bytes) -> object:
    """
    함수 이름: _decode_json_line()
    기능: 한 JSONL line의 UTF-8과 strict JSON 구문만 해석한다.
    인자: raw_line -> LF를 제거한 단일 record bytes
    반환값: JSON decoder가 만든 값
    작성 날짜: 2026/08/21
    """
    line_text = raw_line.decode("utf-8")
    return json.loads(
        line_text,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_build_unique_json_object,
    )


def _trade_from_decoded_json(decoded_record: object) -> Trade:
    """
    함수 이름: _trade_from_decoded_json()
    기능: decode가 끝난 JSON 값을 object shape와 Trade schema/domain으로 검증한다.
    인자: decoded_record -> strict JSON decoder가 반환한 값
    반환값: canonical Trade
    작성 날짜: 2026/08/21
    """
    if not isinstance(decoded_record, Mapping):
        raise TypeError("trade record must decode to a JSON object")

    return trade_from_json_object(decoded_record)


def _fsync_parent_directory(file_path: Path) -> None:
    """
    함수 이름: _fsync_parent_directory()
    기능: 새 backup 이름이 durable해진 뒤에만 원본 truncate가 가능하도록 부모를 fsync한다.
    인자: file_path -> directory entry를 보존할 생성 완료 파일 경로
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor = os.open(file_path.parent, directory_flags)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


class TradeHistoryRepository:
    """
    클래스 이름: TradeHistoryRepository
    기능: local JSONL v1을 streaming parse하고 crash tail 복구와 order index를 관리한다.
    작성 날짜: 2026/08/21
    """

    __slots__ = (
        "_clock",
        "_lock",
        "_storage_path",
        "_trades_by_order_id",
    )

    def __init__(
        self,
        storage_path: str | os.PathLike[str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        함수 이름: __init__()
        기능: JSONL storage 경로와 recovery backup용 UTC clock을 보존한다.
        인자: storage_path -> 거래 이력 JSONL 파일 경로
            clock -> corrupt backup timestamp를 제공할 optional UTC callable
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        try:
            normalized_path = Path(storage_path)
        except TypeError as error:
            raise TypeError("storage_path must be path-like") from error
        if normalized_path.name in {"", ".", ".."}:
            raise ValueError("storage_path must identify a file")

        selected_clock = _utc_now if clock is None else clock
        if not callable(selected_clock):
            raise TypeError("clock must be callable")

        self._storage_path = normalized_path
        self._clock = selected_clock
        self._lock = RLock()
        self._trades_by_order_id: dict[str, Trade] = {}

    @property
    def storage_path(self) -> Path:
        """
        함수 이름: storage_path()
        기능: repository가 읽는 JSONL 파일 경로를 반환한다.
        인자: 없음
        반환값: 거래 이력 Path
        작성 날짜: 2026/08/21
        """
        return self._storage_path

    @property
    def loaded_order_ids(self) -> frozenset[str]:
        """
        함수 이름: loaded_order_ids()
        기능: 마지막 성공 startup load에서 재구성한 order ID 집합을 반환한다.
        인자: 없음
        반환값: 불변 order ID 집합
        작성 날짜: 2026/08/21
        """
        with self._lock:
            return frozenset(self._trades_by_order_id)

    def get_trade_history(self) -> tuple[Trade, ...]:
        """
        함수 이름: get_trade_history()
        기능: JSONL을 line 단위로 읽어 Trade tuple과 order ID index를 원자적으로 복원한다.
        인자: 없음
        반환값: 파일 순서를 보존하고 동일 내용 중복을 제거한 Trade tuple
        작성 날짜: 2026/08/21
        """
        with self._lock:
            try:
                history_file = self._storage_path.open("rb")
            except FileNotFoundError:
                self._trades_by_order_id = {}
                return ()

            with history_file:
                loaded_trades, loaded_index = self._read_history_file(history_file)

            self._trades_by_order_id = loaded_index
            return loaded_trades

    def _read_history_file(
        self,
        history_file: BinaryIO,
    ) -> tuple[tuple[Trade, ...], dict[str, Trade]]:
        """
        함수 이름: _read_history_file()
        기능: open binary file을 streaming parse하고 malformed partial tail만 복구한다.
        인자: history_file -> streaming read할 binary file
        반환값: dedup된 Trade tuple과 rebuilt order ID index
        작성 날짜: 2026/08/21
        """
        loaded_trades: list[Trade] = []
        loaded_index: dict[str, Trade] = {}
        line_number = 0

        while True:
            line_start_offset = history_file.tell()
            raw_line = history_file.readline()
            if raw_line == b"":
                break

            line_number += 1
            has_line_feed = raw_line.endswith(b"\n")
            record_bytes = raw_line[:-1] if has_line_feed else raw_line
            if record_bytes.endswith(b"\r"):
                framing_error = ValueError("JSONL record separator must be LF")
                raise HistoryCorruptedError(
                    line_number,
                    type(framing_error).__name__,
                ) from framing_error

            try:
                decoded_record = _decode_json_line(record_bytes)
            except (
                _NonStandardJsonConstantError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as error:
                if not has_line_feed:
                    self._recover_partial_tail(
                        line_start_offset,
                        raw_line,
                    )
                    break
                raise HistoryCorruptedError(
                    line_number,
                    type(error).__name__,
                ) from error
            except _DuplicateJsonKeyError as error:
                raise HistoryCorruptedError(
                    line_number,
                    type(error).__name__,
                ) from error

            try:
                trade = _trade_from_decoded_json(decoded_record)
            except FeeAssetConversionRequiredError:
                raise
            except (
                TypeError,
                ValueError,
            ) as error:
                raise HistoryCorruptedError(
                    line_number,
                    type(error).__name__,
                ) from error

            existing_trade = loaded_index.get(trade.order_id)
            if existing_trade is not None:
                if existing_trade == trade:
                    continue
                raise OrderHistoryConflictError(
                    f"order_id {trade.order_id} has conflicting trade content"
                )

            loaded_trades.append(trade)
            loaded_index[trade.order_id] = trade

        return tuple(loaded_trades), loaded_index

    def _recover_partial_tail(
        self,
        valid_length: int,
        corrupt_tail: bytes,
    ) -> None:
        """
        함수 이름: _recover_partial_tail()
        기능: malformed non-LF tail을 backup한 뒤 원본을 마지막 정상 LF까지 truncate한다.
        인자: valid_length -> 마지막 정상 LF 다음 byte offset
            corrupt_tail -> 보존할 malformed tail bytes
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        backup_path = self._create_corrupt_backup(corrupt_tail)
        try:
            with self._storage_path.open("r+b") as writable_file:
                writable_file.seek(valid_length)
                current_tail = writable_file.read()
                if current_tail != corrupt_tail:
                    raise RuntimeError(
                        "history changed while malformed tail was recovered"
                    )
                writable_file.seek(valid_length)
                writable_file.truncate()
                writable_file.flush()
                os.fsync(writable_file.fileno())
        except Exception as error:
            error.add_note(f"corrupt tail was preserved at {backup_path.name}")
            raise

    def _create_corrupt_backup(self, corrupt_tail: bytes) -> Path:
        """
        함수 이름: _create_corrupt_backup()
        기능: UTC timestamp를 가진 exclusive backup 파일에 malformed tail bytes를 보존한다.
        인자: corrupt_tail -> 원본 JSONL에서 분리한 malformed tail bytes
        반환값: 생성한 backup Path
        작성 날짜: 2026/08/21
        """
        backup_time = _normalize_utc_datetime(self._clock(), "clock result")
        timestamp_text = backup_time.strftime("%Y%m%dT%H%M%S%fZ")
        backup_stem = f"{self._storage_path.name}.corrupt-{timestamp_text}"
        collision_index = 0

        while True:
            backup_name = backup_stem if collision_index == 0 else (
                f"{backup_stem}-{collision_index}"
            )
            backup_path = self._storage_path.with_name(backup_name)
            try:
                with backup_path.open("xb") as backup_file:
                    backup_file.write(corrupt_tail)
                    backup_file.flush()
                    os.fsync(backup_file.fileno())
                _fsync_parent_directory(backup_path)
                return backup_path
            except FileExistsError:
                collision_index += 1
