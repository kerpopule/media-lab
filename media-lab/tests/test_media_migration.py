import json
import pytest
from media_lab_core.media_inventory import inventory
from media_lab_core.media_migration import stage


def test_organized_copy_preserves_bytes_and_stable_original_mapping(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'media').mkdir()
    (source/'media/old.mp4').write_bytes(b'video')
    (source/'media/unlisted.png').write_bytes(b'image')
    raw=b'[{"id":"stable-id","title":"My video","url":"/media/old.mp4","kind":"musicvideo"}]'
    (source/'gallery.json').write_bytes(raw)
    report=inventory(source);target=tmp_path/'new'
    result=stage(report,target)
    assert result['stagedCopyComplete'] and not result['migrationComplete']
    mapping={x['originalPath']:x['path'] for x in result['files']}
    assert mapping['media/old.mp4'].startswith('Videos/Music Videos/My video')
    assert (target/mapping['gallery.json']).read_bytes()==raw
    assert (source/'media/old.mp4').read_bytes()==b'video'
    assert len(result['files'])==3
    with pytest.raises(FileExistsError):stage(report,target)


def test_changed_asset_stops_copy_and_receipt_never_claims_success(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'media').mkdir()
    p=source/'media/file.png';p.write_bytes(b'old')
    report=inventory(source);p.write_bytes(b'changed')
    with pytest.raises(ValueError,match='match inventory'):stage(report,tmp_path/'new')
    receipt=json.loads((tmp_path/'new/migration-receipt.json').read_text())
    assert not receipt['stagedCopyComplete']
    assert p.read_bytes()==b'changed'


def test_refuses_source_destination_and_links(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'media').mkdir()
    p=source/'media/file.png';p.write_bytes(b'original')
    report=inventory(source)
    with pytest.raises(ValueError,match='separate'):stage(report,source/'nested')
    p.unlink();p.symlink_to('/etc/hosts')
    with pytest.raises(ValueError,match='symlink'):stage(report,tmp_path/'new')


def test_recheck_detects_damage_missing_and_links_without_repairing(tmp_path):
    from media_lab_core.media_migration import verify_copy
    source=tmp_path/'source';source.mkdir();(source/'media').mkdir()
    for name in ('good','damaged','missing','linked'):
        (source/'media'/f'{name}.png').write_bytes(b'original')
    target=tmp_path/'copy';receipt=stage(inventory(source),target)
    assert verify_copy(receipt,target)['copyMatchesReceipt']
    paths={r['originalPath']:target/r['path'] for r in receipt['files']}
    paths['media/damaged.png'].write_bytes(b'changed!')
    paths['media/missing.png'].unlink()
    paths['media/linked.png'].unlink()
    paths['media/linked.png'].symlink_to(source/'media/linked.png')
    result=verify_copy(receipt,target)
    assert result['verifiedFiles']==1 and not result['copyMatchesReceipt']
    assert {r['reason'] for r in result['issues']}=={'content_mismatch','FileNotFoundError','symlink_not_followed'}
    assert not result['migrationComplete']
    assert paths['media/damaged.png'].read_bytes()==b'changed!'
    assert not paths['media/missing.png'].exists()
    assert all(p.read_bytes()==b'original' for p in (source/'media').iterdir())


def test_recheck_external_receipt_and_rejects_invalid_inventory(tmp_path):
    from media_lab_core.media_migration import verify_copy
    import hashlib
    (tmp_path/'copy.png').write_bytes(b'image')
    row={'path':'copy.png','bytes':5,'sha256':hashlib.sha256(b'image').hexdigest()}
    receipt={'complete':True,'files':[row]}
    assert verify_copy(receipt,tmp_path)['verifiedFiles']==1
    with pytest.raises(ValueError,match='duplicate'):
        verify_copy({**receipt,'files':[row,row]},tmp_path)
    with pytest.raises(ValueError,match='unsafe'):
        verify_copy({**receipt,'files':[{**row,'path':'../outside'}]},tmp_path)
    with pytest.raises(ValueError,match='incomplete'):
        verify_copy({**receipt,'complete':False},tmp_path)


def test_recheck_cli_failure_exit_and_no_new_receipt(tmp_path,monkeypatch,capsys):
    from media_lab_core.media_migration import main
    import sys
    receipt=tmp_path/'receipt.json'
    receipt.write_text(json.dumps({'complete':True,'files':[{'path':'missing','bytes':0,'sha256':'unused'}]}))
    monkeypatch.setattr(sys,'argv',['migration',str(receipt),str(tmp_path),'--verify-copy'])
    with pytest.raises(SystemExit) as exc:main()
    assert exc.value.code==1
    assert not json.loads(capsys.readouterr().out)['copyMatchesReceipt']
    assert list(tmp_path.iterdir())==[receipt]
