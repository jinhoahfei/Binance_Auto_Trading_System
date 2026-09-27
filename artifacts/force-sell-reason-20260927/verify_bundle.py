"""앱 내 Python 모듈을 현재 소스와 대조하고 해당 모듈만으로 종료 회귀를 실행한다."""

import hashlib
import importlib.abc
import importlib.util
import json
from pathlib import Path
import runpy
import subprocess
import sys
import unittest
from unittest.mock import patch

from PyInstaller.archive.readers import CArchiveReader


binary = Path(sys.argv[1]).resolve(strict=True)
repository = Path(__file__).resolve().parents[2]
archive = CArchiveReader(str(binary)).open_embedded_archive("PYZ.pyz")
loaded_modules = set()
source_matches = {}
for module_name in archive.toc:
    if module_name == "binance_auto_trader" or module_name.startswith("binance_auto_trader."):
        source_path = repository / "backend/src" / module_name.replace(".", "/")
        source_path = source_path / "__init__.py" if archive.toc[module_name][0] in (1, 3) else source_path.with_suffix(".py")
        if source_path.is_file():
            code = archive.extract(module_name)
            source_matches[module_name] = code == compile(source_path.read_bytes(), code.co_filename, "exec")


class BundledModuleLoader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """
    클래스 이름: BundledModuleLoader
    기능: 테스트가 production 모듈을 원 소스로 우회하지 않고 앱 내 모듈만 사용하게 한다.
    작성 날짜: 2026/09/27
    """

    def find_spec(self, fullname, path=None, target=None):
        """
        함수 이름: find_spec()
        기능: production namespace의 모든 import를 검증할 앱으로 한정한다.
        인자: fullname -> 모듈 이름, path -> 탐색 경로, target -> 재적재 대상
        반환값: 모듈 명세 또는 다른 namespace이면 None
        작성 날짜: 2026/09/27
        """
        if fullname != "binance_auto_trader" and not fullname.startswith("binance_auto_trader."):
            return None
        if fullname not in archive.toc:
            raise ImportError(f"Source fallback forbidden: {fullname}")
        return importlib.util.spec_from_loader(fullname, self, is_package=archive.toc[fullname][0] in (1, 3))

    def create_module(self, spec):
        """
        함수 이름: create_module()
        기능: Python 기본 모듈 생성 동작을 사용한다.
        인자: spec -> 검증된 import 명세
        반환값: 기본 생성을 뜻하는 None
        작성 날짜: 2026/09/27
        """
        return None

    def exec_module(self, module):
        """
        함수 이름: exec_module()
        기능: 앱에서 추출한 코드로 모듈을 초기화하고 원본 사용 증거를 남긴다.
        인자: module -> 초기화할 모듈
        반환값: 없음
        작성 날짜: 2026/09/27
        """
        module.__file__ = f"{binary}!/{module.__name__.replace('.', '/')}.pyc"
        loaded_modules.add(module.__name__)
        exec(archive.extract(module.__name__), module.__dict__)


if not source_matches or not all(source_matches.values()):
    raise SystemExit(f"Bundled source mismatch: {[name for name, match in source_matches.items() if not match]}")
sys.meta_path.insert(0, BundledModuleLoader())
sys.path.insert(0, str(repository / "backend"))
if len(sys.argv) > 2 and sys.argv[2] == "--process-fixture":
    runpy.run_module("tests.integration.shutdown_preparation_process_fixture", run_name="__main__")
    raise SystemExit(0)

original_popen = subprocess.Popen


def start_bundled_fixture(arguments, *positional, **keywords):
    """
    함수 이름: start_bundled_fixture()
    기능: 종료 fixture 자식만 앱 모듈 로더로 시작하며 실제 프로세스와 HTTP는 그대로 사용한다.
    인자: arguments -> 실행 인자, positional -> 추가 인자, keywords -> 실행 옵션
    반환값: 실제 실행한 Popen 자식 프로세스
    작성 날짜: 2026/09/27
    """
    if isinstance(arguments, list) and arguments[1:] == ["-m", "tests.integration.shutdown_preparation_process_fixture"]:
        arguments = [arguments[0], str(Path(__file__).resolve()), str(binary), "--process-fixture"]
    return original_popen(arguments, *positional, **keywords)


suite = unittest.defaultTestLoader.loadTestsFromNames([
    "tests.integration.test_shutdown_completion",
    "tests.integration.test_stop_persistence_recovery_flow",
    "tests.integration.test_sell_minimum_deferral",
    "tests.integration.test_deterministic_production_path_case2_flow",
    "tests.unit.bootstrap.test_spot_fee_policy",
    "tests.unit.binance.test_spot_rest_client",
    "tests.integration.test_shutdown_preparation",
    "tests.integration.test_shutdown_order_storage_recovery",
    "tests.integration.test_earn_shutdown",
    "tests.unit.bootstrap.test_shutdown_lifecycle",
    "tests.unit.binance.test_subscription_cleanup",
    "tests.unit.binance.test_spot_websocket_client",
    "tests.unit.transport.test_shutdown_route",
    "tests.integration.transport.test_shutdown_preparation_process",
])
with patch("subprocess.Popen", side_effect=start_bundled_fixture):
    result = unittest.TextTestRunner(verbosity=1).run(suite)
evidence = {
    "binary": str(binary), "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    "source_modules_compared": len(source_matches), "all_source_modules_match": all(source_matches.values()),
    "production_modules_loaded_from_bundle": len(loaded_modules), "source_fallback_allowed": False,
    "tests_run": result.testsRun, "successful": result.wasSuccessful(),
}
Path(__file__).with_name("bundle-verification.json").write_text(json.dumps(evidence, indent=2))
print(json.dumps(evidence, indent=2))
raise SystemExit(not result.wasSuccessful())
