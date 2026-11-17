"""Run only local tests in a hosted runner with inherited app settings removed."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

PREFIXES = ('NVB_', 'PG', 'DATABASE_', 'ADAPTER_', 'STATEMENTS_', 'LOANS_',
            'GATEWAY_', 'AWS_', 'S3_', 'RABBITMQ_', 'AMQP_', 'KUBERNETES_')

def main():
    root = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(PREFIXES)}
    with tempfile.TemporaryDirectory(prefix='nvb-ci-') as temporary:
        env['HOME'] = temporary
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env['PYTHONPATH'] = str(root / 'libs' / 'nvb_platform')
        if (root / 'backend').is_dir():
            suites = [('backend', [
                'tests/test_health.py', 'tests/test_auth.py',
                'tests/test_accounts_isolation.py', 'tests/test_integration_auth.py',
                'tests/test_integration_exports.py', 'tests/test_integration_consistency.py',
                'tests/test_transfers.py::test_successful_transfer_moves_money_and_writes_ledger'
            ])]
        else:
            suites = [(f'services/{name}', ['tests'])
                      for name in ('core-adapter', 'statements', 'loans')]
        for directory, tests in suites:
            print('Local SQLite/fake-dependency suite:', directory, flush=True)
            subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                            '-o', 'addopts=', *tests], cwd=root / directory,
                           env=env, check=True)

if __name__ == '__main__':
    main()
