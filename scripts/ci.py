"""Fail-closed test runner: real SDK, nonzero discovery and explicit skip policy."""
import os
from pathlib import Path
import sys
import unittest

# Absence must fail, not turn integration tests into skips.
import maibot_sdk  # noqa: F401

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
suite = unittest.defaultTestLoader.discover(str(root), pattern='test_*.py', top_level_dir=str(root))
count = suite.countTestCases()
if count < 161:
    raise SystemExit(f'Expected at least 161 tests, discovered {count}')
result = unittest.TextTestRunner(verbosity=1).run(suite)
allowed = {'test_world_readable_keys_rejected'} if os.name == 'nt' else set()
unexpected = [(str(test), reason) for test, reason in result.skipped
              if test._testMethodName not in allowed or reason != 'POSIX permissions']
if unexpected:
    print('Unexpected skipped tests:', unexpected)
raise SystemExit(0 if result.wasSuccessful() and not unexpected else 1)
