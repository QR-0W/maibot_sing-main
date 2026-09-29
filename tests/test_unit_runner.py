"""Safe unit naming, plan immutability and fail-closed service reconciliation."""
from pathlib import Path
import asyncio
import importlib
import importlib.util
import json
import sys

import pytest

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('unit_test_pkg',root/'__init__.py',submodule_search_locations=[str(root)])
pkg=importlib.util.module_from_spec(spec);sys.modules[spec.name]=pkg;spec.loader.exec_module(pkg)
module=importlib.import_module('unit_test_pkg.services.unit_runner')
Step=importlib.import_module('unit_test_pkg.runtime.render_plan').Step


def test_save_plan_is_private_and_write_once(tmp_path):
    step=Step('convert_000',('/python','-c','pass'),10,('input.bin',),('output.bin',))
    path=tmp_path/'plan.json'
    kwargs={'workspace':tmp_path,'recipe':'a'*64,'inference_lock':tmp_path/'shared.lock','steps':(step,)}
    module.save_plan(path,**kwargs)
    assert path.stat().st_mode & 0o777 == 0o600
    assert module.save_plan(path,**kwargs)==path
    original=path.read_bytes()
    with pytest.raises(module.UnitError,match='different stage plan'):
        module.save_plan(path,**{**kwargs,'recipe':'b'*64})
    assert path.read_bytes()==original
    assert json.loads(original)['steps'][0]['timeout_s']==10


def test_unit_name_cannot_run_arbitrary_service():
    name=module.UnitRunner.name('a'*32,'convert_003')
    assert name=='maibot-sing-'+('a'*32)+'-convert_003'
    for token,stage in [('x','convert_003'),('a'*32,'../maibot-main'),('a'*32,'convert_003.service')]:
        with pytest.raises(ValueError): module.UnitRunner.name(token,stage)


class FakeProcess:
    def __init__(self,raw,code=0):
        self.raw=raw; self.returncode=code
    async def communicate(self): return self.raw,b''


def test_launch_witness_is_versioned_and_bounded(tmp_path):
    unit=module.UnitRunner.name('a'*32,'convert_000')
    logs=tmp_path/'unit-logs';logs.mkdir()
    path=logs/(unit+'.exit.json')
    assert module.UnitRunner.launch_witness(tmp_path,unit) is None
    witness={'schema':2,'completed':True,'returncode':1,
             'service':{'Result':'oom-kill','ExecMainStatus':'9'}}
    path.write_text(json.dumps(witness))
    assert module.UnitRunner.launch_witness(tmp_path,unit)==witness
    assert module.UnitRunner.launch_finished(tmp_path,unit)
    path.write_text(json.dumps({**witness,'schema':1}))
    with pytest.raises(module.UnitError) as exc:
        module.UnitRunner.launch_witness(tmp_path,unit)
    assert exc.value.code=='launch_witness_invalid'


@pytest.mark.asyncio
@pytest.mark.parametrize('raw,code,result',[
    (b'LoadState=not-found\nActiveState=inactive\n',0,'absent'),
    (b'LoadState=loaded\nActiveState=active\n',0,'active'),
    (b'LoadState=loaded\nActiveState=failed\n',0,'stopped'),
    (b'LoadState=loaded\nActiveState=inactive\n',0,'stopped'),
    (b'LoadState=not-found\nActiveState=unknown\n',0,'error'),
    (b'LoadState=not-found\nActiveState=inactive\n',1,'error')])
async def test_systemctl_unknown_is_not_stopped(monkeypatch,raw,code,result):
    async def fake(*args,**kwargs):
        assert args[0:3]==('systemctl','--user','show')
        return FakeProcess(raw,code)
    monkeypatch.setattr(module.asyncio,'create_subprocess_exec',fake)
    runner=module.UnitRunner(root/'runtime/stage_executor.py',Path(sys.executable))
    unit=runner.name('a'*32,'convert_000')
    if result=='error':
        with pytest.raises(module.UnitError,match='service state|query'):
            await runner.state(unit)
    else:
        assert await runner.state(unit)==result
