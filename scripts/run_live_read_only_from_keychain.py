#!/usr/bin/env python3
"""macOS live Keychain을 읽고 주문 method가 없는 signed preflight만 실행한다."""

from collections.abc import Callable
import argparse
from decimal import Decimal
from datetime import datetime, timezone
import json
import os
import resource
import subprocess

from binance_auto_trader.adapters.binance.api_gateway import APIGateway
from binance_auto_trader.adapters.binance.live_clients import BinanceLiveRESTClient
from binance_auto_trader.bootstrap.live_configuration import LiveConfiguration

KEYCHAIN_SECURITY_COMMAND = "/usr/bin/security"
KEYCHAIN_SERVICE = "com.binance-auto.trader.live"
KEYCHAIN_API_KEY_ACCOUNT = "api-key"
KEYCHAIN_API_SECRET_ACCOUNT = "api-secret"
KEYCHAIN_READ_TIMEOUT_SECONDS = 30
MINIMUM_CREDENTIAL_BYTES = 1
MAXIMUM_CREDENTIAL_BYTES = 512


class LiveKeychainRunnerError(RuntimeError):
    """
    클래스 이름: LiveKeychainRunnerError
    기능: live credential 조회 실패를 원문 없는 고정 오류로 표현한다.
    작성 날짜: 2026/09/08
    """


def zeroize_secret_buffer(secret_buffer: bytearray) -> None:
    """
    함수 이름: zeroize_secret_buffer()
    기능: 더 이상 필요하지 않은 mutable credential bytes를 덮어쓰고 비운다.
    인자: secret_buffer -> 폐기할 credential bytearray
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    if not isinstance(secret_buffer, bytearray):
        raise TypeError("secret_buffer must be a bytearray")

    # 논리 길이를 비우기 전에 기존 할당 영역의 모든 byte를 0으로 덮어쓴다.
    for byte_index in range(len(secret_buffer)):
        secret_buffer[byte_index] = 0
    secret_buffer.clear()  # 폐기된 buffer를 이후 코드가 credential로 재사용하지 못하게 한다.


def _clear_captured_output(
    process_result: subprocess.CompletedProcess[bytes],
) -> None:
    """
    함수 이름: _clear_captured_output()
    기능: security subprocess가 보존한 stdout과 stderr 참조를 성공·실패 모두에서 제거한다.
    인자: process_result -> Keychain 조회의 completed process 결과
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    # Test double이 mutable output을 반환하면 참조 제거 전에 backing memory도 덮어쓴다.
    if isinstance(process_result.stdout, bytearray):
        zeroize_secret_buffer(process_result.stdout)
    if isinstance(process_result.stderr, bytearray):
        zeroize_secret_buffer(process_result.stderr)
    process_result.stdout = b""  # Immutable stdout의 결과 객체 참조를 가능한 즉시 끊는다.
    process_result.stderr = b""  # Raw security 진단은 runner stderr로 전달하지 않는다.


def _clear_timeout_output(error: subprocess.TimeoutExpired) -> None:
    """
    함수 이름: _clear_timeout_output()
    기능: Timeout exception에 붙은 partial Keychain output을 원인 연결 전에 제거한다.
    인자: error -> security subprocess timeout
    반환값: 없음
    작성 날짜: 2026/08/31
    """
    # Mutable partial output은 덮어쓰고 immutable output은 exception 참조에서 분리한다.
    if isinstance(error.output, bytearray):
        zeroize_secret_buffer(error.output)
    if isinstance(error.stderr, bytearray):
        zeroize_secret_buffer(error.stderr)
    error.output = None  # Traceback이 partial credential stdout을 표현하지 못하게 한다.
    error.stderr = None  # Security raw stderr도 generic runner 오류 밖으로 내보내지 않는다.


