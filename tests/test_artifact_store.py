"""Synthetic bytes/receipts test storage, NOT real model or media validity."""
from pathlib import Path
import hashlib
import importlib
import importlib.util
import json
import os
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('artifact_test_pkg',ROOT/'__init__.py',submodule_search_locations=[str(ROOT)])
pkg=importlib.util.module_from_spec(spec);sys.modules[spec.name]=pkg;spec.loader.exec_module(pkg)
artifacts=importlib.import_module('artifact_test_pkg.services.artifact_store')
recipes=importlib.import_module('artifact_test_pkg.runtime.recipe_identity')
plan_module=importlib.import_module('artifact_test_pkg.runtime.render_plan')
receipts=importlib.import_module('artifact_test_pkg.runtime.stage_receipts')
ledger=importlib.import_module('artifact_test_pkg.services.job_store')
source=importlib.import_module('artifact_test_pkg.services.source_offer')


@pytest.fixture
def prepared(tmp_path):
    work=tmp_path/'work';work.mkdir()
    root=tmp_path/'library';root.mkdir()
    (work/'source.audio').write_bytes(b'fixed synthetic source bytes')
    paths=dict(workspace=str(work),worker_python='/python',worker_script='/media.py',
               rvc_script='/rvc.py',model='/model.pth',index='/index',hubert='/hubert.pt',
                demucs_repo='/offline-model-repo')
    plan=plan_module.build_plan(**paths,frames=31*44100,instrumental=True)
    hashes={k:format(i+1,'064x') for i,k in enumerate(recipes.HASH_NAMES)}
    hashes['source']=receipts.sha256(work/'source.audio')
    document=recipes.recipe_document(provider='163',track_id='synthetic',hashes=hashes,
        versions={k:'1.2.3' for k in recipes.VERSION_NAMES},steps=plan,**paths)
    key=recipes.fingerprint(document)
    stage_store=receipts.StageReceipts(work,key)
    for step in plan:
        for name in step.outputs:
            if not (work/name).exists():
                (work/name).write_bytes(('synthetic output '+name).encode())
        inputs={name:receipts.sha256(work/name) for name in step.inputs}
        stage_store.seal(step.name,inputs,list(step.outputs))
    return artifacts.ArtifactStore(root,min_free_bytes=0),work,document,key,plan


def publish(prepared):
    store,work,recipe,key,plan=prepared
    return store.publish(work,recipe,title='Synthetic tone',artist='Test',album='Fixture')


def job_ready_to_publish(prepared):
    artifacts_store,work,recipe,key,plan=prepared
    store=ledger.JobStore(work.parent/'jobs.sqlite3')
    store.bind_artifact_root(str(artifacts_store.root))
    job,_=store.submit('verified-test-stream','fixed-test-message',{'query':'synthetic'},
                       auto_reply=True,consent_event='fixed-test-message')
    offer=store.offer(job.id,job.stream_id,[source.CatalogueItem('163','synthetic','Synthetic','Test','Fixture')],
                      expected_revision=job.revision)
    store.select(job.id,job.stream_id,offer.offer_id,1)
    job=store.claim_next()
    for step in plan:
        owner=store.claim_step(job.id,job.run_token,step.name)
        store.settle_step(job.id,job.run_token,owner.unit_name,completed=True)
    return store,store.get(job.id,job.stream_id)


def publish_job(prepared,store,job):
    audio,work,recipe,key,plan=prepared
    return artifacts.publish_job(store,audio,job_id=job.id,stream_id=job.stream_id,
        run_token=job.run_token,workspace=work,recipe=recipe)


def test_commit_is_immutable_browsable_and_replayable_without_scratch(prepared):
    store,work,recipe,key,plan=prepared
    result=publish(prepared)
    assert result.key==key and result.path.read_bytes()==(work/'cover.mp3').read_bytes()
    assert result.path.stat().st_mode & 0o222==0
    metadata=result.path.with_name('metadata.json').read_bytes()
    assert b'${WORK}' in metadata and str(work).encode() not in metadata
    assert store.reconcile_catalog()==[]
    entries=json.loads((store.root/'library-index.json').read_text())['entries']
    assert len(entries)==1 and os.path.samefile(result.path,store.root/entries[0]['file'])
    (work/'source.audio').unlink()  # storage retry relies on permanent commit now
    assert publish(prepared)==result
    assert result.path.with_name('metadata.json').read_bytes()==metadata


