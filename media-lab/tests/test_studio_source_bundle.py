import json
from pathlib import Path
import subprocess
import sys
import zipfile

from media_lab_core.studio_source_bundle import package_source


def test_unpacked_controller_starts_without_legacy_or_model_imports(tmp_path):
    source=Path(__file__).parents[1]
    archive=tmp_path/'controller.zip'
    result=package_source(source,archive)
    unpacked=tmp_path/'unpacked'
    with zipfile.ZipFile(archive) as bundle:
        names=bundle.namelist()
        assert 'app.py' not in names
        assert not any(name.startswith(('runner/','static/','config/')) for name in names)
        assert 'LICENSE' in names
        bundle.extractall(unpacked)
    # Fresh process with only the packaged source on its import path. No source
    # checkout or inherited PYTHONPATH may supply a missing internal module.
    program='''
import json,sys
sys.path.insert(0,sys.argv[1])
from pathlib import Path
from media_lab_core.studio_cli import initialize,inspect_host,application,parser,read_credentials
from fastapi.testclient import TestClient
root=Path(sys.argv[2]);initialize(root)
assert inspect_host(root)['configuration_valid']
(root/'collections.json').write_text(json.dumps({'characters':[{'id':'preserved-character'}]}))
from media_lab_core import media_inventory,media_migration,migrated_catalog,migrated_edits,preserved_edit_import,studio_collections
app=application(parser().parse_args(['serve',str(root)]))
with TestClient(app) as client:
    assert client.post('/api/chat').status_code==404
    token=client.post('/api/gate',json={'code':read_credentials(root).code,'studio_render':True,'studio_device':'a'*32}).json()['token']
    assert client.get('/api/studio/engines',headers={'Authorization':'Bearer '+token}).json()['engines']==[]
    library=client.post('/api/gate',json={'code':read_credentials(root).code,'studio_library':True}).json()['token']
    headers={'Authorization':'Bearer '+library}
    assert client.get('/api/studio/collections/characters',headers=headers).json()['records'][0]['id']=='preserved-character'
    edit=client.post('/api/gate',json={'code':read_credentials(root).code,'studio_edit':True,'studio_device':'a'*32}).json()['editToken']
    headers={'Authorization':'Bearer '+edit,'X-Library-Authorization':'Bearer '+library}
    assert client.post('/api/studio/editing/preserved/cut-1234567890/import',headers=headers).status_code==404
    assert client.post('/api/studio/editing/storyboards',headers=headers,json={'requestId':'unpacked-storyboard-test','storyboardId':'missing','sourceSha256':'0'*64,'title':'Copy','fps':24,'scenes':[{'assetId':'image','seconds':1.0}]}).status_code==404
# Administrator model setup must be importable and servable from the bundle alone.
from media_lab_core import studio_admin, background_setup, background_install, background_remove, runtime_inventory
from media_lab_core.studio_server import setup_web_root
assert setup_web_root().name=='setup-web'
studio_admin.ITERATIONS=1000
code=studio_admin.enroll(root)
managed=application(parser().parse_args(['serve',str(root),'--model-setup']))
with TestClient(managed) as client:
    assert client.get('/manifest.json').json()['vibexStudio']['modelSetup'] is True
    assert client.get('/setup/background').status_code==200
    assert client.get('/static/background-setup.js').status_code==200
    assert client.get('/api/setup/background/plan').status_code==403
    assert client.post('/api/setup/session',json={'code':code},headers={'X-Setup-Request':'1'}).status_code==200
    plan=client.get('/api/setup/background/plan').json()
    assert plan['artifactRoot']==str(root.resolve()/'artifacts')
    assert 'runtime-notices' in str(Path(background_setup.MANIFEST).parent/'runtime-notices') and (Path(background_setup.MANIFEST).parent/'runtime-notices/manifest.json').is_file()
assert not {'app','torch','transformers','runner'} & set(sys.modules)
print(json.dumps({'legacy_imported':False,'controller_started':True}))
'''
    run=subprocess.run([sys.executable,'-I','-c',program,str(unpacked),str(tmp_path/'host')],
                       cwd=tmp_path,capture_output=True,text=True,timeout=30)
    assert run.returncode==0,run.stderr
    assert json.loads(run.stdout)['controller_started']
    # 96 = 94 + the two pinned Qwen-Image-2.1 image-runtime requirement files, which joined the
    # bundle when Qwen-Image-2.1 became the studio's only image model (2026-09-20).
    # 98 = 96 + local_config and engine_licences: the image pack's research licence
    # keeps it off until the host opts in (MEDIA_LAB_PERSONAL_ENGINES, 2026-09-25).
    # 103 = 98 + five shared HDR/SDR finishing modules.
    assert result['files']==103
