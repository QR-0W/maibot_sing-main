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


def selection_document():
    return {'schema':'sing-excerpt-selection-v1','sample_rate':44100,
            'source_frames':31*44100,'output_frames':15*44100,
            'source_ranges':[
                {'start_frame':44101,'end_frame':374851,'output_start_frame':0,
                 'output_end_frame':330750},
                {'start_frame':882001,'end_frame':1212751,'output_start_frame':330750,
                 'output_end_frame':661500}],
            'analysis':{'hop_frames':2205,'rms_threshold':.01,'flatness_max':.5,
                        'breath_gap_frames':26460,'minimum_fragment_frames':52920,
                        'analyzed_hops':620},
            'fades':{'in_frames':2205,'out_frames':5292},
            'normalization':{'target_rms':.12,'applied_gain':1.0}}


def prepare_artifact(tmp_path, render_mode='full'):
    work=tmp_path/'work';work.mkdir()
    root=tmp_path/'library';root.mkdir()
    (work/'source.audio').write_bytes(b'fixed synthetic source bytes')
    paths=dict(workspace=str(work),worker_python='/python',worker_script='/media.py',
               rvc_script='/rvc.py',model='/model.pth',index='/index',hubert='/hubert.pt',
                demucs_repo='/offline-model-repo')
    plan=plan_module.build_plan(**paths,frames=31*44100,instrumental=True,
                               render_mode=render_mode)
    hashes={k:format(i+1,'064x') for i,k in enumerate(recipes.HASH_NAMES)}
    hashes['source']=receipts.sha256(work/'source.audio')
    document=recipes.recipe_document(provider='163',track_id='synthetic',hashes=hashes,
        versions={k:'1.2.3' for k in recipes.VERSION_NAMES},steps=plan,
        render_mode=render_mode,**paths)
    key=recipes.fingerprint(document)
    stage_store=receipts.StageReceipts(work,key)
    for step in plan:
        for name in step.outputs:
            if not (work/name).exists():
                if name=='selection.json':
                    (work/name).write_text(json.dumps(selection_document(),sort_keys=True,
                        separators=(',',':'),ensure_ascii=False,allow_nan=False),encoding='utf-8')
                else:
                    (work/name).write_bytes(('synthetic output '+name).encode())
        inputs={name:receipts.sha256(work/name) for name in step.inputs}
        stage_store.seal(step.name,inputs,list(step.outputs))
    return artifacts.ArtifactStore(root,min_free_bytes=0),work,document,key,plan


@pytest.fixture
def prepared(tmp_path):
    return prepare_artifact(tmp_path)


@pytest.fixture
def excerpt_prepared(tmp_path):
    return prepare_artifact(tmp_path, 'excerpt')


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


