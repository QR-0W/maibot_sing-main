"""End-to-end durable orchestration with network, media, model and QQ fully mocked."""
from pathlib import Path
from types import SimpleNamespace
import asyncio
import importlib
import importlib.util
import json
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'job_service_test_pkg', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
pkg = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pkg
spec.loader.exec_module(pkg)
service_module = importlib.import_module('job_service_test_pkg.services.job_service')
inventory_module = importlib.import_module('job_service_test_pkg.services.asset_inventory')
ledger = importlib.import_module('job_service_test_pkg.services.job_store')
offers = importlib.import_module('job_service_test_pkg.services.source_offer')
download_module = importlib.import_module('job_service_test_pkg.services.source_download')
ownership = importlib.import_module('job_service_test_pkg.services.ownership')
runner_module = importlib.import_module('job_service_test_pkg.services.unit_runner')
recipes = importlib.import_module('job_service_test_pkg.runtime.recipe_identity')


class FakeCatalogue:
    def __init__(self):
        self.resolved = []
        self.searches = 0

    async def search(self, *args, **kwargs):
        self.searches += 1
        raise AssertionError('A selected job must never perform another keyword search')

    async def resolve(self, selected):
        self.resolved.append((selected.provider, selected.track_id))
        return SimpleNamespace(url='https://signed.invalid/private-token')


class FakeCoordinator:
    def __init__(self, store, *, pause_first=False):
        self.store = store
        self.pause_first = pause_first
        self.paused = False
        self.calls = []
        self.reconciles = []

    async def reconcile_step(self, job_id, stream_id, run_token, step, workspace, recipe):
        self.reconciles.append(step.name)
        owner = self.store.get(job_id, stream_id)
        attempt = next(item for item in self.store.stage_attempts(job_id, stream_id)
                       if item['unit_name'] == owner.unit_name)
        assert attempt['status'] == 'claimed' and owner.active_step == step.name
        settled = self.store.settle_step(job_id, run_token, owner.unit_name, completed=True)
        if settled.state == 'cancel_requested':
            result = self.store.finish_failure(job_id, run_token,
                {'code': 'cancelled', 'message': '用户取消；已有阶段停止。'},
                expected_unit=settled.unit_name, expected_revision=settled.revision)
            return {'unit': owner.unit_name, 'state': result.state}
        return {'unit': owner.unit_name, 'state': 'completed', 'reused': True}

    async def run_step(self, job_id, stream_id, run_token, step, workspace, plan, recipe):
        self.calls.append(step.name)
        owner = self.store.claim_step(job_id, run_token, step.name)
        if self.pause_first and not self.paused:
            self.paused = True
            return {'unit': owner.unit_name, 'state': 'running'}
        settled = self.store.settle_step(job_id, run_token, owner.unit_name, completed=True)
        return {'unit': owner.unit_name, 'state': 'completed', 'reused': bool(self.paused),
                'revision': settled.revision}


class StopAfterFirstCoordinator(FakeCoordinator):
    async def run_step(self, *args, **kwargs):
        result = await super().run_step(*args, **kwargs)
        if len(self.calls) == 1:
            # Simulate host reload after one receipt/attempt committed, before
            # another stage can be claimed.
            raise asyncio.CancelledError
        return result


class ActiveReconcileCoordinator(FakeCoordinator):
    async def reconcile_step(self, job_id, stream_id, run_token, step, workspace, recipe):
        self.reconciles.append(step.name)
        return {'unit': self.store.get(job_id, stream_id).unit_name, 'state': 'running'}


class UnknownReconcileCoordinator(FakeCoordinator):
    async def reconcile_step(self, job_id, stream_id, run_token, step, workspace, recipe):
        self.reconciles.append(step.name)
        raise runner_module.UnitError('unit_status_unknown', 'private systemctl detail')


class NoopArtifacts:
    pass


def selected(track_id='track-42'):
    return offers.CatalogueItem('163', track_id, 'Synthetic Song', 'Test Artist',
                                'Fixture Album', 31.0, 'available')


