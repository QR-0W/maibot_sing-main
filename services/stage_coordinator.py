"""Serialize claim/launch/reconcile; never infer a completed launch from a log.

The nonblocking per-job scheduler flock is inherited by a launch witness helper.
Cancellation/reload may leave a user service running: reconcile its exact unit,
retain the slot while active/unknown, and never silently retry partial media.
"""
from pathlib import Path
from typing import Dict
import asyncio
import re

from ..runtime.stage_receipts import CheckpointError, StageReceipts
from .job_store import JobConflict, JobStore
from .ownership import exclusive, OwnershipBusy
from .unit_runner import UnitError, UnitRunner


class StageCoordinator:
    def __init__(self, store: JobStore, runner: UnitRunner):
        self.store=store
        self.runner=runner

    def lock_path(self, job_id):
        if not isinstance(job_id,str) or not re.fullmatch('[0-9a-f]{32}',job_id):
            raise ValueError('Invalid job identity')
        return self.store.path.parent/(job_id+'.scheduler.lock')

    async def _verified(self, workspace: Path, recipe: str, step) -> bool:
        def check() -> bool:
            receipts=StageReceipts(workspace,recipe)
            inputs=receipts._files(list(step.inputs)) if step.inputs else {}
            hashes={name:record['sha256'] for name,record in inputs.items()}
            value=receipts.verify(step.name,hashes)
            return value is not None and set(value['outputs'])==set(step.outputs)
        return await asyncio.to_thread(check)

    async def _cancelled(self, job):
        result=await asyncio.to_thread(self.store.finish_failure,job.id,job.run_token,
            {'code':'cancelled','message':'用户取消；此前阶段已停止，产物保留。'},
            expected_unit=job.unit_name,expected_revision=job.revision)
        return {'unit':job.unit_name,'state':result.state}

    async def _settle(self, owner, workspace, recipe, step):
        try:
            valid=await self._verified(workspace,recipe,step)
        except (CheckpointError,OSError):
            valid=False
        settled=await asyncio.to_thread(self.store.settle_step,owner.id,owner.run_token,
                                        owner.unit_name,completed=valid)
        if settled.state=='cancel_requested':
            return await self._cancelled(settled)
        if not valid:
            raise UnitError('stage_interrupted','Worker ended without a verified receipt; no automatic retry')
        return {'unit':owner.unit_name,'state':'completed','reused':True}

    async def run_step(self, job_id: str, stream_id: str, run_token: str,
                       step, workspace: Path, plan: Path, recipe: str) -> Dict:
        # Authenticate before exposing even a busy/not-busy job observation.
        await asyncio.to_thread(self.store.get,job_id,stream_id)
        try:
            with exclusive(self.lock_path(job_id)) as ownership_fd:
                return await self._run_owned(job_id,stream_id,run_token,step,
                                             workspace,plan,recipe,ownership_fd)
        except OwnershipBusy:
            return {'state':'owned','unit':None}

    async def _run_owned(self, job_id, stream_id, run_token, step, workspace,
                         plan, recipe, ownership_fd):
        job=await asyncio.to_thread(self.store.get,job_id,stream_id)
        if job.run_token!=run_token or job.state not in ('running','cancel_requested'):
            raise JobConflict('Caller no longer owns this job')
        # Cancellation reconciles existing work ONLY; it must never claim a step.
        if job.state=='cancel_requested':
            if job.unit_name is None:
                return await self._cancelled(job)
            if job.active_step!=step.name:
                raise JobConflict('Reconcile the cancelled job current stage, not a new one')
            owner=job
        else:
            owner=await asyncio.to_thread(self.store.claim_step,job_id,run_token,step.name)
        unit=owner.unit_name
        attempts=await asyncio.to_thread(self.store.stage_attempts,job_id,stream_id)
        current=next((item for item in attempts if item['unit_name']==unit),None)
        if current is None:
            raise JobConflict('Stage has no durable unit ownership record')
        state=await self.runner.state(unit)
        if state=='active':
            return {'unit':unit,'state':'running'}
        if state not in ('absent','stopped'):
            raise UnitError('unit_status_unknown','Cannot prove current unit inactivity')
        if current['status']!='claimed':
            if owner.state=='cancel_requested':
                return await self._cancelled(owner)
            if current['status']=='completed':
                if not await self._verified(workspace,recipe,step):
                    raise UnitError('receipt_invalid','Previously completed artifact changed')
                return {'unit':unit,'state':'completed','reused':True}
            raise JobConflict('Interrupted stage requires an explicit retry decision')
        log=workspace/'unit-logs'/(unit+'.log')
        StageReceipts._safe(log)
        if log.exists():
            # A log can exist BEFORE StartTransientUnit. Without the inherited
            # lock and a durable wait result, absence is NOT proof of a failed
            # worker. Preserve ownership for an uncertain launch after a crash.
            if state!='stopped' and not UnitRunner.launch_finished(workspace,unit):
                raise UnitError('launch_unknown','Launch has no durable completion witness; slot retained')
            return await self._settle(owner,workspace,recipe,step)
        if owner.state=='cancel_requested':
            # No launch log, no helper owns our flock and no unit exists: the
            # atomic claim was cancelled before launch preparation began.
            return await self._settle(owner,workspace,recipe,step)
        if state!='absent':
            raise UnitError('unit_status_unknown','Existing unit lacks a launch record')
        try:
            result=await self.runner.run(unit,plan,step,workspace,ownership_fd=ownership_fd)
        except UnitError:
            state=await self.runner.state(unit)
            if state=='stopped' or (state=='absent' and UnitRunner.launch_finished(workspace,unit)):
                return await self._settle(owner,workspace,recipe,step)
            raise
        state=await self.runner.state(unit)
        if state not in ('absent','stopped') or not UnitRunner.launch_finished(workspace,unit):
            raise UnitError('launch_unknown','Launch completion has not been proven')
        settled=await asyncio.to_thread(self.store.settle_step,job_id,run_token,unit,completed=True)
        if settled.state=='cancel_requested':
            return await self._cancelled(settled)
        return {'unit':unit,'reused':result['status']['reused'],'state':'completed'}
