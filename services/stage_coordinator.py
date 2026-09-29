"""Join SQLite job ownership to per-step systemd execution and receipts.

All work happens after the command RPC returns. A plugin reload may leave a
user service running; reconciliation never launches another unit while the old
identity is active or unknown. No automatic retry of incomplete media occurs.
"""
from pathlib import Path
from typing import Dict
import asyncio

from ..runtime.stage_receipts import CheckpointError, StageReceipts
from .job_store import JobConflict, JobStore
from .unit_runner import UnitError, UnitRunner


class StageCoordinator:
    def __init__(self, store: JobStore, runner: UnitRunner):
        self.store=store
        self.runner=runner

    async def _verified(self, workspace: Path, recipe: str, step) -> bool:
        def check() -> bool:
            receipts=StageReceipts(workspace,recipe)
            inputs=receipts._files(list(step.inputs)) if step.inputs else {}
            hashes={name:record['sha256'] for name,record in inputs.items()}
            value=receipts.verify(step.name,hashes)
            return value is not None and set(value['outputs'])==set(step.outputs)
        return await asyncio.to_thread(check)

    async def run_step(self, job_id: str, stream_id: str, run_token: str,
                       step, workspace: Path, plan: Path, recipe: str) -> Dict:
        job=await asyncio.to_thread(self.store.get,job_id,stream_id)
        if job.run_token!=run_token or job.state!='running':
            raise JobConflict('Caller no longer owns this running job')
        owner=await asyncio.to_thread(self.store.claim_step,job_id,run_token,step.name)
        unit=owner.unit_name
        attempts=await asyncio.to_thread(self.store.stage_attempts,job_id,stream_id)
        current=next((item for item in attempts if item['unit_name']==unit),None)
        if current is None:
            raise JobConflict('Stage has no durable unit ownership record')
        if current['status']=='completed':
            if not await self._verified(workspace,recipe,step):
                raise UnitError('receipt_invalid','Previously completed artifact changed')
            return {'unit':unit,'reused':True,'state':'completed'}
        if current['status']!='claimed':
            raise JobConflict('Interrupted stage cannot be auto-restarted')
        log=workspace/'unit-logs'/(unit+'.log')
        state=await self.runner.state(unit)
        if state=='active':
            return {'unit':unit,'state':'running'}
        if log.exists() or log.is_symlink():
            # Unit already launched. On restart the wrapper may be gone while
            # its receipt was atomically committed. Trust bytes, not return text.
            if await self._verified(workspace,recipe,step):
                await asyncio.to_thread(self.store.settle_step,job_id,run_token,unit,completed=True)
                return {'unit':unit,'reused':True,'state':'completed'}
            if state not in ('stopped','absent'):
                raise UnitError('unit_status_unknown','Previous stage cannot be reconciled')
            await asyncio.to_thread(self.store.settle_step,job_id,run_token,unit,completed=False)
            raise UnitError('stage_interrupted','Previous worker stopped without verified output; no automatic retry')
        if state!='absent':
            raise UnitError('unit_status_unknown','Claimed unit might still be running')
        try:
            result=await self.runner.run(unit,plan,step,workspace)
        except UnitError:
            # Do not settle an unknown unit; old worker may still be running.
            state=await self.runner.state(unit)
            if state in ('stopped','absent'):
                if await self._verified(workspace,recipe,step):
                    await asyncio.to_thread(self.store.settle_step,job_id,run_token,unit,completed=True)
                else:
                    await asyncio.to_thread(self.store.settle_step,job_id,run_token,unit,completed=False)
            raise
        await asyncio.to_thread(self.store.settle_step,job_id,run_token,unit,completed=True)
        return {'unit':unit,'reused':result['status']['reused'],'state':'completed'}
