"""The session fixture must fence workers before collection imports the app."""
import os
from pathlib import Path
import subprocess
import sys


def test_session_fixture_disables_workers_before_any_app_import():
    conftest = Path(__file__).with_name('conftest.py')
    env = dict(os.environ)
    env.pop('MEDIA_LAB_DISABLE_BACKGROUND_WORKERS', None)
    result = subprocess.run(
        [sys.executable, '-c',
         "import os,runpy,sys;runpy.run_path(sys.argv[1]);"
         "assert os.environ.get('MEDIA_LAB_DISABLE_BACKGROUND_WORKERS')=='1',"
         "'Session fixture must disable workers before test-module app imports'",
         str(conftest)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
