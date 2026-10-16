"""Shared admission policy with a synthetic catalogue; no model or subprocess."""
from pathlib import Path
import asyncio
import importlib
import importlib.util
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'cover_admission_test_pkg', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
service = importlib.import_module('cover_admission_test_pkg.services.job_service')
ledger = importlib.import_module('cover_admission_test_pkg.services.job_store')
offers = importlib.import_module('cover_admission_test_pkg.services.source_offer')
inventory_module = importlib.import_module('cover_admission_test_pkg.services.asset_inventory')
recipes = importlib.import_module('cover_admission_test_pkg.runtime.recipe_identity')
Item = offers.CatalogueItem


class Catalogue:
    def __init__(self, items):
        self.items = items
        self.calls = []
        self.duration_limits = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def search(self, query, provider, limit, *, max_duration_s):
        self.calls.append((query,provider,limit))
        self.duration_limits.append(max_duration_s)
        # Preserve rank, but derive duration eligibility from this request's mode.
        from dataclasses import replace
        items = [replace(item, availability='over_limit')
                 if item.duration_s is not None and item.duration_s > max_duration_s
                 else item for item in self.items]
        self.entered.set()
        await self.release.wait()
        return items


def make_service(tmp_path, items):
    store = ledger.JobStore(tmp_path / 'jobs.sqlite3')
    # Hash tiny real files through the production inventory API. Only the
    # version-provider callable is synthetic; no model imports or workers run.
    files = {}
    for name in recipes.HASH_NAMES:
        if name == 'source':
            continue
        path = tmp_path / ('upstream' if name == 'rvc_upstream' else 'asset-' + name)
        if name == 'rvc_upstream':
            path.mkdir()
            (path / 'infer.py').write_text('synthetic wrapper source')
        elif name == 'demucs_repo':
            path.mkdir()
            (path / 'htdemucs.yaml').write_text("models: ['955717e8']\n")
            (path / '955717e8-8726e21a.th').write_bytes(b'synthetic checkpoint')
        else:
            path.write_bytes(('synthetic-' + name).encode())
        files[name] = path
    paths = inventory_module.AssetPaths(**files)
    inventory = inventory_module.AssetInventory(
        paths, lambda: {name: '1.2.3' for name in recipes.VERSION_NAMES})
    python = tmp_path / 'never-executed-python'
    python.write_bytes(b'synthetic interpreter')
    inference_lock = tmp_path / 'inference.lock'
    runtime = service.RenderRuntime(work_root=tmp_path / 'work',worker_python=python,
        worker_script=paths.media_stage,rvc_script=paths.rvc_script,
        model=paths.model,index=paths.index,hubert=paths.hubert,
        demucs_repo=paths.demucs_repo,inference_lock=inference_lock)
    artifact_root = tmp_path / 'artifacts'
    runtime_context = {
        'execution_paths': {
            **files, 'worker_python': python, 'worker_script': paths.media_stage,
            'inference_lock': inference_lock},
        'artifact_root': artifact_root,
        'limits': {'max_duration_s': runtime.max_duration_s,
                   'max_download_bytes': runtime.max_download_bytes},
        'parameter_policy': service.RENDER_PARAMETER_POLICY}
    catalogue = Catalogue(items)
    jobs = service.JobService(store,catalogue,None,artifact_root,inventory,runtime,
                              runtime_context=runtime_context)
    return jobs,store,catalogue


async def admit(jobs, *, title='Song', artist='Artist', token='message',
                album='', source_id='', request=None, auto_reply=False, consent_event=None):
    return await jobs.search_and_admit(title,artist,'163',5,'chat',token,
        request or {'title':title,'artist':artist,'auto_reply':auto_reply},
        album=album,source_id=source_id,auto_reply=auto_reply,consent_event=consent_event)


@pytest.mark.asyncio
@pytest.mark.parametrize('render_mode,duration,eligible', [
    ('excerpt',318.186,True), ('full',318.186,False),
    ('excerpt',900,True), ('excerpt',900.001,False)])
