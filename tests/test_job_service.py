"""End-to-end durable orchestration with network, media, model and QQ fully mocked."""
from pathlib import Path
from types import SimpleNamespace
import asyncio
import importlib
import importlib.util
import json
import sys

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


class NoopArtifacts:
    pass


def selected(track_id='track-42'):
    return offers.CatalogueItem('163', track_id, 'Synthetic Song', 'Test Artist',
                                'Fixture Album', 31.0, 'available')


def make_service(tmp_path, *, store=None, catalogue=None, coordinator=None,
                 downloader=None, publisher=None, pause_first=False):
    store = store or ledger.JobStore(tmp_path / 'jobs.sqlite3')
    catalogue = catalogue or FakeCatalogue()
    files = {}
    for number, name in enumerate(name for name in recipes.HASH_NAMES if name != 'source'):
        path = tmp_path / ('asset-' + name)
        if not path.exists():
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
        inference_lock=inference_lock, poll_interval_s=.05)
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
        publisher=publisher or fake_publish)
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
async def test_request_metadata_rejects_signed_url_before_ledger_write(tmp_path):
    instance, store, catalogue, coordinator, downloads = make_service(tmp_path)
    with pytest.raises(ValueError, match='credentials'):
        await instance.submit_selected('stream-e', 'rpc-message-5',
            {'instrumental': False, 'playback_url': 'https://signed.invalid/private'}, selected())
    assert service_module.SqliteJobScanner()(store, ('running', 'cancel_requested')) == []