def read_keychain_credential(
    keychain_account: str,
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> bytearray:
    """
    함수 이름: read_keychain_credential()
    기능: 고정 service의 허용 account 하나를 출력 없이 mutable ASCII buffer로 읽는다.
    인자: keychain_account -> api-key 또는 api-secret 고정 account
        command_runner -> subprocess.run 호환 주입 callable
    반환값: 출력하거나 파일에 기록하면 안 되는 credential bytearray
    작성 날짜: 2026/08/31
    """
    if keychain_account not in {
        KEYCHAIN_API_KEY_ACCOUNT,
        KEYCHAIN_API_SECRET_ACCOUNT,
    }:
        raise LiveKeychainRunnerError()
    if not callable(command_runner):
        raise LiveKeychainRunnerError()

    # Password는 argv가 아닌 security의 captured stdout으로만 읽고 stdin과 inherited FD를 닫는다.
    security_command = (
        KEYCHAIN_SECURITY_COMMAND,
        "find-generic-password",
        "-s",
        KEYCHAIN_SERVICE,
        "-a",
        keychain_account,
        "-w",
    )
    try:
        process_result = command_runner(
            security_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            close_fds=True,
            timeout=KEYCHAIN_READ_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        _clear_timeout_output(error)  # Partial output을 고정 오류 생성 전에 폐기한다.
        raise LiveKeychainRunnerError() from None
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        raise LiveKeychainRunnerError() from None

    credential_buffer = bytearray()
    try:
        # Non-zero status와 bytes가 아닌 test double output은 credential로 해석하지 않는다.
        if process_result.returncode != 0 or not isinstance(
            process_result.stdout,
            (bytes, bytearray),
        ):
            raise LiveKeychainRunnerError()
        credential_buffer.extend(process_result.stdout)

        # macOS security가 붙인 마지막 line ending만 제거하고 내부 개행은 검증에서 거부한다.
        if credential_buffer.endswith(b"\n"):
            credential_buffer.pop()
            if credential_buffer.endswith(b"\r"):
                credential_buffer.pop()
        if (
            len(credential_buffer) < MINIMUM_CREDENTIAL_BYTES
            or len(credential_buffer) > MAXIMUM_CREDENTIAL_BYTES
            or not all(
                0x21 <= byte_value <= 0x7E
                for byte_value in credential_buffer
            )
        ):
            raise LiveKeychainRunnerError()
        return credential_buffer
    except LiveKeychainRunnerError:
        zeroize_secret_buffer(credential_buffer)  # Invalid credential도 caller로 반환하지 않는다.
        raise
    finally:
        _clear_captured_output(process_result)  # Captured immutable bytes 참조도 즉시 제거한다.


def run_preflight(configuration: LiveConfiguration) -> dict[str, object]:
    """
    함수 이름: run_preflight()
    기능: official live GET만으로 계좌·filter·commission·open 상태를 요약한다.
    인자: configuration -> 사용자 확인을 거친 read-only live credential
    반환값: secret·잔액·ID·signature가 없는 검증 결과
    작성 날짜: 2026/09/08
    """
    if not configuration.enabled or configuration.allow_live_orders:
        raise ValueError("preflight requires read-only live configuration")
    rest = BinanceLiveRESTClient(configuration.api_key, configuration.api_secret)
    gateway = APIGateway(rest)
    result: dict[str, object] = {"mode": "live", "order_mutations": 0, "checks": {}}
    checks = result["checks"]

    # Raw 계좌 값은 memory에서만 해석하고 readiness에 필요한 scalar boolean만 반환한다.
    account_snapshot = gateway.fetch_account_snapshot()
    checks["account"] = True
    rules = gateway.fetch_symbol_trading_rules("ETHUSDT")
    checks["symbol_filters"] = rules.symbol == "ETHUSDT"

    # 잔액 원문을 내보내지 않고 공식 MARKET 최소 금액의 필요조건만 검사한다.
    available_quote = next(
        (balance.free for balance in account_snapshot.balances if balance.asset == "USDT"),
        Decimal("0"),
    )
    market_minimum = max(
        (rule.minimum_notional for rule in rules.notional_filters
         if rule.apply_minimum_to_market and rule.minimum_notional is not None),
        default=Decimal("0"),
    )
    checks["quote_balance_meets_market_minimum"] = available_quote > 0 and available_quote >= market_minimum
    gateway.fetch_account_relevant_filters("ETHUSDT")
    checks["account_filters"] = True
    commission = gateway.fetch_commission_discount_policy("ETHUSDT")
    checks["commission"] = True
    checks["supported_fee_asset"] = not commission.can_charge_discount_asset or commission.discount_asset in {"ETH", "USDT", "BNB"}
    # 할인 설정을 유지하되 공식 완료 구간의 가격 근거가 실제 조회되는지도 별도 확인한다.
    if commission.can_charge_discount_asset and commission.discount_asset == "BNB":
        try:
            rest.resolve_bnb_fee(datetime.now(timezone.utc))
            checks["bnb_fee_valuation"] = True
        except (ValueError, RuntimeError, TypeError) as error:
            checks["bnb_fee_valuation"] = False
            # 알려진 고정 진단만 공개하고 transport 예외 원문·서명 URL은 절대 보고하지 않는다.
            safe_reasons = {
                "BNB valuation candle unavailable": "CANDLE_UNAVAILABLE",
                "BNB valuation requires actual market trades": "NO_TRADES_AFTER_BOUNDED_READS",
                "BNB valuation window mismatch": "WINDOW_MISMATCH",
                "invalid BNB valuation kline": "INVALID_KLINE",
                "invalid BNB valuation trade count": "INVALID_TRADE_COUNT",
                "BNB rate must be a decimal string": "INVALID_RATE",
                "valuation requires a positive finite Decimal rate": "INVALID_RATE",
            }
            result["bnb_fee_valuation_failure"] = safe_reasons.get(str(error), "UNCLASSIFIED_REDACTED")
    checks["base_fee_residual_policy"] = True  # Live root는 승인된 durable 잔여 회계를 조립한다.
    rest.fetch_reference_price(symbol="ETHUSDT")
    checks["reference_price"] = True
    checks["account_open_orders_zero"] = not gateway.has_any_exchange_open_orders()
    checks["account_open_order_lists_zero"] = not gateway.has_any_exchange_open_order_lists()
    result["read_only_preflight"] = "PASS"
    result["pilot_prerequisites"] = "PASS" if all(checks.values()) else "NO_GO"
    result["blockers"] = sorted(name for name, passed in checks.items() if not passed)
    return result  # Package READY와 local Position 검증은 native app smoke 증거로 별도 기록한다.


def main() -> int:
    """
    함수 이름: main()
    기능: exact LIVE 확인 뒤 memory-only Keychain read와 고정 preflight를 실행한다.
    인자: 없음; CLI는 --confirm-live LIVE만 허용한다.
    반환값: read-only preflight 성공 0 또는 secret-safe 실패 1
    작성 날짜: 2026/09/08
    """
    parser = argparse.ArgumentParser(description="Official live signed read-only preflight; no orders")
    parser.add_argument("--confirm-live", required=True, choices=["LIVE"])
    parser.parse_args()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    api_key = bytearray()
    api_secret = bytearray()
    try:
        # Secret은 argv/environment/file에 게시하지 않고 REST signing state로만 전달한다.
        api_key = read_keychain_credential("api-key")
        api_secret = read_keychain_credential("api-secret")
        configuration = LiveConfiguration(api_key.decode("ascii"), api_secret.decode("ascii"), enabled=True, confirmation="LIVE")
        result = run_preflight(configuration)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        # Exception 메시지와 traceback은 서명 request를 포함할 수 있으므로 타입과 숫자 code만 기록한다.
        api_code = getattr(error, "api_code", None)
        print(json.dumps({"read_only_preflight": "FAILED", "error_type": type(error).__name__, "api_code": api_code if type(api_code) is int else None, "order_mutations": 0}))
        return 1
    finally:
        zeroize_secret_buffer(api_key)
        zeroize_secret_buffer(api_secret)  # Python immutable 문자열의 물리적 zeroization까지 주장하지 않는다.


if __name__ == "__main__":
    raise SystemExit(main())
