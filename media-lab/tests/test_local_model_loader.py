import hashlib,json
import pytest
from media_lab_core.local_model_loader import load_verified_model

def fixture(tmp_path):
    values={'config.json':json.dumps({'name':'Fixture','args':{}}).encode(),'model.safetensors':b'fixture-not-real-weights'}
    entries={}
    for name,data in values.items():
        (tmp_path/name).write_bytes(data)
        entries[name]={'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    return {'model_class':'Fixture','config':entries['config.json'],'weights':entries['model.safetensors']}


def test_checks_all_files_before_constructor_and_refuses_changed_weights(tmp_path):
    descriptor=fixture(tmp_path);calls=[]
    (tmp_path/'model.safetensors').write_bytes(b'changed')
    with pytest.raises(ValueError):load_verified_model(tmp_path,descriptor,{'Fixture':lambda:calls.append('construct')},lambda path:calls.append('read'))
    assert calls==[]


def test_requires_strict_state_loading_before_return(tmp_path):
    descriptor=fixture(tmp_path);calls=[]
    class Model:
        def load_state_dict(self,state,strict):
            calls.append(strict)
            raise RuntimeError('missing key')
        def eval(self):calls.append('eval')
    with pytest.raises(RuntimeError,match='missing key'):load_verified_model(tmp_path,descriptor,{'Fixture':Model},lambda path:{})
    assert calls==[True]


@pytest.mark.parametrize('path',['../model.safetensors','/model.safetensors','a//model.safetensors','a\\model.safetensors'])
def test_bad_paths_do_not_load(tmp_path,path):
    descriptor=fixture(tmp_path);descriptor['weights']['path']=path
    with pytest.raises(ValueError):load_verified_model(tmp_path,descriptor,{},lambda path:{})


def test_reviewed_constructor_receives_exact_state_and_evaluation(tmp_path):
    descriptor=fixture(tmp_path)
    class Model:
        def load_state_dict(self,state,strict):assert state=={'tensor':'fixture'} and strict
        def eval(self):self.evaluated=True
    result=load_verified_model(tmp_path,descriptor,{'Fixture':Model},lambda path:{'tensor':'fixture'})
    assert result.evaluated


def test_last_component_failure_prevents_all_model_allocations(tmp_path):
    from media_lab_core.local_model_loader import load_verified_components
    descriptor=fixture(tmp_path);calls=[]
    descriptors=[{**descriptor,'role':'shape'},{**descriptor,'role':'texture','weights':{**descriptor['weights'],'path':'missing.safetensors'}}]
    with pytest.raises(ValueError,match='missing'):load_verified_components(tmp_path,descriptors,{'shape','texture'},{'Fixture':lambda:calls.append('allocate')},lambda path:{})
    assert calls==[]


@pytest.mark.parametrize('roles',[['shape'],['shape','shape'],['shape','unexpected']])
def test_component_set_must_match_pipeline_before_construction(tmp_path,roles):
    from media_lab_core.local_model_loader import load_verified_components
    descriptor=fixture(tmp_path)
    with pytest.raises(ValueError,match='roles'):load_verified_components(tmp_path,[{**descriptor,'role':r} for r in roles],{'shape','texture'},{},lambda path:{})


def test_complete_component_set_uses_strict_loading_for_each(tmp_path):
    from media_lab_core.local_model_loader import load_verified_components
    descriptor=fixture(tmp_path);strict_values=[]
    class Model:
        def load_state_dict(self,state,strict):strict_values.append(strict)
        def eval(self):pass
    result=load_verified_components(tmp_path,[{**descriptor,'role':r} for r in ['shape','texture']],{'shape','texture'},{'Fixture':Model},lambda path:{})
    assert set(result)=={'shape','texture'} and strict_values==[True,True]
