"""Launch one bounded media stage and reconcile its named systemd user unit.

Never sends messages, loads models or erases scratch. An absent receipt after a
worker kill is a failure requiring explicit reconciliation, not a cache miss.
"""
from pathlib import Path
from typing import Any, Dict, Optional
import asyncio
import json
import os
import re
import uuid

from ..runtime.stage_receipts import StageReceipts, CheckpointError

_UNIT = re.compile(r'maibot-sing-[0-9a-f]{32}-[a-z][a-z0-9_-]{0,63}\Z')


class UnitError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _safe_unit(name: str) -> str:
    if not isinstance(name, str) or not _UNIT.fullmatch(name):
        raise ValueError('Invalid owned user-service name')
    return name + '.service'


def save_plan(path: Path, *, workspace: Path, recipe: str,
              inference_lock: Path, steps: tuple) -> Path:
    """Write-once private job plan; no signed URLs or account secrets included."""
    if not path.is_absolute() or not workspace.is_absolute() or not inference_lock.is_absolute():
        raise ValueError('Plan, workspace and lock require absolute paths')
    StageReceipts._safe(path)
    StageReceipts(workspace, recipe)
    data = {'schema': 1, 'workspace': str(workspace), 'recipe': recipe,
            'inference_lock': str(inference_lock), 'steps': [
                {'name': s.name, 'argv': list(s.argv), 'timeout_s': s.timeout_s,
                 'inputs': list(s.inputs), 'outputs': list(s.outputs)} for s in steps]}
    raw=json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
    if len(raw)>65536:
        raise ValueError('Stage plan exceeds 64KiB')
    if path.exists() or path.is_symlink():
        if path.read_bytes()!=raw:
            raise UnitError('plan_conflict','A different stage plan already exists; manual reconciliation required')
        return path
    staging=path.with_name(path.name+'.'+uuid.uuid4().hex+'.part')
    try:
        with staging.open('xb') as output:
            os.chmod(staging,0o600)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.link(staging,path)
        parent=os.open(path.parent,os.O_DIRECTORY|os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        staging.unlink(missing_ok=True)
    return path


class UnitRunner:
    def __init__(self, stage_executor: Path, python: Path):
        if (not stage_executor.is_absolute() or not stage_executor.is_file() or
                stage_executor.is_symlink() or not python.is_absolute() or not python.is_file()):
            raise ValueError('Configured stage executor and Python must be existing files')
        # A venv/bin/python is normally a symlink to its interpreter; this is
        # an administrator-supplied path, unlike the untrusted job workspace.
        self.executor=stage_executor
        self.python=python

    @staticmethod
    def name(run_token: str, step: str) -> str:
        if not re.fullmatch('[0-9a-f]{32}',run_token) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}',step):
            raise ValueError('Invalid run token or step')
        unit='maibot-sing-'+run_token+'-'+step
        _safe_unit(unit)
        return unit

    async def state(self, unit: str) -> str:
        """Do not treat a systemctl error as proof the service stopped."""
        name=_safe_unit(unit)
        proc=await asyncio.create_subprocess_exec('systemctl','--user','show',name,
            '-p','LoadState','-p','ActiveState','--no-pager',
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        try:
            raw,_=await asyncio.wait_for(proc.communicate(),10)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise UnitError('unit_status_unknown','Cannot confirm service inactivity') from exc
        if proc.returncode:
            raise UnitError('unit_status_unknown','Cannot query service state')
        data=dict(line.split('=',1) for line in raw.decode('utf-8','replace').splitlines() if '=' in line)
        active=data.get('ActiveState')
        if active in ('active','activating','deactivating','reloading'):
            return 'active'
        if data.get('LoadState')=='not-found' and active=='inactive':
            return 'absent'
        if active in ('inactive','failed'):
            return 'stopped'
        raise UnitError('unit_status_unknown','Unexpected systemd service state')

    async def run(self, unit: str, plan_path: Path, step, workspace: Path) -> Dict[str, Any]:
        """A confirmed-success receipt is necessary even when systemd-run exits 0.

        The scheduler must persist (job,run_token,step,unit) BEFORE calling run.
        Cancellation deliberately does not kill the independent user service;
        the reconciler must inspect it on reload before any retry.
        """
        name=_safe_unit(unit)
        if not plan_path.is_absolute() or not plan_path.is_file() or not workspace.is_absolute():
            raise ValueError('Private plan or workspace missing')
        if type(step.timeout_s) is not int or not 1<=step.timeout_s<=600:
            raise ValueError('Stage runtime must be bounded')
        prior=await self.state(unit)
        if prior!='absent':
            raise UnitError('unit_reconcile_required',
                'Existing unit must be reconciled before launch: '+prior)
        logs=workspace/'unit-logs'
        logs.mkdir(mode=0o700,exist_ok=True)
        StageReceipts._safe(logs)
        logfile=logs/(unit+'.log')
        fd=os.open(logfile,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            argv=['systemd-run','--user','--wait','--pipe','--collect','--unit',unit,
                '-p','MemoryMax=4G','-p','MemoryHigh=3G','-p','MemorySwapMax=0',
                '-p','CPUQuota=150%','-p','TasksMax=64','-p','TimeoutStopSec=15',
                '-p','RuntimeMaxSec='+str(step.unit_limit_s),
                str(self.python),str(self.executor),'--plan',str(plan_path),'--stage',step.name]
            proc=await asyncio.create_subprocess_exec(*argv,stdout=fd,stderr=fd)
        finally:
            os.close(fd)
        try:
            code=await asyncio.wait_for(proc.wait(),step.unit_limit_s+25)
        except asyncio.TimeoutError as exc:
            # The transient unit may still run; do not release ownership or
            # clean scratch. Query state in reconciliation, not via a guessed PID.
            raise UnitError('unit_wait_unknown','Service wait ended without proving worker stopped') from exc
        status=workspace/'.receipts'/(step.name+'.status.json')
        if not status.is_file() or status.is_symlink() or status.stat().st_size>4096:
            raise UnitError('unit_result_missing','Unit ended but did not write a valid stage result')
        try:
            data=json.loads(status.read_text(encoding='utf-8'))
        except (OSError,ValueError) as exc:
            raise UnitError('unit_result_invalid','Stage result could not be decoded') from exc
        if code or data.get('state')!='completed' or data.get('stage')!=step.name:
            raise UnitError(str(data.get('code') or 'unit_failed'),
                'Stage %s failed; detail is in its bounded local job status' % step.name)
        try:
            document=json.loads(plan_path.read_text(encoding='utf-8'))
            if document.get('workspace')!=str(workspace):
                raise UnitError('plan_mismatch','Plan workspace changed during execution')
            receipts=StageReceipts(workspace,document['recipe'])
            current=receipts._files(list(step.inputs)) if step.inputs else {}
            inputs={name:record['sha256'] for name,record in current.items()}
            verified=receipts.verify(step.name,inputs)
            if verified is None or set(verified['outputs'])!=set(step.outputs):
                raise UnitError('receipt_missing','Completed stage has no matching artifact receipt')
        except (KeyError,TypeError,ValueError,CheckpointError) as exc:
            raise UnitError('receipt_invalid','Completed stage receipt cannot be trusted') from exc
        return {'unit':unit,'status':data,'log_path':logfile}
