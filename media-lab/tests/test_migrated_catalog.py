import json
import pytest
from media_lab_core.media_inventory import inventory
from media_lab_core.media_migration import stage
from media_lab_core.migrated_catalog import prepare_catalog
from media_lab_core.studio_library import catalog


def test_preserves_identity_and_fields_and_resolves_organized_library(tmp_path):
    source=tmp_path/'old';source.mkdir();(source/'media').mkdir()
    (source/'media/a.png').write_bytes(b'image')
    rows=[{'id':'original-id','title':'My Image','url':'/media/a.png','prompt':'original prompt','meta':{'character_id':'char-1'}}]
    (source/'gallery.json').write_text(json.dumps(rows))
    stage(inventory(source),tmp_path/'new')
    migrated=prepare_catalog(tmp_path/'new')
    assert migrated[0]['id']=='original-id'
    assert migrated[0]['meta']==rows[0]['meta']
    assert migrated[0]['folder']=='Images/Generated'
    assert len(catalog(migrated,tmp_path/'new'))==1
    copied=catalog(migrated,tmp_path/'new')[0][1]
    copied.write_bytes(b'changed')
    with pytest.raises(ValueError,match='changed'):prepare_catalog(tmp_path/'new')


def test_character_voice_and_storyboard_ids_survive_with_missing_links_reported(tmp_path):
    from media_lab_core.migrated_catalog import prepare_collections
    source=tmp_path/'old';source.mkdir();(source/'media').mkdir()
    (source/'media/face.png').write_bytes(b'face')
    (source/'characters.json').write_text(json.dumps([{'id':'char','name':'Person','voice_id':'voice','sheet_url':'/media/face.png'}]))
    (source/'voices.json').write_text(json.dumps([{'id':'voice','character_id':'char'},{'id':'orphan','character_id':'missing'}]))
    (source/'storyboards.json').write_text(json.dumps([{'id':'board','cast':['char'],'beats':[{'cast':['char'],'still_url':'/media/face.png'}]}]))
    stage(inventory(source),tmp_path/'new')
    result=prepare_collections(tmp_path/'new')
    assert result['characters'][0]['id']=='char'
    assert result['characters'][0]['voice_id']=='voice'
    assert result['characters'][0]['sheet_url'].startswith('/media/Images/')
    assert result['storyboards'][0]['beats'][0]['cast']==['char']
    assert result['rewrittenFileReferences']==2
    assert len(result['relationshipIssues'])==1
    assert result['relationshipIssues'][0]['targetId']=='missing'


def test_song_missing_from_gallery_is_linked_from_preserved_audio(tmp_path):
    from media_lab_core.migrated_catalog import prepare_collections
    source=tmp_path/'old';source.mkdir();(source/'media').mkdir()
    (source/'media/song.mp3').write_bytes(b'music')
    (source/'storyboards.json').write_text('[{"id":"board","cast":[],"beats":[],"song_id":"song"}]')
    stage(inventory(source),tmp_path/'new')
    result=prepare_collections(tmp_path/'new')
    assert result['storyboards'][0]['song_id']=='song'
    assert result['storyboards'][0]['song_url'].startswith('/media/Audio/Music/')
    assert result['relationshipIssues']==[]


def test_unlisted_files_are_discoverable_without_changing_existing_ids(tmp_path):
    source=tmp_path/'old';source.mkdir();(source/'media').mkdir()
    (source/'media/listed.png').write_bytes(b'listed')
    (source/'media/unlisted.mp3').write_bytes(b'unlisted')
    (source/'gallery.json').write_text('[{"id":"original","url":"/media/listed.png"}]')
    stage(inventory(source),tmp_path/'new')
    first=prepare_catalog(tmp_path/'new');second=prepare_catalog(tmp_path/'new')
    assert first==second
    assert len(first)==2 and first[0]['id']=='original'
    assert first[1]['id'].startswith('preserved-')
    assert first[1]['folder']=='Audio/Music'


