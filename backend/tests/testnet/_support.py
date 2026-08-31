"""Opt-in testnet suite가 공유하는 skip gate와 same-ID 주문 helper를 제공한다."""

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

from binance_auto_trader.adapters.persistence import TradeHistoryRepository
from binance_auto_trader.bootstrap.testnet import (
    BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV,
    BINANCE_RUN_TESTNET_ENV,
    BINANCE_RUN_TESTNET_ORDERS_ENV,
    BINANCE_TESTNET_API_KEY_ENV,
    BINANCE_TESTNET_API_SECRET_ENV,
)
from binance_auto_trader.domain.common import RegimeType
from binance_auto_trader.domain.history import Trade
from binance_auto_trader.domain.trading.order import (
    Order,
    OrderResult,
)
from binance_auto_trader.domain.trading.position import Position
from binance_auto_trader.domain.trading.states import (
    ExitReason,
    OrderSide,
    StrategyType,
)


_TESTNET_CREDENTIALS_PRESENT = all(
    isinstance(os.environ.get(variable_name), str)
    and bool(os.environ[variable_name])
    for variable_name in (
        BINANCE_TESTNET_API_KEY_ENV,
        BINANCE_TESTNET_API_SECRET_ENV,
    )
)  # 실제 key pair가 하나라도 없으면 모든 testnet test는 collection 단계에서 skip된다.
_AUTHENTICATED_TESTNET_REQUESTED = (
    os.environ.get(BINANCE_RUN_TESTNET_ENV) == "1"
    and _TESTNET_CREDENTIALS_PRESENT
)
READ_ONLY_TESTNET_REQUESTED = (
    _AUTHENTICATED_TESTNET_REQUESTED
    and os.environ.get(BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV) != "1"
)
ORDER_TESTNET_REQUESTED = (
    _AUTHENTICATED_TESTNET_REQUESTED
    and os.environ.get(BINANCE_RUN_TESTNET_ORDERS_ENV) == "1"
    and os.environ.get(BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV) != "1"
)
PHASE13_PUBLIC_CASE2_REQUESTED = (
    _AUTHENTICATED_TESTNET_REQUESTED
    and os.environ.get(BINANCE_RUN_TESTNET_ORDERS_ENV) == "1"
    and os.environ.get(BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV) == "1"
)  # Phase 13 전용 flag는 legacy actual-order suite와 상호 배타적인 단일 target을 선택한다.
READ_ONLY_SKIP_REASON = (
    f"set {BINANCE_RUN_TESTNET_ENV}=1 with testnet credentials while leaving "
    f"{BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV} disabled to run"
)
ORDER_SKIP_REASON = (
    f"set {BINANCE_RUN_TESTNET_ENV}=1 and "
    f"{BINANCE_RUN_TESTNET_ORDERS_ENV}=1 with a max notional, while leaving "
    f"{BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV} disabled, to run the legacy order suite"
)
PHASE13_PUBLIC_CASE2_SKIP_REASON = (
    f"set {BINANCE_RUN_TESTNET_ENV}=1, "
    f"{BINANCE_RUN_TESTNET_ORDERS_ENV}=1 and "
    f"{BINANCE_RUN_PHASE13_PUBLIC_CASE2_ENV}=1 with a max notional to run"
)
_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
TESTNET_BASELINE_HISTORY_PATH_ENV = "BINANCE_TESTNET_BASELINE_HISTORY_PATH"
TESTNET_BASELINE_HISTORY_FD_ENV = "BINANCE_TESTNET_BASELINE_HISTORY_FD"
TESTNET_BASELINE_HISTORY_SHA256_ENV = (
    "BINANCE_TESTNET_BASELINE_HISTORY_SHA256"
)
TESTNET_BASELINE_PENDING_FD_ENV = "BINANCE_TESTNET_BASELINE_PENDING_FD"
TESTNET_BASELINE_PENDING_SHA256_ENV = (
    "BINANCE_TESTNET_BASELINE_PENDING_SHA256"
)
_BASELINE_HISTORY_COPY_CHUNK_BYTES = 65_536
_MAXIMUM_BASELINE_HISTORY_BYTES = 16 * 1_024 * 1_024
_OPEN_ORDER_PREFLIGHT_FAILURE_MESSAGE = (
    "Testnet preflight requires zero open orders"
)
_RECENT_ORDER_PREFLIGHT_FAILURE_MESSAGE = (
    "Testnet recent orders do not match verified closed history"
)


