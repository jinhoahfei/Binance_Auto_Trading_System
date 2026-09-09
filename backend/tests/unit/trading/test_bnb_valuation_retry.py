"""BNB 공식 동일 구간 재조회와 지속 실패의 차단 경계를 검증한다."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from binance_auto_trader.adapters.binance.bnb_fee_valuator import BnbFeeValuator
from tests.unit.trading.test_bnb_fee_accounting import valuation_for
from tests.unit.trading.test_order import TEST_TIME


def make_row(trade_count: int = 6) -> list[object]:
    """
    함수 이름: make_row()
    기능: 테스트 체결 직전 1초의 공식 12필드 응답 fixture를 만든다.
    인자: trade_count -> 구간 내 체결 수
    반환값: Kline row
    작성 날짜: 2026/09/09
    """
    evidence = valuation_for()
    return [evidence.open_time_ms, "600", "600", "600", "600", "2", evidence.close_time_ms, "1200", trade_count, "0", "0", "0"]  # 네트워크를 사용하지 않는다.


class BnbValuationRetryTests(unittest.TestCase):
    """
    클래스 이름: BnbValuationRetryTests
    기능: 데이터 반영 지연은 같은 구간에서 회복하고 오류·무체결을 성공으로 위장하지 않는다.
    작성 날짜: 2026/09/09
    """

    def test_delayed_candle_recovers_without_changing_window(self) -> None:
        """
        함수 이름: test_delayed_candle_recovers_without_changing_window()
        기능: 빈 응답과 체결 0 이후 원 구간 체결이 나타나면 고정 근거를 반환한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        request = Mock(side_effect=[SimpleNamespace(payload=rows) for rows in ([], [make_row(0)], [make_row()])])
        wait = Mock()
        result = BnbFeeValuator(request, wait=wait).resolve(TEST_TIME)

        # 지연 후에도 첫 요청과 같은 UTC 구간의 public GET만 수행했는지 확인한다.
        self.assertEqual(result, valuation_for())
        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [1, 1])
        for call in request.call_args_list:
            self.assertEqual(call, request.call_args_list[0])
            self.assertEqual(call.kwargs["method"], "GET")
            self.assertFalse(call.kwargs["signed"])  # 주문·인증 endpoint로 재시도하지 않는다.

    def test_persistent_empty_or_zero_trade_response_is_bounded(self) -> None:
        """
        함수 이름: test_persistent_empty_or_zero_trade_response_is_bounded()
        기능: 지속적인 빈/무체결 응답을 4회 뒤 실패로 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        for rows, reason in (([], "candle unavailable"), ([make_row(0)], "requires actual market trades")):
            with self.subTest(reason=reason):
                request = Mock(return_value=SimpleNamespace(payload=rows))
                wait = Mock()
                with self.assertRaisesRegex(ValueError, reason):
                    BnbFeeValuator(request, wait=wait).resolve(TEST_TIME)
                self.assertEqual(request.call_count, 4)
                self.assertEqual([call.args[0] for call in wait.call_args_list], [1, 1, 2])  # 대기 합계는 4초다.

    def test_malformed_data_and_network_failures_are_not_retried(self) -> None:
        """
        함수 이름: test_malformed_data_and_network_failures_are_not_retried()
        기능: 미래 구간·잘못된 타입·가격·네트워크 실패를 지연으로 숨기지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        row = make_row()
        payloads = (None, [row, row], [row[:4]], [[row[0] + 1000] + row[1:]], [row[:8] + [True] + row[9:]], [row[:8] + [-1] + row[9:]], [row[:4] + ["NaN"] + row[5:]])
        for payload in payloads:
            with self.subTest(payload=payload):
                request = Mock(return_value=SimpleNamespace(payload=payload))
                wait = Mock()
                with self.assertRaises((ValueError, TypeError)):
                    BnbFeeValuator(request, wait=wait).resolve(TEST_TIME)
                request.assert_called_once()
                wait.assert_not_called()

        # Timeout과 서버 오류는 원래 호출자에게 전파해 자동 network 재시도를 막는다.
        for error in (TimeoutError(), RuntimeError("server failure")):
            request = Mock(side_effect=error)
            wait = Mock()
            with self.assertRaises(type(error)):
                BnbFeeValuator(request, wait=wait).resolve(TEST_TIME)
            request.assert_called_once()
            wait.assert_not_called()

    def test_immediate_success_does_not_wait(self) -> None:
        """
        함수 이름: test_immediate_success_does_not_wait()
        기능: 이미 준비된 구간의 조회에는 추가 대기나 요청을 넣지 않는다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        request = Mock(return_value=SimpleNamespace(payload=[make_row()]))
        wait = Mock()
        self.assertEqual(BnbFeeValuator(request, wait=wait).resolve(TEST_TIME), valuation_for())
        request.assert_called_once()
        wait.assert_not_called()  # 정상 경로의 latency는 바꾸지 않는다.
