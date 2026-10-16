"""Actual publication with genuine probe/audio receipts; synthetic media only.

No decoding, model, systemd, network or QQ execution. These tests do not certify
media validity: they exercise receipt bindings and the immutable commit boundary.
"""
from pathlib import Path
import importlib
import importlib.util
import json
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('source_publication_fixtures',
                                              ROOT/'tests/test_artifact_store.py')
fixtures = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixtures
spec.loader.exec_module(fixtures)
media = importlib.import_module('artifact_test_pkg.services.media_probe')


def seal_probe(prepared):
    audio, work, recipe, key, plan = prepared
    request = {'schema': 'sing-source-probe-v2',
        'selected': {'provider': '163', 'track_id': 'synthetic', 'duration_s': None},
        'render_mode': recipe['render_mode'],
        'source_max_seconds': 900 if recipe['render_mode'] == 'excerpt' else 300,
        'context_max_seconds': 90, 'context_policy': 'centered-source-v1',
        'context_helper_sha256': recipe['hashes']['context_window'],
        'max_bytes': 64*1024*1024,
        'source_sha256': recipe['hashes']['source'],
        'worker_sha256': recipe['hashes']['worker'],
        'worker_python': '/python', 'worker_script': str(work.parent/'probe-worker.py'),
        'inference_lock': str(work.parent/'inference.lock')}
    (work/'source-probe-request.json').write_text(json.dumps(request))
    step = media.probe_step(work, request)
    probe_key = media.probe_key(request)
    frozen = {'schema': 1, 'workspace': str(work), 'recipe': probe_key,
        'inference_lock': request['inference_lock'], 'steps': [
            {'name': step.name, 'argv': list(step.argv), 'timeout_s': step.timeout_s,
             'inputs': list(step.inputs), 'outputs': list(step.outputs)}]}
    (work/'source-probe-plan.json').write_text(json.dumps(frozen))
    report = {'duration_s': recipe['source_frames']/44100, 'container_duration_s': 31.04,
        'frames': recipe['source_frames'], 'source_frames': recipe['source_frames'],
        'context_window': dict(recipe['context_window']), 'render_mode': recipe['render_mode'],
        'sample_rate': 44100, 'audio_streams': 1,
        'provider': '163', 'source_id': 'synthetic'}
    (work/'source-probe-report.json').write_text(json.dumps(report))
    ledger = fixtures.receipts.StageReceipts(work, probe_key)
    ledger.seal(step.name,
        {name: fixtures.receipts.sha256(work/name) for name in step.inputs},
        list(step.outputs))
    return request, report, probe_key


@pytest.fixture
def prepared_probe(tmp_path):
    prepared = fixtures.prepare_artifact(tmp_path)
    seal_probe(prepared)
    store, job = fixtures.job_ready_to_publish(prepared, prefix=('source_probe',))
    return prepared, store, job


def count_real_publish(monkeypatch, audio):
    calls = []
    real = audio.publish
    def counted(*args, **kwargs):
        calls.append(True)
        return real(*args, **kwargs)
    monkeypatch.setattr(audio, 'publish', counted)
    return calls


def test_completed_independent_probe_and_audio_receipts_reach_real_commit(prepared_probe, monkeypatch):
    prepared, store, job = prepared_probe
    audio, work, recipe, key, plan = prepared
    calls = count_real_publish(monkeypatch, audio)
    probe_key = json.loads((work/'source-probe-plan.json').read_text())['recipe']
    assert probe_key != key
    assert 'source_probe' not in [step['name'] for step in recipe['steps']]
    result = fixtures.publish_job(prepared, store, job)
    assert calls == [True]
    assert result['job'].state == 'ready' and result['job'].artifact_key == key
    assert audio.verify(key) == result['artifact']
    metadata = json.loads(result['artifact'].path.with_name('metadata.json').read_text())
    assert metadata['recipe'] == recipe
    assert metadata['validation']['recipe'] == key
    assert metadata['validation']['stage'] == 'validate'
    assert 'source_probe' not in json.dumps(metadata)