def _copy_inherited_baseline_file(
    destination_file_path: Path,
    raw_descriptor: str,
    expected_sha256: str,
) -> None:
    """
    함수 이름: _copy_inherited_baseline_file()
    기능: Secure runner가 고정한 inode bytes를 digest 대조하며 private staging file로 복제한다.
    인자: destination_file_path -> 아직 존재하지 않는 staging file
        raw_descriptor -> inherited read-only descriptor의 canonical decimal text
        expected_sha256 -> runner가 같은 descriptor에서 계산한 lowercase SHA-256
    반환값: exact bytes의 exclusive create와 fsync가 끝나면 없음
    작성 날짜: 2026/08/31
    """
    if (
        not isinstance(raw_descriptor, str)
        or not raw_descriptor.isdecimal()
        or str(int(raw_descriptor)) != raw_descriptor
        or int(raw_descriptor) < 3
        or not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_sha256
        )
    ):
        raise RuntimeError("Testnet baseline descriptor evidence is invalid")
    baseline_descriptor = int(raw_descriptor)

    # Descriptor의 regular-file identity와 owner-only 단일 link를 destination 생성 전에 고정한다.
    try:
        source_stat_before = os.fstat(baseline_descriptor)
    except OSError:
        raise RuntimeError("Testnet baseline descriptor is unavailable") from None
    if (
        not stat.S_ISREG(source_stat_before.st_mode)
        or source_stat_before.st_nlink != 1
        or source_stat_before.st_uid != os.getuid()
        or stat.S_IMODE(source_stat_before.st_mode) & 0o077
        or source_stat_before.st_size <= 0
        or source_stat_before.st_size > _MAXIMUM_BASELINE_HISTORY_BYTES
    ):
        raise RuntimeError("Testnet baseline descriptor is not an approved file")
    if destination_file_path.exists() or destination_file_path.is_symlink():
        raise RuntimeError("Testnet baseline destination must not already exist")

    destination_descriptor = -1
    copy_succeeded = False
    digest_builder = hashlib.sha256()
    try:
        # O_EXCL·O_NOFOLLOW로 fresh artifact leaf 하나만 만들고 source offset은 pread로 바꾸지 않는다.
        destination_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
        )
        destination_descriptor = os.open(
            destination_file_path,
            destination_flags,
            0o600,
        )
        source_offset = 0
        while source_offset < source_stat_before.st_size:
            source_chunk = os.pread(
                baseline_descriptor,
                min(
                    _BASELINE_HISTORY_COPY_CHUNK_BYTES,
                    source_stat_before.st_size - source_offset,
                ),
                source_offset,
            )
            if not source_chunk:
                raise RuntimeError("Testnet baseline descriptor ended early")
            digest_builder.update(source_chunk)
            written_offset = 0
            while written_offset < len(source_chunk):
                written_length = os.write(
                    destination_descriptor,
                    source_chunk[written_offset:],
                )
                if written_length <= 0:
                    raise OSError("baseline copy made no write progress")
                written_offset += written_length
            source_offset += len(source_chunk)

        # Source mutation과 runner→child digest drift를 모두 거부한 뒤에만 copied bytes를 fsync한다.
        source_stat_after = os.fstat(baseline_descriptor)
        stable_fields_before = (
            source_stat_before.st_dev,
            source_stat_before.st_ino,
            source_stat_before.st_size,
            source_stat_before.st_mtime_ns,
            source_stat_before.st_ctime_ns,
        )
        stable_fields_after = (
            source_stat_after.st_dev,
            source_stat_after.st_ino,
            source_stat_after.st_size,
            source_stat_after.st_mtime_ns,
            source_stat_after.st_ctime_ns,
        )
        if (
            stable_fields_before != stable_fields_after
            or digest_builder.hexdigest() != expected_sha256
        ):
            raise RuntimeError("Testnet baseline descriptor changed before copy")
        os.fsync(destination_descriptor)
        copy_succeeded = True  # 아래 close 뒤 repository가 canonical rows를 별도로 검증한다.
    except OSError:
        raise RuntimeError("Testnet baseline descriptor copy failed") from None
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        if not copy_succeeded:
            try:
                destination_file_path.unlink(missing_ok=True)
            except OSError:
                pass  # Fresh invalid artifact 정리 실패가 원래 fail-closed 판정을 덮지 않는다.


