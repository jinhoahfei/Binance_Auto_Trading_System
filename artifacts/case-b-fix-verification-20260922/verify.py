"""Offline verification. Production imports come only from the chosen source or bundle."""
import sys, unittest, socket, importlib.abc, importlib.util, json, types, hashlib
from pathlib import Path
ROOT = Path('/Users/oscar/Desktop/Binance_Auto')
mode = sys.argv[1]
loaded = set()
def deny_network(*args, **kwargs):
    raise RuntimeError('Network disabled for offline verification')
socket.create_connection = deny_network
socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
sys.path.insert(0, str(ROOT / 'backend'))
if mode == 'source':
    sys.path.insert(0, str(ROOT / 'backend/src'))
else:
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(mode)
    pyz = archive.open_embedded_archive('PYZ.pyz')
    class Loader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != 'binance_auto_trader' and not fullname.startswith('binance_auto_trader.'):
                return None
            if fullname not in pyz.toc:
                raise ImportError(f'Production source fallback forbidden: {fullname}')
            return importlib.util.spec_from_loader(fullname, self, is_package=pyz.toc[fullname][0] in (1, 3))
        def create_module(self, spec):
            return None
        def exec_module(self, module):
            module.__file__ = f'{mode}!/{module.__name__}.pyc'
            loaded.add(module.__name__)
            code = pyz.extract(module.__name__)
            if code is not None:
                exec(code, module.__dict__)
    sys.meta_path.insert(0, Loader())
from binance_auto_trader.application.trading_controller import TradingController
for name in ['_buy_exposure', '_limit_buy_quantity', '_build_buy_risk_budget']:
    fn = getattr(TradingController, name, None)
    print('METHOD', name, 'present' if fn else 'MISSING', 'names', fn.__code__.co_names if fn else (), flush=True)
suite = unittest.defaultTestLoader.loadTestsFromNames([
    'tests.integration.test_buy_remaining_budget',
    'tests.unit.trading.test_risk',
])
result = unittest.TextTestRunner(verbosity=2).run(suite)
print(json.dumps({'target':mode,'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'bundle_modules':len(loaded),'network':'disabled'}), flush=True)
sys.exit(not result.wasSuccessful())
