from pathlib import Path
import asyncio
import importlib.util
import json
import sys

import pytest

spec = importlib.util.spec_from_file_location('shutdown_backend', Path(__file__).resolve().parents[1] / 'services/local_backend.py')
backend = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = backend
spec.loader.exec_module(backend)


@pytest.mark.asyncio
async def test_unknown_stop_state_retains_unit(tmp_path, monkeypatch):
    manager = backend.LocalBackend(tmp_path)
    unit = 'maibot-sing-' + 'a' * 32
    manager._units.add(unit)
    class Process:
        def __init__(self, returncode):
            self.returncode = returncode
        async def wait(self):
            return self.returncode
        async def communicate(self):
            return b'active\n', b''
    async def systemctl(*args, **kwargs):
        return Process(1 if 'stop' in args else 0)
    monkeypatch.setattr(backend.asyncio, 'create_subprocess_exec', systemctl)
    with pytest.raises(RuntimeError, match='无法确认 worker'):
        await manager._stop_unit(unit)
    assert unit in manager._units
    manager._units.clear()  # No real subprocess was created.
    await manager.close()


@pytest.mark.asyncio
async def test_worker_stop_failure_preserves_scratch_and_ownership(tmp_path, monkeypatch):
    model, index = tmp_path / 'model', tmp_path / 'index'
    model.write_bytes(b'model')
    index.write_bytes(b'index')
    manager = backend.LocalBackend(tmp_path / 'library', model=model, index=index,
        worker_python=Path(sys.executable), musicdl_python=Path(sys.executable),
        rvc_script=Path(__file__).resolve(), inference_lock=tmp_path / '.inference.lock')
    class FailedWorker:
        returncode = 1
        async def communicate(self):
            return b'failed', b''
    async def launch(*args, **kwargs):
        return FailedWorker()
    async def cannot_stop(unit):
        raise RuntimeError('cannot confirm worker termination')
    monkeypatch.setattr(backend.asyncio, 'create_subprocess_exec', launch)
    monkeypatch.setattr(manager, '_stop_unit', cannot_stop)
    with pytest.raises(RuntimeError, match='cannot confirm'):
        await manager.cover('Song', 'Artist')
    assert len(list((manager.output / '.scratch').iterdir())) == 1
    assert manager._units and manager._owner is not None
    record = next((manager.output / 'jobs').glob('*.json'))
    assert json.loads(record.read_text())['status'] == 'failed'
    with pytest.raises(RuntimeError, match='cannot confirm'):
        await manager.close()
    assert manager._owner is not None
    manager._units.clear()  # Test doubles only; no live worker exists.
    await manager.close()
