"""SDK-mocked at-most-once voice delivery; never contact QQ."""
from types import SimpleNamespace
import asyncio
import sqlite3

import pytest

from test_plugin_async_lifecycle import plugin, command


async def admit(instance, sends, entry):
    """Admit one cover through the real entry for that ingress kind.

    ``/翻唱`` is the only Command text; natural conversation goes through the
    structured Tool, and the two keep independent render/delivery defaults.
    """
    async def idle():
        return None
    instance._jobs.run_once = idle
    if entry == 'command':
        assert (await instance.handle_cover_command(**command(text='/翻唱 Song - Artist')))[0]
        job_id = sends[-1][0].split('任务 ')[1].split('：')[0]
        mode, consent = 'file', 'platform-message-1'
    else:
        result = await instance.handle_cover_tool(
            title='Song', artist='Artist', stream_id='stream-1',
            chat_id='stream-1', platform='qq', user_id='user-1')
        assert result['success'] is True and result['state'] == 'queued'
        job_id, mode, consent = result['job_id'], 'voice', None
    admitted = instance._jobs.store.get(job_id, 'stream-1')
    assert admitted.request['entry_kind'] == entry
    assert admitted.request['delivery_mode'] == mode
    assert admitted.request['auto_reply'] is True and admitted.consent_event
    if consent is not None:
        assert admitted.consent_event == consent
    assert admitted.delivery_state == 'pending'
    return job_id, mode


async def prepare_ready(instance, job_id, tmp_path, marker, payload):
    """Select the offered version, then publish a fake already-verified artifact."""
    offer = instance._jobs.store.choices(job_id, 'stream-1')
    await instance._jobs.close()  # never run a real systemd unit in this suite
    instance._delivery_task.cancel()
    await asyncio.gather(instance._delivery_task, return_exceptions=True)
    current = instance._jobs.store.get(job_id, 'stream-1')
    if current.state == 'needs_selection':
        instance._jobs.store.select(job_id, 'stream-1', offer['offer_id'], 1)
    else:
        assert current.state == 'queued' and current.selected_source['track_id'] == 'id-1'
    artifact = tmp_path / 'permanent.mp3'
    artifact.write_bytes(payload)
    with sqlite3.connect(instance._jobs.store.path) as db:
        db.execute("UPDATE jobs SET state='ready',artifact_key=? WHERE id=?", (marker, job_id))
    return artifact


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['command', 'tool'])
async def test_unknown_sdk_false_never_retries_and_artifact_remains(plugin, tmp_path, entry):
    instance, sends = plugin
    await instance.on_load()
    try:
        job_id, mode = await admit(instance, sends, entry)
        artifact = await prepare_ready(instance, job_id, tmp_path, 'a' * 64,
                                       b'permanent immutable audio')
        calls = []
        async def sdk_false(*args, **kwargs):
            calls.append((args, kwargs))
            assert args[0].request['delivery_mode'] == mode
            assert args[0].stream_id == 'stream-1'
            return {'sent': False, 'message_id': None}
        instance._outbox._sender = SimpleNamespace(send=sdk_false)
        instance._outbox._validate_artifact = lambda key: SimpleNamespace(path=artifact)
        first = await instance._outbox.dispatch(job_id, 'stream-1')
        assert first.delivery_state == 'unknown'
        assert await instance._outbox.dispatch(job_id, 'stream-1') is None
        assert len(calls) == 1
        assert artifact.read_bytes() == b'permanent immutable audio'
        assert '未知' in instance._job_status(first)
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['command', 'tool'])
async def test_sdk_sent_needs_message_id_and_is_never_duplicate(plugin, tmp_path, entry):
    instance, sends = plugin
    await instance.on_load()
    try:
        job_id, mode = await admit(instance, sends, entry)
        audio = await prepare_ready(instance, job_id, tmp_path, 'b' * 64, b'cover')
        called = []
        async def sdk_ack(*args, **kwargs):
            called.append((args, kwargs))
            assert args[0].request['delivery_mode'] == mode
            return {'sent': True, 'message_id': 'qq-message-123'}
        instance._outbox._sender = SimpleNamespace(send=sdk_ack)
        instance._outbox._validate_artifact = lambda key: SimpleNamespace(path=audio)
        result = await instance._outbox.dispatch(job_id, 'stream-1')
        assert (result.delivery_state, result.message_id) == ('sent', 'qq-message-123')
        assert await instance._outbox.dispatch(job_id, 'stream-1') is None
        assert len(called) == 1
    finally:
        await instance.on_unload()
