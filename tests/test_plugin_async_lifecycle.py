"""Integration at the SDK boundary; fake catalogue and send, no real QQ."""
from pathlib import Path
from types import SimpleNamespace
import asyncio
import importlib
import importlib.util
import logging
import subprocess
import sys
import threading
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sing_async_test', ROOT / 'plugin.py', submodule_search_locations=[str(ROOT)])
plugin_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin_module
spec.loader.exec_module(plugin_module)


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    logger = logging.getLogger('sing-async-test')
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    instance = plugin_module.create_plugin()
    config = tomllib.loads((ROOT / 'config.example.toml').read_text())
    for name in ('model_path', 'index_path', 'hubert_path', 'rvc_script'):
        path = tmp_path / name
        path.write_bytes(name.encode())
        config['local'][name] = str(path)
    upstream = tmp_path / 'upstream'
    upstream.mkdir()
    (upstream / 'infer.py').write_text('# fake upstream')
    config['local']['rvc_upstream_path'] = str(upstream)
    repo = tmp_path / 'htdemucs-repo'
    repo.mkdir()
    (repo / 'htdemucs.yaml').write_text("models: ['955717e8']\n")
    (repo / '955717e8-8726e21a.th').write_bytes(b'fake checkpoint - no torch import')
    config['local']['demucs_repo_path'] = str(repo)
    config['local'].update(worker_python=sys.executable, musicdl_python=sys.executable,
                           inference_lock=str(tmp_path / '.inference.lock'))
    instance.set_plugin_config(config)
    sends = []

    async def fake_text(text, stream_id):
        sends.append((text, stream_id))
        return True

    async def reject_custom(*args, **kwargs):
        raise AssertionError('No QQ send in plugin lifecycle tests')

    instance._ctx = SimpleNamespace(logger=logger,
        paths=SimpleNamespace(data_dir=str(tmp_path / 'data'), runtime_dir=str(tmp_path / 'runtime')),
        send=SimpleNamespace(text=fake_text, custom=reject_custom))
    async def no_login():
        pass
    class FakeCatalogue:
        async def search(self, query, provider, *, limit):
            return [plugin_module.SongInfo('id-1', 'Song', 'Artist', 'Album', provider, duration_s=120),
                    plugin_module.SongInfo('id-2', 'Song Live', 'Artist', 'Album Live', provider, duration_s=125)]
        async def close(self):
            pass
    version_names=importlib.import_module(
        'sing_async_test.runtime.recipe_identity').VERSION_NAMES
    class FakeVersionProbe:
        def __init__(self, worker_python):
            self.worker_python=worker_python
        def __call__(self):
            return {name:'test-1' for name in version_names}
    monkeypatch.setattr(plugin_module,'RuntimeVersionProbe',FakeVersionProbe)
    monkeypatch.setattr(instance, '_build_music_client', FakeCatalogue)
    monkeypatch.setattr(instance, '_restore_music_logins', no_login)
    return instance, sends


def command(stream='stream-1', msg='platform-message-1', *, user='user-1', text='/翻唱 Song - Artist', groups=None):
    return {'stream_id': stream, 'platform': 'qq', 'user_id': user, 'text': text,
            'matched_groups': groups or {'query': 'Song - Artist'},
            'message': {'message_id': msg, 'platform': 'qq', 'session_id': stream,
                        'processed_plain_text': text, 'is_command': True,
                        'message_info': {'user_info': {'user_id': user},
                                          'group_info': {'group_id': 'trusted-group', 'group_name': 'test'},
                                          'additional_config': {'self_id': 'test-bot',
                                                                'platform_io_account_id': 'test-bot',
                                                                'napcat_message_type': 'group',
                                                                'platform_io_target_group_id': 'trusted-group'}}}}


