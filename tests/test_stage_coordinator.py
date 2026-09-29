"""Cross-process coordinator decisions without launching real services in pytest."""
from pathlib import Path
import importlib
import importlib.util
import sys
import asyncio
import json

import pytest

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('coord_test_pkg',root/'__init__.py',submodule_search_locations=[str(root)])
pkg=importlib.util.module_from_spec(spec);sys.modules[spec.name]=pkg;spec.loader.exec_module(pkg)
ledger=importlib.import_module('coord_test_pkg.services.job_store')
source=importlib.import_module('coord_test_pkg.services.source_offer')
coordinator=importlib.import_module('coord_test_pkg.services.stage_coordinator')
runner_module=importlib.import_module('coord_test_pkg.services.unit_runner')
receipts=importlib.import_module('coord_test_pkg.runtime.stage_receipts')
Step=importlib.import_module('coord_test_pkg.runtime.render_plan').Step


class StubUnitRunner:
    def __init__(self,state):self.current=state;self.starts=0
    async def state(self,unit): return self.current
    async def run(self,*args):
        self.starts+=1
        raise AssertionError('Must never start a second unverified unit')


@pytest.fixture
def work(tmp_path):
    store=ledger.JobStore(tmp_path/'jobs.sqlite3')
    job,_=store.submit('verified-stream','stable-message-id',{'query':'synthetic'})
    offered=store.offer(job.id,job.stream_id,[source.CatalogueItem('163','123','Song','Artist','Album')],
                        expected_revision=job.revision)
    store.select(job.id,job.stream_id,offered.offer_id,1)
    run=store.claim_next()
    folder=tmp_path/'workspace';folder.mkdir()
    (folder/'input.bin').write_bytes(b'unchanged synthetic input')
    step=Step('convert_000',('/does/not/run',),10,('input.bin',),('output.bin',))
    return store,run,folder,step


@pytest.mark.asyncio
async def test_active_old_unit_is_never_launched_again(work):
    store,run,folder,step=work
    runner=StubUnitRunner('active')
    result=await coordinator.StageCoordinator(store,runner).run_step(
        run.id,run.stream_id,run.run_token,step,folder,folder/'plan.json','a'*64)
    assert result['state']=='running' and runner.starts==0
    assert store.stage_attempts(run.id,run.stream_id)[0]['status']=='claimed'
    reopened=ledger.JobStore(store.path)
    second=await coordinator.StageCoordinator(reopened,runner).run_step(
        run.id,run.stream_id,run.run_token,step,folder,folder/'plan.json','a'*64)
    assert second['unit']==result['unit'] and runner.starts==0


@pytest.mark.asyncio
async def test_stopped_unit_with_missing_receipt_becomes_interrupted(work):
    store,run,folder,step=work
    runner=StubUnitRunner('active')
    result=await coordinator.StageCoordinator(store,runner).run_step(
        run.id,run.stream_id,run.run_token,step,folder,folder/'plan.json','a'*64)
    logdir=folder/'unit-logs';logdir.mkdir()
    (logdir/(result['unit']+'.log')).write_text('synthetic stopped worker')
    (folder/'output.bin').write_bytes(b'partial, preserve me')
    (logdir/(result['unit']+'.exit.json')).write_text(json.dumps(
        {'schema':2,'completed':True,'returncode':0,'service':{'Result':'success'}}))
    runner.current='absent'
    with pytest.raises(runner_module.UnitError) as exc:
        await coordinator.StageCoordinator(store,runner).run_step(
            run.id,run.stream_id,run.run_token,step,folder,folder/'plan.json','a'*64)
    assert exc.value.code=='unit_result_missing'
    assert store.stage_attempts(run.id,run.stream_id)[0]['status']=='interrupted'
    terminal=store.get(run.id,run.stream_id)
    assert terminal.state=='interrupted' and terminal.error['code']=='unit_result_missing'
    assert (folder/'output.bin').read_bytes()==b'partial, preserve me'
    assert runner.starts==0


@pytest.mark.asyncio
@pytest.mark.parametrize('systemd_result,worker_code,expected,state',[
    ('exit-code','stage_timeout','stage_timeout','interrupted'),
    ('exit-code','stage_exit','stage_exit','failed'),
    ('oom-kill','stage_exit','unit_oom','failed'),
    ('timeout','stage_exit','unit_timeout','interrupted'),
    ('signal','stage_exit','unit_signal','interrupted')])
