"""가격 조회 실패 상태에서 인증된 종료 준비와 실제 child exit까지 검증한다."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from uuid import uuid4
from binance_auto_trader.transport.framing import read_json_frame, write_json_frame
from tests.integration.transport import test_framed_sidecar_process as framed_fixture
from tests.integration.transport.test_framed_sidecar_process import TEST_ORIGIN, _terminate_fixture_process_tree


class ShutdownPreparationProcessTests(unittest.TestCase):
    """
    클래스 이름: ShutdownPreparationProcessTests
    기능: 실제 자식 프로세스와 로컬 HTTP에서 안전 종료와 재시도를 검증한다.
    작성 날짜: 2026/09/27
    """

    def test_prepare_response_replay_status_poll_and_verified_process_exit(self):
        self.exercise_shutdown(False)

    def test_earn_rewards_with_failed_worker_exit_actual_process(self):
        self.exercise_shutdown(True)

    def test_held_position_with_stale_completed_order_liquidates_and_exits_actual_process(self):
        self.exercise_shutdown(False, stale_order=True)

    def test_cleanup_failure_after_liquidation_retries_without_another_sell_and_exits(self):
        """
        함수 이름: test_cleanup_failure_after_liquidation_retries_without_another_sell_and_exits()
        기능: 실제 자식 프로세스에서 매도 뒤 정리 실패·인증 조회·재시도·정상 종료를 확인한다.
        인자: 없음
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        self.exercise_shutdown(False, stale_order=True, retry_cleanup=True)

    def exercise_shutdown(self, earn, stale_order=False, retry_cleanup=False):
        """
        함수 이름: exercise_shutdown()
        기능: 모의 거래소를 쓰는 실제 sidecar의 인증된 종료 흐름과 저장 결과를 검증한다.
        인자: earn -> 보상 잔고 재현, stale_order -> 매도 사유·복귀 상태 재현
            retry_cleanup -> 최종 자원 정리의 일시 실패 재현
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        backend_root = Path(__file__).resolve().parents[3]
        token = secrets.token_urlsafe(32)
        with TemporaryDirectory() as directory:
            environment = {'PYTHONPATH': os.pathsep.join((str(backend_root/'src'),str(backend_root))), 'PYTHONUNBUFFERED':'1','PYTHONDONTWRITEBYTECODE':'1'}
            if earn: environment['SHUTDOWN_FIXTURE_EARN'] = '1'
            if stale_order: environment['SHUTDOWN_FIXTURE_STALE_ORDER'] = '1'
            if retry_cleanup: environment['SHUTDOWN_FIXTURE_RETRY_CLEANUP'] = '1'
            if os.name=='nt': environment['SystemRoot']=os.environ['SystemRoot']
            child = subprocess.Popen([sys.executable,'-m','tests.integration.shutdown_preparation_process_fixture'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=directory,env=environment)
            try:
                write_json_frame(child.stdin,{'type':'BOOTSTRAP','token':token,'configuration':{
                    'schema_version':3,'allowed_origin':TEST_ORIGIN,'history_path':str(Path(directory)/'history.jsonl'),
                    'api_key':'fixture-key','api_secret':'fixture-secret','allow_testnet_orders':False,'max_notional':None}})
                descriptor=read_json_frame(child.stdout,maximum_bytes=4096)
                self.assertIsNotNone(descriptor)
                self.assertIn('port', descriptor, descriptor)
                request=framed_fixture.FramedSidecarProcessTests()._request
                denied,_,=request(descriptor,secrets.token_urlsafe(32),'GET','/v1/shutdown/prepare')
                self.assertEqual(denied,401)
                status,basis=request(descriptor,token,'GET','/v1/shutdown/state')
                self.assertEqual(status,200)
                self.assertEqual(basis['data']['status'],'reconciliation_required')
                malformed,_=request(descriptor,token,'POST','/v1/shutdown/prepare',{'schema_version':3,'expected_version':basis['data']['version'],'liquidation_confirmed':'false'})
                self.assertEqual(malformed,400)
                prepare_body = {'schema_version':3,'expected_version':basis['data']['version'],'liquidation_confirmed':stale_order}
                with patch.object(framed_fixture, 'uuid4', return_value=uuid4()):
                    status,accepted=request(descriptor,token,'POST','/v1/shutdown/prepare',prepare_body)
                    repeated_status,repeated=request(descriptor,token,'POST','/v1/shutdown/prepare',prepare_body)
                self.assertEqual((repeated_status,repeated),(status,accepted))
                self.assertEqual(status,202)
                operation_id=accepted['data']['operation_id']
                deadline=monotonic()+5
                while monotonic()<deadline:
                    status,progress=request(descriptor,token,'GET','/v1/shutdown/prepare')
                    self.assertEqual(status,200)
                    self.assertEqual(progress['data']['operation_id'],operation_id)
                    if progress['data']['phase'] in ('ready','blocked'):break
                    sleep(.01)
                self.assertEqual(progress['data']['phase'],'ready',progress)
                if earn:
                    details=progress['data']['balance_reconciliation']
                    self.assertEqual(details['status'], 'verified')
                    self.assertEqual(details['earn_rewards_quantity'], '0.00000001')
                    self.assertEqual(details['exchange_spot_quantity'], '0.00009601')
                self.assertIsNone(child.poll())
                status,final=request(descriptor,token,'POST','/v1/shutdown',{'schema_version':3,'expected_version':progress['data']['version']})
                if retry_cleanup:
                    self.assertEqual(status, 500, final)
                    self.assertIsNone(child.poll())
                    status, basis = request(descriptor, token, 'GET', '/v1/shutdown/state')
                    self.assertEqual(status, 200, basis)
                    status, final = request(descriptor, token, 'POST', '/v1/shutdown',
                        {'schema_version': 3, 'expected_version': basis['data']['version']})
                self.assertEqual(status,202,final)
                self.assertTrue(final['data']['accepted'])
                write_json_frame(child.stdin,{'type':'CLOSED_ACK'})
                self.assertEqual(child.wait(timeout=5),0)
                ownership=json.loads((Path(directory)/'.backend-runtime.lock').read_text())
                self.assertEqual(ownership['owner_state'],'RELEASED')
                if retry_cleanup:
                    self.assertGreaterEqual(len(json.loads((Path(directory) / 'cleanup-attempts.json').read_text())), 2)
                if stale_order:
                    trades = [json.loads(line) for line in (Path(directory)/'deterministic-case2.jsonl').read_text().splitlines()]
                    self.assertEqual([trade['side'] for trade in trades], ['BUY', 'SELL'])
            finally:
                _terminate_fixture_process_tree(child)
                for pipe in (child.stdin,child.stdout,child.stderr):
                    if pipe:pipe.close()