@pytest.mark.asyncio
@pytest.mark.parametrize('handler,text,groups', [
    ('handle_cover_command', '/翻唱 Song - Artist --auto-reply', {'query':'Song - Artist'}),
    ('handle_cover_select', '/翻唱选择 ' + 'a'*32 + ' 1', {'job_id':'a'*32,'number':'1'}),
    ('handle_cover_status', '/翻唱状态 ' + 'a'*32, {'job_id':'a'*32}),
    ('handle_cover_cancel', '/翻唱取消 ' + 'a'*32, {'job_id':'a'*32}),
    ('handle_speak_command', '/说 hello', {'text':'hello'}),
    ('handle_list_models', '/音色列表', {}),
    ('handle_qq_music_login', '/qq音乐登录', {}),
    ('handle_netease_music_login', '/网易云音乐登录', {}),
    ('handle_netease_cookie_login', '/163cookie secret', {'cookie':'secret'}),
    ('handle_netease_login_test', '/163logintest', {}),
    ('handle_qq_login_test', '/qqlogintest', {}),
])
async def test_virtual_webui_qq_group_spoof_has_zero_effects(plugin, handler, text, groups):
    instance, sends = plugin
    forged = command(text=text, groups=groups)
    # The actual WebUI virtual identity path creates an internally consistent
    # QQ person and arbitrary non-prefixed real group, but only at_bot metadata.
    forged['message']['message_info']['group_info']['group_id'] = '123456789'
    forged['message']['message_info']['additional_config'] = {'at_bot':True}
    forged['matched_groups']['auto_reply'] = '--auto-reply'
    result = await getattr(instance, handler)(**forged)
    assert result[0] is False and sends == []
    assert instance._jobs is None and instance._qq_login_task is None
    assert instance._netease_login_task is None and instance._voice_sender is None


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', ['no_account','wrong_account','wrong_group',
                                    'virtual_group','private_group_mismatch'])
async def test_gateway_route_metadata_requires_coherent_host_proof(plugin, tamper):
    instance, sends = plugin
    raw = command(text='/音色列表')
    info = raw['message']['message_info']
    extra = info['additional_config']
    if tamper == 'no_account':
        extra.pop('platform_io_account_id')
    elif tamper == 'wrong_account':
        extra['platform_io_account_id'] = 'not-the-NapCat-bot'
    elif tamper == 'wrong_group':
        extra['platform_io_target_group_id'] = 'other'
    elif tamper == 'virtual_group':
        info['group_info']['group_id'] = 'webui_virtual_group_123'
        extra['platform_io_target_group_id'] = info['group_info']['group_id']
    else:
        extra['napcat_message_type'] = 'private'
    assert not (await instance.handle_list_models(**raw))[0]
    assert sends == []


def install_cleanup_fakes(instance, monkeypatch, failure, error_type=OSError):
    calls=[]
    async def invoke(label):
        calls.append(label)
        if label==failure:
            raise error_type(label)
    class Outbox:
        async def shutdown(self,*,timeout_s): await invoke('outbox')
    class Jobs:
        async def close(self): await invoke('jobs')
    class Owner:
        def __exit__(self,*args):
            calls.append('owner')
            if failure=='owner': raise error_type('owner')
    class Closer:
        def __init__(self,label): self.label=label
        async def close(self): await invoke(self.label)
    class Voice:
        async def shutdown(self,*,timeout_s): await invoke('voice')
    async def sidecar(proc): await invoke('sidecar')
    monkeypatch.setattr(instance,'_close_sidecar_process',sidecar)
    instance._active_cover=object()
    instance._outbox=Outbox()
    instance._jobs=Jobs()
    instance._scheduler_owner=Owner()
    instance._local=Closer('local')
    instance._voice_sender=Voice()
    instance._music=Closer('music')
    instance._mimo=Closer('mimo')
    instance._rvc=Closer('rvc')
    instance._sidecar_proc=object()
    instance._pipeline=object()
    instance._pending['stream']=([], 'qq', 0)
    async def linger(): await asyncio.sleep(3600)
    delivery=asyncio.create_task(linger())
    login=asyncio.create_task(linger())
    instance._delivery_task=delivery
    instance._qq_login_task=login
    return calls,delivery,login