async def wait_until(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError('Synthetic threaded operation did not reach expected state')
        await asyncio.sleep(.005)


def make_service(tmp_path, *, store=None, catalogue=None, coordinator=None,
                 downloader=None, publisher=None, pause_first=False,
                 ownership_fd=None, close_drain_s=1.0):
    store = store or ledger.JobStore(tmp_path / 'jobs.sqlite3')
    catalogue = catalogue or FakeCatalogue()
    files = {}
    for number, name in enumerate(name for name in recipes.HASH_NAMES if name != 'source'):
        path = tmp_path / ('upstream' if name == 'rvc_upstream' else 'asset-' + name)
        if name == 'rvc_upstream':
            if not path.exists():
                path.mkdir()
                (path / 'infer.py').write_text('synthetic wrapper source')
        elif name == 'demucs_repo':
            if not path.exists():
                path.mkdir()
                (path / 'htdemucs.yaml').write_text("models: ['955717e8']\n")
                (path / '955717e8-8726e21a.th').write_bytes(b'no actual model import')
        elif not path.exists():
            path.write_bytes(('real-' + name + '-' + str(number)).encode())
        files[name] = path
    python = tmp_path / 'worker-python'
    python.write_bytes(b'isolated-python')
    work_root = tmp_path / 'workspaces'
    inference_lock = tmp_path / 'inference.lock'
    paths = inventory_module.AssetPaths(**files)
    inventory = inventory_module.AssetInventory(
        paths, lambda: {name: '1.2.3' for name in recipes.VERSION_NAMES})
    runtime = service_module.RenderRuntime(
        work_root=work_root, worker_python=python,
        worker_script=paths.media_stage, rvc_script=paths.rvc_script,
        model=paths.model, index=paths.index, hubert=paths.hubert,
        demucs_repo=paths.demucs_repo, inference_lock=inference_lock, poll_interval_s=.05)
    coordinator = coordinator or FakeCoordinator(store, pause_first=pause_first)
    calls = []

    async def exact_download(gateway, chosen, workspace, **kwargs):
        calls.append((chosen.provider, chosen.track_id, dict(kwargs)))
        audio = await gateway.resolve(chosen)
        assert 'private-token' in audio.url  # ephemeral inside the adapter only
        path = workspace / 'source.audio'
        path.write_bytes(b'S' * 4096)
        return path, 'ignored-download-digest', 4096

    async def fake_probe(path, chosen, **kwargs):
        assert path.read_bytes() == b'S' * 4096
        return {'duration_s': 31.0, 'audio_streams': 1,
                'provider': chosen.provider, 'source_id': chosen.track_id}

    def fake_publish(job_store, artifacts, *, job_id, stream_id, run_token,
                     workspace, recipe):
        current = job_store.get(job_id, stream_id)
        key = recipes.fingerprint(recipe)
        job = job_store.ready(job_id, run_token, key,
                              expected_unit=current.unit_name,
                              expected_revision=current.revision)
        return {'job': job, 'artifact': SimpleNamespace(key=key), 'catalog_warnings': []}

    instance = service_module.JobService(
        store, catalogue, coordinator, NoopArtifacts(), inventory, runtime,
        downloader=downloader or exact_download, probe=fake_probe,
        publisher=publisher or fake_publish, ownership_fd=ownership_fd,
        close_drain_s=close_drain_s)
    return instance, store, catalogue, coordinator, calls


@pytest.mark.asyncio
async def test_selected_job_returns_queued_then_runs_without_search_or_persisted_url(tmp_path):
    instance, store, catalogue, coordinator, downloads = make_service(tmp_path)
    queued, created = await instance.submit_selected(
        'stream-a', 'rpc-message-1', {'instrumental': False, 'query_label': 'synthetic'},
        selected(), auto_reply=True, consent_event='rpc-message-1')
    assert created and queued.state == 'queued'
    assert downloads == [] and catalogue.searches == 0

    finished = await instance.run_once()
    assert finished.state == 'ready' and len(downloads) == 1
    assert downloads[0][:2] == ('163', 'track-42')
    assert catalogue.resolved == [('163', 'track-42')] and catalogue.searches == 0
    assert coordinator.calls[0:2] == ['decode', 'separate']
    assert coordinator.calls[-2:] == ['encode', 'validate']

    workspace = instance.runtime.work_root / finished.id
    recipe = json.loads((workspace / 'recipe.json').read_text())
    plan = json.loads((workspace / 'plan.json').read_text())
    persisted = json.dumps({'request': finished.request,
                            'selected': finished.selected_source,
                            'recipe': recipe, 'plan': plan})
    assert 'private-token' not in persisted and 'signed.invalid' not in persisted
    assert recipe['provider'] == '163' and recipe['track_id'] == 'track-42'
    assert recipe['hashes']['source'] == __import__('hashlib').sha256(b'S' * 4096).hexdigest()
    assert finished.artifact_key == recipes.fingerprint(recipe)


@pytest.mark.asyncio
async def test_reload_scans_running_job_and_reconciles_same_claimed_unit(tmp_path):
    first, store, catalogue, coordinator, downloads = make_service(tmp_path, pause_first=True)
    queued, _ = await first.submit_selected('stream-a', 'rpc-message-2',
                                             {'instrumental': False}, selected('stable-id'))
    running = await first.run_once()
    assert running.state == 'running'
    attempts = store.stage_attempts(queued.id, queued.stream_id)
    assert [(item['step'], item['status']) for item in attempts] == [('decode', 'claimed')]
    assert len(downloads) == 1

    reopened = ledger.JobStore(store.path)
    second_coordinator = FakeCoordinator(reopened)
    second, _, _, _, resumed_downloads = make_service(
        tmp_path, store=reopened, catalogue=catalogue, coordinator=second_coordinator)
    finished = await second.run_once()
    assert finished.state == 'ready'
    assert resumed_downloads == []  # frozen source/recipe/plan reused after reload
    final_attempts = reopened.stage_attempts(queued.id, queued.stream_id)
    assert final_attempts[0]['unit_name'] == attempts[0]['unit_name']
    assert all(item['status'] == 'completed' for item in final_attempts)
    assert [item['step'] for item in final_attempts][0] == 'decode'
    assert [item['step'] for item in final_attempts][-1] == 'validate'
    assert second_coordinator.reconciles == ['decode']


@pytest.mark.asyncio
async def test_changed_assets_reconcile_active_unit_before_any_rehash(tmp_path):
    first, store, catalogue, coordinator, downloads = make_service(tmp_path, pause_first=True)
    queued, _ = await first.submit_selected('stream-active', 'rpc-message-active',
        {'instrumental': False}, selected('active-before-hash'))
    assert (await first.run_once()).state == 'running'
    first.runtime.model.write_bytes(b'changed-while-existing-unit-active')

    reopened = ledger.JobStore(store.path)
    active = ActiveReconcileCoordinator(reopened)
    second, _, _, _, resumed_downloads = make_service(
        tmp_path, store=reopened, catalogue=catalogue, coordinator=active)
    second.inventory.build = lambda source: (_ for _ in ()).throw(
        AssertionError('Active existing unit must be reconciled before inventory'))
    observed = await second.run_once()
    assert observed.state == 'running'
    assert active.reconciles == ['decode'] and active.calls == []
    assert resumed_downloads == []
    assert reopened.stage_attempts(queued.id, queued.stream_id)[0]['status'] == 'claimed'


@pytest.mark.asyncio
async def test_changed_assets_after_claim_reconcile_then_fail_before_new_stage(tmp_path):
    first, store, catalogue, coordinator, downloads = make_service(tmp_path, pause_first=True)
    queued, _ = await first.submit_selected('stream-reconcile', 'rpc-message-reconcile',
        {'instrumental': False}, selected('reconcile-before-hash'))
    assert (await first.run_once()).state == 'running'
    first.runtime.model.write_bytes(b'changed-before-reconcile-completed')

    reopened = ledger.JobStore(store.path)
    reconciler = FakeCoordinator(reopened)
    second, _, _, _, resumed_downloads = make_service(
        tmp_path, store=reopened, catalogue=catalogue, coordinator=reconciler)
    failed = await second.run_once()
    assert failed.state == 'failed' and failed.error['code'] == 'asset_inventory_changed'
    assert reconciler.reconciles == ['decode'] and reconciler.calls == []
    attempts = reopened.stage_attempts(queued.id, queued.stream_id)
    assert [(item['step'], item['status']) for item in attempts] == [('decode', 'completed')]
    assert resumed_downloads == []


@pytest.mark.asyncio
async def test_unknown_claim_is_publicly_recorded_and_loop_backs_off(tmp_path):
    first, store, catalogue, coordinator, downloads = make_service(tmp_path, pause_first=True)
    queued, _ = await first.submit_selected('stream-unknown', 'rpc-message-unknown',
        {'instrumental': False}, selected('unknown-unit'))
    assert (await first.run_once()).state == 'running'

    reopened = ledger.JobStore(store.path)
    unknown = UnknownReconcileCoordinator(reopened)
    second, _, _, _, resumed_downloads = make_service(
        tmp_path, store=reopened, catalogue=catalogue, coordinator=unknown)
    second.runtime.model.write_bytes(b'changed-but-must-not-hash-while-unit-unknown')
    second.inventory.build = lambda source: (_ for _ in ()).throw(
        AssertionError('Unknown existing unit must retain slot before inventory'))
    await second.start()
    await wait_until(lambda: len(unknown.reconciles) == 1)
    await asyncio.sleep(.15)
    assert unknown.reconciles == ['decode']  # first retry waits at least one second
    record = await second.coordinator_error(queued.id, queued.stream_id)
    global_record = await second.coordinator_error()
    assert record['code'] == 'unit_status_unknown'
    assert '无法确认' in record['message'] and 'private systemctl detail' not in str(record)
    assert global_record['retry_after_s'] >= 1
    assert reopened.get(queued.id, queued.stream_id).state == 'running'
    assert reopened.stage_attempts(queued.id, queued.stream_id)[0]['status'] == 'claimed'
    assert unknown.calls == [] and resumed_downloads == []
    await second.close()


@pytest.mark.asyncio
async def test_cancelled_active_claim_reconciles_without_new_stage(tmp_path):
    first, store, catalogue, coordinator, downloads = make_service(tmp_path, pause_first=True)
    queued, _ = await first.submit_selected('stream-cancel-active', 'rpc-message-cancel-active',
        {'instrumental': False}, selected('cancel-active'))
    assert (await first.run_once()).state == 'running'
    store.cancel(queued.id, queued.stream_id)
    reopened = ledger.JobStore(store.path)
    active = ActiveReconcileCoordinator(reopened)
    second, _, _, _, _ = make_service(
        tmp_path, store=reopened, catalogue=catalogue, coordinator=active)
    observed = await second.run_once()
    assert observed.state == 'cancel_requested'
    assert active.reconciles == ['decode'] and active.calls == []
    assert reopened.stage_attempts(queued.id, queued.stream_id)[0]['status'] == 'claimed'


@pytest.mark.asyncio
async def test_reload_refuses_changed_model_before_claiming_next_stage(tmp_path):
    store = ledger.JobStore(tmp_path / 'jobs.sqlite3')
    interrupted_coordinator = StopAfterFirstCoordinator(store)
    first, _, catalogue, _, downloads = make_service(
        tmp_path, store=store, coordinator=interrupted_coordinator)
    queued, _ = await first.submit_selected('stream-assets', 'rpc-message-assets',
        {'instrumental': False}, selected('frozen-assets'))
    with pytest.raises(asyncio.CancelledError):
        await first.run_once()
    attempts = store.stage_attempts(queued.id, queued.stream_id)
    assert [(item['step'], item['status']) for item in attempts] == [('decode', 'completed')]
    assert len(downloads) == 1

    # Administrator replacement at the same configured absolute path must not
    # let the old recipe identify a later stage that uses these new bytes.
    first.runtime.model.write_bytes(b'administrator-installed-different-model')
    reopened = ledger.JobStore(store.path)
    resumed_coordinator = FakeCoordinator(reopened)
    second, _, _, _, resumed_downloads = make_service(
        tmp_path, store=reopened, catalogue=catalogue, coordinator=resumed_coordinator)
    failed = await second.run_once()
    assert failed.state == 'failed'
    assert failed.error['code'] == 'asset_inventory_changed'
    assert 'model' in failed.error['message']
    assert resumed_downloads == [] and resumed_coordinator.calls == []
    assert [(item['step'], item['status']) for item in
            reopened.stage_attempts(queued.id, queued.stream_id)] == [('decode', 'completed')]


@pytest.mark.asyncio
async def test_completed_frozen_plan_can_publish_after_model_update(tmp_path):
    def crash_before_publish(*args, **kwargs):
        raise SystemExit('synthetic host crash before ledger publication')

    first, store, catalogue, coordinator, downloads = make_service(
        tmp_path, publisher=crash_before_publish)
    queued, _ = await first.submit_selected('stream-publish', 'rpc-message-publish',
        {'instrumental': False}, selected('publish-gap'))
    with pytest.raises(SystemExit, match='synthetic host crash'):
        await first.run_once()
    attempts = store.stage_attempts(queued.id, queued.stream_id)
    assert attempts and all(item['status'] == 'completed' for item in attempts)
    assert attempts[-1]['step'] == 'validate'
    first.runtime.model.write_bytes(b'new-model-after-all-media-receipts')

    reopened = ledger.JobStore(store.path)
    resumed_coordinator = FakeCoordinator(reopened)
    second, _, _, _, resumed_downloads = make_service(
        tmp_path, store=reopened, catalogue=catalogue, coordinator=resumed_coordinator)
    finished = await second.run_once()
    assert finished.state == 'ready'
    assert resumed_downloads == [] and resumed_coordinator.calls == []


@pytest.mark.asyncio
async def test_cancelled_owned_job_never_resolves_or_launches_a_stage(tmp_path):
    instance, store, catalogue, coordinator, downloads = make_service(tmp_path)
    queued, _ = await instance.submit_selected('stream-c', 'rpc-message-3',
                                                {'instrumental': False}, selected())
    running = store.claim_next()
    assert running.id == queued.id
    store.cancel(queued.id, queued.stream_id)
    finished = await instance.run_once()
    assert finished.state == 'cancelled' and finished.error['code'] == 'cancelled'
    assert downloads == [] and catalogue.resolved == [] and coordinator.calls == []


@pytest.mark.asyncio
async def test_production_downloader_receives_lease_preserving_offload(tmp_path, monkeypatch):
    instance, store, catalogue, coordinator, downloads = make_service(tmp_path)
    observed=[]

    async def production_spy(gateway, chosen, workspace, *, max_bytes, offload=None, **kwargs):
        observed.append(offload)
        assert offload == instance._offload
        path=workspace/'source.audio'
        await offload(path.write_bytes,b'S'*4096)
        return path,'ignored',4096

    instance.downloader=production_spy
    monkeypatch.setattr(service_module,'download_selected',production_spy)
    await instance.submit_selected('stream-offload','rpc-message-offload',
                                   {'instrumental':False},selected('offloaded-source'))
    assert (await instance.run_once()).state=='ready'
    assert observed==[instance._offload]


@pytest.mark.asyncio
async def test_specific_download_error_is_preserved_without_fallback_search(tmp_path):
    async def failed_download(gateway, chosen, workspace, **kwargs):
        assert (chosen.provider, chosen.track_id) == ('163', 'unavailable-id')
        raise download_module.DownloadError(
            'source_network_error', 'Selected source stream interrupted')

    instance, store, catalogue, coordinator, downloads = make_service(
        tmp_path, downloader=failed_download)
    queued, _ = await instance.submit_selected('stream-d', 'rpc-message-4',
        {'instrumental': False}, selected('unavailable-id'))
    failed = await instance.run_once()
    assert failed.id == queued.id and failed.state == 'failed'
    assert failed.error == {'code': 'source_network_error',
                            'message': 'Selected source stream interrupted'}
    assert catalogue.searches == 0 and catalogue.resolved == []
    assert coordinator.calls == [] and store.claim_next() is None


@pytest.mark.asyncio
async def test_close_leaves_flock_with_delayed_publisher_until_thread_finishes(tmp_path):
    lock = tmp_path / 'global-scheduler.lock'
    entered, release = threading.Event(), threading.Event()
    owner = ownership.exclusive(lock)
    owner_fd = owner.__enter__()
    instance = None
    try:
        def delayed_publish(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            raise RuntimeError('synthetic late publisher failure')

        instance, store, catalogue, coordinator, downloads = make_service(
            tmp_path, publisher=delayed_publish, ownership_fd=owner_fd,
            close_drain_s=.02)
        await instance.submit_selected('stream-close', 'rpc-message-close',
                                       {'instrumental': False}, selected('close-publish'))
        await instance.start()
        await wait_until(entered.is_set)
        started = time.monotonic()
        await instance.close()
        assert time.monotonic() - started < .5
    finally:
        owner.__exit__(None, None, None)

    try:
        with pytest.raises(ownership.OwnershipBusy):
            with ownership.exclusive(lock):
                pass
    finally:
        release.set()
    await wait_until(lambda: not instance._offloads)
    assert any('late publisher failure' in str(error)
               for error in instance._late_offload_errors)
    with ownership.exclusive(lock):
        pass


@pytest.mark.asyncio
async def test_close_leaves_flock_with_delayed_sqlite_rpc_until_thread_finishes(tmp_path):
    lock = tmp_path / 'global-scheduler.lock'
    entered, release = threading.Event(), threading.Event()
    owner = ownership.exclusive(lock)
    owner_fd = owner.__enter__()
    store = ledger.JobStore(tmp_path / 'jobs.sqlite3')
    instance, _, catalogue, coordinator, downloads = make_service(
        tmp_path, store=store, ownership_fd=owner_fd, close_drain_s=.02)
    original_submit = store.submit

    def delayed_submit(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_submit(*args, **kwargs)

    store.submit = delayed_submit
    request = asyncio.create_task(instance.submit_selected(
        'stream-sqlite', 'rpc-message-sqlite', {'instrumental': False}, selected('sqlite-rpc')))
    await wait_until(entered.is_set)
    try:
        await instance.close()
    finally:
        owner.__exit__(None, None, None)
    try:
        with pytest.raises(ownership.OwnershipBusy):
            with ownership.exclusive(lock):
                pass
    finally:
        release.set()
    with pytest.raises(RuntimeError, match='closing'):
        await request
    await wait_until(lambda: not instance._offloads)
    with ownership.exclusive(lock):
        pass


@pytest.mark.asyncio
async def test_executor_submission_failure_closes_duplicated_lease(tmp_path, monkeypatch):
    lock = tmp_path / 'global-scheduler.lock'
    owner = ownership.exclusive(lock)
    owner_fd = owner.__enter__()
    instance, store, catalogue, coordinator, downloads = make_service(
        tmp_path, ownership_fd=owner_fd, close_drain_s=0)

    def rejected_submit(*args, **kwargs):
        raise RuntimeError('synthetic executor rejection')

    monkeypatch.setattr(instance._executor, 'submit', rejected_submit)
    with pytest.raises(RuntimeError, match='executor rejection'):
        await instance._offload(lambda: None)
    await instance.close()
    owner.__exit__(None, None, None)
    with ownership.exclusive(lock):
        pass


@pytest.mark.asyncio
async def test_request_metadata_rejects_signed_url_before_ledger_write(tmp_path):
    instance, store, catalogue, coordinator, downloads = make_service(tmp_path)
    with pytest.raises(ValueError, match='credentials'):
        await instance.submit_selected('stream-e', 'rpc-message-5',
            {'instrumental': False, 'playback_url': 'https://signed.invalid/private'}, selected())
    assert service_module.SqliteJobScanner()(store, ('running', 'cancel_requested')) == []