def seed_verified_closed_history(
    destination_history_path: Path,
) -> tuple[Trade, ...]:
    """
    함수 이름: seed_verified_closed_history()
    기능: 선택한 실제 Testnet closed history를 새 run artifact에 durable baseline으로 복제한다.
    인자: destination_history_path -> 새 lifecycle 또는 cold-restart history 파일
    반환값: 검증하고 destination에 저장한 canonical Trade tuple
    작성 날짜: 2026/08/24
    """
    # 호출 경계와 optional environment 원문을 파일 접근 전에 strict하게 검증한다.
    if not isinstance(destination_history_path, Path):
        raise TypeError("destination_history_path must be a Path")
    raw_source_path = os.environ.get(TESTNET_BASELINE_HISTORY_PATH_ENV)
    raw_source_descriptor = os.environ.get(TESTNET_BASELINE_HISTORY_FD_ENV)
    expected_source_sha256 = os.environ.get(
        TESTNET_BASELINE_HISTORY_SHA256_ENV
    )
    raw_pending_descriptor = os.environ.get(TESTNET_BASELINE_PENDING_FD_ENV)
    expected_pending_sha256 = os.environ.get(
        TESTNET_BASELINE_PENDING_SHA256_ENV
    )
    inherited_evidence_present = (
        raw_source_descriptor is not None
        or expected_source_sha256 is not None
        or raw_pending_descriptor is not None
        or expected_pending_sha256 is not None
    )
    if raw_source_path is None and not inherited_evidence_present:
        return ()  # 최초 clean Testnet run은 기존과 같이 빈 history에서 시작한다.
    if inherited_evidence_present:
        if (
            raw_source_path is not None
            or raw_source_descriptor is None
            or expected_source_sha256 is None
            or (raw_pending_descriptor is None)
            is not (expected_pending_sha256 is None)
        ):
            raise RuntimeError("Testnet baseline sources must be mutually exclusive")

        # History와 optional pending journal을 private staging에 함께 복제해 active pending을 replay한다.
        with TemporaryDirectory(
            prefix=".testnet-baseline-",
            dir=destination_history_path.parent,
        ) as staging_directory:
            staging_history_path = Path(staging_directory) / "history.jsonl"
            _copy_inherited_baseline_file(
                staging_history_path,
                raw_source_descriptor,
                expected_source_sha256,
            )
            if (
                raw_pending_descriptor is not None
                and expected_pending_sha256 is not None
            ):
                staging_pending_path = staging_history_path.with_name(
                    f"{staging_history_path.name}.pending-orders.jsonl"
                )
                _copy_inherited_baseline_file(
                    staging_pending_path,
                    raw_pending_descriptor,
                    expected_pending_sha256,
                )
            copied_repository = TradeHistoryRepository(staging_history_path)
            baseline_trades = copied_repository.get_trade_history()
            if not baseline_trades:
                raise RuntimeError("Testnet baseline history must contain durable trades")
            if copied_repository.get_pending_order_recovery_records():
                raise RuntimeError("Testnet baseline history must not contain pending orders")

            # Staging의 canonical Trade를 replay해 같은-run BUY를 허용하기 전에 Position 0을 증명한다.
            baseline_position = Position("ETHUSDT")
            for baseline_trade in baseline_trades:
                baseline_position.apply_historical_trade(baseline_trade)
            if baseline_position.quantity != Decimal("0"):
                raise RuntimeError("Testnet baseline history must close Position to zero")

        # 검증된 Trade만 canonical append해 과거 REMOVE journal이 새 run submission count에 섞이지 않게 한다.
        destination_repository = TradeHistoryRepository(destination_history_path)
        try:
            for baseline_trade in baseline_trades:
                destination_repository.save_this_trade_by_order_id(
                    baseline_trade.order_id,
                    baseline_trade,
                )
            destination_history_path.chmod(0o600)  # Caller umask와 무관하게 새 baseline을 owner-only로 고정한다.
        except Exception:
            destination_history_path.unlink(missing_ok=True)
            raise  # Partial canonical copy는 새 run mutation 경계로 넘기지 않는다.
        return baseline_trades
    if not raw_source_path or raw_source_path != raw_source_path.strip():
        raise RuntimeError("Testnet baseline history path must be non-empty and trimmed")

    # 외부 account 이력을 임의 신뢰하지 않고 durable source와 pending 부재를 먼저 검증한다.
    source_history_path = Path(raw_source_path)
    if not source_history_path.is_absolute():
        raise RuntimeError("Testnet baseline history path must be absolute")
    if source_history_path.resolve() == destination_history_path.resolve():
        raise RuntimeError("Testnet baseline source and destination must differ")
    if destination_history_path.exists():
        raise RuntimeError("Testnet baseline destination must not already exist")
    source_repository = TradeHistoryRepository(source_history_path)
    baseline_trades = source_repository.get_trade_history()
    if not baseline_trades:
        raise RuntimeError("Testnet baseline history must contain durable trades")
    if source_repository.get_pending_order_recovery_records():
        raise RuntimeError("Testnet baseline history must not contain pending orders")

    # Domain Position replay가 정확히 0인 history만 새 주문 run의 닫힌 provenance로 허용한다.
    baseline_position = Position("ETHUSDT")
    for baseline_trade in baseline_trades:
        baseline_position.apply_historical_trade(baseline_trade)
    if baseline_position.quantity != Decimal("0"):
        raise RuntimeError("Testnet baseline history must close Position to zero")

    # Repository의 canonical append와 fsync를 재사용해 child os._exit 전에도 baseline을 보존한다.
    destination_repository = TradeHistoryRepository(destination_history_path)
    for baseline_trade in baseline_trades:
        destination_repository.save_this_trade_by_order_id(
            baseline_trade.order_id,
            baseline_trade,
        )

    return baseline_trades  # Startup이 Binance recent orders와 다시 대조할 closed provenance다.


