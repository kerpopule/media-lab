import hashlib
from pathlib import Path
import pytest
from media_lab_core import triposr_runtime as runtime


def setup(tmp_path, monkeypatch):
    wheels=tmp_path/'wheel cache with spaces';wheels.mkdir()
    file=wheels/'sample.whl';file.write_bytes(b'verified')
    monkeypatch.setattr(runtime,'WHEELS',{'sample.whl':hashlib.sha256(b'verified').hexdigest()})
    template=tmp_path/'template';template.write_text('sample @ ${VIBEX_TRIPOSR_WHEEL_URL}/sample.whl\n')
    monkeypatch.setattr(runtime,'TEMPLATE',template)
    monkeypatch.setattr(runtime,'TEMPLATE_SHA',hashlib.sha256(template.read_bytes()).hexdigest())
    return wheels,file,tmp_path/'result.lock'


def test_encodes_portable_path_and_preserves_existing_output(tmp_path,monkeypatch):
    wheels,_,output=setup(tmp_path,monkeypatch)
    receipt=runtime.prepare_lock(wheels,output)
    assert '%20' in output.read_text() and '${' not in output.read_text()
    assert receipt['install_qualified'] is False
    original=output.read_bytes()
    with pytest.raises(FileExistsError):runtime.prepare_lock(wheels,output)
    assert output.read_bytes()==original


def test_corrupt_or_symlink_wheel_never_produces_lock(tmp_path,monkeypatch):
    wheels,file,output=setup(tmp_path,monkeypatch)
    file.write_bytes(b'changed')
    with pytest.raises(ValueError,match='hash'):runtime.prepare_lock(wheels,output)
    assert not output.exists()
    file.unlink();file.symlink_to(tmp_path/'template')
    with pytest.raises(ValueError,match='unavailable'):runtime.prepare_lock(wheels,output)
    assert not output.exists()


def test_lock_drift_never_publishes(tmp_path, monkeypatch):
    wheels, _, output = setup(tmp_path, monkeypatch)
    runtime.TEMPLATE.write_text(runtime.TEMPLATE.read_text() + 'unreviewed-package==1.0\n')
    with pytest.raises(ValueError, match='manifest'):
        runtime.prepare_lock(wheels, output)
    assert not output.exists()


def test_recipe_integrity_and_repository_contract(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    assert runtime.verify_recipes(root)['install_qualified'] is False
    assert hashlib.sha256(runtime.TEMPLATE.read_bytes()).hexdigest() == runtime.TEMPLATE_SHA
    for name, digest in runtime.WHEELS.items():
        assert name in runtime.TEMPLATE.read_text()
        assert digest in runtime.TEMPLATE.read_text()
    recipe = tmp_path / 'build.py'
    recipe.write_bytes(b'reviewed')
    monkeypatch.setattr(runtime, 'BUILD', {'recipe_files': {
        'build.py': {'bytes': 8, 'sha256': hashlib.sha256(b'reviewed').hexdigest()}}})
    runtime.verify_recipes(tmp_path)
    recipe.write_bytes(b'modified')
    with pytest.raises(ValueError, match='hash'):
        runtime.verify_recipes(tmp_path)
    recipe.unlink()
    recipe.symlink_to(root / 'tools/build-triposr-antlr.py')
    with pytest.raises(ValueError, match='unavailable'):
        runtime.verify_recipes(tmp_path)


def test_reduced_profile_is_explicit_and_preserves_original_recipe():
    import json
    root=Path(__file__).resolve().parents[1]
    original=runtime.TEMPLATE.read_bytes()
    receipt=runtime.verify_recipes(root,profile='without-vision-v1')
    assert receipt['install_qualified'] is False
    build,template,wheels,digest=runtime._profile('without-vision-v1')
    assert build['runtime_profile']=='without-vision-v1'
    assert hashlib.sha256(template.read_bytes()).hexdigest()==digest
    assert '\ntorchvision @ ' not in template.read_text()
    assert wheels==runtime.WHEELS
    old=json.loads((root/'media_lab_core/data/triposr-runtime.json').read_text())
    new=json.loads((root/'media_lab_core/data/triposr-runtime-without-vision.json').read_text())
    assert new['runtime_versions']=={k:v for k,v in old['runtime_versions'].items() if k!='torchvision'}
    assert new['profile']=='without-vision-v1'
    assert runtime.TEMPLATE.read_bytes()==original
    assert runtime.verify_recipes(root)['install_qualified'] is False


def test_unknown_profile_never_writes_output(tmp_path):
    output=tmp_path/'result.lock'
    with pytest.raises(ValueError,match='explicit'):
        runtime.prepare_lock(tmp_path,output,profile='auto')
    assert not output.exists()


def test_rebuilt_profile_requires_exact_five_wheels_and_preserves_reduced():
    root=Path(__file__).resolve().parents[1]
    reduced=runtime._profile('without-vision-v1')
    runtime.verify_recipes(root,profile='rebuilt-rust-v1')
    build,template,wheels,digest=runtime._profile('rebuilt-rust-v1')
    assert len(wheels)==5
    assert set(reduced[2]).issubset(wheels)
    assert template.read_text().count('${VIBEX_TRIPOSR_WHEEL_URL}')==5
    assert hashlib.sha256(template.read_bytes()).hexdigest()==digest
    for name,sha in wheels.items():
        assert name in template.read_text() and sha in template.read_text()
    assert build['status']=='candidate-not-installable'
    assert runtime._profile('without-vision-v1')[2]==reduced[2]
