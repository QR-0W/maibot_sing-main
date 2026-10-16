"""Bounded-default orchestration regressions; no actual media/model/unit launches."""
from pathlib import Path
from types import SimpleNamespace
import asyncio
import hashlib
import importlib
import importlib.util
import json
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bounded_probe_fixtures',
                                              ROOT/'tests/test_job_service.py')
fixtures = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixtures
spec.loader.exec_module(fixtures)
service = fixtures.service_module
worker = importlib.import_module('job_service_test_pkg.runtime.worker')
media = importlib.import_module('job_service_test_pkg.services.media_probe')
receipts = importlib.import_module('job_service_test_pkg.runtime.stage_receipts')


class ProbeCoordinator(fixtures.FakeCoordinator):
    """Simulate durable units and real probe receipts, not media execution."""
    def __init__(self, store, mode='completed'):
        super().__init__(store)
        self.mode = mode
        self.units = []
        self.probe_plan = None
        self.probe_key = None

    def seal(self, workspace, step, key):
        request = json.loads((workspace/'source-probe-request.json').read_text())
        report = {'duration_s': 31.0, 'container_duration_s': 31.04,
            'frames': 31*44100, 'sample_rate': 44100, 'audio_streams': 1,
            'provider': request['selected']['provider'],
            'source_id': request['selected']['track_id'],
            'render_mode': request['render_mode'], 'source_frames': 31*44100,
            'context_window': (fixtures.contexts.freeze_context(31*44100)
                if request['render_mode'] == 'excerpt'
                else fixtures.contexts.full_context(31*44100))}
        (workspace/'source-probe-report.json').write_text(json.dumps(report))
        ledger = receipts.StageReceipts(workspace, key)
        inputs = ledger._files(list(step.inputs))
        ledger.seal(step.name, {name: row['sha256'] for name, row in inputs.items()},
                    list(step.outputs))

    async def run_step(self, job_id, stream_id, run_token, step, workspace, plan, recipe):
        if step.name != 'source_probe':
            assert (workspace/'recipe.json').exists()
            assert self.store.stage_attempts(job_id, stream_id)[0]['status'] == 'completed'
            self.calls.append(step.name)
            owner = self.store.claim_step(job_id, run_token, step.name)
            # Synthetic media bytes, but genuine StageReceipts for every audio
            # stage: exercise the actual publisher instead of fake_publish.
            for name in step.outputs:
                if not (workspace/name).exists():
                    (workspace/name).write_bytes(('synthetic audio '+name).encode())
            ledger = receipts.StageReceipts(workspace, recipe)
            ledger.seal(step.name,
                {name: receipts.sha256(workspace/name) for name in step.inputs},
                list(step.outputs))
            self.store.settle_step(job_id, run_token, owner.unit_name, completed=True)
            return {'unit': owner.unit_name, 'state': 'completed'}
        self.calls.append(step.name)
        assert not (workspace/'recipe.json').exists()
        assert not (workspace/'plan.json').exists()
        assert step.timeout_s <= 180 and step.unit_limit_s <= 180
        assert step.inputs == ('source.audio', 'source-probe-request.json')
        assert step.outputs == ('source-probe-report.json',)
        assert step.argv[2] == 'probe' and 'ffmpeg' not in step.argv
        self.probe_plan = json.loads(plan.read_text())
        self.probe_key = recipe
        owner = self.store.claim_step(job_id, run_token, step.name)
        self.units.append(owner.unit_name)
        if self.mode in ('running', 'unknown'):
            return {'unit': owner.unit_name, 'state': 'running'}
        if self.mode in ('refused', 'oom'):
            settled = self.store.settle_step(job_id, run_token, owner.unit_name, completed=False)
            code = 'resource_budget_exhausted' if self.mode == 'refused' else 'unit_oom'
            self.store.finish_failure(job_id, run_token, {'code': code, 'message': 'mock refusal'},
                expected_unit=settled.unit_name, expected_revision=settled.revision)
            raise fixtures.runner_module.UnitError(code, 'mock refusal')
        if self.mode == 'unsealed':
            (workspace/'source-probe-report.json').write_text('{}')
        else:
            self.seal(workspace, step, recipe)
        self.store.settle_step(job_id, run_token, owner.unit_name, completed=True)
        return {'unit': owner.unit_name, 'state': 'completed'}

    async def reconcile_step(self, job_id, stream_id, run_token, step, workspace, recipe):
        assert step.name == 'source_probe'
        self.reconciles.append(step.name)
        owner = self.store.get(job_id, stream_id)
        self.units.append(owner.unit_name)
        if self.mode == 'unknown':
            raise fixtures.runner_module.UnitError('unit_status_unknown', 'mock unknown')
        if self.mode == 'running':
            return {'unit': owner.unit_name, 'state': 'running'}
        if owner.state == 'cancel_requested':
            self.store.settle_step(job_id, run_token, owner.unit_name, completed=False)
            current = self.store.get(job_id, stream_id)
            result = self.store.finish_failure(job_id, run_token,
                {'code': 'cancelled', 'message': 'mock cancellation'},
                expected_unit=current.unit_name, expected_revision=current.revision)
            return {'unit': owner.unit_name, 'state': result.state}
        self.seal(workspace, step, recipe)
        self.store.settle_step(job_id, run_token, owner.unit_name, completed=True)
        return {'unit': owner.unit_name, 'state': 'completed'}