def require_empty_all_client_open_orders(
    open_order_results: tuple[OrderResult, ...],
) -> None:
    """
    함수 이름: require_empty_all_client_open_orders()
    기능: 모든 client ID의 open-order 결과가 비어 있어야 preflight를 통과시킨다.
    인자: open_order_results -> APIGateway가 검증한 전체 open OrderResult tuple
    반환값: 전체 open order가 없으면 없음
    작성 날짜: 2026/08/31
    """
    if not isinstance(open_order_results, tuple) or any(
        type(order_result) is not OrderResult
        for order_result in open_order_results
    ):
        raise TypeError("open_order_results must be an OrderResult tuple")

    # 실패 문장에는 OrderResult repr를 넣지 않아 외부 주문 identity와 fill을 출력하지 않는다.
    if open_order_results:
        raise AssertionError(
            _OPEN_ORDER_PREFLIGHT_FAILURE_MESSAGE
        )  # 고정 문장만 호출자와 unittest stderr에 전달한다.


def verify_exact_recent_order_baseline(
    baseline_trades: tuple[Trade, ...],
    recent_order_results: tuple[OrderResult, ...],
) -> frozenset[str]:
    """
    함수 이름: verify_exact_recent_order_baseline()
    기능: 전체 recent-order identity가 verified closed Trade identity와 정확히 같은지 검증한다.
    인자: baseline_trades -> inode/digest와 semantic 검증을 통과한 closed Trade tuple
        recent_order_results -> 모든 client ID를 포함한 recent OrderResult tuple
    반환값: 검증된 exchange order ID frozenset
    작성 날짜: 2026/08/31
    """
    if not isinstance(baseline_trades, tuple) or any(
        type(baseline_trade) is not Trade
        for baseline_trade in baseline_trades
    ):
        raise TypeError("baseline_trades must be a Trade tuple")
    if not isinstance(recent_order_results, tuple) or any(
        type(order_result) is not OrderResult
        for order_result in recent_order_results
    ):
        raise TypeError("recent_order_results must be an OrderResult tuple")

    # History와 exchange 양쪽 identity를 pair로 만들되 비교 실패에는 실제 값을 반사하지 않는다.
    baseline_identities = tuple(
        (baseline_trade.order_id, baseline_trade.client_order_id)
        for baseline_trade in baseline_trades
    )
    if any(
        order_result.exchange_order_id is None
        for order_result in recent_order_results
    ):
        raise AssertionError(_RECENT_ORDER_PREFLIGHT_FAILURE_MESSAGE)
    recent_identities = tuple(
        (
            str(order_result.exchange_order_id),
            order_result.client_order_id,
        )
        for order_result in recent_order_results
    )
    baseline_identity_set = frozenset(baseline_identities)
    recent_identity_set = frozenset(recent_identities)

    # Terminal status·fill 의미는 startup reconciliation이 담당하며 여기서는 provenance pair만 고정한다.
    # Duplicate, missing, manual 또는 추가 identity 하나라도 있으면 actual mutation 전에 닫는다.
    if (
        len(baseline_identity_set) != len(baseline_identities)
        or len(recent_identity_set) != len(recent_identities)
        or baseline_identity_set != recent_identity_set
    ):
        raise AssertionError(_RECENT_ORDER_PREFLIGHT_FAILURE_MESSAGE)

    return frozenset(
        exchange_order_id
        for exchange_order_id, _client_order_id in recent_identity_set
    )  # Fresh baseline은 양쪽 empty만 통과하므로 빈 frozenset을 반환한다.


