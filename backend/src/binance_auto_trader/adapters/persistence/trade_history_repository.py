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
    trade_to_json_object,
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
        # UI와 trace가 raw record 없이도 진단할 수 있는 line과 안전한 원인만 보존한다.
        self.line_number = line_number
        self.reason = reason
        super().__init__(
            f"history line {line_number} is corrupted: {reason}"
        )  # credential이나 원본 JSON은 예외 문자열에 포함하지 않는다.


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
    return datetime.now(timezone.utc)  # backup 충돌 방지 이름은 canonical UTC를 사용한다.


def _normalize_utc_datetime(value: object, field_name: str) -> datetime:
    """
    함수 이름: _normalize_utc_datetime()
    기능: 주입 clock 결과가 timezone-aware UTC인지 검증하고 정규화한다.
    인자: value -> 검증할 datetime
        field_name -> 오류 메시지에 표시할 필드 이름
    반환값: timezone.utc datetime
    작성 날짜: 2026/08/21
    """
    # backup 이름에 쓰기 전에 naive 시각과 UTC 외 offset을 명시적으로 거부한다.
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")

    return value.astimezone(timezone.utc)  # 동등한 UTC offset도 canonical 객체로 맞춘다.


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
    )  # json.loads parse_constant callback은 정상 값을 반환하지 않는다.


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
    # pair 순서를 그대로 순회해야 Python dict 변환 전에 중복 key를 발견할 수 있다.
    decoded_object: dict[str, object] = {}
    for key, value in object_pairs:
        if key in decoded_object:
            raise _DuplicateJsonKeyError("duplicate JSON object key is forbidden")
        decoded_object[key] = value  # 최초 key의 wire 순서도 canonical decode에 보존한다.

    return decoded_object  # 중복이 없는 object만 schema 검증 단계로 넘긴다.


def _decode_json_line(raw_line: bytes) -> object:
    """
    함수 이름: _decode_json_line()
    기능: 한 JSONL line의 UTF-8과 strict JSON 구문만 해석한다.
    인자: raw_line -> LF를 제거한 단일 record bytes
    반환값: JSON decoder가 만든 값
    작성 날짜: 2026/08/21
    """
    # byte framing과 문자·JSON 구문 검증을 분리해 손상 원인을 정확히 분류한다.
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
    # array나 scalar JSON이 Trade parser에 record처럼 들어가지 못하게 shape를 고정한다.
    if not isinstance(decoded_record, Mapping):
        raise TypeError("trade record must decode to a JSON object")

    return trade_from_json_object(decoded_record)  # exact JSONL v1 schema는 domain이 검증한다.


def _fsync_parent_directory(file_path: Path) -> None:
    """
    함수 이름: _fsync_parent_directory()
    기능: 새 backup 이름이 durable해진 뒤에만 원본 truncate가 가능하도록 부모를 fsync한다.
    인자: file_path -> directory entry를 보존할 생성 완료 파일 경로
    반환값: 없음
    작성 날짜: 2026/08/21
    """
    # backup file fsync 뒤 directory entry 자체도 durable하게 만들 descriptor를 연다.
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor = os.open(file_path.parent, directory_flags)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)  # 성공·실패와 관계없이 descriptor 누수를 막는다.


def _normalize_order_id(order_id: object) -> str:
    """
    함수 이름: _normalize_order_id()
    기능: repository operation의 Long 또는 JSONL string order ID를 canonical string으로 만든다.
    인자: order_id -> 양의 정수 또는 양의 정수 형식 문자열
    반환값: canonical order ID 문자열
    작성 날짜: 2026/08/22
    """
    # bool은 int subclass이므로 정수 branch보다 먼저 차단한다.
    if isinstance(order_id, bool):
        raise TypeError("order_id must be an integer or string")
    if isinstance(order_id, int):
        if order_id <= 0:
            raise ValueError("order_id must be greater than zero")
        return str(order_id)  # memory와 JSONL index가 공유할 문자열 key로 통일한다.
    if not isinstance(order_id, str):
        raise TypeError("order_id must be an integer or string")
    if not order_id.isascii() or not order_id.isdigit() or order_id.startswith("0"):
        raise ValueError("order_id must be a positive integer string")

    return order_id  # 선행 0 없는 wire 표현은 그대로 canonical key가 된다.