def setup(tmp_path, mode='completed'):
    store = fixtures.ledger.JobStore(tmp_path/'jobs.sqlite3')
    coordinator = ProbeCoordinator(store, mode)
    instance, _, catalogue, _, downloads = fixtures.make_service(
        tmp_path, store=store, coordinator=coordinator, probe=media.probe_download,
        publisher=fixtures.artifact_module.publish_job)
    return instance, store, coordinator, catalogue, downloads


async def submit(instance):
    job, _ = await instance.submit_selected('stream-probe', 'message-probe',
        {'instrumental': False}, fixtures.selected())
    return job


@pytest.mark.asyncio
async def test_production_default_freezes_audio_only_after_verified_probe(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('Host must never launch naked ffprobe/ffmpeg')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    instance, store, coordinator, _, downloads = setup(tmp_path)
    queued = await submit(instance)
    result = await instance.run_once()
    assert result.state == 'ready'
    committed = instance.artifacts.verify(result.artifact_key)
    assert committed.path.is_file() and committed.bytes > 0
    assert instance.publisher is fixtures.artifact_module.publish_job
    assert coordinator.calls[0] == 'source_probe'
    attempts = store.stage_attempts(queued.id, queued.stream_id)
    assert attempts[0]['status'] == 'completed' and attempts[1]['step'] == 'decode'
    workspace = instance.runtime.work_root/queued.id
    request = json.loads((workspace/'source-probe-request.json').read_text())
    assert request['schema'] == 'sing-source-probe-v2'
    assert request['render_mode'] == 'full' and request['source_max_seconds'] == 300
    assert request['context_max_seconds'] == 90
    assert request['context_policy'] == 'centered-source-v1'
    assert request['context_helper_sha256'] == instance._runtime_inventory.hashes['context_window']
    assert request['source_sha256'] == hashlib.sha256(b'S'*4096).hexdigest()
    assert request['worker_sha256'] == instance._runtime_inventory.hashes['worker']
    assert request['worker_script'] == instance._runtime_context['execution_paths']['worker']
    assert request['selected'] == {'provider': '163', 'track_id': 'track-42', 'duration_s': 31.0}
    recipe = json.loads((workspace/'recipe.json').read_text())
    assert coordinator.probe_key != fixtures.recipes.fingerprint(recipe)
    assert 'source_probe' not in [step['name'] for step in recipe['steps']]
    assert len(downloads) == 1
    await instance.close()


@pytest.mark.asyncio
async def test_active_probe_is_pending_and_same_unit_survives_restart(tmp_path):
    first, store, coordinator, catalogue, downloads = setup(tmp_path, 'running')
    queued = await submit(first)
    active = await first.run_once()
    unit = active.unit_name
    workspace = first.runtime.work_root/queued.id
    assert active.state == 'running' and active.active_step == 'source_probe'
    assert not (workspace/'recipe.json').exists() and not (workspace/'plan.json').exists()
    reopened = fixtures.ledger.JobStore(store.path)
    recovered_coordinator = ProbeCoordinator(reopened, 'running')
    second, _, _, _, resumed_downloads = fixtures.make_service(tmp_path, store=reopened,
        catalogue=catalogue, coordinator=recovered_coordinator, probe=media.probe_download,
        publisher=fixtures.artifact_module.publish_job)
    pending = await second.run_once()
    assert pending.unit_name == unit and pending.state == 'running'
    assert recovered_coordinator.calls == [] and resumed_downloads == []
    assert recovered_coordinator.units == [unit]
    recovered_coordinator.mode = 'completed'
    completed = await second.run_once()
    assert completed.state == 'ready'
    assert recovered_coordinator.calls[0] == 'decode'
    assert all(name != 'source_probe' for name in recovered_coordinator.calls)
    assert reopened.stage_attempts(queued.id, queued.stream_id)[0]['unit_name'] == unit
    assert len(downloads) == 1
    await first.close()
    await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['running', 'unknown'])
