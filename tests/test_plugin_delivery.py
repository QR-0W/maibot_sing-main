"""SDK-mocked at-most-once voice delivery; never contact QQ."""
from pathlib import Path
from types import SimpleNamespace
import asyncio
import sqlite3

import pytest

from test_plugin_async_lifecycle import plugin, command


@pytest.mark.asyncio
async def test_unknown_sdk_false_never_retries_and_artifact_remains(plugin, tmp_path):
    instance, sends = plugin
    await instance.on_load()
    try:
        assert (await instance.handle_cover_command(**command()))[0]
        job_id = sends[-1][0].split('任务 ')[1].split('：')[0]
        offer = instance._jobs.store.choices(job_id, 'stream-1')
        await instance._jobs.close()  # never run a real systemd unit in this suite
        instance._delivery_task.cancel()
        await asyncio.gather(instance._delivery_task, return_exceptions=True)
        instance._jobs.store.select(job_id, 'stream-1', offer['offer_id'], 1)
        # Simulate a verified committed artifact without running actual inference.
        artifact = tmp_path / 'permanent.mp3'
        artifact.write_bytes(b'permanent immutable audio')
        with sqlite3.connect(instance._jobs.store.path) as db:
            db.execute("UPDATE jobs SET state='ready',artifact_key=? WHERE id=?", ('a'*64, job_id))
        calls = []
        async def sdk_false(*args, **kwargs):
            calls.append((args, kwargs))
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
async def test_sdk_sent_needs_message_id_and_is_never_duplicate(plugin, tmp_path):
    instance, sends = plugin
    await instance.on_load()
    try:
        assert (await instance.handle_cover_command(**command()))[0]
        job_id = sends[-1][0].split('任务 ')[1].split('：')[0]
        offer = instance._jobs.store.choices(job_id, 'stream-1')
        await instance._jobs.close()
        instance._delivery_task.cancel()
        await asyncio.gather(instance._delivery_task, return_exceptions=True)
        instance._jobs.store.select(job_id, 'stream-1', offer['offer_id'], 1)
        audio = tmp_path / 'permanent.mp3'
        audio.write_bytes(b'cover')
        with sqlite3.connect(instance._jobs.store.path) as db:
            db.execute("UPDATE jobs SET state='ready',artifact_key=? WHERE id=?", ('b'*64, job_id))
        called = []
        async def sdk_ack(*args, **kwargs):
            called.append(1)
            return {'sent': True, 'message_id': 'qq-message-123'}
        instance._outbox._sender = SimpleNamespace(send=sdk_ack)
        instance._outbox._validate_artifact = lambda key: SimpleNamespace(path=audio)
        result = await instance._outbox.dispatch(job_id, 'stream-1')
        assert (result.delivery_state, result.message_id) == ('sent', 'qq-message-123')
        assert await instance._outbox.dispatch(job_id, 'stream-1') is None
        assert len(called) == 1
    finally:
        await instance.on_unload()
