"""Cross-process coordinator decisions without launching real services in pytest."""
from pathlib import Path
import importlib
import importlib.util
import sys

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
    runner.current='absent'
    with pytest.raises(runner_module.UnitError,match='no automatic retry'):
        await coordinator.StageCoordinator(store,runner).run_step(
            run.id,run.stream_id,run.run_token,step,folder,folder/'plan.json','a'*64)
    assert store.stage_attempts(run.id,run.stream_id)[0]['status']=='interrupted'
    assert (folder/'output.bin').read_bytes()==b'partial, preserve me'
    assert runner.starts==0


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
    runner.current='absent'
    closed=await coordinator.StageCoordinator(ledger.JobStore(store.path),runner).run_step(
        run.id,run.stream_id,run.run_token,step,folder,folder/'plan.json','a'*64)
    assert closed['reused'] and closed['state']=='completed' and runner.starts==0
    assert store.stage_attempts(run.id,run.stream_id)[0]['status']=='completed'