def test_production_history_and_external_media_are_offered_and_verified(tmp_path):
    import hashlib
    source=tmp_path/'old';source.mkdir()
    (source/'gallery.json').write_text('[]')
    for name in ['jobs/job1/output.mp4','productions/show1/music.wav','uploads/reference.png']:
        path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(name.encode())
    target=tmp_path/'new'
    stage(inventory(source),target)
    external=target/'Projects/External References/ref.jpg'
    external.parent.mkdir(parents=True);external.write_bytes(b'external-image')
    receipt={'complete':True,'files':[{'originalPath':'/workspace/maestro/uploads/ref.jpg',
        'path':external.relative_to(target).as_posix(),'bytes':external.stat().st_size,
        'sha256':hashlib.sha256(external.read_bytes()).hexdigest()}]}
    (target/'external-reference-receipt.json').write_text(json.dumps(receipt))
    rows=prepare_catalog(target)
    assert len(rows)==4
    assert rows==prepare_catalog(target)
    assert {r['folder'] for r in rows}=={'Projects/Job History/job1','Projects/Productions/show1','Images/Uploads','Projects/External References'}
    assert len(catalog(rows,target))==4
    assert next(r for r in rows if r['folder']=='Projects/External References')['originalUrl']=='/workspace/maestro/uploads/ref.jpg'
    external.write_bytes(b'changed')
    with pytest.raises(ValueError,match='changed'):prepare_catalog(target)
    receipt['complete']=False
    (target/'external-reference-receipt.json').write_text(json.dumps(receipt))
    with pytest.raises(ValueError,match='incomplete'):prepare_catalog(target)


def test_archived_characters_resolve_old_cast_without_becoming_active(tmp_path):
    from media_lab_core.migrated_catalog import prepare_collections
    source=tmp_path/'old';source.mkdir()
    archived=source/'archive/characters';archived.mkdir(parents=True)
    (source/'characters.json').write_text('[{"id":"active","name":"Active"}]')
    (archived/'archived.json').write_text('[{"id":"retired","name":"Retired","sheet_url":"/media/char_retired.png","archived_ts":123}]')
    (archived/'char_retired.png').write_bytes(b'original archived sheet')
    (source/'voices.json').write_text('[{"id":"voice","character_id":"retired"}]')
    (source/'storyboards.json').write_text('[{"id":"board","cast":["retired"],"beats":[{"cast":["retired"]}]}]')
    target=tmp_path/'new';stage(inventory(source),target)
    result=prepare_collections(target)
    assert result['relationshipIssues']==[]
    assert len(result['characters'])==2
    active,retired=result['characters']
    assert not active.get('archived')
    assert retired['archived'] is True and retired['archived_ts']==123
    from urllib.parse import unquote
    assert (target/unquote(retired['sheet_url'].removeprefix('/media/'))).read_bytes()==b'original archived sheet'
    assert result['storyboards'][0]['cast']==['retired']
    receipt=json.loads((target/'migration-receipt.json').read_text())
    metadata=next(row for row in receipt['files'] if row['originalPath']=='archive/characters/archived.json')
    (target/metadata['path']).write_text('[]')
    with pytest.raises(ValueError,match='changed'):prepare_collections(target)


def test_active_and_archived_identity_conflict_requires_review(tmp_path):
    from media_lab_core.migrated_catalog import prepare_collections
    source=tmp_path/'old';source.mkdir();(source/'archive/characters').mkdir(parents=True)
    (source/'characters.json').write_text('[{"id":"same"}]')
    (source/'archive/characters/archived.json').write_text('[{"id":"same"}]')
    target=tmp_path/'new';stage(inventory(source),target)
    with pytest.raises(ValueError,match='active and archived'):prepare_collections(target)


def test_missing_archived_reference_images_are_reported_without_changing_links(tmp_path):
    from media_lab_core.migrated_catalog import prepare_collections
    source=tmp_path/'old';source.mkdir();(source/'archive/characters').mkdir(parents=True)
    (source/'archive/characters/archived.json').write_text('[{"id":"old","refs":["/media/missing.png"]}]')
    target=tmp_path/'new';stage(inventory(source),target)
    result=prepare_collections(target)
    assert result['characters'][0]['refs']==['/media/missing.png']
    assert result['relationshipIssues']==[{'recordId':'old','field':'refs/0','targetId':'/media/missing.png','reason':'archived_reference_file_missing'}]