def create_test_client_order_id(side: OrderSide) -> str:
    """
    함수 이름: create_test_client_order_id()
    기능: testnet suite 전용 prefix와 방향을 가진 36자 이하 client order ID를 만든다.
    인자: side -> BUY 또는 SELL 방향
    반환값: 충돌 가능성이 낮은 client order ID
    작성 날짜: 2026/08/22
    """
    # 공식 최대 길이 안에서 application prefix와 한 test run의 UUID 일부를 결합한다.
    if not isinstance(side, OrderSide):
        raise TypeError("side must be an OrderSide")
    side_token = "b" if side is OrderSide.BUY else "s"

    return f"bat-t9-{uuid4().hex[:20]}-{side_token}"  # live 주문과 혼동하지 않을 testnet prefix다.


def build_testnet_order(
    *,
    side: OrderSide,
    quantity: Decimal,
    decision_price: Decimal,
    client_order_id: str | None = None,
) -> Order:
    """
    함수 이름: build_testnet_order()
    기능: testnet market lifecycle에 사용할 최소 canonical Order intent를 만든다.
    인자: side -> BUY 또는 SELL 방향
        quantity -> filter 전 요청·제출 수량
        decision_price -> notional cap 계산에 사용한 시장 가격
        client_order_id -> same-ID fault test용 override 또는 None
    반환값: 제출 전 canonical Order
    작성 날짜: 2026/08/22
    """
    # SELL cleanup만 STOP exit reason을 가져 history domain 불변식을 그대로 유지한다.
    selected_client_order_id = (
        create_test_client_order_id(side)
        if client_order_id is None
        else client_order_id
    )
    exit_reason = ExitReason.STOP if side is OrderSide.SELL else None
    return Order(
        intent_id=f"testnet-{selected_client_order_id}",
        client_order_id=selected_client_order_id,
        submission_attempt=0,
        symbol="ETHUSDT",
        side=side,
        strategy=StrategyType.CASE_B,
        regime_type=RegimeType.TYPE_0,
        requested_quantity=quantity,
        submitted_quantity=quantity,
        market_price_at_decision=decision_price,
        exit_reason=exit_reason,
    )  # 실제 제출 직전 Gateway prepare_order가 symbol filter로 수량을 내린다.