@pytest.mark.asyncio
async def test_lifecycle_restarts_durable_service_without_deleting_offer(plugin):
    instance, sends = plugin
    await instance.on_load()
    assert instance._local is None
    assert instance._jobs._task is not None
    ok, _, _ = await instance.handle_cover_command(**command())
    assert ok and '请选择明确版本' in sends[-1][0]
    job_id = sends[-1][0].split('任务 ')[1].split('：')[0]
    before = instance._jobs.store.get(job_id, 'stream-1')
    assert before.state == 'needs_selection' and before.consent_event is None
    assert before.delivery_state == 'not_requested'
    async def fake_login():
        await asyncio.sleep(3600)
    login = asyncio.create_task(fake_login())
    instance._qq_login_task = login
    await instance.on_unload()
    assert login.cancelled()
    assert instance._scheduler_owner is None
    assert instance._jobs is None and instance._delivery_task is None and instance._cache_cleanup_task is None
    assert not instance._ctx.paths.data_dir.endswith('nonexistent')
    await instance.on_load()
    assert instance._jobs.store.get(job_id, 'stream-1').state == 'needs_selection'
    first = instance._jobs
    await instance.on_config_update('other-plugin', {}, '1.0.0')
    assert instance._jobs is first
    await instance.on_config_update(plugin_module.CONFIG_RELOAD_SCOPE_SELF, {}, '1.0.1')
    assert instance._jobs is not first
    assert instance._jobs.store.get(job_id, 'stream-1').state == 'needs_selection'
    await instance.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',[
    'outbox','jobs','owner','local','voice','music','mimo','rvc','sidecar'])
async def test_cleanup_attempts_every_resource_after_one_failure(plugin,monkeypatch,failure):
    instance,_=plugin
    calls,delivery,login=install_cleanup_fakes(instance,monkeypatch,failure)
    with pytest.raises(OSError,match=failure):
        await instance._close_resources()
    assert calls==['outbox','jobs','owner','local','voice','music','mimo','rvc','sidecar']
    assert delivery.cancelled() and login.cancelled()
    assert instance._active_cover is None and instance._outbox is None
    assert instance._jobs is None and instance._scheduler_owner is None
    assert instance._local is None and instance._voice_sender is None
    assert instance._music is None and instance._mimo is None and instance._rvc is None
    assert instance._sidecar_proc is None and instance._pipeline is None
    assert instance._pending=={}


@pytest.mark.asyncio
async def test_cleanup_aggregates_multiple_failures_after_all_attempts(plugin,monkeypatch):
    instance,_=plugin
    calls,delivery,login=install_cleanup_fakes(instance,monkeypatch,'outbox')
    class BadJobs:
        async def close(self):
            calls.append('jobs')
            raise OSError('jobs')
    instance._jobs=BadJobs()
    with pytest.raises(ExceptionGroup) as grouped:
        await instance._close_resources()
    messages=' '.join(str(error) for error in grouped.value.exceptions)
    assert 'outbox' in messages and 'jobs' in messages
    assert calls==['outbox','jobs','owner','local','voice','music','mimo','rvc','sidecar']
    assert delivery.cancelled() and login.cancelled()