async def test_owned_probe_reconciles_before_changed_assets_or_generation(tmp_path, monkeypatch, mode):
    first, store, _, catalogue, _ = setup(tmp_path, 'running')
    queued = await submit(first)
    active = await first.run_once()
    reopened = fixtures.ledger.JobStore(store.path)
    coordinator = ProbeCoordinator(reopened, mode)
    second, _, _, _, downloads = fixtures.make_service(tmp_path, store=reopened,
        catalogue=catalogue, coordinator=coordinator, probe=media.probe_download,
        publisher=fixtures.artifact_module.publish_job)
    second.runtime.model.unlink()
    second.runtime.index.unlink()
    def forbidden_inventory():
        raise AssertionError('Recovered probe must reconcile before reading missing assets')
    monkeypatch.setattr(second.inventory, 'build_runtime', forbidden_inventory)
    if mode == 'unknown':
        with pytest.raises(fixtures.runner_module.UnitError, match='mock unknown'):
            await second.run_once()
    else:
        assert (await second.run_once()).state == 'running'
    current = reopened.get(queued.id, queued.stream_id)
    assert current.state == 'running' and current.unit_name == active.unit_name
    assert coordinator.reconciles == ['source_probe']
    assert coordinator.calls == [] and downloads == []
    assert not (first.runtime.work_root/queued.id/'recipe.json').exists()
    await first.close()
    await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['refused', 'oom', 'unsealed'])
async def test_probe_failure_or_unsealed_report_never_retries_freezes_or_publishes(tmp_path, monkeypatch, mode):
    async def forbidden(*args, **kwargs):
        raise AssertionError('A refused probe cannot fall back to naked media')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    instance, store, coordinator, _, _ = setup(tmp_path, mode)
    queued = await submit(instance)
    failed = await instance.run_once()
    assert failed.state in ('failed', 'interrupted')
    assert failed.artifact_key is None
    assert coordinator.calls == ['source_probe']
    assert not (instance.runtime.work_root/queued.id/'recipe.json').exists()
    assert await instance.run_once() is None
    assert coordinator.calls == ['source_probe']
    await instance.close()


@pytest.mark.asyncio
async def test_cancellation_only_reconciles_old_probe_not_changed_assets(tmp_path):
    instance, store, coordinator, _, downloads = setup(tmp_path, 'running')
    queued = await submit(instance)
    active = await instance.run_once()
    await instance.cancel(queued.id, queued.stream_id)
    instance.runtime.model.write_bytes(b'changed-while-cancelling')
    coordinator.mode = 'completed'
    cancelled = await instance.run_once()
    assert cancelled.state == 'cancelled'
    assert coordinator.calls == ['source_probe'] and coordinator.reconciles == ['source_probe']
    assert coordinator.units == [active.unit_name, active.unit_name]
    assert len(downloads) == 1 and cancelled.artifact_key is None
    await instance.close()


@pytest.mark.asyncio
async def test_cancel_after_validate_completion_never_enters_real_publisher(tmp_path, monkeypatch):
    instance, store, coordinator, _, _ = setup(tmp_path)
    queued = await submit(instance)
    real_run_step = coordinator.run_step
    real_publisher = fixtures.artifact_module.publish_job
    publisher_calls = []
    artifact_calls = []
    async def cancel_after_validate(*args, **kwargs):
        result = await real_run_step(*args, **kwargs)
        step = args[3]
        if step.name == 'validate':
            store.cancel(queued.id, queued.stream_id)
        return result
    def counted_publisher(*args, **kwargs):
        publisher_calls.append(True)
        return real_publisher(*args, **kwargs)
    monkeypatch.setattr(coordinator, 'run_step', cancel_after_validate)
    instance.publisher = counted_publisher
    # prepare is lightweight storage/inventory, not a media/service launch.
    await instance.prepare()
    real_artifact_publish = instance.artifacts.publish
    def counted_artifact(*args, **kwargs):
        artifact_calls.append(True)
        return real_artifact_publish(*args, **kwargs)
    monkeypatch.setattr(instance.artifacts, 'publish', counted_artifact)
    result = await instance.run_once()
    assert result.state == 'cancelled' and result.artifact_key is None
    assert coordinator.calls[-1] == 'validate'
    assert store.stage_attempts(queued.id, queued.stream_id)[-1]['status'] == 'completed'
    assert publisher_calls == [] and artifact_calls == []
    assert not (instance.artifacts.root/'.publishing').exists()
    await instance.close()


