"""Integration at the SDK boundary; fake catalogue and send, no real QQ."""
from pathlib import Path
from types import SimpleNamespace
import asyncio
import importlib
import importlib.util
import json
import logging
import re
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

    async def fake_text(text, stream_id, *, return_details=False, **kwargs):
        sends.append((text, stream_id))
        return {'sent': True, 'message_id': 'fixture-text-ack'} if return_details else True

    async def reject_custom(*args, **kwargs):
        raise AssertionError('No QQ send in plugin lifecycle tests')

    instance._ctx = SimpleNamespace(logger=logger,
        paths=SimpleNamespace(data_dir=str(tmp_path / 'data'), runtime_dir=str(tmp_path / 'runtime')),
        send=SimpleNamespace(text=fake_text, custom=reject_custom))
    async def no_login():
        pass
    class FakeCatalogue:
        def __init__(self):
            self.search_calls = []
        async def search(self, query, provider, *, limit):
            self.search_calls.append((query, provider, limit))
            if query == '生如夏花 - 朴树':
                return [plugin_module.SongInfo('id-1', '生如夏花', '朴树', 'Album', provider, duration_s=120),
                        plugin_module.SongInfo('id-2', '生如夏花', '朴树', 'Album Live', provider, duration_s=125)]
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
                        'raw_message': [{'type': 'text', 'data': text}],
                        'is_notify': False,
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
    assert instance._netease_login_task is None and instance._active_cover is None


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
    instance._active_cover=object()
    instance._outbox=Outbox()
    instance._jobs=Jobs()
    instance._scheduler_owner=Owner()
    instance._music=Closer('music')
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
    assert not hasattr(instance, '_local')
    assert instance._jobs._task is not None
    ok, _, _ = await instance.handle_cover_command(**command())
    assert ok and '请选择标为可选的版本' in sends[-1][0]
    job_id = sends[-1][0].split('任务 ')[1].split('：')[0]
    before = instance._jobs.store.get(job_id, 'stream-1')
    assert before.state == 'needs_selection' and before.consent_event == 'platform-message-1'
    assert before.delivery_state == 'pending'
    assert (before.request['entry_kind'], before.request['render_mode'],
            before.request['instrumental'], before.request['delivery_mode']) == ('command', 'full', True, 'file')
    async def fake_login():
        await asyncio.sleep(3600)
    login = asyncio.create_task(fake_login())
    instance._qq_login_task = login
    await instance.on_unload()
    assert login.cancelled()
    assert instance._scheduler_owner is None
    assert instance._jobs is None and instance._delivery_task is None
    assert not hasattr(instance, '_cache_cleanup_task')
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
    'outbox','jobs','owner','music'])
async def test_cleanup_attempts_every_resource_after_one_failure(plugin,monkeypatch,failure):
    instance,_=plugin
    calls,delivery,login=install_cleanup_fakes(instance,monkeypatch,failure)
    with pytest.raises(OSError,match=failure):
        await instance._close_resources()
    assert calls==['outbox','jobs','owner','music']
    assert delivery.cancelled() and login.cancelled()
    assert instance._active_cover is None and instance._outbox is None
    assert instance._jobs is None and instance._scheduler_owner is None
    assert instance._music is None
    assert instance._qq_login_task is None and instance._netease_login_task is None


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
    assert calls==['outbox','jobs','owner','music']
    assert delivery.cancelled() and login.cancelled()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['outbox','jobs','owner','music'])
