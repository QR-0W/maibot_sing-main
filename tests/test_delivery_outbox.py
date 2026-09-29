"""Offline delivery-outbox tests; every platform transport is a local mock."""
from pathlib import Path
from types import SimpleNamespace
import asyncio
import importlib
import importlib.util
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1] / 'services'
spec = importlib.util.spec_from_file_location(
    'delivery_test_pkg', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)]
)
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
ledger = importlib.import_module('delivery_test_pkg.job_store')
choices = importlib.import_module('delivery_test_pkg.source_offer')
delivery = importlib.import_module('delivery_test_pkg.delivery_outbox')

JobStore = ledger.JobStore
CatalogueItem = choices.CatalogueItem
CustomVoiceSender = delivery.CustomVoiceSender
DeliveryOutbox = delivery.DeliveryOutbox


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / 'jobs.sqlite3', max_pending=3)


def ready(store, *, token='message-1', stream='stream-a', consent=True):
    job, created = store.submit(
        stream,
        token,
        {'query': 'radiohead creep'},
        auto_reply=consent,
        consent_event=token if consent else None,
    )
    assert created
    offered = store.offer(
        job.id,
        stream,
        [CatalogueItem('163', 'track-1', 'Creep', 'Radiohead', 'Pablo Honey', 238.64)],
        expected_revision=job.revision,
    )
    store.select(job.id, stream, offered.offer_id, 1)
    running = store.claim_next()
    validation = store.claim_step(job.id, running.run_token, 'validate')
    settled = store.settle_step(
        job.id, running.run_token, validation.unit_name, completed=True
    )
    return store.ready(
        job.id,
        running.run_token,
        'a' * 64,
        expected_unit=settled.unit_name,
        expected_revision=settled.revision,
    )


def artifact(tmp_path):
    path = tmp_path / 'committed' / 'cover.mp3'
    path.parent.mkdir()
    path.write_bytes(b'committed cover')
    return SimpleNamespace(path=path)


@pytest.mark.asyncio
async def test_two_senders_claim_once_and_use_detailed_sdk_contract(store, tmp_path):
    job = ready(store)
    committed = artifact(tmp_path)
    calls = []

    async def send_custom(*args, **kwargs):
        calls.append((args, kwargs))
        await asyncio.sleep(0)
        return {'sent': True, 'message_id': 'platform-123'}

    sender = CustomVoiceSender(send_custom, rpc_timeout_ms=9_000)
    first = DeliveryOutbox(store, sender, lambda key: committed)
    second = DeliveryOutbox(JobStore(store.path), sender, lambda key: committed)
    results = await asyncio.gather(
        first.dispatch(job.id, job.stream_id),
        second.dispatch(job.id, job.stream_id),
    )

    assert len([result for result in results if result is not None]) == 1
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ('voiceurl', {'url': committed.path.as_uri()}, job.stream_id)
    assert kwargs == {'return_details': True, 'timeout_ms': 9_000}
    persisted = store.get(job.id, job.stream_id)
    assert persisted.delivery_state == 'sent'
    assert persisted.message_id == 'platform-123'
    assert await first.dispatch(job.id, job.stream_id) is None
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('result', 'expected'),
    [
        ({'sent': False, 'message_id': None}, 'unknown'),
        (False, 'unknown'),
        ({'sent': True, 'message_id': None}, 'unknown'),
        (True, 'unknown'),
        ({'success': True, 'message_id': 'not-an-ack'}, 'unknown'),
    ],
)
async def test_only_platform_ack_is_sent_and_only_rejection_is_failed(
    store, tmp_path, result, expected
):
    job = ready(store)
    committed = artifact(tmp_path)

    async def send_custom(*args, **kwargs):
        return result

    outbox = DeliveryOutbox(
        store,
        CustomVoiceSender(send_custom),
        lambda key: committed,
    )
    observed = await outbox.dispatch(job.id, job.stream_id)
    assert observed.delivery_state == expected
    assert store.claim_delivery(job.id, job.stream_id) is None


