import hashlib
import pickle
import zipfile
import pytest
from media_lab_core.checkpoint_inspect import inspect_checkpoint


def test_static_review_never_executes_pickle(tmp_path):
    marker=tmp_path/'must-not-exist'
    class Payload:
        def __reduce__(self):
            return eval, (f"__import__('pathlib').Path({str(marker)!r}).touch()",)
    path=tmp_path/'checkpoint.zip'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('archive/data.pkl',pickle.dumps(Payload(),protocol=2))
    data=path.read_bytes()
    result=inspect_checkpoint(path,expected_bytes=len(data),expected_sha256=hashlib.sha256(data).hexdigest())
    assert not marker.exists()
    assert any('eval' in name for name in result['globals'])
    assert result['load_approved'] is False
    with pytest.raises(ValueError,match='hash'):
        inspect_checkpoint(path,expected_bytes=len(data),expected_sha256='0'*64)
