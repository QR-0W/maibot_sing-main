"""Real entry/notice ledger boundary; fake text only, no QQ or media execution."""
import asyncio
import importlib
import sqlite3

import pytest

from test_plugin_async_lifecycle import plugin, plugin_module

source_offer = importlib.import_module('sing_async_test.services.source_offer')


async def idle_scheduler(self):
    return None


async def idle_delivery():
    await asyncio.Event().wait()


def failed_job(instance, *, authorized=True):
    store = instance._jobs.store
    request = {'platform': 'qq', 'user_id': '233333', 'auto_reply': authorized,
               'ingress_proof': plugin_module.TRUSTED_QQ_INGRESS,
               'render_mode': 'excerpt', 'instrumental': False, 'delivery_mode': 'voice'}
    candidate = source_offer.CatalogueItem(
        provider='163', track_id='notice-track', title='Song', artist='Artist',
        album='Album', duration_s=30, availability='unknown')
    job, _ = store.admit_offer('stream-1', 'notice-request', request, [candidate],
        auto_reply=authorized, consent_event='original-message' if authorized else None)
    owned = store.claim_next()
    assert owned.id == job.id
    return store.finish_failure(job.id, owned.run_token,
        {'code': 'unit_oom', 'message': 'PRIVATE-ASSET-PATH-AND-SECRET'},
        expected_unit=None, expected_revision=owned.revision)


def notice_row(store, job_id):
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM failure_notices WHERE job_id=?', (job_id,)).fetchone()
        return dict(row) if row else None


@pytest.mark.asyncio
@pytest.mark.parametrize('ack,outcome', [
    ({'sent': True, 'message_id': 'notice-ack'}, 'sent'),
    (True, 'unknown'), (False, 'unknown'), (None, 'unknown'),
    ({'sent': False, 'message_id': 'unconfirmed'}, 'unknown'),
    ({'sent': True}, 'unknown'), ({'sent': True, 'message_id': ''}, 'unknown'),
])
async def test_notice_detailed_ack_is_independent_of_media_and_never_retried(plugin, monkeypatch, ack, outcome):
    instance, _ = plugin
    monkeypatch.setattr(plugin_module.JobService, 'run_once', idle_scheduler)
    monkeypatch.setattr(instance, '_delivery_loop', idle_delivery)
    calls = []
    async def send(text, stream_id, **kwargs):
        calls.append((text, stream_id, kwargs))
        return ack
    monkeypatch.setattr(instance.ctx.send, 'text', send)
    await instance.on_load()
    try:
        before = failed_job(instance)
        jobs = instance._jobs
        await instance._dispatch_failure_notice(jobs, before.id, before.stream_id)
        await instance._dispatch_failure_notice(jobs, before.id, before.stream_id)
        assert len(calls) == 1
        text, stream, options = calls[0]
        assert stream == 'stream-1' and before.id in text and 'unit_oom' in text
        assert 'PRIVATE-ASSET' not in text and 'SECRET' not in text
        assert options == {'return_details': True, '_rpc_timeout_ms': 15_000}
        row = notice_row(jobs.store, before.id)
        assert row['state'] == outcome
        assert row['message_id'] == ('notice-ack' if outcome == 'sent' else None)
        after = jobs.store.get(before.id, before.stream_id)
        assert after == before  # Notice does not rewrite terminal replay timestamps or media fields.
        assert after.delivery_state == 'pending' and after.delivery_token is None
        assert await jobs.failure_notice_candidates() == []
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError('private transport secret'), asyncio.CancelledError()])
async def test_notice_transport_exception_or_cancellation_fences_unknown(plugin, monkeypatch, error):
    instance, _ = plugin
    monkeypatch.setattr(plugin_module.JobService, 'run_once', idle_scheduler)
    monkeypatch.setattr(instance, '_delivery_loop', idle_delivery)
    calls = []
    async def send(text, stream_id, **kwargs):
        calls.append(text)
        raise error
    monkeypatch.setattr(instance.ctx.send, 'text', send)
    await instance.on_load()
    try:
        job = failed_job(instance)
        jobs = instance._jobs
        with pytest.raises(type(error)):
            await instance._dispatch_failure_notice(jobs, job.id, job.stream_id)
        assert notice_row(jobs.store, job.id)['state'] == 'unknown'
        await instance._dispatch_failure_notice(jobs, job.id, job.stream_id)
        assert len(calls) == 1 and jobs.store.get(job.id, job.stream_id) == job
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
async def test_historical_unrequested_job_never_acquires_notice_permission(plugin, monkeypatch):
    instance, sends = plugin
    monkeypatch.setattr(plugin_module.JobService, 'run_once', idle_scheduler)
    monkeypatch.setattr(instance, '_delivery_loop', idle_delivery)
    await instance.on_load()
    try:
        job = failed_job(instance, authorized=False)
        await instance._dispatch_failure_notice(instance._jobs, job.id, job.stream_id)
        assert sends == [] and notice_row(instance._jobs.store, job.id) is None
        assert instance._jobs.store.get(job.id, job.stream_id) == job
    finally:
        await instance.on_unload()
