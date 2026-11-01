from email.message import Message
from pathlib import Path
from types import SimpleNamespace
import hashlib
import pytest
from media_lab_core.runtime_inventory import collect


def test_captures_exact_versions_license_text_and_missing_evidence(tmp_path):
    license=tmp_path/'LICENCE.txt';license.write_text('Example notice')
    metadata=Message();metadata['License-Expression']='MIT'
    packages={'one':SimpleNamespace(version='1',metadata=metadata,files=[Path('LICENCE.txt'),Path('code.py')],locate_file=lambda p:tmp_path/p),
              'two':SimpleNamespace(version='2',metadata=Message(),files=[],locate_file=lambda p:tmp_path/p)}
    result=collect({'one':'1','two':'2'},packages.__getitem__)
    assert result['missing_notice_files']==['two']
    evidence=result['packages'][0]['files'][0]
    assert evidence['text']=='Example notice'
    assert evidence['sha256']==hashlib.sha256(license.read_bytes()).hexdigest()
    with pytest.raises(ValueError,match='version'):
        collect({'one':'wrong'},packages.__getitem__)


def test_supplement_is_exact_version_hash_verified_and_attributed(tmp_path):
    import json
    from media_lab_core.runtime_inventory import supplemental_notices
    data=b'Pinned upstream license'
    (tmp_path/'LICENSE.txt').write_bytes(data)
    spec={'version':1,'notices':[{'package':'tokenizers','version':'0.22.2','file':'LICENSE.txt',
          'sha256':hashlib.sha256(data).hexdigest(),'source':'https://example.invalid/commit/LICENSE',
          'revision':'exact-commit','license':'Apache-2.0'}]}
    (tmp_path/'manifest.json').write_text(json.dumps(spec))
    assert supplemental_notices({'tokenizers':'different'},tmp_path)=={}
    found=supplemental_notices({'tokenizers':'0.22.2'},tmp_path)['tokenizers'][0]
    assert found['source']==spec['notices'][0]['source'] and found['text']==data.decode()
    (tmp_path/'LICENSE.txt').write_bytes(b'changed')
    with pytest.raises(ValueError,match='hash changed'):
        supplemental_notices({'tokenizers':'0.22.2'},tmp_path)
