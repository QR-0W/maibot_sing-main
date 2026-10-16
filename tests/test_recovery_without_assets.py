"""Storage-only recovery with real publication/receipts, no media or model run."""
from pathlib import Path
import asyncio
import importlib.util
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('missing_assets_probe_fixtures',
                                              ROOT/'tests/test_bounded_source_probe.py')
fixtures = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixtures
spec.loader.exec_module(fixtures)
base = fixtures.fixtures
coordinator_module = __import__(
    'job_service_test_pkg.services.stage_coordinator', fromlist=['StageCoordinator'])


async def submit_authorized(instance):
    job, _ = await instance.submit_selected('stream-probe', 'message-probe',
        {'instrumental': False, 'auto_reply': True, 'platform': 'qq',
         'ingress_proof': base.ledger.TRUSTED_QQ_INGRESS}, base.selected(),
        auto_reply=True, consent_event='message-probe')
    return job


def forbid_inventory(*args, **kwargs):
    raise AssertionError('Recovery/publication must not read current generation assets')


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['deleted', 'modified'])
async def test_all_completed_frozen_render_publishes_real_bytes_without_live_assets(
        tmp_path, monkeypatch, change):
    first, store, coordinator, catalogue, _ = fixtures.setup(tmp_path)
    queued = await submit_authorized(first)
    real_step = coordinator.run_step
    async def stop_after_validate(*args, **kwargs):
        result = await real_step(*args, **kwargs)
        if args[3].name == 'validate':
            raise asyncio.CancelledError
        return result
    monkeypatch.setattr(coordinator, 'run_step', stop_after_validate)
    with pytest.raises(asyncio.CancelledError):
        await first.run_once()
    assert all(row['status'] == 'completed'
               for row in store.stage_attempts(queued.id, queued.stream_id))
    await first.close()
    reopened = base.ledger.JobStore(store.path)
    recovered = fixtures.ProbeCoordinator(reopened)
    second, _, _, _, downloads = base.make_service(tmp_path, store=reopened,
        catalogue=catalogue, coordinator=recovered, probe=fixtures.media.probe_download,
        publisher=base.artifact_module.publish_job)
    try:
        for path in (second.runtime.model, second.runtime.index,
                     second.inventory.paths.worker, second.inventory.paths.context_window):
            if change == 'deleted':
                path.unlink()
            else:
                path.write_bytes(b'administrator replacement after completed receipts')
        monkeypatch.setattr(second.inventory, 'build_runtime', forbid_inventory)
        monkeypatch.setattr(second.inventory, 'build', forbid_inventory)
        assert second._generation is None
        ready = await second.run_once()
        assert ready.state == 'ready' and ready.artifact_key
        assert second.publisher is base.artifact_module.publish_job
        assert second.artifacts.verify(ready.artifact_key).path.is_file()
        assert second._generation is None and second._runtime_inventory is None
        assert second._context_task is None
        assert recovered.calls == recovered.reconciles == downloads == []
        assert await second.delivery_candidates() == [(queued.id, queued.stream_id)]
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_confirmed_old_probe_oom_and_failure_notice_do_not_need_assets(tmp_path, monkeypatch):
    first, store, _, catalogue, _ = fixtures.setup(tmp_path, 'running')
    queued = await submit_authorized(first)
    active = await first.run_once()
    await first.close()
    reopened = base.ledger.JobStore(store.path)
    coordinator = fixtures.ProbeCoordinator(reopened, 'running')
    second, _, _, _, downloads = base.make_service(tmp_path, store=reopened,
        catalogue=catalogue, coordinator=coordinator, probe=fixtures.media.probe_download,
        publisher=base.artifact_module.publish_job)
    async def confirmed_oom(job_id, stream_id, token, step, workspace, recipe):
        coordinator.reconciles.append(step.name)
        owner = reopened.get(job_id, stream_id)
        assert owner.unit_name == active.unit_name
        settled = reopened.settle_step(job_id, token, owner.unit_name, completed=False)
        reopened.finish_failure(job_id, token,
            {'code': 'unit_oom', 'message': 'Synthetic confirmed whole-unit OOM'},
            expected_unit=settled.unit_name, expected_revision=settled.revision)
        raise base.runner_module.UnitError('unit_oom', 'sealed synthetic OOM evidence')
    monkeypatch.setattr(coordinator, 'reconcile_step', confirmed_oom)
    second.runtime.model.unlink()
    monkeypatch.setattr(second.inventory, 'build_runtime', forbid_inventory)
    try:
        failed = await second.run_once()
        assert failed.state == 'failed' and failed.error['code'] == 'unit_oom'
        assert failed.artifact_key is None and second._generation is None
        assert coordinator.reconciles == ['source_probe'] and coordinator.calls == downloads == []
        assert (await second.get_job(queued.id, queued.stream_id)).state == 'failed'
        assert await second.coordinator_error(queued.id, queued.stream_id) is None
        assert await second.failure_notice_candidates() == [(queued.id, queued.stream_id)]
        notice = await second.claim_failure_notice(queued.id, queued.stream_id)
        assert notice['code'] == 'unit_oom' and notice['token']
        await second.settle_failure_notice(queued.id, notice['token'], 'unknown')
        assert await second.claim_failure_notice(queued.id, queued.stream_id) is None
        await second.recover_failure_notices()
        assert await second.delivery_candidates() == []
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_cached_generation_does_not_admit_after_live_model_disappears(tmp_path):
    instance, store, catalogue, coordinator, downloads = base.make_service(tmp_path)
    generation = await instance.prepare()
    instance.runtime.model.unlink()
    try:
        with pytest.raises(base.inventory_module.InventoryError):
            await instance.submit_selected('new-stream', 'new-after-removal', {}, base.selected())
        assert store.find_request('new-stream', 'new-after-removal') is None
        assert instance.generation == generation  # real prior generation, not a fallback
        assert downloads == coordinator.calls == [] and catalogue.searches == 0
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_missing_inventory_refuses_new_admission_and_leaves_queue_unclaimed(tmp_path, monkeypatch):
    first, store, catalogue, _, _ = base.make_service(tmp_path)
    queued, _ = await first.submit_selected('stream-queued', 'old-queued', {}, base.selected())
    await first.close()
    second, _, _, coordinator, downloads = base.make_service(tmp_path,
        store=base.ledger.JobStore(store.path), catalogue=catalogue)
    second.runtime.model.unlink()
    calls = []
    def forbidden_expire():
        calls.append('expire')
        raise AssertionError('Offers cannot expire before initial generation preparation')
    def forbidden_claim():
        calls.append('claim')
        raise AssertionError('Queue cannot be claimed before initial generation preparation')
    monkeypatch.setattr(second.store, 'expire_candidates', forbidden_expire)
    monkeypatch.setattr(second.store, 'claim_next', forbidden_claim)
    try:
        await second.prepare_storage()
        assert second._generation is None and second._runtime_inventory is None
        with pytest.raises(base.inventory_module.InventoryError):
            await second.submit_selected('stream-new', 'new-request', {}, base.selected())
        assert second.store.find_request('stream-new', 'new-request') is None
        with pytest.raises(base.inventory_module.InventoryError):
            await second.run_once()
        assert (await second.get_job(queued.id, queued.stream_id)).state == 'queued'
        assert calls == downloads == coordinator.calls == []
        assert catalogue.searches == 0 and second._generation is None
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_frozen_render_reconciles_old_unit_then_refuses_missing_next_stage(tmp_path):
    first, store, catalogue, _, _ = base.make_service(tmp_path, pause_first=True)
    queued, _ = await first.submit_selected('stream-render', 'render-request', {}, base.selected())
    active = await first.run_once()
    await first.close()
    reopened = base.ledger.JobStore(store.path)
    coordinator = base.FakeCoordinator(reopened)
    second, _, _, _, downloads = base.make_service(tmp_path, store=reopened,
        catalogue=catalogue, coordinator=coordinator,
        publisher=base.artifact_module.publish_job)
    second.runtime.model.unlink()
    try:
        failed = await second.run_once()
        assert failed.state == 'failed' and failed.artifact_key is None
        assert coordinator.reconciles == ['decode'] and coordinator.calls == downloads == []
        attempts = reopened.stage_attempts(queued.id, queued.stream_id)
        assert attempts[0]['unit_name'] == active.unit_name and attempts[0]['status'] == 'completed'
        assert len(attempts) == 1 and second._generation is None
    finally:
        await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence', ['report', 'receipt', 'source'])