@pytest.mark.asyncio
async def test_cleanup_preserves_cancellation_after_other_resources(plugin,monkeypatch):
    instance,_=plugin
    calls,delivery,login=install_cleanup_fakes(
        instance,monkeypatch,'music',asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await instance._close_resources()
    assert calls==['outbox','jobs','owner','local','voice','music','mimo','rvc','sidecar']
    assert delivery.cancelled() and login.cancelled()
    assert instance._jobs is None and instance._scheduler_owner is None


@pytest.mark.asyncio
async def test_startup_error_remains_primary_when_cleanup_also_fails(plugin,monkeypatch):
    instance,_=plugin
    async def failed_load():
        raise ValueError('original startup failure')
    async def failed_cleanup():
        raise OSError('cleanup failure')
    monkeypatch.setattr(instance,'_load_resources',failed_load)
    monkeypatch.setattr(instance,'_close_resources',failed_cleanup)
    with pytest.raises(ValueError,match='original startup failure') as error:
        await instance.on_load()
    assert isinstance(error.value.__cause__,OSError)
    assert 'cleanup failure' in str(error.value.__cause__)


@pytest.mark.asyncio
async def test_cleanup_cancellation_remains_primary_over_startup_error(plugin,monkeypatch):
    instance,_=plugin
    async def failed_load():
        raise ValueError('startup before cancellation')
    async def cancelled_cleanup():
        raise asyncio.CancelledError('cleanup cancelled')
    monkeypatch.setattr(instance,'_load_resources',failed_load)
    monkeypatch.setattr(instance,'_close_resources',cancelled_cleanup)
    with pytest.raises(asyncio.CancelledError) as error:
        await instance.on_load()
    assert isinstance(error.value.__cause__,ValueError)


@pytest.mark.asyncio
async def test_command_idempotent_identity_and_tool_fail_closed(plugin):
    instance, sends = plugin
    await instance.on_load()
    try:
        original = command()
        assert (await instance.handle_cover_command(**original))[0]
        job_id = sends[-1][0].split('任务 ')[1].split('：')[0]
        assert (await instance.handle_cover_command(**original))[0]
        assert instance._jobs.store.get(job_id, 'stream-1').state == 'needs_selection'
        forged = command()
        forged['message']['session_id'] = 'different-session'
        assert not (await instance.handle_cover_command(**forged))[0]
        assert instance._jobs.store.get(job_id, 'stream-1').state == 'needs_selection'
        response = await instance.handle_cover_tool(query='Song - Artist', stream_id='forged')
        assert '未入队' in response['content']
        assert (await instance.handle_speak_tool(text='hi', stream_id='forged'))['content'].startswith('工具没有')
        assert instance._jobs.store.get(job_id, 'stream-1').state == 'needs_selection'
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
async def test_explicit_auto_reply_and_selector_replay_fence(plugin):
    instance, sends = plugin
    await instance.on_load()
    try:
        authorized = command(text='/翻唱 Song - Artist --auto-reply')
        assert (await instance.handle_cover_command(**authorized))[0]
        job_id = sends[-1][0].split('任务 ')[1].split('：')[0]
        saved = instance._jobs.store.get(job_id, 'stream-1')
        assert saved.consent_event == 'platform-message-1'
        assert saved.delivery_state == 'pending' and saved.request['auto_reply'] is True
        assert (await instance.handle_cover_command(**authorized))[0]
        # Same original message identity cannot change selector or consent.
        for text in ('/翻唱 Song - Artist',
                     '/翻唱 Song - Artist --album Album --auto-reply',
                     '/翻唱 Song - Artist --source-id id-1 --auto-reply',
                     '/翻唱 Song - Artist -v model --auto-reply'):
            assert not (await instance.handle_cover_command(**command(text=text)))[0]
        forged = command()
        forged['matched_groups'] = {'query': 'Song - Artist', 'auto_reply': '--auto-reply'}
        assert not (await instance.handle_cover_command(**forged))[0]
        tool = await instance.handle_cover_tool(query='Song - Artist', auto_reply=True,
                                                 consent_event='forged', stream_id='stream-1')
        assert '--auto-reply' in tool['content'] and '缺省只保存' in tool['content']
        assert instance._jobs.store.get(job_id, 'stream-1').request == saved.request
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
async def test_chat_cookie_is_never_applied_or_echoed(plugin):
    instance, sends = plugin
    class ForbiddenMusic:
        def apply_netease_cookies(self, *args):
            raise AssertionError('Chat secrets must never enter music client')
    instance._music = ForbiddenMusic()
    secret = 'MUSIC_U=unique-private-secret; __csrf=other-secret'
    result = await instance.handle_netease_cookie_login(**command(
        text=f'/163cookie {secret}', groups={'cookie': secret}))
    assert result[0] is False
    assert '安全配置' in sends[-1][0] and '扫码' in sends[-1][0]
    assert 'unique-private-secret' not in str(result) + str(sends)


@pytest.mark.asyncio
async def test_select_status_cancel_only_owner_and_no_render_rpc(plugin, monkeypatch):
    instance, sends = plugin
    await instance.on_load()
    try:
        assert (await instance.handle_cover_command(**command()))[0]
        job_id = sends[-1][0].split('任务 ')[1].split('：')[0]
        select = command(msg='platform-message-2', text=f'/翻唱选择 {job_id} 2',
                         groups={'job_id': job_id, 'number': '2'})
        select['user_id'] = 'forged'
        assert not (await instance.handle_cover_select(**select))[0]
        assert instance._jobs.store.get(job_id, 'stream-1').state == 'needs_selection'
        other_user = command(msg='platform-message-5', user='user-2', text=f'/翻唱状态 {job_id}', groups={'job_id': job_id})
        assert not (await instance.handle_cover_status(**other_user))[0]
        assert instance._jobs.store.get(job_id, 'stream-1').state == 'needs_selection'
        # Prevent scheduling in this test while retaining command cancel API.
        async def idle():
            return None
        monkeypatch.setattr(instance._jobs, 'run_once', idle)
        select['user_id'] = 'user-1'
        assert (await instance.handle_cover_select(**select))[0]
        state = instance._jobs.store.get(job_id, 'stream-1')
        assert state.state == 'queued' and state.selected_source['track_id'] == 'id-2'
        async def safe_fault(*args):
            return {'schema':1,'code':'unit_status_unknown','message':'无法确认计算单元；任务槽位保留。',
                    'retry_after_s':4,'updated_at':1}
        monkeypatch.setattr(instance._jobs,'coordinator_error',safe_fault)
        assert (await instance.handle_cover_status(**command(msg='platform-message-3', text='/翻唱状态 '+job_id,
            groups={'job_id': job_id})))[0]
        assert 'unit_status_unknown' in sends[-1][0] and '无法确认计算单元' in sends[-1][0]
        assert (await instance.handle_cover_cancel(**command(msg='platform-message-4', text='/翻唱取消 '+job_id,
            groups={'job_id': job_id})))[0]
        assert instance._jobs.store.get(job_id, 'stream-1').state == 'cancelled'
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
async def test_duplicate_host_scheduler_owner_fails_closed(plugin):
    instance, _ = plugin
    await instance.on_load()
    try:
        current = instance._jobs
        settings=instance._capture_durable_settings()
        with pytest.raises(plugin_module.OwnershipBusy):
            await instance._start_durable_services(settings)
        assert instance._jobs is current
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
async def test_real_plan_uses_media_stage_cli_and_reaches_sandbox_guard(plugin):
    instance, _ = plugin
    await instance.on_load()
    try:
        runtime = instance._jobs.runtime
        assert runtime.worker_script == ROOT / 'runtime' / 'media_stage.py'
        from sing_async_test.runtime.render_plan import build_plan
        plan = build_plan(workspace=runtime.work_root / 'example', worker_python=runtime.worker_python,
            worker_script=runtime.worker_script, rvc_script=runtime.rvc_script,
            model=runtime.model, index=runtime.index, hubert=runtime.hubert,
            demucs_repo=runtime.demucs_repo, frames=44_100 * 45)
        for stage in ('separate', 'mix'):
            argv = next(item.argv for item in plan if item.name == stage)
            assert argv[:3] == (str(runtime.worker_python), str(runtime.worker_script), stage)
            if stage == 'separate':
                assert argv[argv.index('--demucs-repo') + 1] == str(runtime.demucs_repo)
            probe = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
            assert probe.returncode != 0
            assert '必须在 systemd user service 内运行' in probe.stderr
            assert 'the following arguments are required' not in probe.stderr
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
async def test_reload_closes_old_search_before_atomic_admission(plugin):
    instance,sends=plugin
    await instance.on_load()
    try:
        old=instance._active_cover
        entered,release=asyncio.Event(),asyncio.Event()
        async def blocked_search(query,provider,*,limit):
            assert limit==old.search_limit
            entered.set()
            await release.wait()
            from sing_async_test.services.source_offer import CatalogueItem
            return [CatalogueItem(
                provider,'late-id','Song','Artist','Album',120,'available')]
        old.jobs.catalogue.search=blocked_search
        invocation=command(msg='reload-search')
        request=asyncio.create_task(instance.handle_cover_command(**invocation))
        await entered.wait()
        data=instance.get_plugin_config_data()
        data['music']['search_limit']=1
        instance.set_plugin_config(data)
        await instance.on_config_update(plugin_module.CONFIG_RELOAD_SCOPE_SELF,{},'next')
        assert instance._active_cover is not old
        assert instance._active_cover.search_limit==1
        release.set()
        result=await request
        assert result[0] is False and ('closing' in result[1] or 'closed' in result[1])
        identity=instance._command_identity('stream-1',invocation)
        token=instance._request_token(identity)
        assert old.jobs.store.find_request('stream-1',token) is None
    finally:
        release.set()
        await instance.on_unload()


@pytest.mark.asyncio
async def test_output_root_reload_fails_closed_without_consuming_old_namespace(plugin):
    instance,_=plugin
    await instance.on_load()
    old_root=str(instance._jobs.artifacts.root)
    data=instance.get_plugin_config_data()
    data['local']['output_dir']=str(Path(instance.ctx.paths.data_dir).parent/'new-covers')
    instance.set_plugin_config(data)
    with pytest.raises(plugin_module.JobConflict,match='root'):
        await instance.on_config_update(plugin_module.CONFIG_RELOAD_SCOPE_SELF,{},'new-root')
    assert instance._active_cover is None and instance._jobs is None
    assert instance._scheduler_owner is None and instance._outbox is None
    store=plugin_module.JobStore(Path(instance.ctx.paths.data_dir)/'jobs.sqlite3')
    assert store.bind_artifact_root(old_root)==old_root


@pytest.mark.asyncio
async def test_root_is_bound_before_outbox_recovery(plugin,monkeypatch):
    instance,_=plugin
    original=plugin_module.DeliveryOutbox.recover
    async def checked(outbox):
        jobs=instance._jobs
        assert jobs is not None and isinstance(jobs.artifacts,plugin_module.ArtifactStore)
        root=str(jobs.artifacts.root)
        assert outbox._store.bind_artifact_root(root)==root
        return await original(outbox)
    monkeypatch.setattr(plugin_module.DeliveryOutbox,'recover',checked)
    await instance.on_load()
    await instance.on_unload()


@pytest.mark.asyncio
async def test_cancelled_owner_acquire_closes_late_lock_finitely(plugin,monkeypatch):
    instance,_=plugin
    acquired,release=threading.Event(),threading.Event()
    original=plugin_module.exclusive
    def delayed(path):
        real=original(path)
        class Context:
            def __enter__(self):
                fd=real.__enter__()
                acquired.set()
                assert release.wait(5)
                return fd
            def __exit__(self,*args):
                return real.__exit__(*args)
        return Context()
    monkeypatch.setattr(plugin_module,'exclusive',delayed)
    loading=asyncio.create_task(instance.on_load())
    assert await asyncio.to_thread(acquired.wait,2)
    loading.cancel()
    started=asyncio.get_running_loop().time()
    with pytest.raises(asyncio.CancelledError):
        await loading
    assert asyncio.get_running_loop().time()-started<1.5
    lock=Path(instance.ctx.paths.data_dir).resolve()/'.durable-scheduler.lock'
    with pytest.raises(plugin_module.OwnershipBusy):
        with original(lock): pass
    release.set()
    for _ in range(200):
        try:
            with original(lock): pass
            break
        except plugin_module.OwnershipBusy:
            await asyncio.sleep(.01)
    else:
        raise AssertionError('late scheduler owner was not released')
    assert instance._scheduler_owner is None and instance._jobs is None


@pytest.mark.asyncio
async def test_cancelled_store_creation_retains_lease_until_schema_finishes(plugin,monkeypatch):
    instance,_=plugin
    entered,release=threading.Event(),threading.Event()
    original_store=plugin_module.JobStore
    def delayed_store(*args,**kwargs):
        entered.set()
        assert release.wait(5)
        return original_store(*args,**kwargs)
    monkeypatch.setattr(plugin_module,'JobStore',delayed_store)
    loading=asyncio.create_task(instance.on_load())
    assert await asyncio.to_thread(entered.wait,2)
    loading.cancel()
    started=asyncio.get_running_loop().time()
    with pytest.raises(asyncio.CancelledError):
        await loading
    assert asyncio.get_running_loop().time()-started<1.5
    lock=Path(instance.ctx.paths.data_dir).resolve()/'.durable-scheduler.lock'
    with pytest.raises(plugin_module.OwnershipBusy):
        with plugin_module.exclusive(lock): pass
    release.set()
    for _ in range(200):
        try:
            with plugin_module.exclusive(lock): pass
            break
        except plugin_module.OwnershipBusy:
            await asyncio.sleep(.01)
    else:
        raise AssertionError('startup database lease was not released')
    assert (Path(instance.ctx.paths.data_dir)/'jobs.sqlite3').is_file()
    assert instance._scheduler_owner is None and instance._jobs is None


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome,message_id,expected_text',[
    ('sent','platform-positive-id',None),
    ('unknown',None,'语音发送结果未知，可能已送达；不会自动重发。')])
