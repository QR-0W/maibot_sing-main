"""Lifecycle invariants independent of network, ML and the old cover() implementation."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import importlib
import importlib.util
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1] / 'services'
spec = importlib.util.spec_from_file_location('ledger_test_pkg', ROOT/'__init__.py', submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
ledger = importlib.import_module('ledger_test_pkg.job_store')
choices = importlib.import_module('ledger_test_pkg.source_offer')
JobStore, JobConflict, JobNotFound = ledger.JobStore, ledger.JobConflict, ledger.JobNotFound
Item = choices.CatalogueItem


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path/'jobs.sqlite3', max_pending=3)


def catalogue():
    return [Item('163','first','Creep','Radiohead','Pablo Honey',238.64),
            Item('163','second','Creep','Radiohead','The Best Of',237.923)]


def queued(store, token='message-1', stream='stream-a', consent=True):
    job, created = store.submit(stream,token,{'query':'radiohead creep'},
                                auto_reply=consent, consent_event=token if consent else None)
    assert created
    job = store.offer(job.id,stream,catalogue(),expected_revision=job.revision)
    return store.select(job.id,stream,job.offer_id,1)


def ready(store, consent=True):
    job = queued(store,consent=consent)
    running = store.claim_next()
    assert running.id == job.id
    store.progress(job.id,running.run_token,stage='publishing',done=1,total=1)
    return store.ready(job.id,running.run_token,'a'*64)


def test_submission_returns_durable_search_job_not_a_render(store):
    job, created = store.submit('a','request',{'query':'radiohead creep'})
    assert created and job.state=='searching' and job.run_token is None
    assert store.claim_next() is None
    reopened = JobStore(store.path)
    again, created = reopened.submit('a','request',{'query':'radiohead creep'})
    assert not created and again.id==job.id
    with pytest.raises(JobConflict):
        reopened.submit('a','request',{'query':'another song'})


def test_consent_required_and_never_implicitly_added(store):
    with pytest.raises(ValueError):
        store.submit('a','request',{},auto_reply=True)
    store.submit('a','request',{})
    with pytest.raises(JobConflict):
        store.submit('a','request',{},auto_reply=True,consent_event='new-consent')
    job = ready(store,consent=False)
    assert job.state=='ready' and job.delivery_state=='not_requested'
    assert store.claim_delivery(job.id,job.stream_id) is None


def test_default_first_and_command_choose_share_snapshot(store):
    items = catalogue()
    assert choices.default_row(items,mode='first',requested_artist='Radiohead')==1
    assert choices.default_row(items,mode='manual') is None
    assert choices.default_row(items[:1],mode='manual')==1
    job, _ = store.submit('a','msg',{'query':'radiohead creep'})
    offer = store.offer(job.id,'a',items,expected_revision=0)
    snapshot = store.choices(job.id,'a')
    assert [r['track_id'] for r in snapshot['items']]==['first','second']
    chosen = store.select(job.id,'a',offer.offer_id,1)
    assert chosen.state=='queued' and chosen.selected_source['track_id']=='first'
    assert store.select(job.id,'a',offer.offer_id,1)==chosen
    with pytest.raises(JobConflict):
        store.select(job.id,'a',offer.offer_id,2)


def test_first_result_restriction_never_silently_selects_second(store):
    items = [Item('163','first','Creep','Radiohead','Pablo Honey',238.64,'requires_login'),catalogue()[1]]
    assert choices.default_row(items,mode='first')==1
    job, _ = store.submit('a','msg',{})
    offered = store.offer(job.id,'a',items,expected_revision=0)
    with pytest.raises(JobConflict,match='requires_login'):
        store.select(job.id,'a',offered.offer_id,1)
    assert store.get(job.id,'a').selected_source is None
    assert len(store.choices(job.id,'a')['items'])==2
    assert store.claim_next() is None


def test_first_result_must_respect_explicit_artist_and_version():
    items = catalogue()
    assert choices.default_row(items,mode='first',requested_artist='TLC') is None
    assert choices.default_row(items,mode='first',requested_version='studio') is None
    live = Item('163','live','Creep','Radiohead','Concert',250,version='live')
    assert choices.default_row([live],mode='first',requested_version='studio') is None
    assert choices.default_row([live],mode='first',requested_version='live')==1


def test_source_snapshot_expiry_refresh_and_ownership(store,monkeypatch):
    now=[1000.]
    monkeypatch.setattr(ledger.time,'time',lambda:now[0])
    job, _ = store.submit('a','msg',{})
    offer = store.offer(job.id,'a',catalogue(),expected_revision=0,ttl_s=30)
    with pytest.raises(JobNotFound):
        store.select(job.id,'another-stream',offer.offer_id,1)
    now[0]=1031.
    with pytest.raises(JobConflict,match='expired'):
        store.select(job.id,'a',offer.offer_id,1)
    newer = store.offer(job.id,'a',list(reversed(catalogue())),expected_revision=offer.revision)
    with pytest.raises(JobConflict,match='changed'):
        store.select(job.id,'a',offer.offer_id,1)
    assert store.select(job.id,'a',newer.offer_id,1).selected_source['track_id']=='second'


def test_only_one_worker_can_be_claimed_across_coordinators(store):
    first=queued(store,'first'); queued(store,'second')
    other=JobStore(store.path)
    with ThreadPoolExecutor(max_workers=2) as threads:
        runs=list(threads.map(lambda s:s.claim_next(),[store,other]))
    assert len([r for r in runs if r])==1
    assert next(r for r in runs if r).id==first.id
    assert JobStore(store.path).claim_next() is None


def test_progress_persists_but_stale_worker_and_regression_are_rejected(store):
    queued(store); run=store.claim_next()
    store.progress(run.id,run.run_token,stage='converting',done=2,total=3)
    assert JobStore(store.path).get(run.id,run.stream_id).chunk_done==2
    for token,done,total,stage in [(run.run_token,1,3,'converting'),('wrong',3,3,'converting'),
                                    (run.run_token,2,4,'converting'),(run.run_token,2,3,'downloading')]:
        with pytest.raises(JobConflict):
            store.progress(run.id,token,stage=stage,done=done,total=total)


def test_cancel_does_not_release_worker_until_stop_confirmed(store):
    job=queued(store); queued(store,'second')
    run=store.claim_next()
    cancelled=store.cancel(job.id,job.stream_id)
    assert cancelled.state=='cancel_requested' and cancelled.delivery_state=='cancelled'
    assert store.claim_next() is None
    stopped=store.finish_failure(job.id,run.run_token,{'code':'user_cancelled','message':'Stopped'})
    assert stopped.state=='cancelled'
    assert store.claim_next() is not None


def test_cancel_losing_race_to_commit_keeps_artifact_without_delivery(store):
    job=queued(store); run=store.claim_next()
    store.cancel(job.id,job.stream_id)
    finished=store.ready(job.id,run.run_token,'a'*64)
    assert finished.state=='ready' and finished.artifact_key=='a'*64
    assert store.claim_delivery(job.id,job.stream_id) is None


def test_crash_during_send_becomes_unknown_never_automatically_resent(store):
    job=ready(store)
    claimed=store.claim_delivery(job.id,job.stream_id)
    assert claimed.delivery_state=='dispatching'
    reopened=JobStore(store.path)
    assert reopened.recover_dispatches()==1
    assert reopened.get(job.id,job.stream_id).delivery_state=='unknown'
    assert reopened.claim_delivery(job.id,job.stream_id) is None
    with pytest.raises(JobConflict):
        reopened.cancel(job.id,job.stream_id)
    late=reopened.delivery_result(job.id,claimed.delivery_token,'sent',message_id='platform-123')
    assert late.message_id=='platform-123' and late.artifact_key=='a'*64
    assert reopened.claim_delivery(job.id,job.stream_id) is None


def test_two_coordinators_cannot_send_twice(store):
    job=ready(store)
    other=JobStore(store.path)
    with ThreadPoolExecutor(max_workers=2) as threads:
        claims=list(threads.map(lambda s:s.claim_delivery(job.id,job.stream_id),[store,other]))
    assert len([v for v in claims if v])==1


def test_failure_is_structured_and_never_deletes_checkpoint(store,tmp_path):
    checkpoint=tmp_path/'vocal-9.wav'; checkpoint.write_bytes(b'already-rendered')
    job=queued(store); run=store.claim_next()
    error={'code':'stage_deadline','stage':'converting','message':'Saved 9/12 chunks','done':9,'total':12}
    failed=store.finish_failure(job.id,run.run_token,error,interrupted=True)
    assert failed.state=='interrupted' and failed.error==error
    assert checkpoint.read_bytes()==b'already-rendered'
    assert JobStore(store.path).get(job.id,job.stream_id).artifact_key is None
    assert store.claim_next() is None  # No hidden automatic retry.


def test_stage_unit_is_claimed_before_launch_and_survives_restart(store):
    job=queued(store)
    run=store.claim_next()
    first=store.claim_step(job.id,run.run_token,'convert_000')
    assert first.active_step=='convert_000' and first.unit_name.startswith('maibot-sing-')
    reopened=JobStore(store.path)
    assert reopened.claim_step(job.id,run.run_token,'convert_000').unit_name==first.unit_name
    with pytest.raises(JobConflict,match='reconciled'):
        reopened.claim_step(job.id,run.run_token,'convert_001')
    assert reopened.stage_attempts(job.id,job.stream_id)[0]['status']=='claimed'
    settled=reopened.settle_step(job.id,run.run_token,first.unit_name,completed=True)
    assert settled.unit_name==first.unit_name
    second=reopened.claim_step(job.id,run.run_token,'convert_001')
    assert second.unit_name!=first.unit_name
    with pytest.raises(JobConflict,match='Stale'):
        reopened.settle_step(job.id,run.run_token,first.unit_name,completed=False)
    assert [entry['status'] for entry in reopened.stage_attempts(job.id,job.stream_id)]==['completed','claimed']


def test_interrupted_stage_requires_explicit_reconciliation(store):
    job=queued(store)
    run=store.claim_next()
    first=store.claim_step(job.id,run.run_token,'separate')
    store.settle_step(job.id,run.run_token,first.unit_name,completed=False)
    with pytest.raises(JobConflict,match='explicit retry'):
        JobStore(store.path).claim_step(job.id,run.run_token,'convert_000')
    stopped=store.finish_failure(job.id,run.run_token,
        {'code':'stage_timeout','message':'Service stopped without full separation'},interrupted=True)
    assert stopped.state=='interrupted' and store.claim_next() is None


def test_cancelled_job_cannot_claim_new_stage(store):
    job=queued(store)
    run=store.claim_next()
    store.cancel(job.id,job.stream_id)
    with pytest.raises(JobConflict,match='Cancelled'):
        store.claim_step(job.id,run.run_token,'convert_000')


def test_queue_cap_applies_to_searching_and_history_is_monotonic(store):
    for number in range(3):
        store.submit('a',str(number),{})
    with pytest.raises(JobConflict,match='full'):
        store.submit('b','extra',{})
    job, created=store.submit('a','0',{})
    assert not created
    cancelled=store.cancel(job.id,'a')
    assert cancelled.state=='cancelled'
    store.submit('b','extra',{})
    history=store.history(job.id,'a')
    assert [e['revision'] for e in history]==[0,1]
    with pytest.raises(JobNotFound):
        store.history(job.id,'b')