@pytest.mark.asyncio
async def test_legacy_host_probe_explicitly_refuses_without_media_launch(monkeypatch, tmp_path):
    async def forbidden(*args, **kwargs):
        raise AssertionError('naked media launch')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    with pytest.raises(fixtures.service_module.CatalogueError) as exc:
        await media.probe_download(tmp_path/'source.audio', fixtures.selected())
    assert exc.value.code == 'source_probe_requires_budget'


@pytest.mark.parametrize('check', ['limits', 'headroom'])
def test_worker_cli_admission_failure_starts_zero_media(tmp_path, monkeypatch, check):
    launched = []
    async def forbidden(*args, **kwargs):
        launched.append(args)
        raise AssertionError('media child before admission')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    monkeypatch.setattr(worker, 'verify_limits', lambda: None)
    monkeypatch.setattr(worker, 'verify_host_headroom', lambda: None)
    def refuse():
        raise RuntimeError('mock admission refused')
    monkeypatch.setattr(worker, 'verify_limits' if check == 'limits' else 'verify_host_headroom', refuse)
    with pytest.raises(RuntimeError, match='mock admission refused'):
        worker.probe_cli(['--source', str(tmp_path/'source.audio'),
            '--request', str(tmp_path/'source-probe-request.json'),
            '--report', str(tmp_path/'source-probe-report.json')])
    assert launched == []


@pytest.mark.parametrize('status', ['claimed', 'interrupted', 'unknown'])
def test_audio_prefix_never_discards_unconfirmed_probe(status):
    with pytest.raises(service.JobServiceError) as exc:
        service.JobService._attempt_prefix((), [{'step': 'source_probe', 'status': status}])
    assert exc.value.code == 'attempt_state_invalid'


@pytest.mark.parametrize('change', ['source_frames', 'context', 'duration', 'mode'])
def test_v2_report_rejects_complete_source_or_context_tampering(tmp_path, change):
    frames = 14032003
    report = {'duration_s':frames/44100,'container_duration_s':318.186,
              'frames':frames,'source_frames':frames,'sample_rate':44100,
              'audio_streams':1,'provider':'163','source_id':'track-42',
              'render_mode':'excerpt','context_window':fixtures.contexts.freeze_context(frames)}
    path = tmp_path/'source-probe-report.json'
    path.write_text(json.dumps(report))
    assert media.read_probe_report(path, fixtures.selected(), max_seconds=900,
        schema='sing-source-probe-v2', render_mode='excerpt') == report
    if change == 'source_frames': report['source_frames'] += 1
    elif change == 'context':
        report['context_window']['start_frame'] += 1
        report['context_window']['end_frame'] += 1
    elif change == 'duration': report['duration_s'] = 318.186
    elif change == 'mode': report['render_mode'] = 'full'
    path.write_text(json.dumps(report))
    with pytest.raises(service.CatalogueError) as exc:
        media.read_probe_report(path, fixtures.selected(), max_seconds=900,
            schema='sing-source-probe-v2', render_mode='excerpt')
    assert exc.value.code == 'source_probe_invalid'


def test_legacy_v1_report_reader_is_bounded_and_rejects_padding_as_frame_clock(tmp_path):
    path = tmp_path/'source-probe-report.json'
    path.write_bytes(b'x'*65537)
    with pytest.raises(service.CatalogueError):
        media.read_probe_report(path, fixtures.selected())
    legacy = {'duration_s':31.0, 'container_duration_s':31.04,
        'frames':31*44100, 'sample_rate':44100, 'audio_streams':1,
        'provider':'163', 'source_id':'track-42'}
    path.write_text(json.dumps(legacy))
    assert media.read_probe_report(path, fixtures.selected(),
        schema='sing-source-probe-v1') == legacy
    with pytest.raises(service.CatalogueError):
        media.read_probe_report(path, fixtures.selected(),
            schema='sing-source-probe-v2', render_mode='full')
    path.write_text(json.dumps({'duration_s': 31.04, 'container_duration_s': 31.04,
        'frames': 31*44100, 'sample_rate': 44100, 'audio_streams': 1,
        'provider': '163', 'source_id': 'track-42'}))
    with pytest.raises(service.CatalogueError):
        media.read_probe_report(path, fixtures.selected())
