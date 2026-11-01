import hashlib
from media_lab_core.media_inventory import inventory


def test_includes_unlisted_media_and_preserves_catalog_bytes(tmp_path):
    (tmp_path/'media').mkdir()
    (tmp_path/'media/unlisted.mp4').write_bytes(b'fixture')
    raw=b'[{"id":"old-id","url":"/media/unlisted.mp4"}]'
    (tmp_path/'gallery.json').write_bytes(raw)
    report=inventory(tmp_path)
    assert report['summary']['files']==2
    assert report['references'][0]['exists']
    assert report['files'][0]['sha256']==hashlib.sha256(raw).hexdigest()
    assert (tmp_path/'gallery.json').read_bytes()==raw
    assert report['migrationComplete'] is False


def test_reports_missing_references_and_does_not_follow_links(tmp_path):
    (tmp_path/'media').mkdir()
    (tmp_path/'media/link').symlink_to('/etc')
    (tmp_path/'characters.json').write_text('[{"sheet_url":"/media/missing.png","appearance":"private text"}]')
    report=inventory(tmp_path)
    assert report['summary']['missingReferences']==1
    assert report['summary']['issues']==1
    assert 'private text' not in str(report)


def test_credentials_and_models_are_outside_inventory_scope(tmp_path):
    (tmp_path/'credentials.json').write_text('secret')
    (tmp_path/'runner').mkdir()
    (tmp_path/'runner/model.safetensors').write_bytes(b'model')
    assert inventory(tmp_path)['files']==[]


def test_container_paths_are_unresolved_not_misreported_missing(tmp_path):
    (tmp_path/'jobs.json').write_text('{"jobs":{"id":{"path":"/workspace/maestro/uploads/file.png"}}}')
    report=inventory(tmp_path)
    assert report['summary']['missingReferences']==0
    assert report['summary']['externalReferences']==1
    assert report['references'][0]['path'].startswith('/workspace/')


def test_preservation_comparison_detects_source_drift_without_mutation(tmp_path):
    from media_lab_core.media_inventory import compare_preservation
    import pytest
    media=tmp_path/'media';media.mkdir()
    for name in ['unchanged','changed','removed']:(media/f'{name}.png').write_bytes(b'old')
    initial=inventory(tmp_path)
    receipt={'source':initial['source'],'stagedCopyComplete':True,
             'files':[{'originalPath':r['path'],'bytes':r['bytes'],'sha256':r['sha256']} for r in initial['files']]}
    assert compare_preservation(initial,receipt)['localSnapshotMatches']
    (media/'changed.png').write_bytes(b'new')  # Same size, different hash.
    (media/'removed.png').unlink()
    (media/'added.png').write_bytes(b'added')
    current=inventory(tmp_path)
    result=compare_preservation(current,receipt)
    assert not result['localSnapshotMatches']
    assert result['added']==['media/added.png']
    assert result['changed']==['media/changed.png']
    assert result['removedFromSource']==['media/removed.png']
    assert result['unchangedFiles']==1
    assert (media/'changed.png').read_bytes()==b'new'
    unstable={**initial,'issues':[{'reason':'changed_during_inventory'}]}
    assert not compare_preservation(unstable,receipt)['localSnapshotMatches']
    with pytest.raises(ValueError,match='different sources'):
        compare_preservation(current,{**receipt,'source':'/different'})


def test_cli_writes_private_comparison_report_outside_source(tmp_path,monkeypatch):
    import json,sys
    from media_lab_core.media_inventory import main
    source=tmp_path/'source';source.mkdir();(source/'media').mkdir()
    (source/'media/a.png').write_bytes(b'a')
    initial=inventory(source)
    receipt=tmp_path/'receipt.json'
    receipt.write_text(json.dumps({'source':initial['source'],'stagedCopyComplete':True,
        'files':[{'originalPath':r['path'],'bytes':r['bytes'],'sha256':r['sha256']} for r in initial['files']]}))
    output=tmp_path/'report.json'
    monkeypatch.setattr(sys,'argv',['media-inventory',str(source),'--output',str(output),'--compare-receipt',str(receipt)])
    main()
    assert json.loads(output.read_text())['snapshotComparison']['localSnapshotMatches']
    assert output.stat().st_mode & 0o777==0o600
    assert list((source/'media').iterdir())==[source/'media/a.png']


def test_unreadable_subdirectory_prevents_clean_source_comparison(tmp_path, monkeypatch):
    import errno
    from media_lab_core import media_inventory
    (tmp_path/'media').mkdir()
    original_walk = media_inventory.os.walk
    def partial_walk(path, **options):
        options['onerror'](PermissionError(errno.EACCES, 'denied', str(path/'private')))
        yield from original_walk(path, **options)
    monkeypatch.setattr(media_inventory.os, 'walk', partial_walk)
    report = inventory(tmp_path)
    assert report['issues'] == [{'path':'media/private','reason':'directory_unreadable','errorType':'PermissionError'}]
    receipt = {'source':str(tmp_path),'stagedCopyComplete':True,'files':[]}
    assert not media_inventory.compare_preservation(report,receipt)['localSnapshotMatches']
    from media_lab_core.media_migration import stage
    import pytest
    with pytest.raises(ValueError, match='inventory has unresolved'):
        stage(report,tmp_path.with_name(tmp_path.name+'-copy'))