@pytest.mark.asyncio
async def test_timeout_is_unknown_but_same_rpc_late_ack_can_settle_sent(store, tmp_path):
    job = ready(store)
    committed = artifact(tmp_path)
    release = asyncio.Event()
    calls = 0

    async def send_custom(*args, **kwargs):
        nonlocal calls
        calls += 1
        await release.wait()
        return {'sent': True, 'message_id': 'late-platform-ack'}

    outbox = DeliveryOutbox(
        store,
        CustomVoiceSender(send_custom),
        lambda key: committed,
        acknowledgement_timeout_s=0.01,
    )
    timed_out = await outbox.dispatch(job.id, job.stream_id)
    assert timed_out.delivery_state == 'unknown'
    assert calls == 1
    assert await outbox.dispatch(job.id, job.stream_id) is None

    release.set()
    await outbox.drain_late_acknowledgements()
    settled = store.get(job.id, job.stream_id)
    assert settled.delivery_state == 'sent'
    assert settled.message_id == 'late-platform-ack'
    assert calls == 1


@pytest.mark.asyncio
async def test_host_cancellation_persists_unknown_and_never_resends(store, tmp_path):
    job = ready(store)
    committed = artifact(tmp_path)
    entered = asyncio.Event()
    transport_task = None
    calls = 0

    async def send_custom(*args, **kwargs):
        nonlocal calls, transport_task
        calls += 1
        transport_task = asyncio.current_task()
        entered.set()
        await asyncio.Event().wait()

    outbox = DeliveryOutbox(store, CustomVoiceSender(send_custom), lambda key: committed)
    attempt = asyncio.create_task(outbox.dispatch(job.id, job.stream_id))
    await entered.wait()
    attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attempt

    assert store.get(job.id, job.stream_id).delivery_state == 'unknown'
    assert await outbox.dispatch(job.id, job.stream_id) is None
    assert calls == 1
    transport_task.cancel()
    await outbox.drain_late_acknowledgements()
    assert store.get(job.id, job.stream_id).delivery_state == 'unknown'


@pytest.mark.asyncio
async def test_exception_and_artifact_validation_failure_are_unknown(store, tmp_path):
    job = ready(store)
    validation_calls = []
    send_calls = 0

    def reject_artifact(key):
        validation_calls.append(key)
        raise ValueError('corrupt committed artifact')

    async def send_custom(*args, **kwargs):
        nonlocal send_calls
        send_calls += 1
        raise RuntimeError('transport should not be reached')

    outbox = DeliveryOutbox(store, CustomVoiceSender(send_custom), reject_artifact)
    observed = await outbox.dispatch(job.id, job.stream_id)
    assert validation_calls == ['a' * 64]
    assert send_calls == 0
    assert observed.delivery_state == 'unknown'
    assert await outbox.dispatch(job.id, job.stream_id) is None

    other = ready(store, token='message-2', stream='stream-b')
    committed = artifact(tmp_path)
    failing = DeliveryOutbox(
        store,
        CustomVoiceSender(send_custom),
        lambda key: committed,
    )
    failed_transport = await failing.dispatch(other.id, other.stream_id)
    assert failed_transport.delivery_state == 'unknown'
    assert send_calls == 1


@pytest.mark.asyncio
async def test_post_send_host_exception_is_not_a_rejection(store, tmp_path):
    job = ready(store)
    async def host_rpc(*args, **kwargs):
        # Platform succeeded, then an internal Host hook/store failed and the
        # Host converted that exception to this SDK response.
        return {'sent': False, 'message_id': None}
    outbox = DeliveryOutbox(store, CustomVoiceSender(host_rpc), lambda key: artifact(tmp_path))
    assert (await outbox.dispatch(job.id, job.stream_id)).delivery_state == 'unknown'
    assert await outbox.dispatch(job.id, job.stream_id) is None


@pytest.mark.asyncio
async def test_late_ack_after_recover_does_not_overwrite_sent(store, tmp_path):
    job = ready(store)
    release = asyncio.Event()
    async def host_rpc(*args, **kwargs):
        await release.wait()
        return {'sent': True, 'message_id': 'after-recovery'}
    outbox = DeliveryOutbox(store, CustomVoiceSender(host_rpc),
                            lambda key: artifact(tmp_path), acknowledgement_timeout_s=0.01)
    attempt = asyncio.create_task(outbox.dispatch(job.id, job.stream_id))
    while store.get(job.id, job.stream_id).delivery_state != 'dispatching':
        await asyncio.sleep(0)
    assert await outbox.recover() == 1
    assert (await attempt).delivery_state == 'unknown'
    release.set()
    await outbox.drain_late_acknowledgements()
    assert store.get(job.id, job.stream_id).message_id == 'after-recovery'