class TradeHistoryRepository:
    """
    클래스 이름: TradeHistoryRepository
    기능: local JSONL v1 streaming 복구와 RLock 기반 durable idempotent append를 관리한다.
    작성 날짜: 2026/08/22
    """

    __slots__ = (
        "_clock",
        "_index_loaded",
        "_lock",
        "_storage_path",
        "_trades_by_order_id",
        "_uncertain_order_ids",
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
        작성 날짜: 2026/08/22
        """
        # Path 변환과 clock callable을 내부 mutable 상태를 만들기 전에 검증한다.
        try:
            normalized_path = Path(storage_path)
        except TypeError as error:
            raise TypeError("storage_path must be path-like") from error
        if normalized_path.name in {"", ".", ".."}:
            raise ValueError("storage_path must identify a file")

        selected_clock = _utc_now if clock is None else clock
        if not callable(selected_clock):
            raise TypeError("clock must be callable")

        # disk index는 최초 load/save 시점에 만들고 생성자에서는 빈 비공개 상태로 둔다.
        self._storage_path = normalized_path
        self._clock = selected_clock
        self._lock = RLock()
        self._trades_by_order_id: dict[str, Trade] = {}
        self._index_loaded = False  # 첫 save도 기존 파일 index를 먼저 확인하게 한다.
        self._uncertain_order_ids: set[str] = set()

    @property
    def storage_path(self) -> Path:
        """
        함수 이름: storage_path()
        기능: repository가 읽는 JSONL 파일 경로를 반환한다.
        인자: 없음
        반환값: 거래 이력 Path
        작성 날짜: 2026/08/22
        """
        return self._storage_path  # immutable Path identity만 공개한다.

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
            return frozenset(
                self._trades_by_order_id
            )  # caller가 repository index를 변경하지 못하게 snapshot을 만든다.

    def get_trade_history(self) -> tuple[Trade, ...]:
        """
        함수 이름: get_trade_history()
        기능: JSONL을 line 단위로 읽어 Trade tuple과 order ID index를 원자적으로 복원한다.
        인자: 없음
        반환값: 파일 순서를 보존하고 동일 내용 중복을 제거한 Trade tuple
        작성 날짜: 2026/08/21
        """
        with self._lock:
            # 파일이 없으면 빈 durable history로 취급하고 이후 save를 위한 index를 확정한다.
            try:
                history_file = self._storage_path.open("rb")
            except FileNotFoundError:
                self._trades_by_order_id = {}
                self._index_loaded = True
                return ()  # 존재하지 않는 첫 startup은 손상이나 복구 대상이 아니다.

            # 완전한 load가 끝나기 전에는 이전 공개 index를 유지한다.
            try:
                with history_file:
                    loaded_trades, loaded_index = self._read_history_file(
                        history_file
                    )
                self._confirm_storage_durability()
            except Exception:
                self._index_loaded = False
                raise

            # streaming parse가 완전히 성공한 뒤에만 이전 공개 index를 한 번에 교체한다.
            self._trades_by_order_id = loaded_index
            self._index_loaded = True
            return loaded_trades  # file order가 보존된 immutable tuple이다.

    def save_this_trade_by_order_id(
        self,
        order_id: int | str,
        trade: Trade,
    ) -> None:
        """
        함수 이름: save_this_trade_by_order_id()
        기능: order ID idempotency를 확인하고 canonical UTF-8 JSON line을 durable append한다.
        인자: order_id -> exchange order ID인 양의 정수 또는 canonical 문자열
            trade -> 같은 order ID의 terminal Trade
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # lock을 잡기 전에 호출 인자와 Trade identity의 정확한 일치를 검증한다.
        normalized_order_id = _normalize_order_id(order_id)
        if not isinstance(trade, Trade):
            raise TypeError("trade must be a Trade")
        if normalized_order_id != trade.order_id:
            raise ValueError("order_id must match trade.order_id")

        with self._lock:
            # 새 Repository instance의 첫 append도 disk의 기존 order index를 조회한다.
            if not self._index_loaded:
                self.get_trade_history()
            existing_trade = self._trades_by_order_id.get(normalized_order_id)
            if existing_trade is not None:
                if existing_trade == trade:
                    if normalized_order_id in self._uncertain_order_ids:
                        self._confirm_uncertain_append(normalized_order_id)
                    return  # durable 동일 내용은 중복 line을 추가하지 않는다.
                raise OrderHistoryConflictError(
                    f"order_id {normalized_order_id} has conflicting trade content"
                )

            # canonical object 하나와 LF를 단일 bytes write 대상으로 준비한다.
            encoded_record = json.dumps(
                trade_to_json_object(trade),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            append_payload = self._build_append_payload(encoded_record)
            try:
                with self._storage_path.open("ab") as history_file:
                    written_length = history_file.write(append_payload)
                    if written_length != len(append_payload):
                        raise OSError("history append wrote an incomplete record")
                    history_file.flush()  # 사용자 공간과 OS buffer 사이의 내용을 내린다.
                    os.fsync(history_file.fileno())
            except Exception:
                # write 여부가 불명인 모든 실패는 disk 재조회와 same-line fsync 대상으로 남긴다.
                self._uncertain_order_ids.add(normalized_order_id)
                self._index_loaded = False
                raise

            self._trades_by_order_id[normalized_order_id] = trade  # fsync 성공 뒤에만 index를 게시한다.
            self._uncertain_order_ids.discard(normalized_order_id)

    def _build_append_payload(self, encoded_record: bytes) -> bytes:
        """
        함수 이름: _build_append_payload()
        기능: 유효하지만 LF가 없던 기존 마지막 record와 새 canonical line을 안전하게 구분한다.
        인자: encoded_record -> LF로 끝나는 새 JSON record bytes
        반환값: 한 번의 append write에 사용할 bytes
        작성 날짜: 2026/08/22
        """
        # 빈 파일은 canonical record 자체만 append하면 완전한 첫 line이 된다.
        if not self._storage_path.exists() or self._storage_path.stat().st_size == 0:
            return encoded_record  # 불필요한 선행 LF를 만들지 않는다.

        # startup에서 허용한 valid non-LF 마지막 record는 선행 LF로 먼저 완결한다.
        with self._storage_path.open("rb") as history_file:
            history_file.seek(-1, os.SEEK_END)
            final_byte = history_file.read(1)
        if final_byte == b"\n":
            return encoded_record  # 이미 완결된 JSONL 뒤에는 새 line만 이어 붙인다.

        return b"\n" + encoded_record  # valid non-LF tail을 먼저 완결한 뒤 새 record를 쓴다.

    def _confirm_uncertain_append(self, order_id: str) -> None:
        """
        함수 이름: _confirm_uncertain_append()
        기능: 이전 fsync failure 뒤 disk에서 발견한 동일 line을 다시 fsync해 durable로 확정한다.
        인자: order_id -> 불명확한 이전 append의 canonical order ID
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # 같은 line을 다시 append하지 않고 기존 file descriptor 자체를 durable sync한다.
        try:
            self._confirm_storage_durability()
        except Exception:
            self._index_loaded = False
            raise

        self._uncertain_order_ids.discard(order_id)  # 재fsync 성공 뒤에만 불명 상태를 해제한다.

    def _confirm_storage_durability(self) -> None:
        """
        함수 이름: _confirm_storage_durability()
        기능: startup 재생성 후에도 현재 보이는 JSONL bytes를 fsync해 이전 process의 불확실 append를 확정한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/22
        """
        # append mode는 내용을 추가하지 않은 채 write-capable descriptor의 durability 요청을 보장한다.
        with self._storage_path.open("ab") as history_file:
            history_file.flush()
            os.fsync(history_file.fileno())

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
        # file order list와 order-id dedup index를 공개 state와 분리된 local 값으로 만든다.
        loaded_trades: list[Trade] = []
        loaded_index: dict[str, Trade] = {}
        line_number = 0

        # readline 하나씩 처리해 전체 JSONL을 메모리에 올리지 않고 마지막 offset을 보존한다.
        while True:
            line_start_offset = history_file.tell()
            raw_line = history_file.readline()
            if raw_line == b"":
                break

            # LF/CR framing은 JSON decoder보다 먼저 검사해 JSONL separator 오류를 구분한다.
            line_number += 1
            has_line_feed = raw_line.endswith(b"\n")
            record_bytes = raw_line[:-1] if has_line_feed else raw_line
            if record_bytes.endswith(b"\r"):
                framing_error = ValueError("JSONL record separator must be LF")
                raise HistoryCorruptedError(
                    line_number,
                    type(framing_error).__name__,
                ) from framing_error

            # UTF-8, strict JSON constant와 중복 key를 domain schema보다 먼저 검증한다.
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

            # decode가 끝난 object는 exact Trade schema와 fee 환산 정책으로 변환한다.
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

            # 같은 order의 동일 record는 복구 시 no-op이고 다른 내용은 conflict로 중단한다.
            existing_trade = loaded_index.get(trade.order_id)
            if existing_trade is not None:
                if existing_trade == trade:
                    continue
                raise OrderHistoryConflictError(
                    f"order_id {trade.order_id} has conflicting trade content"
                )

            loaded_trades.append(trade)
            loaded_index[trade.order_id] = trade

        return tuple(loaded_trades), loaded_index  # 완전한 local 결과만 caller가 게시한다.

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
        # 원본을 바꾸기 전에 손상 tail을 exclusive backup과 directory fsync로 보존한다.
        backup_path = self._create_corrupt_backup(corrupt_tail)
        try:
            with self._storage_path.open("r+b") as writable_file:
                # 최초 관찰 offset의 tail이 그대로인지 재확인한 뒤 마지막 정상 LF에서 자른다.
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
        # microsecond UTC 이름과 collision suffix를 결합해 기존 backup을 덮어쓰지 않는다.
        backup_time = _normalize_utc_datetime(self._clock(), "clock result")
        timestamp_text = backup_time.strftime("%Y%m%dT%H%M%S%fZ")
        backup_stem = f"{self._storage_path.name}.corrupt-{timestamp_text}"
        collision_index = 0

        # exclusive create가 성공할 때까지 suffix만 증가시키고 같은 tail bytes를 유지한다.
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
                return backup_path  # file과 directory entry가 모두 durable해진 경로다.
            except FileExistsError:
                collision_index += 1  # 이미 있는 backup은 건드리지 않고 다음 이름을 시도한다.