async def test_cleanup_preserves_cancellation_after_other_resources(plugin,monkeypatch,failure):
    instance,_=plugin
    calls,delivery,login=install_cleanup_fakes(
        instance,monkeypatch,failure,asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await instance._close_resources()
    assert calls==['outbox','jobs','owner','music']
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
        response = await instance.handle_cover_tool(title='Song', artist='Artist', stream_id='forged')
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
        tool = await instance.handle_cover_tool(title='Song', artist='Artist', auto_reply=True,
                                                 consent_event='forged', stream_id='stream-1',
                                                 chat_id='other-chat')
        assert '未入队' in tool['content']
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
            # This validation process is a systemd unit but deliberately lacks
            # UnitRunner's frozen media budget environment. Either guard must
            # stop before model imports or media reads.
            assert ('必须在 systemd user service 内运行' in probe.stderr
                    or '媒体单元缺少有效预算字段' in probe.stderr)
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
        async def blocked_search(query,provider,*,limit,max_duration_s):
            assert max_duration_s==old.jobs.runtime.max_duration_s
            assert limit==old.search_limit
            entered.set()
            await release.wait()
            from sing_async_test.services.source_offer import CatalogueItem
            return [CatalogueItem(
                provider,'late-id','Song','Artist','Album',120,'available')]
        old.jobs.catalogue.search=blocked_search
        invocation=command(msg='reload-search')
        request=asyncio.create_task(instance.handle_cover_command(**invocation))
        await asyncio.wait_for(entered.wait(),timeout=2)
        data=instance.get_plugin_config_data()
        data['music']['search_limit']=1
        instance.set_plugin_config(data)
        await instance.on_config_update(plugin_module.CONFIG_RELOAD_SCOPE_SELF,{},'next')
        assert instance._active_cover is not old
        assert instance._active_cover.search_limit==1
        release.set()
        # Shared admission owns the catalogue task; close cancels it before
        # releasing scheduler ownership rather than allowing a late admission.
        with pytest.raises(asyncio.CancelledError):
            await request
        assert request.cancelled()
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
async def test_speak_reports_unsupported_without_media_or_reference_io(plugin,monkeypatch):
    instance,sends=plugin
    # Retirement is independent of load state or legacy TTS/clone configuration.
    data=instance.get_plugin_config_data()
    data['mimo'].update(voice_mode='clone',reference_audio='/forbidden/reference.wav',
                        rvc_after_tts=True,api_key='unused-secret')
    instance.set_plugin_config(data)
    def forbidden_io(*args,**kwargs):
        raise AssertionError('retired speech must not read/write audio or spawn media')
    with monkeypatch.context() as guard:
        guard.setattr(Path,'read_bytes',forbidden_io)
        guard.setattr(Path,'write_bytes',forbidden_io)
        guard.setattr(asyncio,'create_subprocess_exec',forbidden_io)
        result=await instance.handle_speak_command(**command(
            text='/说 hello',groups={'text':'hello'}))
        tool=await instance.handle_speak_tool(text='hello',stream_id='stream-1',
                                             chat_id='stream-1',platform='qq',user_id='user-1')
    assert result[0] is False and result[2] is True
    assert '暂不支持' in result[1] and '受限媒体单元' in result[1]
    assert sends==[(result[1],'stream-1')]
    assert tool['success'] is False and tool['code']=='unsupported'
    assert '可信原始用户消息身份' in tool['content'] and '暂不支持' in tool['content']
    assert instance._jobs is None and instance._active_cover is None


def test_retired_resources_and_entry_helpers_are_absent(plugin):
    instance,_=plugin
    for name in ('_local','_rvc','_mimo','_pipeline','_sidecar_proc','_pending',
                 '_pending_lock','_voice_sender','_cache_cleanup_task',
                 '_make_local_backend','_start_sidecar','_ensure_sidecar_ready',
                 '_terminate_stale_sidecar','_stop_sidecar','_close_sidecar_process',
                 '_resolve_python_path','_sidecar_script_path','_convert_kwargs',
                 '_resolve_f0_up_key','_resolve_model','_run_speak','_send_voice',
                 '_voice_cache_dir','_cleanup_voice_cache','_voice_cache_cleanup_loop'):
        assert not hasattr(instance,name),name
    for name in ('LocalBackend','RVCClient','MiMoTTSService','Pipeline','SingleVoiceSender'):
        assert not hasattr(plugin_module,name),name


@pytest.mark.asyncio
async def test_model_list_uses_active_snapshot_and_never_sidecar(plugin):
    instance,sends=plugin
    await instance.on_load()
    try:
        assert not hasattr(instance,'_rvc')
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
    assert not hasattr(instance,'_rvc')
    result=await instance.handle_list_models(**command(text='/音色列表'))
    assert result[0] is False and '尚未就绪或正在重载' in sends[-1][0]


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['model_index', 'worker_python', 'stage_executor'])
@pytest.mark.parametrize('proof', ['active', 'oom'])
async def test_real_on_load_recovers_owned_probe_without_execution_assets(
        plugin, monkeypatch, tmp_path, missing, proof):
    instance, sends = plugin
    media = importlib.import_module('sing_async_test.services.media_probe')
    jobs_module = importlib.import_module('sing_async_test.services.job_service')
    offers = importlib.import_module('sing_async_test.services.source_offer')
    await instance.on_load()
    jobs = instance._jobs
    # Persist an old exact probe claim without invoking the real runner/media.
    jobs._task.cancel()
    await asyncio.gather(jobs._task, return_exceptions=True)
    jobs._task = None
    await jobs.prepare()
    chosen = offers.CatalogueItem('163', 'old-track', 'Song', 'Artist', 'Album', 31., 'available')
    queued, _ = await jobs.submit_selected('stream-1', 'old-probe-request',
        {'instrumental': False, 'auto_reply': True, 'platform': 'qq',
         'ingress_proof': importlib.import_module(
             'sing_async_test.services.job_store').TRUSTED_QQ_INGRESS}, chosen,
        auto_reply=True, consent_event='old-probe-request')
    active = jobs.store.claim_next()
    workspace = jobs.runtime.work_root/queued.id
    workspace.mkdir(mode=0o700)
    (workspace/'source.audio').write_bytes(b'synthetic bounded probe source')
    request = {'schema': 'sing-source-probe-v1',
        'selected': {'provider': '163', 'track_id': 'old-track', 'duration_s': 31.},
        'max_seconds': jobs.runtime.max_duration_s,
        'max_bytes': jobs.runtime.max_download_bytes,
        'source_sha256': media.source_hash(workspace/'source.audio', jobs.runtime.max_download_bytes),
        'worker_sha256': jobs._runtime_inventory.hashes['worker'],
        'worker_python': str(jobs.runtime.worker_python),
        'worker_script': jobs._runtime_context['execution_paths']['worker'],
        'inference_lock': str(jobs.runtime.inference_lock)}
    (workspace/'source-probe-request.json').write_text(json.dumps(request))
    step = media.probe_step(workspace, request)
    jobs_module.save_plan(workspace/'source-probe-plan.json', workspace=workspace,
        recipe=media.probe_key(request), inference_lock=jobs.runtime.inference_lock, steps=(step,))
    claimed = jobs.store.claim_step(active.id, active.run_token, 'source_probe')
    await instance.on_unload()
    if missing == 'model_index':
        Path(instance.config.local.model_path).unlink()
        Path(instance.config.local.index_path).unlink()
    elif missing == 'worker_python':
        data = instance.get_plugin_config_data()
        data['local']['worker_python'] = str(tmp_path/'missing-python')
        instance.set_plugin_config(data)
    else:
        # Redirect only the frozen startup runtime directory: never delete actual
        # repository execution files to simulate an administrator generation loss.
        from dataclasses import replace
        capture = instance._capture_durable_settings
        monkeypatch.setattr(instance, '_capture_durable_settings',
            lambda: replace(capture(), runtime_dir=tmp_path/'missing-runtime'))
    inventory_module = importlib.import_module('sing_async_test.services.asset_inventory')
    # Restore the actual startup probe constructor. The fixture metadata provider
    # was used only to seed a previous valid generation, never for recovery.
    monkeypatch.setattr(plugin_module, 'RuntimeVersionProbe', inventory_module.RuntimeVersionProbe)
    def forbidden_metadata(*args, **kwargs):
        raise AssertionError('No metadata subprocess permitted in this static recovery fixture')
    monkeypatch.setattr(inventory_module.RuntimeVersionProbe, '_run', forbidden_metadata)
    calls = []
    async def state(runner, unit):
        assert unit == claimed.unit_name
        calls.append(unit)
        return 'active' if proof == 'active' else 'stopped'
    async def forbidden_launch(*args, **kwargs):
        raise AssertionError('Recovery cannot launch a replacement or new stage')
    monkeypatch.setattr(plugin_module.UnitRunner, 'state', state)
    monkeypatch.setattr(plugin_module.UnitRunner, 'run', forbidden_launch)
    if proof == 'oom':
        logs = workspace/'unit-logs'
        logs.mkdir()
        (logs/(claimed.unit_name+'.exit.json')).write_text(json.dumps({
            'schema': 2, 'completed': True, 'returncode': 1,
            'service': {'LoadState': 'loaded', 'ActiveState': 'failed',
                        'Result': 'oom-kill', 'ExecMainCode': '2', 'ExecMainStatus': '9'}}))
    # Keep real on_load/storage/outbox startup, but run recovery deterministically
    # by hand and keep the actual notification RPC loop idle (zero fake QQ send).
    async def idle_jobs(service):
        await service.prepare_storage()
    async def idle_delivery():
        await asyncio.Event().wait()
    monkeypatch.setattr(plugin_module.JobService, 'start', idle_jobs)
    monkeypatch.setattr(instance, '_delivery_loop', idle_delivery)
    await instance.on_load()
    try:
        recovered = instance._jobs
        assert recovered._generation is None and recovered._runtime_inventory is None
        assert recovered._context_task is None
        assert instance._outbox is not None and instance._scheduler_owner is not None
        assert instance._active_cover is not None
        await recovered.run_once()
        current = await recovered.get_job(queued.id, queued.stream_id)
        assert calls == [claimed.unit_name] and sends == []
        assert current.artifact_key is None and not (workspace/'recipe.json').exists()
        if proof == 'active':
            assert current.state == 'running' and current.unit_name == claimed.unit_name
        else:
            assert current.state == 'failed' and current.error['code'] == 'unit_oom'
            assert await recovered.failure_notice_candidates() == [(queued.id, queued.stream_id)]
            notice = await recovered.claim_failure_notice(queued.id, queued.stream_id)
            assert notice['code'] == 'unit_oom' and notice['token']
            await recovered.settle_failure_notice(queued.id, notice['token'], 'unknown')
        with pytest.raises(inventory_module.InventoryError):
            await recovered.submit_selected('stream-new', 'missing-assets-new', {}, chosen)
        assert recovered.store.find_request('stream-new', 'missing-assets-new') is None
        assert recovered._generation is None and recovered._runtime_inventory is None
        assert sends == []
    finally:
        await instance.on_unload()
    assert instance._jobs is None and instance._scheduler_owner is None