def calculate_capped_quantity(
    max_notional: Decimal,
    decision_price: Decimal,
) -> Decimal:
    """
    함수 이름: calculate_capped_quantity()
    기능: 가격 변동 여유를 둔 max-notional 90% 이하 BUY 요청 수량을 계산한다.
    인자: max_notional -> 환경에서 승인한 quote 상한
        decision_price -> 최신 testnet ETHUSDT 가격
    반환값: filter 전 양수 base 수량
    작성 날짜: 2026/08/22
    """
    # 금융 입력은 float를 거치지 않고 finite 양수 Decimal만 허용한다.
    for field_name, field_value in (
        ("max_notional", max_notional),
        ("decision_price", decision_price),
    ):
        if not isinstance(field_value, Decimal) or not field_value.is_finite():
            raise TypeError(f"{field_name} must be a finite Decimal")
        if field_value <= Decimal("0"):
            raise ValueError(f"{field_name} must be greater than zero")

    with localcontext() as decimal_context:
        decimal_context.prec = 34
        return (
            max_notional * Decimal("0.90") / decision_price
        )  # 짧은 testnet 가격 이동이 환경 상한을 넘기지 않도록 10% 여유를 둔다.


def submit_and_wait_for_terminal(
    api_gateway: object,
    order: Order,
    *,
    timeout_seconds: int = 30,
) -> OrderResult:
    """
    함수 이름: submit_and_wait_for_terminal()
    기능: 한 번만 submit하고 timeout 포함 후속 관찰은 같은 client ID query로 terminal까지 기다린다.
    인자: api_gateway -> submit_order와 query_order_result를 제공할 APIGateway
        order -> 준비를 마친 동일 Order aggregate
        timeout_seconds -> terminal query 최대 대기 초
    반환값: 마지막 terminal OrderResult
    작성 날짜: 2026/08/22
    """
    # timeout 뒤 재제출을 금지하도록 submit 호출과 same-ID query branch를 분리한다.
    if not isinstance(order, Order):
        raise TypeError("order must be an Order")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a positive integer")
    submit_order = getattr(api_gateway, "submit_order", None)
    query_order_result = getattr(api_gateway, "query_order_result", None)
    if not callable(submit_order) or not callable(query_order_result):
        raise TypeError("api_gateway must provide submit and query operations")

    try:
        result = submit_order(order)
    except Exception:
        result = query_order_result(order)  # accepted-response timeout도 같은 ID 조회로만 해소한다.
    order.apply_order_result(result)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    while not order.is_terminal:
        if datetime.now(timezone.utc) >= deadline:
            raise TimeoutError("testnet order did not reach terminal status")
        time.sleep(1)
        result = query_order_result(order)
        order.reapply_order_result(result)

    return result  # 신규 submit 없이 마지막 same-ID terminal 관찰만 반환한다.


def unix_milliseconds(value: datetime) -> int:
    """
    함수 이름: unix_milliseconds()
    기능: deterministic executionReport fixture의 UTC datetime을 Unix millisecond로 변환한다.
    인자: value -> timezone-aware UTC datetime
    반환값: Unix millisecond 정수
    작성 날짜: 2026/08/22
    """
    # fault injection timestamp도 float timestamp 변환 없이 정수 산술만 사용한다.
    if not isinstance(value, datetime):
        raise TypeError("value must be a datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("value must be timezone-aware UTC")
    elapsed = value.astimezone(timezone.utc) - _UTC_EPOCH

    return (
        elapsed.days * 86_400_000
        + elapsed.seconds * 1_000
        + elapsed.microseconds // 1_000
    )  # WebSocket JSON fixture와 REST result가 같은 source time을 공유한다.
