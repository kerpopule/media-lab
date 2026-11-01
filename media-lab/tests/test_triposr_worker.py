from pathlib import Path
from unittest.mock import patch
import pytest
from media_lab_core.triposr_worker import run_triposr_job
from media_lab_core.cpu_worker import WorkerBusy, WorkerStopped, cpu_slot


def args(root):
    return dict(job_id='a'*32,data=b'cutout',root=root,runtime=root/'python',package=root/'package')


def test_lock_and_cancellation_prevent_publication(tmp_path):
    calls=[]
    def process(command, **kwargs):
        calls.append(command)
        with pytest.raises(WorkerBusy), cpu_slot(tmp_path): pass
        Path(command[-1]).mkdir()
    with patch('media_lab_core.triposr_worker.run_owned_process',side_effect=process), patch(
            'media_lab_core.triposr_worker.verify_triposr_result',return_value=({'output_sha256':'b'*64},b'glb')):
        with pytest.raises(WorkerStopped):
            run_triposr_job(**args(tmp_path),cancelled=lambda:bool(calls))
    assert not (tmp_path/('a'*32)).exists()


def test_verification_failure_never_publishes(tmp_path):
    def process(command, **kwargs): Path(command[-1]).mkdir()
    with patch('media_lab_core.triposr_worker.run_owned_process',side_effect=process), patch(
            'media_lab_core.triposr_worker.verify_triposr_result',side_effect=ValueError('corrupt')):
        with pytest.raises(ValueError): run_triposr_job(**args(tmp_path))
    assert not (tmp_path/('a'*32)).exists()


def test_verified_existing_result_recovers_without_execution(tmp_path):
    (tmp_path/('a'*32)).mkdir()
    with patch('media_lab_core.triposr_worker.run_owned_process') as process, patch(
            'media_lab_core.triposr_worker.verify_triposr_result',return_value=({'output_sha256':'b'*64},b'glb')):
        result=run_triposr_job(**args(tmp_path))
    assert result['recovered'] and result['bytes']==3
    process.assert_not_called()


def test_reduced_profile_is_forwarded_to_child_and_result_verification(tmp_path):
    def process(command,**kwargs):
        assert command[command.index('--runtime-profile')+1]=='without-vision-v1'
        Path(command[-1]).mkdir()
    with patch('media_lab_core.triposr_worker.run_owned_process',side_effect=process), patch(
            'media_lab_core.triposr_worker.verify_triposr_result',return_value=({'output_sha256':'b'*64},b'glb')) as verify:
        result=run_triposr_job(**args(tmp_path),runtime_profile='without-vision-v1')
        assert result['recovered'] is False
        assert verify.call_args.kwargs=={'runtime_profile':'without-vision-v1'}


def test_unknown_profile_cannot_start_worker(tmp_path):
    with patch('media_lab_core.triposr_worker.run_owned_process') as process:
        with pytest.raises(ValueError,match='explicit'):
            run_triposr_job(**args(tmp_path),runtime_profile='auto')
        process.assert_not_called()
    assert not list(tmp_path.iterdir())
