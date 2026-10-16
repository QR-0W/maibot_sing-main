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


@pytest.mark.parametrize('executor,python', [
    (Path('relative-executor'), Path('/missing-python')),
    (Path('/missing-executor'), Path('relative-python'))])
def test_constructor_rejects_relative_execution_paths(executor, python):
    with pytest.raises(ValueError, match='absolute paths'):
        module.UnitRunner(executor, python)


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['executor_missing', 'executor_directory',
    'executor_symlink', 'python_missing', 'python_directory', 'python_broken_symlink',
    'launcher_missing', 'launcher_symlink'])
async def test_execution_assets_are_rejected_at_run_before_any_launch_or_log(
        tmp_path, monkeypatch, invalid):
    executor = tmp_path/'stage_executor.py'
    python = tmp_path/'python'
    launcher = tmp_path/'unit_launcher.py'
    for path in (executor, python, launcher):
        path.write_text('# synthetic execution asset, never executed')
    target = {'executor': executor, 'python': python, 'launcher': launcher}[
        invalid.split('_', 1)[0]]
    target.unlink()
    if invalid.endswith('directory'):
        target.mkdir()
    elif invalid.endswith('broken_symlink'):
        target.symlink_to(tmp_path/'absent-interpreter')
    elif invalid.endswith('symlink'):
        other = tmp_path/'regular-file'
        other.write_text('# synthetic target')
        target.symlink_to(other)
    runner = module.UnitRunner(executor, python)
    async def forbidden(*args, **kwargs):
        raise AssertionError('Invalid execution assets must fail before subprocesses')
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', forbidden)
    step = Step('decode', ('/unused',), 10, (), ('decoded.bin',))
    with pytest.raises(module.UnitError) as error:
        await runner.run(runner.name('a'*32, step.name), tmp_path/'plan.json',
                         step, tmp_path, ownership_fd=-1)
    assert error.value.code == ('launcher_missing' if invalid.startswith('launcher')
                                else 'execution_config_invalid')
    assert not (tmp_path/'unit-logs').exists()


@pytest.mark.asyncio
async def test_venv_python_symlink_reaches_existing_unit_fence(tmp_path, monkeypatch):
    executor = tmp_path/'stage_executor.py'
    executor.write_text('# never execute')
    (tmp_path/'unit_launcher.py').write_text('# never execute')
    python = tmp_path/'python'
    python.symlink_to(sys.executable)
    plan = tmp_path/'plan.json'
    plan.write_text('{}')
    runner = module.UnitRunner(executor, python)
    async def active(unit):
        return 'active'
    monkeypatch.setattr(runner, 'state', active)
    step = Step('decode', ('/unused',), 10, (), ('decoded.bin',))
    with pytest.raises(module.UnitError) as error:
        await runner.run(runner.name('a'*32, step.name), plan, step, tmp_path,
                         ownership_fd=-1)
    assert error.value.code == 'unit_reconcile_required'
    assert not (tmp_path/'unit-logs').exists()


def test_default_budget_snapshot_and_whole_unit_oom_policy():
    runner = module.UnitRunner(root/'runtime/stage_executor.py', Path(sys.executable))
    step = Step('convert_000', ('/does/not/run',), 10, ('input.bin',), ('output.bin',))
    props = runner._unit_properties(step)
    assert 'MemoryMax=2147483648' in props
    assert 'MemoryHigh=1610612736' in props and 'MemorySwapMax=0' in props
    assert 'KillMode=control-group' in props and 'OOMPolicy=kill' in props
    assert 'RuntimeMaxSec=%d' % step.unit_limit_s in props
    environment = runner._unit_environment(runner.name('a'*32, 'convert_000'))
    assert 'MAIBOT_SING_BUDGET_MEMORY_MAX=2147483648' in environment
    assert 'MAIBOT_SING_BUDGET_HEADROOM=536870912' in environment


@pytest.mark.asyncio
async def test_destination_gate_uses_user_manager_ancestors_not_maibot_leaf(monkeypatch):
    queried, checked = [], []
    async def query(*args, **kwargs):
        queried.append(args)
        return FakeProcess(b'/user.slice/user-1001.slice/user@1001.service\n')
    def check(budget, target, *, root):
        checked.append((budget, target, root))
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', query)
    monkeypatch.setattr(module, 'check_ancestors', check)
    runner = module.UnitRunner(root/'runtime/stage_executor.py', Path(sys.executable))
    await runner.check_destination_budget()
    assert queried[0][-1] == '-.slice'
    assert str(checked[0][1]).endswith('/user@1001.service/app.slice')
    assert checked[0][2] == Path('/sys/fs/cgroup')


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [b'', b'not/absolute\n', b'/../escape\n'])
async def test_unusable_destination_cgroup_refuses_launch(monkeypatch, raw):
    async def query(*args, **kwargs):
        return FakeProcess(raw)
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', query)
    runner = module.UnitRunner(root/'runtime/stage_executor.py', Path(sys.executable))
    with pytest.raises(module.UnitError) as exc:
        await runner.check_destination_budget()
    assert exc.value.code == 'cgroup_unavailable'
