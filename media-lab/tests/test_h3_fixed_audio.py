"""Opt-in H3 audio locking, never a different model or an implicit default."""
import base64
import os
import wave
from pathlib import Path
import pytest
os.environ['MEDIA_LAB_DISABLE_BACKGROUND_WORKERS']='1'
import app
from runner import h3_singularity as sing
from tools import scoped_release


def graph(**kwargs):
    return sing.build_graph(prompt='fixture',frames=192,orientation='landscape',seed=1,prefix='fixture',**kwargs)


def test_fixed_audio_uses_existing_h3_vae_and_zero_audio_mask_in_both_passes():
    g=graph(audio_refs=[{'path':'/private/approved.wav'}],preserve_audio=True)
    assert g['210']=={'class_type':'VAEEncodeAudio','inputs':{'audio':['190',0],'vae':['66',0]}}
    assert g['211']['class_type']=='SolidMask' and g['211']['inputs']['value']==0.0
    assert g['212']=={'class_type':'SetLatentNoiseMask','inputs':{'samples':['210',0],'mask':['211',0]}}
    assert g['213']=={'class_type':'LTXVConcatAVLatent','inputs':{'video_latent':['56',1],'audio_latent':['212',0]}}
    assert g['108']['inputs']['latent_image']==['213',0]
    assert g['105']['inputs']['audio_latent']==['102',1]
    assert g['102']['inputs']['av_latent']==['108',0]
    assert g['106']['inputs']['latent_image']==['105',0]
    assert g['69']['inputs']['unet_name']==sing.DEFAULT_MODELS['dit']


def test_default_h3_graph_is_exactly_unchanged():
    assert graph(audio_refs=[{'path':'/private/approved.wav'}])==graph(audio_refs=[{'path':'/private/approved.wav'}],preserve_audio=False)
    assert '210' not in graph()


def test_opt_in_start_anchor_uses_actual_installed_h3_guide_in_both_passes():
    g=graph(image_refs=['heather.png','steve.png','church.png','fresh.png'],start_frame_ref_index=3)
    assert g['214']=={'class_type':'MiniMaxH3AddGuide','inputs':{
        'positive':['56',0],'latent':['56',1],'vae':['65',0],
        'image':['173',0],'frame_idx':0}}
    assert g['57']['inputs']['conditioning']==['214',0]
    assert g['215']=={'class_type':'MiniMaxH3AddGuide','inputs':{
        'positive':['56',0],'latent':['105',0],'vae':['65',0],
        'image':['173',0],'frame_idx':0}}
    assert g['126']['inputs']['conditioning']==['215',0]
    assert g['108']['inputs']['latent_image']==['56',1]
    assert '214' not in graph(image_refs=['fresh.png'])


@pytest.mark.parametrize('index',[-1,4,True,'3'])
def test_start_anchor_index_is_explicit_and_in_range(index):
    with pytest.raises(sing.SingularityRequestError):graph(image_refs=['a','b','c','d'],start_frame_ref_index=index)


@pytest.mark.parametrize('refs',[[],[{'path':'a'},{'path':'b'}]])
def test_fixed_audio_requires_one_unambiguous_master(refs):
    with pytest.raises(sing.SingularityRequestError):graph(audio_refs=refs,preserve_audio=True)


@pytest.mark.parametrize('value',['true',1,None])
def test_renderer_refuses_truthy_nonboolean_audio_lock(value):
    with pytest.raises(sing.SingularityRequestError):graph(audio_refs=[{'path':'a'}],preserve_audio=value)


def test_request_model_keeps_explicit_audio_lock():
    r=app.GenReq(prompt='fixture',model='h3-real',h3_preserve_audio=True,audio_references=[{'source':'/media/approved.wav'}])
    assert r.model_dump()['h3_preserve_audio'] is True