def test_excerpt_selection_survives_workspace_removal_and_catalog_is_precise(excerpt_prepared):
    import shutil
    store,work,recipe,key,plan=excerpt_prepared
    result=publish(excerpt_prepared)
    metadata_path=result.path.with_name('metadata.json')
    metadata=json.loads(metadata_path.read_text())
    assert metadata['selection']==selection_document()
    raw=json.dumps(metadata['selection'],sort_keys=True,separators=(',',':'),
                   ensure_ascii=False,allow_nan=False).encode()
    assert metadata['excerpt_receipt']['outputs']['selection.json']=={
        'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    assert metadata['excerpt_receipt']['stage']=='excerpt'
    assert metadata['excerpt_receipt']['recipe']==key
    assert store.reconcile_catalog()==[]
    entry=json.loads((store.root/'library-index.json').read_text())['entries'][0]
    assert entry['excerpt']=='00分01.000秒至00分08.500秒+00分20.000秒至00分27.500秒拼接'
    assert ':' not in Path(entry['file']).name
    assert len(Path(entry['file']).name.encode())<=255
    before=metadata_path.read_bytes()
    shutil.rmtree(work)
    assert store.verify(key)==result
    assert publish(excerpt_prepared)==result
    assert metadata_path.read_bytes()==before


@pytest.mark.parametrize('corrupt',[
    'missing_selection','missing_receipt','source_frames','range','valid_but_unsealed_range',
    'nonfinite','receipt_digest','receipt_bytes','receipt_stage','receipt_recipe',
    'receipt_outputs','receipt_input','bool_bytes'])
def test_malformed_excerpt_metadata_is_rejected_by_store_and_catalog(excerpt_prepared,corrupt):
    store,work,recipe,key,plan=excerpt_prepared
    result=publish(excerpt_prepared)
    path=result.path.with_name('metadata.json')
    metadata=json.loads(path.read_text())
    if corrupt=='missing_selection':metadata.pop('selection')
    elif corrupt=='missing_receipt':metadata.pop('excerpt_receipt')
    elif corrupt=='source_frames':metadata['selection']['source_frames']=True
    elif corrupt=='range':metadata['selection']['source_ranges'][0]['output_end_frame']+=1
    elif corrupt=='valid_but_unsealed_range':
        record=metadata['selection']['source_ranges'][0]
        record['start_frame']+=100;record['end_frame']+=100
    elif corrupt=='nonfinite':metadata['selection']['normalization']['applied_gain']=float('nan')
    elif corrupt=='receipt_digest':metadata['excerpt_receipt']['outputs']['selection.json']['sha256']='0'*64
    elif corrupt=='receipt_bytes':metadata['excerpt_receipt']['outputs']['selection.json']['bytes']+=1
    elif corrupt=='receipt_stage':metadata['excerpt_receipt']['stage']='separate'
    elif corrupt=='receipt_recipe':metadata['excerpt_receipt']['recipe']='f'*64
    elif corrupt=='receipt_outputs':metadata['excerpt_receipt']['outputs'].pop('excerpt_backing.wav')
    elif corrupt=='receipt_input':metadata['excerpt_receipt']['inputs']['vocals.wav']='not-a-digest'
    elif corrupt=='bool_bytes':metadata['excerpt_receipt']['outputs']['vocal_000.wav']['bytes']=True
    os.chmod(path,0o600);path.write_text(json.dumps(metadata))
    with pytest.raises(artifacts.ArtifactError):store.verify(key)
    with pytest.raises(ValueError):artifacts.library_catalog.rebuild_library(store.root)
    assert len(store.reconcile_catalog())==1


def test_selection_file_must_be_canonical_and_match_stage_receipt(excerpt_prepared):
    store,work,recipe,key,plan=excerpt_prepared
    path=work/'selection.json'
    data=json.loads(path.read_text())
    path.write_text(json.dumps(data,indent=2)+'\n')
    # Even a valid receipt for noncanonical JSON must not bless metadata that
    # cannot reproduce the same bytes after the workspace is gone.
    excerpt=next(step for step in plan if step.name=='excerpt')
    (work/'.receipts/excerpt.json').unlink()
    receipts.StageReceipts(work,key).seal('excerpt',
        {name:receipts.sha256(work/name) for name in excerpt.inputs},list(excerpt.outputs))
    # Re-seal dependent receipts too, so rejection comes from canonical evidence.
    for step in plan[3:]:
        (work/'.receipts'/f'{step.name}.json').unlink()
        receipts.StageReceipts(work,key).seal(step.name,
            {name:receipts.sha256(work/name) for name in step.inputs},list(step.outputs))
    with pytest.raises(artifacts.ArtifactError,match='selection'):publish(excerpt_prepared)
    assert not (store.root/key).exists()


def test_v2_committed_artifact_is_readonly_compatible(prepared):
    store,work,recipe,key,plan=prepared
    result=publish(prepared)
    metadata=json.loads(result.path.with_name('metadata.json').read_text())
    legacy=metadata['recipe']
    legacy['schema']='sing-render-v2';legacy.pop('render_mode')
    legacy['hashes'].pop('excerpt_selection')
    # A real v2 plan predates explicit render-mode switches.
    for step in legacy['steps']:
        argv=step['argv']
        if '--render-mode' in argv:
            position=argv.index('--render-mode');del argv[position:position+2]
    old_key=recipes.fingerprint(legacy)
    metadata['key']=old_key;metadata['recipe_schema']='sing-render-v2'
    metadata['parameters']={'policy':'sing-render-v2'}
    metadata['validation']['recipe']=old_key
    folder=store.root/old_key;result.path.parent.rename(folder)
    path=folder/'metadata.json';os.chmod(path,0o600)
    path.write_text(json.dumps(metadata));os.chmod(path,0o444)
    before={name:((folder/name).read_bytes(),(folder/name).stat().st_ino,
                  (folder/name).stat().st_mode) for name in ('metadata.json','cover.mp3')}
    assert store.verify(old_key).key==old_key
    assert store.reconcile_catalog()==[]
    for name,value in before.items():
        item=folder/name
        assert (item.read_bytes(),item.stat().st_ino,item.stat().st_mode)==value
    with pytest.raises(artifacts.ArtifactError,match='Legacy recipes'):
        store.publish(work,legacy,title='No v2 writes',artist='Fixture')


def test_excerpt_publish_recovery_after_rename_is_idempotent(excerpt_prepared,monkeypatch):
    store,work,recipe,key,plan=excerpt_prepared
    sync=artifacts._sync_directory
    def crash(path):
        if path==store.root:raise OSError('synthetic excerpt post-rename crash')
        sync(path)
    monkeypatch.setattr(artifacts,'_sync_directory',crash)
    with pytest.raises(OSError):publish(excerpt_prepared)
    path=store.root/key/'metadata.json';before=path.read_bytes();inode=path.stat().st_ino
    monkeypatch.setattr(artifacts,'_sync_directory',sync)
    result=publish(excerpt_prepared)
    assert result.key==key and path.read_bytes()==before and path.stat().st_ino==inode
