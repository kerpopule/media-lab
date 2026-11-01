import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from media_lab_core import cpu_worker as worker


class CPUWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def run_child(self, code, **options):
        with worker.cpu_slot(self.root) as fd:
            return worker.run_owned_process(
                [sys.executable, '-c', code], cwd=self.root, lock_fd=fd,
                memory_bytes=options.pop('memory_bytes', 128 * 1024**2),
                timeout=options.pop('timeout', 5), **options)

    def test_output_is_drained_without_deadlock_and_tail_is_bounded(self):
        result = self.run_child("print('x' * 1000000); print('finished')")
        self.assertLessEqual(len(result['log_tail']), 65536)
        self.assertTrue(result['log_tail'].endswith('finished\n'))

    def test_deadline_cancellation_and_failed_child(self):
        for options, message in [({'timeout': .15}, 'time budget'),
                                 ({'cancelled': lambda: True}, 'cancelled')]:
            with self.subTest(message=message), self.assertRaisesRegex(worker.WorkerStopped, message):
                self.run_child('import time; time.sleep(60)', **options)
        with self.assertRaisesRegex(worker.WorkerStopped, 'failed'):
            self.run_child('raise SystemExit(3)')
        with worker.cpu_slot(self.root):
            pass  # every stopped child released its inherited lock

    def test_memory_admission_never_starts_child(self):
        with patch.object(worker.psutil, 'virtual_memory', return_value=SimpleNamespace(available=1)), \
                patch.object(worker.subprocess, 'Popen') as spawn:
            with self.assertRaises(worker.WorkerBusy):
                self.run_child('pass')
            spawn.assert_not_called()

    def test_running_memory_limit(self):
        with self.assertRaisesRegex(worker.WorkerStopped, 'memory budget'):
            self.run_child('import time; allocation=bytearray(80*1024**2); time.sleep(60)',
                           memory_bytes=48 * 1024**2)

    def test_cancellation_stops_owned_descendant(self):
        pid_file = self.root / 'child.pid'
        code = ("import subprocess,sys,time,pathlib; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(60)")
        with self.assertRaisesRegex(worker.WorkerStopped, 'time budget'):
            self.run_child(code, timeout=.5)
        pid = int(pid_file.read_text())
        try:
            process = worker.psutil.Process(pid)
            self.assertEqual(process.status(), worker.psutil.STATUS_ZOMBIE)
        except worker.psutil.NoSuchProcess:
            pass

    def test_inherited_lock_survives_controller_close(self):
        with worker.cpu_slot(self.root) as fd:
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], pass_fds=(fd,))
        try:
            with self.assertRaises(worker.WorkerBusy), worker.cpu_slot(self.root):
                pass
        finally:
            child.terminate()
            child.wait(timeout=5)
        with worker.cpu_slot(self.root):
            pass

    def make_result(self, directory, source):
        directory.mkdir(exist_ok=True)
        output = io.BytesIO()
        Image.new('RGBA', (4, 3), (10, 20, 30, 128)).save(output, format='PNG')
        png = output.getvalue()
        receipt = {'engine': 'birefnet-cpu', 'revision': json.loads(worker.MANIFEST.read_text())['revision'],
                   'device': 'cpu', 'dtype': 'float32', 'input_sha256': hashlib.sha256(source).hexdigest(),
                   'output_sha256': hashlib.sha256(png).hexdigest()}
        (directory / 'output.png').write_bytes(png)
        (directory / 'receipt.json').write_text(json.dumps(receipt))

    def test_publication_recovery_and_input_conflict(self):
        source = io.BytesIO()
        Image.new('RGB', (4, 3)).save(source, format='PNG')
        data = source.getvalue()
        args = dict(job_id='a'*32, data=data, root=self.root, runtime=Path(sys.executable),
                    package=self.root, cache=self.root)

        def fake_process(command, **kwargs):
            self.make_result(Path(command[-1]), data)
            return {}

        with patch.object(worker, 'run_owned_process', side_effect=fake_process) as run:
            first = worker.run_background_job(**args)
            second = worker.run_background_job(**args)
            self.assertFalse(first.pop('recovered'))
            self.assertTrue(second.pop('recovered'))
            self.assertEqual(first, second)
            self.assertEqual(run.call_count, 1)
            altered = io.BytesIO()
            Image.new('RGB', (4, 3), 'red').save(altered, format='PNG')
            with self.assertRaisesRegex(ValueError, 'integrity'):
                worker.run_background_job(**{**args, 'data': altered.getvalue()})
        self.assertEqual(list(self.root.glob('.' + 'a'*32 + '-*')), [])

    def test_invalid_or_cancelled_results_are_never_published(self):
        source = io.BytesIO()
        Image.new('RGB', (4, 3)).save(source, format='PNG')
        data = source.getvalue()
        args = dict(job_id='b'*32, data=data, root=self.root, runtime=Path(sys.executable),
                    package=self.root, cache=self.root)
        stopped = False

        def fake_process(command, **kwargs):
            nonlocal stopped
            self.make_result(Path(command[-1]), data)
            stopped = True

        with patch.object(worker, 'run_owned_process', side_effect=fake_process):
            with self.assertRaises(worker.WorkerStopped):
                worker.run_background_job(**args, cancelled=lambda: stopped)
        self.assertFalse((self.root / ('b'*32)).exists())
        directory = self.root / 'invalid'
        self.make_result(directory, data)
        (directory / 'output.png').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'integrity'):
            worker.verify_result(directory, hashlib.sha256(data).hexdigest(), (4, 3))
        link = self.root / 'link'
        link.symlink_to(directory, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'directory'):
            worker.verify_result(link, '', (4, 3))
        self.make_result(directory, data)
        receipt = directory / 'receipt.json'
        saved = self.root / 'saved.json'
        receipt.rename(saved)
        receipt.symlink_to(saved)
        with self.assertRaisesRegex(ValueError, 'receipt'):
            worker.verify_result(directory, hashlib.sha256(data).hexdigest(), (4, 3))


if __name__ == '__main__':
    unittest.main()
