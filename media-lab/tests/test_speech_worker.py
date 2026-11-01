import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from media_lab_core import speech_worker as worker
from media_lab_core.cpu_worker import WorkerStopped
from .test_speech_artifact import wav


def setup(tmp_path):
    data=json.dumps({'text':'Hello','voice':'upstream-default-english','seed':7}).encode()
    return dict(job_id='a'*32,data=data,root=tmp_path/'results',runtime=tmp_path/'python',
        models=tmp_path/'models',watermark=tmp_path/'watermark',voice=tmp_path/'voice',revision='test-revision')


def output(command, **kwargs):
    target=Path(command[command.index('--output')+1]);data=Path(command[command.index('--input')+1]).read_bytes()
    blob=wav();(target/'output.wav').write_bytes(blob)
    (target/'receipt.json').write_text(json.dumps({'version':1,'revision':'test-revision',
        'inputSha256':hashlib.sha256(data).hexdigest(),'audio':worker.inspect_wav(blob)}))
    assert kwargs['env']['HOME']==str(kwargs['cwd'])
    assert 'AWS_SECRET_ACCESS_KEY' not in kwargs['env']
    return {}


def test_publish_and_verified_recovery(tmp_path):
    args=setup(tmp_path)
    with patch.object(worker,'run_owned_process',side_effect=output) as execute:
        result=worker.run_speech_job(**args)
        assert result['path']==args['job_id']+'/output.wav' and not result['recovered']
        assert worker.run_speech_job(**args)['recovered']
        assert execute.call_count==1
    receipt=args['root']/args['job_id']/'receipt.json';receipt.write_text('{}')
    with pytest.raises(ValueError):worker.run_speech_job(**args)


def test_cancellation_after_render_never_promotes_staging(tmp_path):
    args=setup(tmp_path);cancel=[False]
    def execute(*a,**kw):output(*a,**kw);cancel[0]=True
    with patch.object(worker,'run_owned_process',side_effect=execute),pytest.raises(WorkerStopped):
        worker.run_speech_job(**args,cancelled=lambda:cancel[0])
    assert not (args['root']/args['job_id']).exists()
    assert not list(args['root'].glob('.*-*/accepted'))


def test_invalid_output_never_publishes(tmp_path):
    args=setup(tmp_path)
    def execute(command,**kw):
        output(command,**kw)
        target=Path(command[command.index('--output')+1]);(target/'output.wav').write_bytes(b'invalid')
    with patch.object(worker,'run_owned_process',side_effect=execute),pytest.raises(ValueError):
        worker.run_speech_job(**args)
    assert not (args['root']/args['job_id']).exists()