@pytest.mark.asyncio
async def test_missing_demucs_bundle_keeps_recovery_but_refuses_new_admission(plugin):
    instance, sends = plugin
    repo = Path(instance.config.local.demucs_repo_path)
    (repo / 'htdemucs.yaml').unlink()
    await instance.on_load()
    try:
        assert instance._jobs is not None and instance._scheduler_owner is not None
        assert instance._outbox is not None and instance._active_cover is not None
        assert isinstance(instance._jobs.artifacts, plugin_module.ArtifactStore)
        result = await instance.handle_cover_tool(title='Song', artist='Artist',
            stream_id='stream-1', chat_id='stream-1', platform='qq', user_id='user-1')
        assert result['success'] is False and result['state'] == 'not_admitted'
        assert result['job_id'] is None and sends == []
        assert instance._music.search_calls == []
        assert instance._jobs._generation is None
        assert instance._jobs.scanner(instance._jobs.store,
            ('running', 'cancel_requested')) == []
        assert not hasattr(instance,'_voice_sender')
    finally:
        await instance.on_unload()
    assert instance._jobs is None and instance._scheduler_owner is None


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


def test_sdk_cover_command_does_not_capture_natural_requests(plugin):
    instance,_=plugin
    components=[item for item in instance.get_components()
                if item['type']=='COMMAND' and item['name']=='翻唱']
    assert len(components)==1
    metadata=components[0]['metadata']
    assert metadata['handler_name']=='handle_cover_command'
    assert re.match(metadata['command_pattern'],'/翻唱 Song - Artist')
    for text in ('唱一下《Song - Artist》','唱一段《Song - Artist》',
                 '你能唱一下朴树的生如夏花吗？','请翻唱朴树的生如夏花'):
        assert re.match(metadata['command_pattern'],text) is None,text


