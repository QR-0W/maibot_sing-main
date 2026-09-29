from pathlib import Path
import asyncio
import importlib.util
import json
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sing_local_backend', ROOT / 'services/local_backend.py')
backend = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = backend
spec.loader.exec_module(backend)


def fake_runtime_paths(tmp_path):
    """Mocked subprocess tests still supply explicit, existing admin runtime paths."""
    return {'worker_python': Path(sys.executable), 'musicdl_python': Path(sys.executable),
            'rvc_script': Path(__file__).resolve(), 'inference_lock': tmp_path / '.inference.lock'}


def test_source_identity_and_parameters_change_key():
    source = {'type': 'musicdl-native', 'title': 'song', 'artist': 'artist'}
    first = backend.cache_key(source, 'model1', 'index1', False)
    assert first != backend.cache_key({**source, 'artist': 'other'}, 'model1', 'index1', False)
    assert first != backend.cache_key(source, 'model2', 'index1', False)
    assert first != backend.cache_key(source, 'model1', 'index2', False)
    assert first != backend.cache_key(source, 'model1', 'index1', True)
    assert backend.PARAMETERS['protect'] == .33
    assert backend.PARAMETERS['seed'] == 20260928


@pytest.mark.parametrize('value', ['../hi', '/etc/passwd', '-h', 'a\\b', 'a\nb', ''])
def test_unsafe_titles_rejected(value):
    with pytest.raises(ValueError):
        backend.safe_text(value)


def test_atomic_metadata(tmp_path):
    path = tmp_path / 'metadata.json'
    backend.atomic_json(path, {'status': 'completed'})
    assert json.loads(path.read_text())['status'] == 'completed'
    assert list(tmp_path.glob('*.tmp')) == []


@pytest.mark.asyncio
async def test_dedup_queue_full_and_send_failure_preserves_output(tmp_path, monkeypatch):
    model, index = tmp_path / 'model.pth', tmp_path / 'model.index'
    model.write_bytes(b'model')
    index.write_bytes(b'index')
    manager = backend.LocalBackend(tmp_path / 'covers', model=model, index=index, max_queue=0)
    gate = asyncio.Event()
    calls = []
    async def fake_execute(key, title, artist, *args):
        calls.append(key)
        await gate.wait()
        folder = manager.output / key
        folder.mkdir(parents=True)
        path = folder / 'cover.mp3'
        path.write_bytes(b'permanent-cover')
        return backend.CoverResult(path, title, artist, key)
    monkeypatch.setattr(manager, '_execute', fake_execute)
    one = asyncio.create_task(manager.cover('Song', 'Artist'))
    await asyncio.sleep(.05)
    two = asyncio.create_task(manager.cover('Song', 'Artist'))
    await asyncio.sleep(.05)
    with pytest.raises(RuntimeError, match='队列已满'):
        await manager.cover('Other', 'Artist')
    gate.set()
    a, b = await asyncio.gather(one, two)
    assert a.path == b.path and len(calls) == 1
    assert json.loads((manager.output / 'jobs' / (a.key + '.json')).read_text())['status'] == 'completed'
    # Sending is downstream; no send exception may delete the completed file.
    async def failed_send(_):
        raise RuntimeError('network failed')
    with pytest.raises(RuntimeError):
        await failed_send(a.path)
    assert a.path.read_bytes() == b'permanent-cover'
    await manager.close()


@pytest.mark.asyncio
async def test_close_cancels_running_and_no_untrusted_unit_stop(tmp_path, monkeypatch):
    model, index = tmp_path / 'm', tmp_path / 'i'
    model.write_bytes(b'm')
    index.write_bytes(b'i')
    manager = backend.LocalBackend(tmp_path / 'covers', model=model, index=index)
    entered = asyncio.Event()
    async def fake_execute(*args):
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr(manager, '_execute', fake_execute)
    task = asyncio.create_task(manager.cover('Song', 'Artist'))
    await entered.wait()
    await manager.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    await manager._stop_unit('foreign-service')
    assert not manager._units