@pytest.mark.parametrize('damage', ['missing', 'mutated'])
@pytest.mark.parametrize('proof', ['active', 'unknown', 'oom', 'absent', 'cancelled', 'completed'])
async def test_claimed_decode_reconciles_before_damaged_completed_probe(
        tmp_path, monkeypatch, evidence, damage, proof):
    """Real ledger/receipts/coordinator; mocked systemd observations are not proof
    that a real service ran or suffered OOM. No subprocess or media is executed.
    """
    first, store, launch_fixture, catalogue, _ = fixtures.setup(tmp_path)
    queued = await submit_authorized(first)
    real_run = launch_fixture.run_step
    async def pause_decode(*args, **kwargs):
        if args[3].name == 'decode':
            owner = store.claim_step(args[0], args[2], 'decode')
            return {'unit': owner.unit_name, 'state': 'running'}
        return await real_run(*args, **kwargs)
    monkeypatch.setattr(launch_fixture, 'run_step', pause_decode)
    active = await first.run_once()
    workspace = first.runtime.work_root/queued.id
    assert [row['status'] for row in store.stage_attempts(queued.id, queued.stream_id)] == [
        'completed', 'claimed']
    recipe, steps, _ = first._read_frozen(workspace, base.selected())
    key = base.recipes.fingerprint(recipe)
    if proof == 'completed':
        step = steps[0]
        for name in step.outputs:
            (workspace/name).write_bytes(b'synthetic completed decode output')
        receipts = fixtures.receipts.StageReceipts(workspace, key)
        receipts.seal(step.name,
            {name: fixtures.receipts.sha256(workspace/name) for name in step.inputs},
            list(step.outputs))
    if proof == 'oom':
        logs = workspace/'unit-logs'
        logs.mkdir()
        import json
        (logs/(active.unit_name+'.exit.json')).write_text(json.dumps({
            'schema': 2, 'completed': True, 'returncode': 1,
            'service': {'LoadState': 'loaded', 'ActiveState': 'failed',
                        'Result': 'oom-kill', 'ExecMainCode': '2', 'ExecMainStatus': '9'}}))
    path = {'report': workspace/'source-probe-report.json',
            'receipt': workspace/'.receipts'/'source_probe.json',
            'source': workspace/'source.audio'}[evidence]
    assert path.is_file()
    if damage == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'damaged historical probe evidence')
    await first.close()
    reopened = base.ledger.JobStore(store.path)
    runner = base.runner_module.UnitRunner(tmp_path/'missing-executor', tmp_path/'missing-python')
    observations = []
    async def state(unit):
        observations.append(unit)
        assert unit == active.unit_name
        if proof == 'unknown':
            raise base.runner_module.UnitError('unit_status_unknown', 'mock systemd unavailable')
        if proof == 'active':
            return 'active'
        return 'absent' if proof in ('absent', 'cancelled') else 'stopped'
    monkeypatch.setattr(runner, 'state', state)
    async def forbidden_launch(*args, **kwargs):
        raise AssertionError('Recovery must not launch or retry')
    monkeypatch.setattr(runner, 'run', forbidden_launch)
    coordinator = coordinator_module.StageCoordinator(reopened, runner)
    second, _, _, _, downloads = base.make_service(tmp_path, store=reopened,
        catalogue=catalogue, coordinator=coordinator, probe=fixtures.media.probe_download,
        publisher=base.artifact_module.publish_job)
    monkeypatch.setattr(second.inventory, 'build_runtime', forbid_inventory)
    monkeypatch.setattr(second.inventory, 'build', forbid_inventory)
    preflight_calls = []
    real_verify = base.service_module.verify_source_preflight
    def verify(*args):
        assert observations == [active.unit_name]  # exact reconciliation came first
        assert reopened.stage_attempts(queued.id, queued.stream_id)[-1]['status'] == 'completed'
        preflight_calls.append(True)
        return real_verify(*args)
    monkeypatch.setattr(base.service_module, 'verify_source_preflight', verify)
    if proof == 'cancelled':
        await second.cancel(queued.id, queued.stream_id)
    try:
        if proof == 'unknown':
            with pytest.raises(base.runner_module.UnitError):
                await second.run_once()
        else:
            await second.run_once()
        current = reopened.get(queued.id, queued.stream_id)
        attempts = reopened.stage_attempts(queued.id, queued.stream_id)
        assert observations == [active.unit_name]
        assert len(attempts) == 2 and current.artifact_key is None
        assert downloads == [] and second._generation is None
        # A changed decode input invalidates its own receipt during reconciliation;
        # damaged report/probe receipt instead reaches the shared preflight fence.
        assert preflight_calls == ([True] if proof == 'completed' and evidence != 'source' else [])
        if proof in ('active', 'unknown'):
            assert current.state == 'running' and current.unit_name == active.unit_name
            assert attempts[-1]['status'] == 'claimed'
        else:
            expected = ('cancelled' if proof == 'cancelled' else
                        'interrupted' if proof == 'absent' or
                        (proof == 'completed' and evidence == 'source') else 'failed')
            assert current.state == expected
            assert attempts[-1]['status'] != 'claimed'
            assert reopened.claim_next() is None  # terminal job is not automatically retried
            if proof == 'oom':
                assert current.error['code'] == 'unit_oom'
            if proof == 'cancelled':
                assert current.error['code'] == 'cancelled'
    finally:
        await second.close()
