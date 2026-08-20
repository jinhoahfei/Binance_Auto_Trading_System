"""Application runtime factory와 fail-closed execution mode 경계를 검증한다."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import unittest

from binance_auto_trader.bootstrap import (
    ApplicationStatus,
    ExecutionMode,
    create_application_runtime,
    parse_execution_mode,
)


FIXED_TIME = datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc)  # 시간 의존 상태를 고정한다.


class _StubSubscription:
    """
    클래스 이름: _StubSubscription
    기능: factory 조립 중 client Protocol을 만족하는 최소 subscription을 제공한다.
    작성 날짜: 2026/08/21
    """

    def close(self) -> None:
        """
        함수 이름: close()
        기능: 조립 identity test에서 외부 효과 없이 종료한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        return None


class _StubRestClient:
    """
    클래스 이름: _StubRestClient
    기능: APIGateway의 Kline과 account client Protocol shape만 제공한다.
    작성 날짜: 2026/08/21
    """

    def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> object:
        """
        함수 이름: get_klines()
        기능: 실행하지 않는 factory test용 빈 Kline payload를 반환한다.
        인자: symbol -> 요청 symbol
            interval -> 요청 interval
            limit -> 요청 최대 행 수
        반환값: 빈 payload
        작성 날짜: 2026/08/21
        """
        return []

    def get_account(self) -> object:
        """
        함수 이름: get_account()
        기능: 실행하지 않는 factory test용 빈 account payload를 반환한다.
        인자: 없음
        반환값: 빈 mapping
        작성 날짜: 2026/08/21
        """
        return {}


class _StubWebSocketClient:
    """
    클래스 이름: _StubWebSocketClient
    기능: WebSocketGateway의 두 subscription client Protocol shape를 제공한다.
    작성 날짜: 2026/08/21
    """

    def subscribe_all_kline_streams(
        self,
        *,
        symbol: str,
        intervals: tuple[str, ...],
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> _StubSubscription:
        """
        함수 이름: subscribe_all_kline_streams()
        기능: factory test에서 호출 가능한 Kline subscription handle을 반환한다.
        인자: symbol -> 구독 symbol
            intervals -> 구독 interval tuple
            on_message -> message callback
            on_disconnect -> disconnect callback
        반환값: 최소 subscription handle
        작성 날짜: 2026/08/21
        """
        return _StubSubscription()

    def subscribe_account_info(
        self,
        *,
        on_message: Callable[[object], None],
        on_disconnect: Callable[[], None],
    ) -> _StubSubscription:
        """
        함수 이름: subscribe_account_info()
        기능: factory test에서 호출 가능한 account subscription handle을 반환한다.
        인자: on_message -> message callback
            on_disconnect -> disconnect callback
        반환값: 최소 subscription handle
        작성 날짜: 2026/08/21
        """
        return _StubSubscription()


class _StubHistoryRepository:
    """
    클래스 이름: _StubHistoryRepository
    기능: TradeHistoryController 조립에 필요한 read port만 제공한다.
    작성 날짜: 2026/08/21
    """

    def get_trade_history(self) -> tuple[object, ...]:
        """
        함수 이름: get_trade_history()
        기능: factory test에서 복원할 거래가 없음을 반환한다.
        인자: 없음
        반환값: 빈 tuple
        작성 날짜: 2026/08/21
        """
        return ()


class ApplicationFactoryTests(unittest.TestCase):
    """
    클래스 이름: ApplicationFactoryTests
    기능: mode parser, history port 선택과 runtime 객체 identity 조립을 검증한다.
    작성 날짜: 2026/08/21
    """

    def test_execution_mode_parser_accepts_only_exact_canonical_strings(
        self,
    ) -> None:
        """
        함수 이름: test_execution_mode_parser_accepts_only_exact_canonical_strings()
        기능: 네 canonical 문자열만 해당 ExecutionMode로 해석되는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        expected_modes = {
            "disabled": ExecutionMode.DISABLED,
            "fake": ExecutionMode.FAKE,
            "testnet": ExecutionMode.TESTNET,
            "live": ExecutionMode.LIVE,
        }

        # 정확한 wire 문자열과 이미 검증된 Enum은 값을 보존한다.
        for raw_mode, expected_mode in expected_modes.items():
            with self.subTest(raw_mode=raw_mode):
                self.assertIs(parse_execution_mode(raw_mode), expected_mode)
                self.assertIs(parse_execution_mode(expected_mode), expected_mode)

    def test_execution_mode_parser_fails_closed_for_invalid_values(self) -> None:
        """
        함수 이름: test_execution_mode_parser_fails_closed_for_invalid_values()
        기능: 누락, unknown, 공백·대소문자 변형과 비문자 설정이 disabled인지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        invalid_values = (
            None,
            "",
            "LIVE",
            " live ",
            "production",
            1,
            True,
            b"fake",
            object(),
        )

        # 설정 parsing 실패는 더 권한이 큰 모드로 절대 fallback하지 않는다.
        for invalid_value in invalid_values:
            with self.subTest(invalid_value=invalid_value):
                self.assertIs(
                    parse_execution_mode(invalid_value),
                    ExecutionMode.DISABLED,
                )

    def test_factory_preserves_controller_entity_and_lock_identity(self) -> None:
        """
        함수 이름: test_factory_preserves_controller_entity_and_lock_identity()
        기능: 조립된 Controller가 runtime의 동일 entity, gateway, repository와 RLock을 공유하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        history_repository = _StubHistoryRepository()
        runtime = create_application_runtime(
            _StubRestClient(),
            _StubWebSocketClient(),
            history_repository=history_repository,
            execution_mode="fake",
            clock=lambda: FIXED_TIME,
        )

        # Runtime 공개 field와 기존 Controller의 내부 owner가 한 identity인지 확인한다.
        self.assertIs(runtime.lock, runtime.application_lock)
        self.assertIs(runtime.trading_controller.account, runtime.account)
        self.assertIs(runtime.trade_history_repository, history_repository)
        self.assertIs(
            runtime.market_data_controller._market_snapshot,
            runtime.market_snapshot,
        )
        self.assertIs(
            runtime.market_data_controller._regime_controller,
            runtime.regime_controller,
        )
        self.assertIs(
            runtime.regime_controller._regime_stm,
            runtime.regime_stm,
        )
        self.assertIs(
            runtime.trade_history_controller._repository,
            history_repository,
        )
        self.assertIs(runtime.execution_mode, ExecutionMode.FAKE)
        self.assertIs(runtime.state.status, ApplicationStatus.CREATED)
        self.assertFalse(runtime.ready)
        self.assertIsNone(runtime.failure)

    def test_factory_requires_exactly_one_history_source(self) -> None:
        """
        함수 이름: test_factory_requires_exactly_one_history_source()
        기능: path와 repository 누락 또는 동시 주입을 조립 오류로 거부하는지 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/08/21
        """
        rest_client = _StubRestClient()
        web_socket_client = _StubWebSocketClient()
        repository = _StubHistoryRepository()

        # History owner가 모호하거나 없으면 runtime을 생성하지 않는다.
        with self.assertRaisesRegex(ValueError, "exactly one"):
            create_application_runtime(rest_client, web_socket_client)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            create_application_runtime(
                rest_client,
                web_socket_client,
                history_path="history.jsonl",
                history_repository=repository,
            )


if __name__ == "__main__":
    unittest.main()