def test_no_output_cleanup_or_shell_execution():
    text = (ROOT / 'services/local_backend.py').read_text()
    assert 'create_subprocess_exec(*command' in text
    assert 'shutil.rmtree, scratch' in text
    assert 'shutil.rmtree, final' not in text
    assert "'MemoryMax=4G'" in text
    assert "'MemorySwapMax=0'" in text
    worker = (ROOT / 'runtime/worker.py').read_text()
    assert 'fcntl.flock(lock, fcntl.LOCK_EX)' in worker
    assert "if not os.environ.get('INVOCATION_ID')" in worker
    assert '_parsewiththirdpartapis = MethodType(native_only' in worker
    plugin = (ROOT / 'plugin.py').read_text()
    assert 'auto_start: bool = Field(default=False' in plugin
    assert 'os.kill(' not in plugin


@pytest.mark.asyncio
async def test_publish_keeps_only_song_and_metadata(tmp_path, monkeypatch):
    """A finished cover must not retain the original, stems or worker scratch."""
    model, index, original = (tmp_path / name for name in ('model.pth', 'model.index', 'source.mp3'))
    model.write_bytes(b'model')
    index.write_bytes(b'index')
    original.write_bytes(b'test original')
    manager = backend.LocalBackend(tmp_path / 'library', model=model, index=index,
                                   allowlist=(original,), **fake_runtime_paths(tmp_path))

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b'ok', b''

    async def run_worker(*arguments, **kwargs):
        scratch = Path(arguments[arguments.index('--scratch') + 1])
        (scratch / 'cover.mp3').write_bytes(b'B' * 2048)
        (scratch / 'result.json').write_text(json.dumps({'verified_source': {'source': 'allowlist'}}))
        (scratch / 'original.wav').write_bytes(b'original stem')
        (scratch / 'backing.wav').write_bytes(b'accompaniment stem')
        return FakeProcess()

    monkeypatch.setattr(backend.asyncio, 'create_subprocess_exec', run_worker)
    try:
        result = await manager.cover('Song', 'Artist', source_file=original, instrumental=True)
        assert result.path.read_bytes() == b'B' * 2048
        assert {item.name for item in result.path.parent.iterdir()} == {'cover.mp3', 'metadata.json'}
        assert list((manager.output / '.scratch').iterdir()) == []
        metadata = json.loads((result.path.parent / 'metadata.json').read_text())
        assert metadata['sha256'] == backend.digest_file(result.path)
        repeated = await manager.cover('Song', 'Artist', source_file=original, instrumental=True)
        assert repeated.path == result.path
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_library_exclusive_owner_and_release(tmp_path):
    one, two = backend.LocalBackend(tmp_path), backend.LocalBackend(tmp_path)
    await one.start()
    with pytest.raises(RuntimeError, match='另一个本地后端'):
        await two.start()
    await one.close()
    await two.start()
    await two.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('active', [False, True])
async def test_recover_never_deletes_live_scratch_or_final(tmp_path, monkeypatch, active):
    key, uid = 'a' * 64, 'b' * 32
    scratch = tmp_path / '.scratch' / (key + '-' + uid)
    scratch.mkdir(parents=True)
    (scratch / 'temporary.wav').write_bytes(b'temporary')
    final = tmp_path / key
    final.mkdir()
    (final / 'cover.mp3').write_bytes(b'permanent')
    jobs = tmp_path / 'jobs'
    jobs.mkdir()
    record = jobs / (key + '.json')
    backend.atomic_json(record, {'request_key': key, 'status': 'processing',
        'unit': 'maibot-sing-' + uid, 'scratch': scratch.name})
    class Process:
        returncode = 0 if active else 4
        async def communicate(self):
            return (b'active\n' if active else b'inactive\n'), b''
    async def systemctl(*args, **kwargs):
        assert args == ('systemctl', '--user', 'is-active', 'maibot-sing-' + uid + '.service')
        return Process()
    monkeypatch.setattr(backend.asyncio, 'create_subprocess_exec', systemctl)
    manager = backend.LocalBackend(tmp_path)
    try:
        if active:
            with pytest.raises(RuntimeError, match='遗留 worker'):
                await manager.start()
            assert scratch.exists()
        else:
            await manager.start()
            assert not scratch.exists()
            assert json.loads(record.read_text())['status'] == 'interrupted'
        assert (final / 'cover.mp3').read_bytes() == b'permanent'
    finally:
        await manager.close()