@pytest.mark.parametrize('change',['missing_validation','source','chunk','recipe'])
def test_no_publication_with_incomplete_or_changed_evidence(prepared,change):
    store,work,recipe,key,plan=prepared
    if change=='missing_validation': (work/'.receipts/validate.json').unlink()
    if change=='source': (work/'source.audio').write_bytes(b'different source')
    if change=='chunk': (work/'vocal_000_converted.wav').write_bytes(b'tampered')
    if change=='recipe': recipe['hashes']['hubert']='f'*64
    with pytest.raises(artifacts.ArtifactError):publish(prepared)
    assert not (store.root/key).exists()


def test_corrupt_existing_artifact_is_never_overwritten(prepared):
    store,work,recipe,key,plan=prepared
    result=publish(prepared)
    os.chmod(result.path,0o600);result.path.write_bytes(b'corrupt existing bytes')
    with pytest.raises(artifacts.ArtifactError,match='never overwrite'):publish(prepared)
    assert result.path.read_bytes()==b'corrupt existing bytes'


def test_rename_race_cannot_replace_even_empty_destination(prepared,monkeypatch):
    store,work,recipe,key,plan=prepared
    rename=artifacts._rename_exclusive
    def race(source,destination):
        destination.mkdir()
        rename(source,destination)
    monkeypatch.setattr(artifacts,'_rename_exclusive',race)
    with pytest.raises(FileExistsError):publish(prepared)
    assert list((store.root/key).iterdir())==[]
    attempts=list((store.root/'.publishing').iterdir())
    assert len(attempts)==1 and (attempts[0]/'cover.mp3').exists()


def test_crash_before_commit_keeps_evidence_and_retry_is_safe(prepared,monkeypatch):
    store,work,recipe,key,plan=prepared
    rename=artifacts._rename_exclusive
    def crash(*args):raise OSError('synthetic pre-commit crash')
    monkeypatch.setattr(artifacts,'_rename_exclusive',crash)
    with pytest.raises(OSError):publish(prepared)
    attempts=list((store.root/'.publishing').iterdir())
    original=(attempts[0]/'cover.mp3').read_bytes()
    assert not (store.root/key).exists()
    monkeypatch.setattr(artifacts,'_rename_exclusive',rename)
    assert publish(prepared).path.read_bytes()==original
    assert (attempts[0]/'cover.mp3').read_bytes()==original


def test_crash_after_rename_is_reopened_without_rewriting(prepared,monkeypatch):
    store,work,recipe,key,plan=prepared
    sync=artifacts._sync_directory
    def crash(path):
        if path==store.root:raise OSError('synthetic post-rename crash')
        sync(path)
    monkeypatch.setattr(artifacts,'_sync_directory',crash)
    with pytest.raises(OSError):publish(prepared)
    path=store.root/key/'cover.mp3';inode=path.stat().st_ino
    monkeypatch.setattr(artifacts,'_sync_directory',sync)
    assert publish(prepared).path.stat().st_ino==inode


def test_commit_to_ledger_gap_replays_and_catalog_error_is_separate(prepared,monkeypatch):
    audio,work,recipe,key,plan=prepared
    store,job=job_ready_to_publish(prepared)
    original=store.ready
    def crash(*args,**kwargs):raise RuntimeError('crash before ledger ready')
    monkeypatch.setattr(store,'ready',crash)
    with pytest.raises(RuntimeError):publish_job(prepared,store,job)
    committed=audio.verify(key)
    assert store.get(job.id,job.stream_id).state=='running'
    # An unrelated README must never be overwritten, nor undo audio commit.
    (audio.root/'README.md').write_text('my private unrelated notes')
    monkeypatch.setattr(store,'ready',original)
    result=publish_job(prepared,store,job)
    assert result['job'].state=='ready' and result['artifact']==committed
    assert result['catalog_warnings']==['catalog_rebuild_failed']
    assert (audio.root/'README.md').read_text()=='my private unrelated notes'
    assert publish_job(prepared,store,job)['artifact']==committed


