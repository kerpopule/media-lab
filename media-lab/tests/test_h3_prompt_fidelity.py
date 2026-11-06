"""Native H3 prompt fidelity and explicit refusal; no engines or network."""
import os
os.environ['MEDIA_LAB_DISABLE_BACKGROUND_WORKERS']='1'
import pytest
import app

@pytest.fixture
def constructor(monkeypatch):
 monkeypatch.setattr(app,'singularity_refusal',lambda:None)
 monkeypatch.setattr(app.engine_licences,'enabled',lambda engine:True)
 monkeypatch.setattr(app,'engine_up',lambda *args:False)
 monkeypatch.setattr(app,'h3_variant_warm',lambda *args:False)
 monkeypatch.setattr(app,'submit_job',lambda kind,request,extra:{'kind':kind,'request':request,**extra})
 return app.make_video_job

def test_native_h3_keeps_every_character_of_a_recovered_length_prompt(constructor):
 prompt='DIRECTING '+('full original sentence ' * 110)+' FINAL WORD HANDOFFS AND CAMERA END'
 assert len(prompt)>2177
 req=app.GenReq(prompt=prompt,model='h3',h3_engine='singularity',duration='8',style='none').model_dump()
 job=constructor(req)
 assert job['request']['prompt']==prompt
 assert job['full_prompt']==prompt
 assert 'FINAL WORD HANDOFFS AND CAMERA END' in job['full_prompt']

def test_native_h3_refuses_overlong_prompt_instead_of_silent_truncation(constructor,monkeypatch):
 monkeypatch.setattr(app,'submit_job',lambda *args,**kwargs:pytest.fail('Overlong H3 prompt reached queue'))
 with pytest.raises(ValueError,match='8000'):
  constructor({'prompt':'x'*8001,'model':'h3','h3_engine':'singularity','style':'none'})

def test_ordinary_ltx_keeps_existing_prompt_boundary(constructor):
 prompt='x'*2400
 job=constructor({'prompt':prompt,'model':'ltx25','style':'none','duration':'8'})
 assert job['full_prompt']=='x'*2000