async def test_specific_failure_survives_reconciliation(work,systemd_result,worker_code,expected,state):
    store,run,folder,step=work
    runner=StubUnitRunner('active')
    claimed=await coordinator.StageCoordinator(store,runner).run_step(
        run.id,run.stream_id,run.run_token,step,folder,folder/'plan.json','a'*64)
    logs=folder/'unit-logs';logs.mkdir()
    (logs/(claimed['unit']+'.log')).write_text('private diagnostics')
    (logs/(claimed['unit']+'.exit.json')).write_text(json.dumps(
        {'schema':2,'completed':True,'returncode':1,'service':{'Result':systemd_result}}))
    status={'stage':step.name,'state':'failed','recipe':'a'*64,'unit':claimed['unit'],
            'elapsed_s':1.0,'code':worker_code,'message':'private raw failure detail'}
    receipt_dir=folder/'.receipts';receipt_dir.mkdir(exist_ok=True)
    (receipt_dir/(step.name+'.status.json')).write_text(json.dumps(status))
    runner.current='absent'
    with pytest.raises(runner_module.UnitError) as exc:
        await coordinator.StageCoordinator(store,runner).run_step(
            run.id,run.stream_id,run.run_token,step,folder,folder/'plan.json','a'*64)
    assert exc.value.code==expected
    terminal=store.get(run.id,run.stream_id)
    assert terminal.state==state and terminal.error['code']==expected
    assert terminal.error['stage']==step.name
    assert terminal.error['message'] and 'private raw failure detail' not in terminal.error['message']
    assert store.claim_next() is None


@pytest.mark.asyncio
async def test_stale_status_from_prior_unit_never_supplies_failure_cause(work):
    store,run,folder,step=work
    runner=StubUnitRunner('active')
    claimed=await coordinator.StageCoordinator(store,runner).run_step(
        run.id,run.stream_id,run.run_token,step,folder,folder/'plan.json','a'*64)
    logs=folder/'unit-logs';logs.mkdir()
    (logs/(claimed['unit']+'.log')).write_text('private diagnostics')
    (logs/(claimed['unit']+'.exit.json')).write_text(json.dumps(
        {'schema':2,'completed':True,'returncode':1,'service':{'Result':'exit-code'}}))
    receipt_dir=folder/'.receipts';receipt_dir.mkdir(exist_ok=True)
    (receipt_dir/(step.name+'.status.json')).write_text(json.dumps(
        {'stage':step.name,'state':'failed','recipe':'a'*64,'unit':'maibot-sing-'+('f'*32)+'-convert_000',
         'code':'stage_timeout','message':'stale','elapsed_s':1.0}))
    runner.current='absent'
    with pytest.raises(runner_module.UnitError) as exc:
        await coordinator.StageCoordinator(store,runner).run_step(
            run.id,run.stream_id,run.run_token,step,folder,folder/'plan.json','a'*64)
    assert exc.value.code=='unit_exit'
    assert store.get(run.id,run.stream_id).error['code']=='unit_exit'


@pytest.mark.asyncio
async def test_receipt_committed_before_crash_is_recognized(work):
    store,run,folder,step=work
    runner=StubUnitRunner('active')
    result=await coordinator.StageCoordinator(store,runner).run_step(
        run.id,run.stream_id,run.run_token,step,folder,folder/'plan.json','a'*64)
    logdir=folder/'unit-logs';logdir.mkdir()
    (logdir/(result['unit']+'.log')).write_text('synthetic completed worker')
    (folder/'output.bin').write_bytes(b'valid output')
    inp=receipts.sha256(folder/'input.bin')
    receipts.StageReceipts(folder,'a'*64).seal(step.name,{'input.bin':inp},['output.bin'])
    (logdir/(result['unit']+'.exit.json')).write_text(json.dumps(
        {'schema':2,'completed':True,'returncode':0,'service':{'Result':'success'}}))
    runner.current='absent'
    closed=await coordinator.StageCoordinator(ledger.JobStore(store.path),runner).run_step(
        run.id,run.stream_id,run.run_token,step,folder,folder/'plan.json','a'*64)
    assert closed['reused'] and closed['state']=='completed' and runner.starts==0
    assert store.stage_attempts(run.id,run.stream_id)[0]['status']=='completed'