def test_cancel_during_copy_preserves_audio_but_disables_delivery(prepared,monkeypatch):
    audio,work,recipe,key,plan=prepared
    store,job=job_ready_to_publish(prepared)
    original=audio.publish
    def cancel_after_copy(*args,**kwargs):
        value=original(*args,**kwargs)
        store.cancel(job.id,job.stream_id)
        return value
    monkeypatch.setattr(audio,'publish',cancel_after_copy)
    result=publish_job(prepared,store,job)
    assert result['job'].state=='ready' and result['job'].delivery_state=='cancelled'
    assert store.claim_delivery(job.id,job.stream_id) is None
    assert audio.verify(key)==result['artifact']


def test_oversized_existing_media_is_rejected_before_hash(prepared,monkeypatch):
    audio,work,recipe,key,plan=prepared
    result=publish(prepared)
    os.chmod(result.path,0o600)
    with result.path.open('r+b') as output:output.truncate(1025)
    bounded=artifacts.ArtifactStore(audio.root,max_bytes=1024,min_free_bytes=0)
    def unexpected_hash(path):raise AssertionError('must reject by size first')
    monkeypatch.setattr(artifacts.library_catalog,'_sha256',unexpected_hash)
    with pytest.raises(artifacts.ArtifactError):bounded.verify(key)


@pytest.mark.parametrize('corrupt',['missing_validation','bad_recipe'])
def test_catalog_and_artifact_reject_same_current_schema_corruption(prepared,corrupt):
    audio,work,recipe,key,plan=prepared
    result=publish(prepared)
    path=result.path.with_name('metadata.json')
    data=json.loads(path.read_text())
    if corrupt=='missing_validation':
        data.pop('validation')
    else:
        data['recipe']['versions']={}
        new_key=hashlib.sha256(json.dumps(data['recipe'],sort_keys=True,separators=(',',':'),
                                        ensure_ascii=False).encode()).hexdigest()
        data['key']=new_key;data['validation']['recipe']=new_key
        path.parent.rename(audio.root/new_key)
        path=audio.root/new_key/'metadata.json'
        key=new_key
    os.chmod(path,0o600);path.write_text(json.dumps(data))
    with pytest.raises(artifacts.ArtifactError):audio.verify(key)
    with pytest.raises(ValueError):artifacts.library_catalog.rebuild_library(audio.root)
    warnings=audio.reconcile_catalog()
    assert len(warnings)==1
    assert json.loads((audio.root/'library-index.json').read_text())['entries']==[]


def test_deep_corrupt_neighbor_becomes_catalog_warning_after_ready(prepared):
    audio,work,recipe,key,plan=prepared
    store,job=job_ready_to_publish(prepared)
    bad=audio.root/('f'*64);bad.mkdir()
    (bad/'metadata.json').write_text('['*2000+'0'+']'*2000)
    result=publish_job(prepared,store,job)
    assert result['job'].state=='ready'
    assert len(result['catalog_warnings'])==1
    assert audio.verify(key)==result['artifact']


def test_readonly_mode_is_applied_before_file_fsync(prepared,monkeypatch):
    import stat
    synced=[]
    original=artifacts.os.fsync
    def checked(fd):
        info=os.fstat(fd)
        if stat.S_ISREG(info.st_mode):
            assert info.st_mode & 0o222==0
            synced.append(fd)
        return original(fd)
    monkeypatch.setattr(artifacts.os,'fsync',checked)
    publish(prepared)
    assert len(synced)==2


def test_no_publication_before_all_attempts_settled(prepared):
    audio,work,recipe,key,plan=prepared
    store,job=job_ready_to_publish(prepared)
    # A new unfinished stage must make even already valid media ineligible.
    store.claim_step(job.id,job.run_token,'post_validate')
    with pytest.raises(ledger.JobConflict):publish_job(prepared,store,job)
    assert not (audio.root/key).exists()