@pytest.mark.parametrize('enabled',[False,True])
def test_parser_maps_only_supplied_start_to_guide_and_preserves_ref_order(tmp_path,enabled):
    pipe=sing.SingularityPipeline({'H3_SINGULARITY_COMFY_DIR':str(tmp_path),'H3_SINGULARITY_MODELS_ROOT':str(tmp_path)},tmp_path/'runtime',media_dir=tmp_path)
    pipe.comfy.dirs['in'].mkdir(parents=True)
    # Parser transport fixture; no inference or identity assertion.
    b64=base64.b64encode(b'fixture-bytes').decode()
    req={'frames':192,'prompt':'fixture','references':[{'b64':b64,'role':x} for x in ['Heather','Steve','church']],
         'start_image_b64':b64,'anchor_start_frame':enabled}
    spec=pipe.request_spec(req,'fixture')
    assert spec['image_refs']==['fixture-ref0.png','fixture-ref1.png','fixture-ref2.png','fixture-start.png']
    assert spec['start_frame_ref_index']==(3 if enabled else None)


@pytest.mark.parametrize('value',[True,'true',1])
def test_anchor_parser_refuses_missing_start_or_nonboolean(tmp_path,value):
    pipe=sing.SingularityPipeline({'H3_SINGULARITY_COMFY_DIR':str(tmp_path),'H3_SINGULARITY_MODELS_ROOT':str(tmp_path)},tmp_path/'runtime',media_dir=tmp_path)
    with pytest.raises(sing.SingularityRequestError,match='starting image'):
        pipe.request_spec({'anchor_start_frame':value},'fixture')


@pytest.mark.parametrize('payload',[{'model':'ltx25','source':'/media/a.png'}, {'model':'h3','h3_engine':'singularity'}])
def test_anchor_api_policy_refuses_wrong_engine_or_missing_image(payload):
    assert app._h3_preserved_audio_error({**payload,'h3_anchor_first_frame':True})


@pytest.mark.parametrize('changes',[{'model':'ltx25'},{'audio_references':[]},{'audio_references':[{'source':'/media/a.wav'},{'source':'/media/b.wav'}]},{'h3_preserve_audio':'true'}])
def test_audio_lock_policy_fails_closed(changes):
    r={'model':'h3','h3_engine':'singularity','h3_preserve_audio':True,'audio_references':[{'source':'/media/a.wav'}]}
    r.update(changes)
    assert app._h3_preserved_audio_error(r)


@pytest.mark.parametrize('enabled',[False,True])
@pytest.mark.parametrize('anchor',[False,True])
def test_actual_video_worker_forwards_only_explicit_audio_lock(monkeypatch,tmp_path,enabled,anchor):
    captured=[]
    source=tmp_path/'fresh.png';source.write_bytes(b'fixture-image-transport')
    monkeypatch.setattr(app,'media_path',lambda *a:source)
    monkeypatch.setattr(app,'save_state',lambda:None)
    monkeypatch.setattr(app,'_h3_v2v_stage',lambda j:([],None))
    monkeypatch.setattr(app,'_stage_audio_references',lambda *a:[{'file':'approved.wav','role':'original'}])
    monkeypatch.setattr(app,'ensure_engine',lambda *a:'up')
    monkeypatch.setattr(app,'touch_engine',lambda *a:None)
    monkeypatch.setattr(app,'stop_engine',lambda *a:None)
    def render(engine,body,*args,**kwargs):
        captured.append(body)
        return {'ok':False,'error':'synthetic stop after actual worker body capture'}
    monkeypatch.setattr(app,'engine_generate',render)
    j={'id':'fixture','engine':'h3','frames':192,'w':1216,'h':672,'full_prompt':'fixture',
       'request':{'model':'h3','h3_engine':'singularity','seed':1,'h3_preserve_audio':enabled,
                  'source':'/media/fresh.png' if anchor else '', 'h3_anchor_first_frame':anchor,
                  'audio_references':[{'source':'/media/approved.wav'}]}}
    app.run_video(j)
    assert len(captured)==1
    assert captured[0].get('preserve_audio',False) is enabled
    assert captured[0].get('anchor_start_frame',False) is anchor
    if anchor:
        assert captured[0]['start_image_b64']==base64.b64encode(source.read_bytes()).decode()