@pytest.mark.asyncio
async def test_launch_gap_is_owned_not_interrupted(work):
    store,run,folder,step=work
    entered,release=asyncio.Event(),asyncio.Event()
    class PausedLauncher(StubUnitRunner):
        async def run(self,unit,plan,step,workspace,*,ownership_fd):
            self.starts+=1
            logs=workspace/'unit-logs';logs.mkdir()
            (logs/(unit+'.log')).write_text('created before actual submission')
            entered.set()
            await release.wait()
            (workspace/'output.bin').write_bytes(b'completed')
            inp=receipts.sha256(workspace/'input.bin')
            receipts.StageReceipts(workspace,'a'*64).seal(step.name,{'input.bin':inp},['output.bin'])
            (logs/(unit+'.exit.json')).write_text(json.dumps({'schema':2,'completed':True,'returncode':0,'service':{'Result':'success'}}))
            return {'status':{'reused':False}}
    runner=PausedLauncher('absent')
    first=coordinator.StageCoordinator(store,runner)
    task=asyncio.create_task(first.run_step(run.id,run.stream_id,run.run_token,
                                          step,folder,folder/'plan.json','a'*64))
    try:
        await asyncio.wait_for(entered.wait(),2)
        competing=coordinator.StageCoordinator(ledger.JobStore(store.path),runner)
        result=await competing.run_step(run.id,run.stream_id,run.run_token,
                                       step,folder,folder/'plan.json','a'*64)
        assert result['state']=='owned'
        assert store.stage_attempts(run.id,run.stream_id)[0]['status']=='claimed'
        assert store.claim_next() is None
    finally:
        release.set()
        settled=await asyncio.wait_for(task,2)
    assert settled['state']=='completed' and runner.starts==1


@pytest.mark.asyncio
async def test_absent_unit_without_launch_witness_stays_unknown(work):
    store,run,folder,step=work
    owner=store.claim_step(run.id,run.run_token,step.name)
    logs=folder/'unit-logs';logs.mkdir()
    (logs/(owner.unit_name+'.log')).write_text('crash before launch ack')
    runner=StubUnitRunner('absent')
    with pytest.raises(runner_module.UnitError) as err:
        await coordinator.StageCoordinator(store,runner).run_step(
            run.id,run.stream_id,run.run_token,step,folder,folder/'plan.json','a'*64)
    assert err.value.code=='launch_unknown'
    assert store.stage_attempts(run.id,run.stream_id)[0]['status']=='claimed'
    assert runner.starts==0 and store.claim_next() is None


@pytest.mark.asyncio
async def test_cancel_reopen_reconciles_old_unit_without_new_launch(work):
    store,run,folder,step=work
    owner=store.claim_step(run.id,run.run_token,step.name)
    logs=folder/'unit-logs';logs.mkdir()
    (logs/(owner.unit_name+'.log')).write_text('launched previously')
    store.cancel(run.id,run.stream_id)
    next_job,_=store.submit(run.stream_id,'next-message',{'query':'next'})
    offered=store.offer(next_job.id,next_job.stream_id,[source.CatalogueItem('163','2','S','A','B')],
                        expected_revision=next_job.revision)
    store.select(next_job.id,next_job.stream_id,offered.offer_id,1)
    runner=StubUnitRunner('active')
    restarted=coordinator.StageCoordinator(ledger.JobStore(store.path),runner)
    result=await restarted.run_step(run.id,run.stream_id,run.run_token,step,folder,folder/'plan.json','a'*64)
    assert result['state']=='running' and store.claim_next() is None
    runner.current='absent'
    with pytest.raises(runner_module.UnitError,match='witness'):
        await restarted.run_step(run.id,run.stream_id,run.run_token,step,folder,folder/'plan.json','a'*64)
    assert store.claim_next() is None
    (logs/(owner.unit_name+'.exit.json')).write_text(json.dumps({'schema':2,'completed':True,'returncode':1,'service':{'Result':'exit-code'}}))
    result=await restarted.run_step(run.id,run.stream_id,run.run_token,step,folder,folder/'plan.json','a'*64)
    assert result['state']=='cancelled' and runner.starts==0
    assert store.claim_next().id==next_job.id


@pytest.mark.asyncio
async def test_inherited_flock_survives_parent_close(tmp_path):
    ownership=importlib.import_module('coord_test_pkg.services.ownership')
    path=tmp_path/'job.lock'
    child=None
    try:
        with ownership.exclusive(path) as fd:
            child=await asyncio.create_subprocess_exec(sys.executable,'-c',
                "import sys; print('ready',flush=True); sys.stdin.buffer.read(1)",
                pass_fds=(fd,),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE)
            assert await asyncio.wait_for(child.stdout.readline(),2)==b'ready\n'
        with pytest.raises(ownership.OwnershipBusy):
            with ownership.exclusive(path):
                pass
    finally:
        if child:
            await asyncio.wait_for(child.communicate(b'x'),2)
            assert child.returncode==0
    with ownership.exclusive(path):
        pass
