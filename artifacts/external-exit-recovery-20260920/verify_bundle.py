"""수정한 production 모듈을 앱의 PYZ에서만 로드해 오프라인 회귀를 실행한다."""
import importlib.abc
import importlib.util
from pathlib import Path
import sys
import unittest
from PyInstaller.archive.readers import CArchiveReader

binary = Path(sys.argv[1]).resolve(strict=True)
repository = Path('/Users/oscar/Desktop/Binance_Auto')
archive = CArchiveReader(str(binary))
pyz = archive.open_embedded_archive('PYZ.pyz')
loaded = set()

class BundledLoader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != 'binance_auto_trader' and not fullname.startswith('binance_auto_trader.'):
            return None
        if fullname not in pyz.toc:
            raise ImportError(f'Production source fallback forbidden: {fullname}')
        is_package = pyz.toc[fullname][0] in (1, 3)
        return importlib.util.spec_from_loader(fullname, self, is_package=is_package)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__file__ = f'{binary}!/{module.__name__.replace(".", "/")}.pyc'
        code = pyz.extract(module.__name__)
        loaded.add(module.__name__)
        if code is not None:
            exec(code, module.__dict__)

sys.meta_path.insert(0, BundledLoader())
sys.path.insert(0, str(repository / 'backend'))
if len(sys.argv) > 2 and sys.argv[2] == '--live-copy':
    import runpy
    sys.path.insert(0, str(repository))
    runpy.run_path(str(Path(__file__).with_name('verify_live_copy.py')), run_name='__main__')
    raise SystemExit(0)
suite = unittest.defaultTestLoader.loadTestsFromNames([
    'tests.integration.test_external_exit_recovery',
    'tests.integration.test_earn_residual_recovery',
    'tests.unit.trading.test_residual_settlement',
    'tests.unit.trading.test_position',
    'tests.unit.trading.test_order_duplicate_fill_regression',
])
result = unittest.TextTestRunner(verbosity=2).run(suite)
print(f'Bundled production modules loaded: {len(loaded)}')
print('Production source fallback: forbidden')
print(f'Binary: {binary}')
raise SystemExit(not result.wasSuccessful())
