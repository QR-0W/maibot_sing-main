"""Run one finite media step within a verified systemd service and shared lock.

Only the scheduler creates private plan files. This module does not accept chat
arguments as process argv. Systemd must limit the entire process cgroup; this
supervisor reports a typed failure if a child times out before its unit cap.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid

from stage_receipts import StageReceipts, CheckpointError


class StageFailure(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def atomic_status(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def stop_group(child):
    """Never release the lock while a launched process group may still compute."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # The parent can have exited while a grandchild in its group remains.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait(timeout=5)


def execute(root, recipe, stage, argv, inputs, outputs, *, timeout_s, lock_path, lock_wait_s=10):
    """Run a step or verify its receipt; never overwrite unsealed evidence.

    inputs/outputs are relative file names. Input hashes are calculated inside
    the lock, not trusted from an old plan. A failed step with partial outputs
    requires a new attempt workspace; no input or previous receipt is deleted.
    """
    if (type(timeout_s) is not int or not 1 <= timeout_s <= 600 or
            type(lock_wait_s) is not int or not 0 <= lock_wait_s <= 30):
        raise ValueError('Invalid finite execution deadline')
    if not isinstance(argv, (list, tuple)) or not argv or not all(
            isinstance(x, str) and '\x00' not in x for x in argv):
        raise ValueError('Invalid private execution argv')
    if not isinstance(inputs, (list, tuple)) or not isinstance(outputs, (list, tuple)) or not outputs:
        raise ValueError('Invalid file lists')
    receipts = StageReceipts(root, recipe)
    receipt_path = receipts._receipt(stage)
    status = receipt_path.with_suffix('.status.json')
    started = time.monotonic()

    def report(state, **extra):
        value = {'stage': stage, 'state': state, 'recipe': recipe,
                 'unit': os.environ.get('MAIBOT_SING_UNIT'),
                 'elapsed_s': round(time.monotonic()-started, 3), **extra}
        atomic_status(status, value)
        return value

    lock_path = Path(lock_path)
    if not lock_path.is_absolute() or not lock_path.parent.is_dir():
        raise ValueError('Shared lock must have an existing absolute parent')
    receipts._safe(lock_path)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    child = None
    try:
        report('waiting_for_lock')
        deadline = time.monotonic() + lock_wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StageFailure('inference_busy', 'Inference lock busy; no stage launched')
                time.sleep(.1)
        # Shared input files are checked within the service after acquiring
        # the inference lock; resume verifies their current hashes.
        input_fingerprints = receipts._files(list(inputs)) if inputs else {}
        input_fingerprints = {name: item['sha256'] for name, item in input_fingerprints.items()}
        existing = receipts.verify(stage, input_fingerprints)
        if existing is not None:
            if set(existing['outputs']) != set(outputs):
                raise CheckpointError('Completed stage output contract changed')
            return report('completed', reused=True)
        # _files validates paths before a child can write, but absent targets
        # need their own validation without requiring an existing file.
        for name in outputs:
            if (not isinstance(name, str) or not name or
                    not __import__('re').fullmatch('[a-zA-Z0-9][a-zA-Z0-9_./-]{0,159}', name) or
                    any(part in ('', '.', '..', '.receipts') for part in name.split('/')) or
                    name.endswith('.part')):
                raise CheckpointError('Unsafe output path')
            target = Path(root) / name
            receipts._safe(target)
            if target.exists() and not (stage == 'validate' and name in inputs):
                raise StageFailure('unsealed_output', 'Existing unsealed output preserved; use a fresh attempt')
        report('running')
        child = subprocess.Popen(list(argv), cwd=root, start_new_session=True,
                                 stdin=subprocess.DEVNULL)
        try:
            code = child.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            raise StageFailure('stage_timeout', 'Stage timed out before unit hard limit')
        if code:
            raise StageFailure('stage_exit', 'Stage process returned ' + str(code))
        stop_group(child)
        child = None
        receipts.seal(stage, input_fingerprints, list(outputs))
        return report('completed', reused=False)
    except BaseException as exc:
        if child is not None:
            stop_group(child)
        report('failed', code=getattr(exc, 'code',
               'checkpoint_error' if isinstance(exc, CheckpointError) else 'executor_error'),
               message=str(exc)[:400])
        raise
    finally:
        os.close(fd)


def main():
    from worker import verify_limits
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--stage', required=True)
    args = parser.parse_args()
    verify_limits()
    StageReceipts._safe(args.plan)
    if args.plan.stat().st_size > 65536:
        raise ValueError('Oversized private plan')
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    step = next(item for item in plan['steps'] if item['name'] == args.stage)
    def interrupted(signum, frame):
        raise StageFailure('stage_terminated', 'Service termination requested')
    signal.signal(signal.SIGTERM, interrupted)
    execute(Path(plan['workspace']), plan['recipe'], step['name'], step['argv'],
            step['inputs'], step['outputs'], timeout_s=step['timeout_s'],
            lock_path=plan['inference_lock'])


if __name__ == '__main__':
    main()