def test_short_pinned_audio_fails_before_allocation(tmp_path,monkeypatch):
    source=tmp_path/'short.wav'
    with wave.open(str(source),'wb') as f:
        f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(b'\0\0'*1600)
    monkeypatch.setattr(app,'MEDIA',tmp_path)
    monkeypatch.setattr(app,'POOL_DIR',tmp_path/'pool')
    monkeypatch.setattr(app,'save_state',lambda:None)
    j={'id':'fixture-short','frames':192,'request':{'h3_preserve_audio':True}}
    assert app._stage_audio_references(j,[{'source':'/media/short.wav'}]) is None
    assert j['status']=='error' and 'entire' in j['message'].lower()


def test_scoped_release_admits_only_the_named_h3_runner():
    assert scoped_release.safe_name('runner/h3_singularity.py')=='runner/h3_singularity.py'
    with pytest.raises(ValueError):scoped_release.safe_name('runner/unknown_gpu.py')


@pytest.mark.parametrize('duration,valid',[(.1,False),(8,True)])
def test_real_renderer_request_spec_checks_the_full_audio_timeline(tmp_path,duration,valid):
    source=tmp_path/'approved.wav'
    with wave.open(str(source),'wb') as f:
        f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(b'\0\0'*round(16000*duration))
    pipe=sing.SingularityPipeline({'H3_SINGULARITY_COMFY_DIR':str(tmp_path),'H3_SINGULARITY_MODELS_ROOT':str(tmp_path)},tmp_path/'runtime',media_dir=tmp_path)
    request={'prompt':'fixture','frames':192,'preserve_audio':True,'audio_references':[{'file':source.name}]}
    if not valid:
        with pytest.raises(sing.SingularityRequestError,match='entire'):pipe.request_spec(request,'fixture')
    else:
        spec=pipe.request_spec(request,'fixture')
        assert spec['preserve_audio'] is True and sing.build_graph(**spec)['108']['inputs']['latent_image']==['213',0]
    assert pipe.process is None


def test_invalid_persisted_audio_lock_never_reaches_staging_or_gpu(monkeypatch):
    monkeypatch.setattr(app,'save_state',lambda:None)
    monkeypatch.setattr(app,'_h3_v2v_stage',lambda *a:pytest.fail('invalid audio lock reached staging'))
    monkeypatch.setattr(app,'ensure_engine',lambda *a:pytest.fail('invalid audio lock reached GPU'))
    j={'id':'fixture','engine':'ltx25','request':{'model':'ltx25','h3_preserve_audio':True,'audio_references':[{'source':'/media/a.wav'}]}}
    app.run_video(j)
    assert j['status']=='error' and 'H3 Real' in j['message']


def test_fixed_audio_staging_retains_fractional_source_offset_exactly(tmp_path,monkeypatch):
    import math,subprocess
    from array import array
    source=tmp_path/'chirp.wav'
    samples=array('h',(round(12000*math.sin(2*math.pi*(220*i/48000+25*(i/48000)**2))) for i in range(9*48000)))
    with wave.open(str(source),'wb') as f:
        f.setparams((1,2,48000,0,'NONE','not compressed'));f.writeframes(samples.tobytes())
    monkeypatch.setattr(app,'MEDIA',tmp_path);monkeypatch.setattr(app,'POOL_DIR',tmp_path/'pool')
    j={'id':'fixture-precise','frames':192,'request':{'h3_preserve_audio':True}}
    result=app._stage_audio_references(j,[{'source':'/media/chirp.wav','start_sec':.12345}]);assert result
    with wave.open(str(tmp_path/'pool/h3-out'/result[0]['file'])) as f:actual=f.readframes(f.getnframes())
    expected=__import__('subprocess').check_output(['ffmpeg','-v','error','-i',str(source),'-ss','0.123450','-t','8.000000','-vn','-ac','2','-ar','48000','-f','s16le','-'])
    assert actual==expected,'Rounding source offsets creates hidden phoneme timing drift'
