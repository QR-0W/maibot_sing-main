"""Offline single-attempt speech tests; no QQ RPC is invoked."""
from pathlib import Path
import asyncio
import importlib
import importlib.util
import math
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1] / 'services'
spec = importlib.util.spec_from_file_location(
    'voice_test_pkg', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
SingleVoiceSender = importlib.import_module('voice_test_pkg.voice_sender').SingleVoiceSender


def voice_file(tmp_path):
    path = tmp_path / 'speech.wav'
    path.write_bytes(b'fake speech bytes')
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize('result,expected', [
    ({'sent': True, 'message_id': 'platform-id'}, 'sent'),
    ({'sent': True, 'message_id': None}, 'unknown'),
    ({'sent': False, 'message_id': None}, 'unknown'),
    (False, 'unknown'),
])
async def test_exactly_one_detailed_voiceurl_attempt(tmp_path, result, expected):
    calls = []
    async def fake_custom(*args, **kwargs):
        calls.append((args, kwargs))
        return result
    sender = SingleVoiceSender(fake_custom, rpc_timeout_ms=1234)
    receipt = await sender.send_file(voice_file(tmp_path), 'stream-1')
    assert receipt.outcome == expected
    assert calls == [(('voiceurl', {'url': (tmp_path / 'speech.wav').as_uri()}, 'stream-1'),
                      {'return_details': True, 'timeout_ms': 1234})]


@pytest.mark.asyncio
async def test_post_platform_host_store_exception_or_raise_never_falls_back(tmp_path):
    calls = []
    async def fake_custom(*args, **kwargs):
        calls.append(args[0])
        return False  # Host caught a post-platform store/hook exception.
    sender = SingleVoiceSender(fake_custom)
    assert (await sender.send_file(voice_file(tmp_path), 'stream')).outcome == 'unknown'
    assert calls == ['voiceurl']
    async def raising(*args, **kwargs):
        calls.append(args[0]); raise RuntimeError('post-send hook failed')
    sender = SingleVoiceSender(raising)
    assert (await sender.send_file(voice_file(tmp_path), 'stream')).outcome == 'unknown'
    assert calls == ['voiceurl', 'voiceurl']  # one per independent request


@pytest.mark.asyncio
async def test_timeout_observes_late_ack_without_sending_text(tmp_path):
    gate = asyncio.Event()
    seen = []
    calls = []
    async def fake_custom(*args, **kwargs):
        calls.append(args[0]); await gate.wait()
        return {'sent': True, 'message_id': 'late-id'}
    sender = SingleVoiceSender(fake_custom, acknowledgement_timeout_s=0.01,
                               late_receipt=seen.append)
    assert (await sender.send_file(voice_file(tmp_path), 'stream')).outcome == 'unknown'
    gate.set()
    await asyncio.wait_for(asyncio.gather(*tuple(sender._watchers)), 1)
    assert [(item.outcome, item.message_id) for item in seen] == [('sent', 'late-id')]
    assert calls == ['voiceurl']


@pytest.mark.asyncio
async def test_cancellation_and_shutdown_are_bounded_and_never_retry(tmp_path):
    started = asyncio.Event()
    calls = []
    async def fake_custom(*args, **kwargs):
        calls.append(args[0]); started.set()
        await asyncio.Event().wait()
    sender = SingleVoiceSender(fake_custom)
    attempt = asyncio.create_task(sender.send_file(voice_file(tmp_path), 'stream'))
    await started.wait()
    attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(attempt, 0.2)
    await asyncio.wait_for(sender.shutdown(timeout_s=0.01), 0.2)
    assert calls == ['voiceurl']
    with pytest.raises(RuntimeError, match='closed'):
        await sender.send_file(voice_file(tmp_path), 'stream')


@pytest.mark.asyncio
async def test_symlink_rejected_and_shutdown_during_file_validation(tmp_path, monkeypatch):
    path = voice_file(tmp_path)
    alias = tmp_path / 'alias.wav'
    alias.symlink_to(path)
    calls = []
    async def fake_custom(*args, **kwargs):
        calls.append(args)
        return {'sent': True, 'message_id': 'accepted'}
    sender = SingleVoiceSender(fake_custom)
    with pytest.raises(ValueError, match='regular'):
        await sender.send_file(alias, 'stream')
    assert not calls
    started = asyncio.Event()
    release = asyncio.Event()
    original = asyncio.to_thread
    async def delayed_stat(fn, *args, **kwargs):
        started.set()
        await release.wait()
        return await original(fn, *args, **kwargs)
    module = importlib.import_module('voice_test_pkg.voice_sender')
    monkeypatch.setattr(module.asyncio, 'to_thread', delayed_stat)
    pending = asyncio.create_task(sender.send_file(path, 'stream'))
    await started.wait()
    await sender.shutdown(timeout_s=0)
    release.set()
    with pytest.raises(RuntimeError, match='closed'):
        await pending
    assert not calls


@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), True, -1, 11])
def test_shutdown_timeout_bounds(invalid):
    async def fake_custom(*args, **kwargs):
        return False
    async def run():
        with pytest.raises(ValueError, match='shutdown timeout'):
            await SingleVoiceSender(fake_custom).shutdown(timeout_s=invalid)
    asyncio.run(run())


@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), True, 0, 121])
def test_ack_deadline_bounds(invalid):
    async def fake_custom(*args, **kwargs):
        return False
    with pytest.raises(ValueError, match='acknowledgement timeout'):
        SingleVoiceSender(fake_custom, acknowledgement_timeout_s=invalid)
