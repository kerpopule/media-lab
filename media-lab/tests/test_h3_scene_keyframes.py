"""CPU contracts only: optional H3 scene anchors, never GPU quality proof."""
import base64
import hashlib
import json
import os
import pytest
os.environ['MEDIA_LAB_DISABLE_BACKGROUND_WORKERS']='1'
import app
from runner import h3_singularity as sing

BASE=dict(prompt='synthetic scene fixture',frames=192,orientation='landscape',seed=17,prefix='fixture')
CASES={
 'default':({},'58e741feebf068018421084ed6bbc2d7975e24ad6f3d67727a1c8d5c9b33b996'),
 'anchored_audio':(dict(image_refs=['lead-a.png','lead-b.png','architecture.png','scene.png'],start_frame_ref_index=3,audio_refs=[{'path':'/fixture/guide.wav'}],preserve_audio=True),'ed8d09fc57c68ca34c83658d832dacbca0860279d61de9e641174d85dde4a35e'),
 'expanded_audio_video':(dict(image_refs=[f'portrait-{i}.png' for i in range(8)]+['scene.png'],start_frame_ref_index=8,audio_refs=[{'path':'/fixture/guide.wav'}],video_refs=[{'path':'/fixture/layout.mp4','skip':0,'cap':192}],preserve_audio=True),'321da1d70c884bf27e35d544cccf2114f4233f31480813237815a4c5ecd44130')}

def payload():return {'model':'h3','h3_engine':'singularity','source':'/media/scene.png','seed':17,'h3_anchor_first_frame':True,'h3_scene_keyframes':True}

@pytest.mark.parametrize('name',list(CASES))
def test_disabled_graph_is_byte_bound_to_prechange_d790b37(name):
    kw,expected=CASES[name]
    default=sing.build_graph(**BASE,**kw)
    explicit=sing.build_graph(**BASE,**kw,scene_keyframes=False)
    assert default==explicit
    assert hashlib.sha256(json.dumps(default,sort_keys=True,separators=(',',':')).encode()).hexdigest()==expected

@pytest.mark.parametrize('name',['anchored_audio','expanded_audio_video'])
def test_same_scene_mid_and_end_guides_chain_after_original_audio(name):
    kw,_=CASES[name];g=sing.build_graph(**BASE,**kw,scene_keyframes=True)
    source=[str(170+kw['start_frame_ref_index']),0]
    for first,mid,end,latent,guider in [('214','222','223',['56',1],'57'),('215','224','225',['105',0],'126')]:
        assert g[first]['inputs']['frame_idx']==0
        assert g[first]['inputs']['audio']==['190',0]
        for key,previous,frame in [(mid,first,96),(end,mid,-1)]:
            assert g[key]=={'class_type':'MiniMaxH3AddGuide','inputs':{'positive':[previous,0],'latent':latent,'vae':['65',0],'image':source,'frame_idx':frame}}
            assert 'audio' not in g[key]['inputs'] and 'audio_vae' not in g[key]['inputs']
        assert g[guider]['inputs']['conditioning']==[end,0]
    assert g['110']['inputs']['samples']==['106',0]
    assert g['108']['inputs']['latent_image']==['56',1]
    assert g['106']['inputs']['latent_image']==['105',0]
    assert g['220']['inputs']['audio']==['190',0]
    assert g['221']['inputs']['audio']==['219',0]

@pytest.mark.parametrize('value',[None,1,0,'true'])
def test_graph_requires_explicit_boolean(value):
    with pytest.raises(sing.SingularityRequestError,match='scene'):
        sing.build_graph(**BASE,scene_keyframes=value)

def test_graph_requires_an_explicit_opening_scene():
    with pytest.raises(sing.SingularityRequestError,match='scene'):
        sing.build_graph(**BASE,scene_keyframes=True)

@pytest.mark.parametrize('change',[{'model':'ltx25'},{'source':''},{'h3_anchor_first_frame':False},{'h3_scene_keyframes':'true'},{'h3_scene_keyframes':1},{'h3_scene_keyframes':None}])
def test_api_policy_refuses_unsupported_scene_anchoring(change):
    p=payload();p.update(change);assert app._h3_preserved_audio_error(p)