async def test_first_exact_candidate_obeys_mode_limit_without_skipping(
        tmp_path, render_mode, duration, eligible):
    items = [Item('163','first','Song','Artist','Album',duration),
             Item('163','shorter','Song (Live)','Artist','Live',30)]
    jobs, store, catalogue = make_service(tmp_path, items)
    request = {'title':'Song','artist':'Artist','entry_kind':'tool',
               'source_selection_policy':'first-candidate-v1','render_mode':render_mode,
               'auto_reply':False}
    try:
        if eligible:
            job = await admit(jobs, request=request)
            assert job.state == 'queued'
            assert job.selected_source['track_id'] == 'first'
            assert job.selected_source['duration_s'] == duration
        else:
            with pytest.raises(service.CatalogueError) as error:
                await admit(jobs, request=request)
            assert error.value.code == 'source_duration_limit'
            assert store.find_request('chat','message') is None
            assert not jobs._wake.is_set()
        assert catalogue.duration_limits == [900 if render_mode=='excerpt' else 300]
        assert len(catalogue.calls) == 1
    finally:
        await jobs.close()


@pytest.mark.asyncio
async def test_exact_policy_retains_ranking_choices_and_generation(tmp_path):
    items = [Item('163','wrong-artist','Song','Other','Album',30),
             Item('163','wrong-title','Songbird','Artist','Album',30),
             Item('163','live','Song (Live)','Artist','Album',30),
             Item('163','studio','Song','Artist','Album',30)]
    jobs,store,catalogue = make_service(tmp_path,items)
    try:
        job = await admit(jobs)
        assert job.state == 'needs_selection' and job.selected_source is None
        assert [item['track_id'] for item in (await jobs.choices(job.id,'chat'))['items']] == ['live','studio']
        assert job.request['service_generation'] == jobs.generation
        assert len(jobs.generation) == 64
        original_choices = store.choices(job.id,'chat')
        catalogue.items = list(reversed(items))
        assert await admit(jobs) == job
        assert len(catalogue.calls) == 1 and store.choices(job.id,'chat') == original_choices
        with pytest.raises(ledger.JobConflict):
            await admit(jobs,request={'title':'Changed','artist':'Artist','auto_reply':False})
        assert len(catalogue.calls) == 1
    finally:
        await jobs.close()


@pytest.mark.asyncio
async def test_structured_title_and_artist_hyphens_are_not_resplit(tmp_path):
    items = [Item('163','match','Song - Part','Artist - Band','Album',30)]
    jobs,_,catalogue = make_service(tmp_path,items)
    try:
        job = await admit(jobs,title='Song - Part',artist='Artist - Band')
        assert job.state == 'queued' and job.selected_source['track_id'] == 'match'
        assert catalogue.calls == [('Song - Part - Artist - Band','163',5)]
    finally:
        await jobs.close()


@pytest.mark.asyncio
async def test_album_source_filters_and_unavailable_single_are_preserved(tmp_path):
    items = [Item('163','one','Song','Artist','First',30),
             Item('163','two','Song','Artist','Second',30,'requires_login')]
    jobs,store,_ = make_service(tmp_path,items)
    try:
        with pytest.raises(service.CatalogueError, match='均不可选') as error:
            await admit(jobs,album='Second',source_id='two')
        assert error.value.code == 'source_unavailable'
        assert store.find_request('chat','message') is None
        # The low-level immutable-offer API still preserves blocked rows and
        # refuses explicit selection; normal entry admission rejects all-blocked.
        job,_ = await jobs.admit_offer('chat','explicit-offer',
            {'title':'Song','artist':'Artist','auto_reply':False},[items[1]])
        assert job.state == 'needs_selection' and job.selected_source is None
        assert store.choices(job.id,'chat')['items'][0]['track_id'] == 'two'
        with pytest.raises(ledger.JobConflict,match='requires_login'):
            await jobs.select(job.id,'chat',job.offer_id,1)
        with pytest.raises(ValueError,match='未找到'):
            await admit(jobs,token='missing',album='Second',source_id='one')
        assert store.find_request('chat','missing') is None
    finally:
        await jobs.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', ['over_limit', 'requires_login', 'unavailable'])
