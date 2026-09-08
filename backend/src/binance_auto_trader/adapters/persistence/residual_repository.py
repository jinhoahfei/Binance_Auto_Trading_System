"""Live 잔여 ETH 장부를 엄격한 JSON과 atomic replace·fsync로 저장한다."""

from dataclasses import asdict
from decimal import Decimal
import json
import os
from pathlib import Path
import stat
import tempfile

from binance_auto_trader.adapters.platform.file_durability import flush_created_file_metadata
from binance_auto_trader.domain.trading.residual import ResidualTransfer


class ResidualRepository:
    """
    클래스 이름: ResidualRepository
    기능: 기존 Trade JSONL과 분리된 잔여 장부의 파일 책임만 담당한다.
    작성 날짜: 2026/09/08
    """

    def __init__(self, path: Path) -> None:
        """
        함수 이름: __init__()
        기능: bootstrap이 선택한 live 저장소의 고정 장부 경로를 보존한다.
        인자: path -> residual-ledger.json 절대 경로
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        if not path.is_absolute() or path.name != "residual-ledger.json":
            raise ValueError("invalid residual ledger path")
        self.path = path

    def _validate_path(self) -> None:
        """
        함수 이름: _validate_path()
        기능: live namespace 파일과 ancestor의 symlink 및 hardlink를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        for path in (self.path, *self.path.parents):
            if path.is_symlink():
                raise ValueError("residual path cannot contain symlinks")
        if self.path.exists():
            metadata = self.path.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("residual ledger must be single-link regular file")

    def load(self) -> tuple[ResidualTransfer, ...]:
        """
        함수 이름: load()
        기능: bounded JSON 전체를 검증해 단조 이력 prefix의 잔여 장부를 반환한다.
        인자: 없음
        반환값: 불변 잔여 이관 tuple; 최초 파일 부재만 빈 장부
        작성 날짜: 2026/09/08
        """
        self._validate_path()
        try:
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return ()
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("invalid residual descriptor")
            payload = stream.read(4_000_001)
            os.fsync(stream.fileno())  # 직전 replace 후 durability 실패도 재조회에서 다시 확인한다.
        if len(payload) > 4_000_000:
            raise ValueError("residual ledger size exceeded")
        decoded = json.loads(payload, object_pairs_hook=_unique_object)
        if not isinstance(decoded, dict) or set(decoded) != {"schema_version", "transfers"} or type(decoded["schema_version"]) is not int or decoded["schema_version"] != 1 or not isinstance(decoded["transfers"], list):
            raise ValueError("invalid residual ledger schema")
        transfers = []
        for record in decoded["transfers"]:
            if not isinstance(record, dict) or set(record) != {"history_count", "history_sha256", "quantity", "cost_basis", "step_size"}:
                raise ValueError("invalid residual record schema")
            # 금액은 JSON number가 아닌 원본 Decimal 문자열만 받는다.
            for field in ("quantity", "cost_basis", "step_size"):
                if not isinstance(record[field], str):
                    raise ValueError("residual amounts must be strings")
                record[field] = Decimal(record[field])
            transfer = ResidualTransfer(**record)
            if transfers and transfer.history_count <= transfers[-1].history_count:
                raise ValueError("duplicate or unordered residual transfer")
            transfers.append(transfer)
        flush_created_file_metadata(self.path)
        return tuple(transfers)

    def save(self, previous: tuple[ResidualTransfer, ...], transfer: ResidualTransfer) -> None:
        """
        함수 이름: save()
        기능: 알려진 장부 prefix에 단일 이관을 append한 snapshot을 원자적으로 fsync한다.
        인자: previous -> 읽어 검증한 prefix, transfer -> 새 이관
        반환값: 없음
        작성 날짜: 2026/09/08
        """
        if self.load() != previous or (previous and transfer.history_count <= previous[-1].history_count):
            raise ValueError("residual ledger changed before commit")
        records = []
        for entry in (*previous, transfer):
            record = asdict(entry)
            for field in ("quantity", "cost_basis", "step_size"):
                record[field] = str(record[field])
            records.append(record)
        payload = json.dumps({"schema_version": 1, "transfers": records}, sort_keys=True).encode("utf-8")
        if len(payload) > 4_000_000:
            raise ValueError("residual ledger size exceeded")
        descriptor, temporary = tempfile.mkstemp(prefix=".residual-", dir=self.path.parent)
        try:
            # 파일과 directory의 내구성 모두 확인한 뒤에만 application이 Position을 분리한다.
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._validate_path()
            os.replace(temporary, self.path)
            flush_created_file_metadata(self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)  # 실패한 임시 파일만 정리하고 실제 장부는 삭제하지 않는다.


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """
    함수 이름: _unique_object()
    기능: 중복 JSON key로 잔여 기록을 덮어쓰는 입력을 거부한다.
    인자: pairs -> JSON object의 원래 key/value 순서
    반환값: 중복 없는 dict
    작성 날짜: 2026/09/08
    """
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate residual JSON key")
        result[key] = value
    return result
