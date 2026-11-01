import pytest
from PIL import Image
from media_lab_core import cut
from media_lab_core.media_inventory import inventory
from media_lab_core.media_migration import stage
from media_lab_core.migrated_edits import migrate_edits


def test_edit_snapshot_and_history_keep_ids_and_nested_paths(tmp_path):
    old=tmp_path/'old';(old/'media').mkdir(parents=True)
    image=old/'media/frame.png';Image.new('RGB',(64,48),'blue').save(image)
    asset={**cut.probe_gallery_file(image),'id':'original-asset'}
    manifest=cut.build_gallery_project('cut-original','Original edit',[asset])
    store=cut.create_project(old,manifest)
    clip=manifest['timeline']['tracks'][0]['clips'][0]['id']
    store.transact([{'id':'trim','type':'clip.trim','payload':{'clip_id':clip,'trim_in_frames':0,'trim_out_frames':48}}],actor='human',transaction_id='original-transaction',expected_revision=0)
    stage(inventory(old),tmp_path/'preserved')
    result=migrate_edits(tmp_path/'preserved',tmp_path/'restored')
    assert result[0]['projectId']=='cut-original' and result[0]['revision']==1
    restored=cut.open_project(tmp_path/'restored','cut-original').load()
    assert restored['assets'][0]['source']['path'].startswith('/media/Images/')
    assert restored['timeline']['tracks'][0]['clips'][0]['id']==clip
    journal=(tmp_path/'restored/cut/projects/cut-original/journal.jsonl').read_text()
    assert 'original-transaction' in journal
    assert '/media/frame.png' not in journal
    assert cut.open_project(old,'cut-original').load()['assets'][0]['source']['path']=='/media/frame.png'
    with pytest.raises(FileExistsError):migrate_edits(tmp_path/'preserved',tmp_path/'restored')