@pytest.mark.asyncio
async def test_repeated_cancellation_keeps_sqlite_result_observed(store, tmp_path):
    job = ready(store)
    started = asyncio.Event()
    release = asyncio.Event()
    def delayed_result(*args, **kwargs):
        import time
        started_loop.call_soon_threadsafe(started.set)
        while not release.is_set():
            time.sleep(0.001)
        return original(*args, **kwargs)
    started_loop = asyncio.get_running_loop()
    original = store.delivery_result
    store.delivery_result = delayed_result
    async def host_rpc(*args, **kwargs):
        return {'sent': True, 'message_id': 'confirmed'}
    outbox = DeliveryOutbox(store, CustomVoiceSender(host_rpc), lambda key: artifact(tmp_path))
    attempt = asyncio.create_task(outbox.dispatch(job.id, job.stream_id))
    await started.wait()
    attempt.cancel()
    attempt.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await attempt
    while outbox._write_tasks:
        await asyncio.sleep(0.001)
    assert store.get(job.id, job.stream_id).message_id == 'confirmed'


@pytest.mark.asyncio
async def test_cancel_then_shutdown_is_bounded_with_blocked_sqlite(store, tmp_path):
    job = ready(store)
    started = asyncio.Event()
    release = __import__('threading').Event()
    loop = asyncio.get_running_loop()
    original = store.delivery_result
    def blocked_result(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        release.wait(5)
        return original(*args, **kwargs)
    store.delivery_result = blocked_result
    async def host_rpc(*args, **kwargs):
        return {'sent': True, 'message_id': 'eventual'}
    outbox = DeliveryOutbox(store, CustomVoiceSender(host_rpc), lambda key: artifact(tmp_path))
    attempt = asyncio.create_task(outbox.dispatch(job.id, job.stream_id))
    try:
        await started.wait()
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(attempt, 0.2)
        await asyncio.wait_for(outbox.shutdown(timeout_s=0.01), 0.2)
        assert store.get(job.id, job.stream_id).delivery_state == 'dispatching'
    finally:
        release.set()
    while outbox._write_tasks:
        await asyncio.sleep(0.001)
    assert store.get(job.id, job.stream_id).message_id == 'eventual'


@pytest.mark.asyncio
async def test_recovery_changes_dispatching_to_unknown_without_transport(store, tmp_path):
    job = ready(store)
    claimed = store.claim_delivery(job.id, job.stream_id)
    calls = 0

    async def send_custom(*args, **kwargs):
        nonlocal calls
        calls += 1

    outbox = DeliveryOutbox(
        JobStore(store.path),
        CustomVoiceSender(send_custom),
        lambda key: artifact(tmp_path),
    )
    assert claimed.delivery_state == 'dispatching'
    assert await outbox.recover() == 1
    assert store.get(job.id, job.stream_id).delivery_state == 'unknown'
    assert await outbox.dispatch(job.id, job.stream_id) is None
    assert calls == 0


@pytest.mark.asyncio
async def test_not_ready_or_unconsented_jobs_are_never_validated_or_sent(store, tmp_path):
    searching, _ = store.submit('stream-a', 'searching', {'query': 'not ready'})
    unconsented = ready(store, token='message-2', stream='stream-b', consent=False)
    validation_calls = 0
    send_calls = 0

    def validate(key):
        nonlocal validation_calls
        validation_calls += 1
        return artifact(tmp_path)

    async def send_custom(*args, **kwargs):
        nonlocal send_calls
        send_calls += 1

    outbox = DeliveryOutbox(store, CustomVoiceSender(send_custom), validate)
    assert await outbox.dispatch(searching.id, searching.stream_id) is None
    assert await outbox.dispatch(unconsented.id, unconsented.stream_id) is None
    assert validation_calls == send_calls == 0