async def test_all_known_blocked_candidates_refuse_before_persisting_job(tmp_path, blocked):
    duration = 500 if blocked == 'over_limit' else 30
    items = [Item('163','one','Song','Artist','Album',duration,blocked),
             Item('163','two','Song (Live)','Artist','Live',duration,blocked)]
    jobs,store,catalogue = make_service(tmp_path,items)
    try:
        with pytest.raises(service.CatalogueError) as error:
            await admit(jobs)
        expected = 'source_duration_limit' if blocked == 'over_limit' else 'source_unavailable'
        assert error.value.code == expected
        assert '未入队' in str(error.value) and '无需选择序号' in str(error.value)
        if blocked == 'over_limit':
            assert '300 秒' in str(error.value)
        assert store.find_request('chat','message') is None
        assert len(catalogue.calls) == 1 and not jobs._wake.is_set()
    finally:
        await jobs.close()


@pytest.mark.asyncio
async def test_mixed_blocked_offer_keeps_original_order_without_substitution(tmp_path):
    items = [Item('163','blocked','Song','Artist','Album',500,'over_limit'),
             Item('163','possible','Song (Live)','Artist','Live',30,'unknown')]
    jobs,store,_ = make_service(tmp_path,items)
    try:
        job = await admit(jobs)
        assert job.state == 'needs_selection' and job.selected_source is None
        shown = store.choices(job.id,'chat')['items']
        assert [item['track_id'] for item in shown] == ['blocked','possible']
        with pytest.raises(ledger.JobConflict,match='over_limit'):
            await jobs.select(job.id,'chat',job.offer_id,1)
        assert store.get(job.id,'chat').selected_source is None
    finally:
        await jobs.close()


@pytest.mark.asyncio
async def test_concurrent_stable_replay_and_cancelled_rpc_share_one_search(tmp_path):
    jobs,_,catalogue = make_service(tmp_path,[Item('163','one','Song','Artist','Album',30)])
    catalogue.release.clear()
    first = asyncio.create_task(admit(jobs))
    try:
        await catalogue.entered.wait()
        second = asyncio.create_task(admit(jobs))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        catalogue.release.set()
        job = await second
        assert job.state == 'queued' and len(catalogue.calls) == 1
        assert await admit(jobs) == job
        assert len(catalogue.calls) == 1
    finally:
        catalogue.release.set()
        await jobs.close()


@pytest.mark.asyncio
async def test_tool_binding_uses_generated_token_and_failure_permission(tmp_path):
    jobs,store,catalogue = make_service(tmp_path,[Item('163','one','Song','Artist','Album',30)])
    request = {'title':'Song','artist':'Artist','auto_reply':True,'platform':'qq',
               'ingress_proof':ledger.TRUSTED_TOOL_INGRESS,'user_id':'user'}
    try:
        token,existing = await jobs.bind_tool_request('chat','intent-key',request,auto_reply=True)
        assert existing is None and token != 'intent-key'
        job = await admit(jobs,token=token,request=request,auto_reply=True,consent_event=token)
        assert await jobs.bind_tool_request('chat','intent-key',request,auto_reply=True) == (token,job)
        assert len(catalogue.calls) == 1 and job.consent_event == token
        running = store.claim_next()
        store.finish_failure(job.id,running.run_token,{'code':'stage_timeout','message':'private'},
                             expected_unit=None,expected_revision=running.revision)
        assert await jobs.failure_notice_candidates() == [(job.id,'chat')]
        claim = await jobs.claim_failure_notice(job.id,'chat')
        assert claim['code'] == 'stage_timeout'
        assert await jobs.recover_failure_notices() == 1
        assert await jobs.claim_failure_notice(job.id,'chat') is None
        result = await jobs.settle_failure_notice(job.id,claim['token'],'sent',message_id='ack')
        assert result['state'] == 'sent'
    finally:
        await jobs.close()