@pytest.mark.asyncio
async def test_structured_tool_real_admission_defaults_replay_and_owned_status(plugin,monkeypatch):
    instance,sends=plugin
    await instance.on_load()
    try:
        async def idle():
            return None
        monkeypatch.setattr(instance._jobs,'run_once',idle)
        context={'stream_id':'stream-1','chat_id':'stream-1','platform':'qq','user_id':'user-1'}
        first=await instance.handle_cover_tool(title='生如夏花',artist='朴树',**context)
        assert first['success'] is True and first['state']=='queued'
        assert first['code']=='queued'
        assert first['job_id'] and '无需选择序号' in first['content']
        saved=instance._jobs.store.get(first['job_id'],'stream-1')
        assert (saved.request['entry_kind'],saved.request['render_mode'],
                saved.request['instrumental'],saved.request['delivery_mode'])==('tool','excerpt',False,'voice')
        assert saved.request['ingress_proof']==plugin_module.TRUSTED_TOOL_INGRESS
        assert saved.request['auto_reply'] is True and saved.consent_event
        assert saved.delivery_state=='pending' and sends==[]
        assert instance._music.search_calls==[('生如夏花 - 朴树',saved.request['provider'],5)]
        again=await instance.handle_cover_tool(title='生如夏花',artist='朴树',**context)
        assert again==first and len(instance._music.search_calls)==1
        status=await instance.handle_cover_status_tool(job_id=first['job_id'],**context)
        assert status==first
        for change in ({'user_id':'user-2'},
                       {'stream_id':'other-stream','chat_id':'other-stream'}):
            denied=await instance.handle_cover_status_tool(job_id=first['job_id'],**(context|change))
            assert denied['success'] is False and denied['job_id'] is None
            assert denied['code']=='job_unavailable'
        assert instance._jobs.store.get(first['job_id'],'stream-1')==saved
        assert len(instance._music.search_calls)==1 and sends==[]
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
async def test_structured_tool_single_source_uses_shared_admission_and_format(plugin,monkeypatch):
    instance,sends=plugin
    await instance.on_load()
    try:
        async def idle():
            return None
        monkeypatch.setattr(instance._jobs,'run_once',idle)
        reply=await instance.handle_cover_tool(title='Song',artist='Artist',source_id='id-1',
            render_mode='full',instrumental=True,delivery_mode='file',
            stream_id='stream-1',chat_id='stream-1',platform='qq',user_id='user-1')
        assert reply['success'] is True and reply['state']=='queued'
        saved=instance._jobs.store.get(reply['job_id'],'stream-1')
        assert saved.selected_source['track_id']=='id-1'
        assert (saved.request['render_mode'],saved.request['instrumental'],
                saved.request['delivery_mode'])==('full',True,'file')
        assert saved.delivery_state=='pending' and saved.consent_event
        assert len(instance._music.search_calls)==1 and sends==[]
    finally:
        await instance.on_unload()
