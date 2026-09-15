"""BNB 공식 동일 구간 재조회와 지속 실패의 차단 경계를 검증한다."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from binance_auto_trader.adapters.binance.bnb_fee_valuator import (
    BnbFeeValuator, BnbValuationInvalidError, BnbValuationUnavailableError,
)
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
    기능: 빈 응답은 같은 구간에서 재확인하고 거래 0건인 정상 종가도 사용한다.
    작성 날짜: 2026/09/09
    """

    def test_delayed_candle_recovers_without_changing_window(self) -> None:
        """
        함수 이름: test_delayed_candle_recovers_without_changing_window()
        기능: 빈 응답 이후 거래 0건인 공식 종가가 나타나면 원 구간 근거를 반환한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        request = Mock(side_effect=[SimpleNamespace(payload=rows) for rows in ([], [make_row(0)], [make_row()])])
        wait = Mock()
        result = BnbFeeValuator(request, wait=wait).resolve(TEST_TIME)

        # 지연 후에도 첫 요청과 같은 UTC 구간의 public GET만 수행했는지 확인한다.
        self.assertEqual(result, valuation_for())
        self.assertEqual(request.call_count, 2)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [1])
        for call in request.call_args_list:
            self.assertEqual(call, request.call_args_list[0])
            self.assertEqual(call.kwargs["method"], "GET")
            self.assertFalse(call.kwargs["signed"])  # 주문·인증 endpoint로 재시도하지 않는다.

    def test_persistent_empty_response_is_bounded(self) -> None:
        """
        함수 이름: test_persistent_empty_response_is_bounded()
        기능: 지속적인 빈 응답을 4회 뒤 조회 불가 오류로 보존한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/09
        """
        for rows, reason in (([], "candle unavailable"),):
            with self.subTest(reason=reason):
                request = Mock(return_value=SimpleNamespace(payload=rows))
                wait = Mock()
                with self.assertRaisesRegex(BnbValuationUnavailableError, reason):
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
                with self.assertRaises(BnbValuationInvalidError):
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

    def test_incident_zero_trade_candle_is_valid_official_close(self) -> None:
        from datetime import datetime, timezone
        from decimal import Decimal
        row = [1789479701000, "717.77", "717.77", "717.77", "717.77", "0",
               1789479701999, "0", 0, "0", "0", "0"]
        request, wait = Mock(return_value=SimpleNamespace(payload=[row])), Mock()
        instant = datetime(2026, 9, 15, 13, 41, 42, tzinfo=timezone.utc)
        valuation = BnbFeeValuator(request, wait=wait).resolve(instant)
        self.assertEqual(valuation.quote_amount(Decimal("0.000001"), instant), Decimal("0.00071777"))
        request.assert_called_once()
        wait.assert_not_called()

        # 주문 준비와 실제 myTrades 비용 계산 모두 같은 공식 봉 검증기를 거친다.
        from binance_auto_trader.adapters.binance.mappers import map_fill_payloads
        from binance_auto_trader.bootstrap.live_permission import LiveOrderPermissionRESTClient
        from tests.unit.bootstrap.test_live_bootstrap import live_configuration, live_order
        from tests.unit.bootstrap.test_testnet_configuration import _zero_commission_payload
        delegate = Mock()
        commission = _zero_commission_payload()
        commission['standardCommission']['taker'] = '0.001'
        commission['discount'] = {'enabledForAccount': True, 'enabledForSymbol': True,
            'discountAsset': 'BNB', 'discount': '0.25'}
        delegate.get_account_commission.return_value = commission
        delegate.resolve_bnb_fee.side_effect = BnbFeeValuator(request, wait=wait).resolve
        order = live_order()
        delegate.prepare_order.return_value = order
        permission = LiveOrderPermissionRESTClient(delegate, live_configuration(orders=True),
            base_fee_residual_enabled=True, bnb_fee_accounting_enabled=True)
        with patch('binance_auto_trader.bootstrap.live_permission.datetime') as current_time:
            current_time.now.return_value = instant
            self.assertIs(permission.prepare_order(order=order), order)
        fills = map_fill_payloads([{'id': 11, 'orderId': 1001, 'price': '2444', 'qty': '0.001',
            'commission': '0.000001', 'commissionAsset': 'BNB', 'time': 1789479702000}],
            symbol='ETHUSDT', exchange_order_id='1001', base_asset='ETH', quote_asset='USDT',
            fallback_executed_at=instant, bnb_fee_resolver=delegate.resolve_bnb_fee)
        self.assertEqual(fills[0].fee_quote_amount, Decimal('0.00071777'))
        self.assertEqual(request.call_count, 3)
        self.assertTrue(all(call == request.call_args_list[0] for call in request.call_args_list))
        delegate.submit_order.assert_not_called()
        wait.assert_not_called()