@pytest.mark.parametrize('change', ['modified', 'deleted'])
def test_completed_publication_is_independent_of_live_worker(prepared_probe, monkeypatch, change):
    prepared, store, job = prepared_probe
    audio, work, recipe, key, plan = prepared
    calls = count_real_publish(monkeypatch, audio)
    worker_path = Path(json.loads((work/'source-probe-request.json').read_text())['worker_script'])
    if change == 'modified':
        worker_path.write_bytes(b'new administrator worker, not the completed execution')
    else:
        worker_path.unlink()
    # The immutable boundary binds request WorkerSHA to recipe and the exact
    # request/source/report bytes to receipts, not today's external worker file.
    result = fixtures.publish_job(prepared, store, job)
    assert calls == [True] and result['job'].state == 'ready'
    assert audio.verify(key) == result['artifact']


@pytest.mark.parametrize('change', [
    'missing_request', 'missing_plan', 'missing_receipt', 'missing_report',
    'mutated_request', 'plan_argv', 'plan_lock', 'plan_key', 'plan_workspace',
    'oversized_report', 'receipt_input', 'receipt_outputs', 'receipt_key',
    'receipt_report_bytes', 'unsealed_report', 'sealed_bad_identity',
    'sealed_bad_frames', 'sealed_bad_source_frames', 'sealed_bad_context',
    'sealed_bad_mode', 'sealed_other_source', 'sealed_bad_schema', 'source_recipe_hash',
    'worker_recipe_hash', 'source_bytes', 'request_worker_sha',
    'claimed_probe', 'failed_probe', 'duplicate_probe', 'unknown_prefix',
    'missing_audio_receipt', 'mutated_audio_receipt'])
