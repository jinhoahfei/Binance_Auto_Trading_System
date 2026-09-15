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
    def test_prepare_response_replay_status_poll_and_verified_process_exit(self):
        backend_root = Path(__file__).resolve().parents[3]
        token = secrets.token_urlsafe(32)
        with TemporaryDirectory() as directory:
            environment = {'PYTHONPATH': os.pathsep.join((str(backend_root/'src'),str(backend_root))), 'PYTHONUNBUFFERED':'1','PYTHONDONTWRITEBYTECODE':'1'}
            if os.name=='nt': environment['SystemRoot']=os.environ['SystemRoot']
            child = subprocess.Popen([sys.executable,'-m','tests.integration.shutdown_preparation_process_fixture'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=directory,env=environment)
            try:
                write_json_frame(child.stdin,{'type':'BOOTSTRAP','token':token,'configuration':{
                    'schema_version':3,'allowed_origin':TEST_ORIGIN,'history_path':str(Path(directory)/'history.jsonl'),
                    'api_key':'fixture-key','api_secret':'fixture-secret','allow_testnet_orders':False,'max_notional':None}})
                descriptor=read_json_frame(child.stdout,maximum_bytes=4096)
                self.assertIsNotNone(descriptor)
                request=framed_fixture.FramedSidecarProcessTests()._request
                denied,_,=request(descriptor,secrets.token_urlsafe(32),'GET','/v1/shutdown/prepare')
                self.assertEqual(denied,401)
                status,basis=request(descriptor,token,'GET','/v1/shutdown/state')
                self.assertEqual(status,200)
                self.assertEqual(basis['data']['status'],'reconciliation_required')
                malformed,_=request(descriptor,token,'POST','/v1/shutdown/prepare',{'schema_version':3,'expected_version':basis['data']['version'],'liquidation_confirmed':'false'})
                self.assertEqual(malformed,400)
                prepare_body = {'schema_version':3,'expected_version':basis['data']['version'],'liquidation_confirmed':False}
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
                self.assertIsNone(child.poll())
                status,final=request(descriptor,token,'POST','/v1/shutdown',{'schema_version':3,'expected_version':progress['data']['version']})
                self.assertEqual(status,202,final)
                self.assertTrue(final['data']['accepted'])
                write_json_frame(child.stdin,{'type':'CLOSED_ACK'})
                self.assertEqual(child.wait(timeout=5),0)
                ownership=json.loads((Path(directory)/'.backend-runtime.lock').read_text())
                self.assertEqual(ownership['owner_state'],'RELEASED')
            finally:
                _terminate_fixture_process_tree(child)
                for pipe in (child.stdin,child.stdout,child.stderr):
                    if pipe:pipe.close()
