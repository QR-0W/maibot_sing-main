import importlib.util
from pathlib import Path
import sys
import pytest

spec=importlib.util.spec_from_file_location('render_plan_test',Path(__file__).resolve().parents[1]/'runtime/render_plan.py')
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)


def plan(seconds):
    return m.build_plan('/workspace','/python','/runtime/media_stage.py','/rvc.py','/model.pth','/index',int(seconds*44100))


@pytest.mark.parametrize('seconds,count',[(30,2),(40,2),(41,2),(44.9,2),(45,3),(237.923,12),(300,15)])
def test_finite_chunk_plan(seconds,count):
    steps=plan(seconds)
    conversion=[s for s in steps if s.name.startswith('convert_')]
    assert len(conversion)==count
    assert all(s.unit_limit_s<900 for s in steps)
    assert all(s.argv[s.argv.index('--pitch')+1]=='0' for s in conversion)
    assert steps[-1].name=='validate'
    assert m.total_compute_budget(steps)==sum(s.timeout_s+30 for s in steps)
    produced=set(['source.audio'])
    for step in steps:
        assert set(step.inputs)<=produced
        produced.update(step.outputs)


@pytest.mark.parametrize('seconds',[0,29.9,300.1])
def test_reject_unbounded_inputs(seconds):
    with pytest.raises(ValueError):plan(seconds)