def test_probe_or_render_evidence_corruption_is_rejected_before_publish(
        prepared_probe, monkeypatch, change):
    prepared, store, job = prepared_probe
    audio, work, recipe, key, plan = prepared
    calls = count_real_publish(monkeypatch, audio)
    request_path = work/'source-probe-request.json'
    plan_path = work/'source-probe-plan.json'
    report_path = work/'source-probe-report.json'
    receipt_path = work/'.receipts/source_probe.json'
    if change.startswith('missing_') and change != 'missing_audio_receipt':
        {'missing_request': request_path, 'missing_plan': plan_path,
         'missing_receipt': receipt_path, 'missing_report': report_path}[change].unlink()
    elif change == 'mutated_request':
        value = json.loads(request_path.read_text()); value['source_max_seconds'] = 299
        request_path.write_text(json.dumps(value))
    elif change.startswith('plan_'):
        value = json.loads(plan_path.read_text())
        if change == 'plan_argv': value['steps'][0]['argv'][-1] = str(work/'wrong.json')
        elif change == 'plan_lock': value['inference_lock'] = str(work/'different.lock')
        elif change == 'plan_key': value['recipe'] = 'f'*64
        elif change == 'plan_workspace': value['workspace'] = str(work.parent)
        plan_path.write_text(json.dumps(value))
    elif change == 'oversized_report': report_path.write_bytes(b'x'*65537)
    elif change.startswith('receipt_'):
        value = json.loads(receipt_path.read_text())
        if change == 'receipt_input': value['inputs']['source.audio'] = 'f'*64
        elif change == 'receipt_outputs': value['outputs']['extra.json'] = value['outputs']['source-probe-report.json']
        elif change == 'receipt_key': value['recipe'] = key
        elif change == 'receipt_report_bytes': value['outputs']['source-probe-report.json']['bytes'] += 1
        receipt_path.write_text(json.dumps(value))
    elif change == 'unsealed_report': report_path.write_text('{}')
    elif change.startswith('sealed_bad_') or change == 'sealed_other_source':
        value = json.loads(report_path.read_text())
        if change == 'sealed_bad_identity': value['source_id'] = 'other'
        elif change == 'sealed_bad_frames': value['sample_rate'] = 48000
        elif change == 'sealed_bad_source_frames': value['source_frames'] += 1
        elif change == 'sealed_bad_context': value['context_window']['start_frame'] += 1
        elif change == 'sealed_bad_mode': value['render_mode'] = 'excerpt'
        elif change == 'sealed_other_source':
            value['frames'] = value['source_frames'] = 32*44100
            value['duration_s'] = 32.0
            value['context_window'] = fixtures.contexts.full_context(32*44100)
        elif change == 'sealed_bad_schema': value['extra'] = True
        report_path.write_text(json.dumps(value))
        probe_key = json.loads(plan_path.read_text())['recipe']
        receipt_path.unlink()
        fixtures.receipts.StageReceipts(work, probe_key).seal('source_probe',
            {name: fixtures.receipts.sha256(work/name) for name in ('source.audio', 'source-probe-request.json')},
            ['source-probe-report.json'])
    elif change == 'source_recipe_hash': recipe['hashes']['source'] = 'f'*64
    elif change == 'worker_recipe_hash': recipe['hashes']['worker'] = 'f'*64
    elif change == 'source_bytes': (work/'source.audio').write_bytes(b'changed')
    elif change == 'request_worker_sha':
        value = json.loads(request_path.read_text()); value['worker_sha256'] = 'f'*64
        request_path.write_text(json.dumps(value))
    elif change in ('claimed_probe', 'failed_probe', 'duplicate_probe', 'unknown_prefix'):
        # Deliberately corrupt persisted history, not a mocked stage_attempts or
        # publisher: the real publication guard must distrust all such ledgers.
        with store._transaction() as db:
            if change in ('claimed_probe', 'failed_probe'):
                status = 'claimed' if change == 'claimed_probe' else 'interrupted'
                db.execute('UPDATE stage_attempts SET status=? WHERE job_id=? AND step=?',
                           (status, job.id, 'source_probe'))
            elif change == 'duplicate_probe':
                db.execute('UPDATE stage_attempts SET step=? WHERE job_id=? AND step=?',
                           ('source_probe', job.id, 'decode'))
            else:
                db.execute('UPDATE stage_attempts SET step=? WHERE job_id=? AND step=?',
                           ('unknown_prefix', job.id, 'source_probe'))
    elif change == 'missing_audio_receipt': (work/'.receipts/validate.json').unlink()
    elif change == 'mutated_audio_receipt':
        path = work/'.receipts/validate.json'
        value = json.loads(path.read_text()); value['inputs']['cover.mp3'] = 'f'*64
        path.write_text(json.dumps(value))
    with pytest.raises((fixtures.ledger.JobConflict, fixtures.artifacts.ArtifactError)):
        fixtures.publish_job(prepared, store, job)
    # Audio receipts are checked inside real publish, but preflight/history
    # corruption must be rejected BEFORE even entering its copy transaction.
    assert calls == ([True] if change in ('missing_audio_receipt', 'mutated_audio_receipt') else [])
    assert not (audio.root/key).exists() and not (audio.root/'.publishing').exists()
    assert store.get(job.id, job.stream_id).artifact_key is None


def test_cancel_requested_before_real_publish_calls_zero_artifact_publish(prepared_probe, monkeypatch):
    prepared, store, job = prepared_probe
    audio, work, recipe, key, plan = prepared
    calls = count_real_publish(monkeypatch, audio)
    store.cancel(job.id, job.stream_id)
    with pytest.raises(fixtures.ledger.JobConflict, match='Cancellation requested'):
        fixtures.publish_job(prepared, store, job)
    assert calls == [] and not (audio.root/key).exists()
    assert not (audio.root/'.publishing').exists()
    assert store.get(job.id, job.stream_id).artifact_key is None


def test_ready_replay_keeps_committed_artifact_without_probe_scratch(prepared_probe, monkeypatch):
    import shutil
    prepared, store, job = prepared_probe
    audio, work, recipe, key, plan = prepared
    committed = fixtures.publish_job(prepared, store, job)['artifact']
    store.cancel(job.id, job.stream_id)
    shutil.rmtree(work)
    calls = count_real_publish(monkeypatch, audio)
    replay = fixtures.publish_job(prepared, store, job)
    assert calls == [] and replay['artifact'] == committed
    assert replay['job'].state == 'ready' and replay['job'].delivery_state == 'cancelled'
    assert store.claim_delivery(job.id, job.stream_id) is None
    assert audio.verify(key) == committed
