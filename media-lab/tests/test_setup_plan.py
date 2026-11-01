from dataclasses import replace
import pytest
from media_lab_core.setup_wizard import make_plan
from media_lab_core.model_catalog import ModelOption
BASE=ModelOption(id='a',name='A',category='image',status='qualified',recommended=False,quality='test',speed='test',summary='fixture',disk_gb=1,memory_floor_gb=1,license_name='MIT',license_url='https://example.com/LICENSE',terms_acceptance_required=False,source_url='https://example.com/source',immutable_revision='pinned-fixture',sha256='a'*64,requires=(),exclusive_group='',hardware=('any',))


def test_plan_never_treats_profile_label_as_hardware_verification(tmp_path):
    plan=make_plan(tmp_path/'catalog.toml',[BASE],'nvidia-sm121')
    assert plan['hardware_verified'] is False
    assert plan['next_stage']=='hardware-preflight'
    assert plan['resource_estimates_complete'] is True
    assert plan['hardware_requirements']=={'a':['any']}


def test_empty_selection_and_license_review_have_distinct_next_steps(tmp_path):
    assert make_plan(tmp_path,[],'auto-detect')['next_stage']=='choose-capabilities'
    plan=make_plan(tmp_path,[replace(BASE,terms_acceptance_required=True)],'auto-detect')
    assert plan['next_stage']=='license-review'
    assert plan['hardware_verified'] is False


@pytest.mark.parametrize('value',[float('nan'),float('inf'),-1])
def test_invalid_resource_estimates_rejected(tmp_path,value):
    with pytest.raises(ValueError,match='finite'):make_plan(tmp_path,[replace(BASE,disk_gb=value)],'auto-detect')


def test_unknown_estimates_and_unqualified_models_cannot_look_ready(tmp_path):
    assert make_plan(tmp_path,[replace(BASE,memory_floor_gb=0)],'auto-detect')['resource_estimates_complete'] is False
    with pytest.raises(ValueError,match='not selectable'):make_plan(tmp_path,[replace(BASE,status='planned')],'auto-detect')


def test_json_plan_never_prompts_or_selects_implicitly(monkeypatch,capsys,tmp_path):
    import json
    from media_lab_core import setup_wizard
    monkeypatch.setattr(setup_wizard,"load_model_catalog",lambda _: {"a":BASE})
    monkeypatch.setattr("builtins.input",lambda *_: pytest.fail("JSON mode prompted"))
    output=tmp_path/"plan.json"
    assert setup_wizard.main(["--json","--output-plan",str(output)])==0
    result=json.loads(capsys.readouterr().out)
    assert result==json.loads(output.read_text())
    assert result["selected"]==[]
    assert result["next_stage"]=="choose-capabilities"


def test_json_catalog_keeps_blocked_entries_visible_and_refuses_selection(monkeypatch,capsys):
    import json
    from media_lab_core import setup_wizard
    monkeypatch.setattr(setup_wizard,"load_model_catalog",lambda _: {"a":replace(BASE,status="planned")})
    assert setup_wizard.main(["--list","--json"])==0
    row=json.loads(capsys.readouterr().out)["models"][0]
    assert row["selectable"] is False
    assert row["refusal_reason"]
    assert setup_wizard.main(["--select","a","--json"])==2
    result=capsys.readouterr()
    assert not result.out
    assert "error" in json.loads(result.err)


def test_default_3d_candidates_are_visible_but_never_installable(capsys):
    import json
    from media_lab_core import setup_wizard
    assert setup_wizard.main(["--list","--json"])==0
    models=json.loads(capsys.readouterr().out)["models"]
    candidates=[model for model in models if model["category"]=="model"]
    assert {model["id"] for model in candidates}=={"triposr-image-to-3d","trellis2-image-to-3d"}
    assert all(not model["selectable"] and not model["recommended"] for model in candidates)
    for model in candidates:
        assert setup_wizard.main(["--json","--select",model["id"]])==2
    capsys.readouterr()
    assert setup_wizard.main(["--list"])==0
    output=capsys.readouterr().out
    assert "3D game assets" in output
    assert "Disk: not measured" in output
