"""실제 signed transport와 Earn 이력의 원금·페이지·권한 경계를 검증한다."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit
import unittest

from binance_auto_trader.adapters.binance.earn_residual import read_earn_residual_evidence, _HISTORY_WINDOW_MS
from binance_auto_trader.adapters.binance.live_clients import BinanceLiveRESTClient, _LiveHTTPTransport
from tests.unit.binance.test_spot_rest_client import _json_response

SINCE = datetime(2026, 9, 10, tzinfo=timezone.utc)
SINCE_MS = int(SINCE.timestamp() * 1000)


class EarnHTTP:
    """
    클래스 이름: EarnHTTP
    기능: 실제 장애의 자동 예치 원금과 보상을 익명 fixture로 제공한다.
    작성 날짜: 2026/09/15
    """
    def __init__(self):
        """
        함수 이름: __init__()
        기능: 읽기 응답과 요청 기록을 준비한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/15
        """
        self.requests = []
        self.position = {'asset':'ETH','productId':'ETH001','totalAmount':'0.00009601','cumulativeRealTimeRewards':'0.00000001','collateralAmount':'0','canRedeem':True}
        self.subscription = {'asset':'ETH','productId':'ETH001','amount':'0.000096','time':SINCE_MS+1000,'purchaseId':123,'type':'AUTO','sourceAccount':'SPOT','status':'SUCCESS'}
        self.redemptions = []
        self.rewards = []
        self.total_override = None

    def read(self, endpoint, parameters):
        """
        함수 이름: read()
        기능: 시간 구간에 해당하는 ETH fixture만 반환한다.
        인자: endpoint -> 요청 경로, parameters -> 페이지·시간 조건
        반환값: 공식 형태의 payload
        작성 날짜: 2026/09/15
        """
        self.requests.append((endpoint, parameters))
        if endpoint.endswith('/position'):
            rows = [self.position] if self.position else []
        elif endpoint.endswith('/rewardsRecord'):
            rows = self.rewards
        elif endpoint.endswith('/redemptionRecord'):
            rows = self.redemptions
        else:
            rows = [self.subscription] if parameters['startTime'] <= self.subscription['time'] <= parameters['endTime'] else []
        return deepcopy({'total':len(rows) if self.total_override is None else self.total_override,'rows':rows})

    def request(self, *, method, url, headers, body, timeout_seconds, before_send=None):
        """
        함수 이름: request()
        기능: native와 같은 live client의 signed GET 및 fixed host를 검사한다.
        인자: HTTP 요청 필드
        반환값: memory HTTP 응답
        작성 날짜: 2026/09/15
        """
        parsed = urlsplit(url)
        if method != 'GET' or parsed.netloc != 'api.binance.com' or body is not None:
            raise AssertionError('unexpected mutation or host')
        if parsed.path == '/api/v3/time':
            return _json_response({'serverTime':SINCE_MS+5000})
        parameters = parse_qs(parsed.query)
        if 'signature' not in parameters or headers.get('X-MBX-APIKEY') != 'fixture-key':
            raise AssertionError('missing signed GET')
        clean = {key:int(values[0]) if key in ('current','size','startTime','endTime') else values[0] for key,values in parameters.items() if key not in ('signature','timestamp','recvWindow')}
        return _json_response(self.read(parsed.path, clean))


class EarnResidualTests(unittest.TestCase):
    """
    클래스 이름: EarnResidualTests
    기능: 완전한 자동 예치만 허용하며 이력·보유량·전송 범위 위반을 거부한다.
    작성 날짜: 2026/09/15
    """
    def test_redeemed_principal_and_extra_reward_require_exact_history(self):
        """
        함수 이름: test_redeemed_principal_and_extra_reward_require_exact_history()
        기능: 상환된 원금과 초과 보상은 공식 기록이 모두 일치할 때만 인정한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/15
        """
        transport = EarnHTTP()
        transport.position = None
        transport.redemptions = [{'asset':'ETH','productId':'ETH001','amount':'0.00009601','time':SINCE_MS+4000,'redeemId':456,'destAccount':'SPOT','status':'PAID'}]
        transport.rewards = [{'asset':'ETH','productId':'ETH001','rewards':'0.00000001','type':'REALTIME','time':SINCE_MS+3000}]
        evidence = read_earn_residual_evidence(transport.read,SINCE,SINCE_MS+5000)
        self.assertEqual(evidence.quantity,0)
        self.assertEqual(evidence.rewards,Decimal('0.00000001'))
        self.assertEqual(evidence.redemptions[0][2],Decimal('0.00009601'))
        transport.rewards = []
        with self.assertRaises(ValueError):
            read_earn_residual_evidence(transport.read,SINCE,SINCE_MS+5000)
        transport.redemptions[0]['destAccount'] = 'FUNDING'
        with self.assertRaises(ValueError):
            read_earn_residual_evidence(transport.read,SINCE,SINCE_MS+5000)

    def test_signed_read_only_client_preserves_principal_and_rewards(self):
        """
        함수 이름: test_signed_read_only_client_preserves_principal_and_rewards()
        기능: 실제 서명 GET 경계와 예치·상환 mutation 거부를 검증한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/15
        """
        transport = EarnHTTP()
        client = BinanceLiveRESTClient('fixture-key','fixture-secret',transport=transport)
        evidence = client.fetch_earn_residual_evidence(since=SINCE)
        self.assertEqual(evidence.quantity, Decimal('0.00009601'))
        self.assertEqual(evidence.rewards, Decimal('0.00000001'))
        self.assertEqual(evidence.subscriptions[0][2], Decimal('0.000096'))
        self.assertEqual(len(transport.requests), 3)
        for method, endpoint in [('POST','/sapi/v1/simple-earn/flexible/subscribe'),('POST','/sapi/v1/simple-earn/flexible/position'),('GET','/sapi/v1/asset/dribblet')]:
            with self.subTest(method=method, endpoint=endpoint), self.assertRaises(ValueError):
                client._request_json(method=method,endpoint=endpoint,parameters={},signed=True)
        with self.assertRaises(ValueError):
            _LiveHTTPTransport(transport,order_capability=None).request(method='POST',url='https://api.binance.com/sapi/v1/simple-earn/flexible/redeem',headers={},body=None,timeout_seconds=1)

    def test_history_is_complete_across_thirty_day_windows(self):
        """
        함수 이름: test_history_is_complete_across_thirty_day_windows()
        기능: 30일 구간 경계의 이력 누락 없이 예치 원금을 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/15
        """
        transport = EarnHTTP()
        evidence = read_earn_residual_evidence(transport.read, SINCE, SINCE_MS+_HISTORY_WINDOW_MS+2000)
        self.assertEqual(len(evidence.subscriptions), 1)
        history = [parameters for endpoint, parameters in transport.requests if '/history/' in endpoint]
        self.assertEqual(len(history), 4)
        self.assertTrue(all(p['endTime']-p['startTime'] < _HISTORY_WINDOW_MS for p in history))
        self.assertEqual(history[2]['startTime'], history[0]['endTime']+1)

    def test_ambiguous_or_incomplete_evidence_is_rejected(self):
        """
        함수 이름: test_ambiguous_or_incomplete_evidence_is_rejected()
        기능: 변조·미완료·누락된 Earn 근거를 거부한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/15
        """
        cases = [
            ('position', 'totalAmount', '0.1'), ('position','collateralAmount','0.000096'),
            ('position','totalAmount',0.00009601), ('position','canRedeem',False),
            ('subscription','type','MANUAL'), ('subscription','sourceAccount','FUNDING'),
            ('subscription','status','PENDING'), ('subscription','productId','different'),
            ('subscription','amtFromSpot','0'), ('subscription','time',SINCE_MS-1),
        ]
        for group, field, value in cases:
            transport = EarnHTTP()
            getattr(transport, group)[field] = value
            with self.subTest(group=group,field=field):
                if field == 'time':
                    self.assertIsNone(read_earn_residual_evidence(transport.read,SINCE,SINCE_MS+5000))
                else:
                    with self.assertRaises(ValueError):
                        read_earn_residual_evidence(transport.read,SINCE,SINCE_MS+5000)
        for mutate in ('redemptions','truncated'):
            transport = EarnHTTP()
            if mutate == 'redemptions':
                transport.redemptions = [{'asset':'ETH','amount':'0.000096','status':'PAID'}]
            else:
                transport.total_override = 2
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                read_earn_residual_evidence(transport.read,SINCE,SINCE_MS+5000)