async def test_speak_preserves_single_sender_receipt(plugin,outcome,message_id,expected_text):
    instance,sends=plugin
    await instance.on_load()
    try:
        from sing_async_test.services.delivery_outbox import DeliveryReceipt
        previous=instance._voice_sender
        await previous.shutdown(timeout_s=0)
        class Sender:
            def __init__(self): self.calls=[]
            async def send_file(self,path,stream_id):
                self.calls.append((path,stream_id))
                return DeliveryReceipt(outcome,message_id)
            async def shutdown(self,*,timeout_s=1): pass
        sender=Sender()
        instance._voice_sender=sender
        async def speech(text,model,stream_id):
            return b'one synthetic wav attempt'
        instance._run_speak=speech
        instance._resolve_model=lambda selector: 'fixed-test-model'
        before=len(sends)
        result=await instance.handle_speak_command(**command(
            text='/说 hello',groups={'text':'hello'}))
        assert len(sender.calls)==1 and result[0] is True
        if expected_text is None:
            assert 'platform-positive-id' in result[1] and len(sends)==before
        else:
            assert result[1]==expected_text and sends[-1][0]==expected_text
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
async def test_model_list_uses_active_snapshot_and_never_sidecar(plugin):
    instance,sends=plugin
    await instance.on_load()
    try:
        class ForbiddenSidecar:
            async def list_models(self):
                raise AssertionError('disabled sidecar model listing must not be used')
            async def close(self):
                pass
        instance._rvc=ForbiddenSidecar()
        active_name=Path(instance._active_cover.model_path).name
        data=instance.get_plugin_config_data()
        data['local']['model_path']=str(Path(instance.ctx.paths.data_dir)/'changed-after-await.pth')
        instance.set_plugin_config(data)
        result=await instance.handle_list_models(**command(text='/音色列表'))
        assert result[0] is True and active_name in sends[-1][0]
        assert 'changed-after-await.pth' not in sends[-1][0]
        assert '不代表角色身份、训练来源或使用权已经验证' in sends[-1][0]
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
async def test_model_list_reports_not_ready_without_sidecar(plugin):
    instance,sends=plugin
    class ForbiddenSidecar:
        async def list_models(self):
            raise AssertionError('unready model listing must not access sidecar')
    instance._rvc=ForbiddenSidecar()
    result=await instance.handle_list_models(**command(text='/音色列表'))
    assert result[0] is False and '尚未就绪或正在重载' in sends[-1][0]


@pytest.mark.asyncio
async def test_missing_demucs_bundle_fails_before_scheduler_or_qq(plugin):
    instance, _ = plugin
    repo = Path(instance.config.local.demucs_repo_path)
    (repo / 'htdemucs.yaml').unlink()
    from sing_async_test.services.asset_inventory import InventoryError
    with pytest.raises(InventoryError, match='Demucs'):
        await instance.on_load()
    assert instance._jobs is None and instance._scheduler_owner is None
    assert instance._outbox is None and instance._active_cover is None
    assert instance._voice_sender is None


def test_rvc_wrapper_must_match_inventoried_upstream(plugin):
    instance, _ = plugin
    data = instance.get_plugin_config_data()
    data['local']['rvc_upstream_path'] = str(Path(data['local']['rvc_script']).parent / 'elsewhere')
    instance.set_plugin_config(data)
    with pytest.raises(ValueError, match='upstream'):
        instance._render_paths()


def test_missing_hubert_rejected_before_any_render(plugin):
    instance, _ = plugin
    data = instance.get_plugin_config_data()
    data['local']['hubert_path'] = ''
    instance.set_plugin_config(data)
    with pytest.raises(ValueError, match='hubert_path'):
        instance._render_paths()