def test_api_retains_strict_opt_in_and_default_is_off():
    assert app.GenReq(prompt='fixture').model_dump()['h3_scene_keyframes'] is False
    assert app.GenReq(prompt='fixture',**payload()).model_dump()['h3_scene_keyframes'] is True
    assert app._h3_preserved_audio_error(payload()) is None

@pytest.mark.parametrize('value',['true',1,None])
def test_api_model_refuses_loose_boolean(value):
    with pytest.raises(ValueError):app.GenReq(prompt='fixture',h3_scene_keyframes=value)

@pytest.mark.parametrize('enabled',[False,True])
def test_actual_parser_maps_opt_in_without_changing_reference_order(tmp_path,enabled):
    p=sing.SingularityPipeline({'H3_SINGULARITY_COMFY_DIR':str(tmp_path)},tmp_path/'runtime',media_dir=tmp_path)
    p.comfy.dirs['in'].mkdir(parents=True)
    b64=base64.b64encode(b'synthetic image transport').decode()
    req={'frames':192,'prompt':'fixture','references':[{'b64':b64,'role':str(i)} for i in range(8)],'start_image_b64':b64,'anchor_start_frame':True,'scene_keyframes':enabled}
    spec=p.request_spec(req,'fixture')
    assert spec['scene_keyframes'] is enabled and spec['start_frame_ref_index']==8
    assert spec['image_refs']==[f'fixture-ref{i}.png' for i in range(8)]+['fixture-start.png']
    assert p.process is None

@pytest.mark.parametrize('value',[True,'true',1,None])
def test_parser_refuses_missing_opening_anchor_before_staging(tmp_path,monkeypatch,value):
    p=sing.SingularityPipeline({'H3_SINGULARITY_COMFY_DIR':str(tmp_path)},tmp_path/'runtime',media_dir=tmp_path)
    monkeypatch.setattr(p,'_stage_image',lambda *a:pytest.fail('invalid scene request reached staging'))
    with pytest.raises(sing.SingularityRequestError,match='[Ss]cene'):
        p.request_spec({'scene_keyframes':value},'fixture')

@pytest.mark.parametrize('enabled',[False,True])
def test_actual_worker_forwards_only_explicit_scene_flag(monkeypatch,tmp_path,enabled):
    source=tmp_path/'scene.png';source.write_bytes(b'synthetic transport image');captured=[]
    for name in ['save_state','touch_engine','stop_engine','maybe_release_pool']:monkeypatch.setattr(app,name,lambda *a:None)
    monkeypatch.setattr(app,'media_path',lambda *a:source)
    monkeypatch.setattr(app,'_h3_v2v_stage',lambda j:([],None))
    monkeypatch.setattr(app,'ensure_engine',lambda *a:'up')
    def render(engine,body,*a,**kw):captured.append(body);return {'ok':False,'error':'synthetic stop after body capture'}
    monkeypatch.setattr(app,'engine_generate',render)
    request={**payload(),'h3_scene_keyframes':enabled}
    job={'id':'fixture','engine':'h3','frames':192,'w':1216,'h':672,'full_prompt':'fixture','request':request}
    app.run_video(job)
    assert len(captured)==1 and captured[0].get('scene_keyframes',False) is enabled
    assert captured[0]['anchor_start_frame'] is True
    assert captured[0]['start_image_b64']==base64.b64encode(source.read_bytes()).decode()

def test_invalid_persisted_scene_request_never_reaches_staging_or_gpu(monkeypatch):
    monkeypatch.setattr(app,'save_state',lambda:None)
    monkeypatch.setattr(app,'_h3_v2v_stage',lambda *a:pytest.fail('bad scene request reached staging'))
    monkeypatch.setattr(app,'ensure_engine',lambda *a:pytest.fail('bad scene request reached GPU'))
    job={'id':'fixture','engine':'ltx25','request':{'model':'ltx25','h3_scene_keyframes':True}}
    app.run_video(job)
    assert job['status']=='error' and 'scene' in job['message'].lower()
