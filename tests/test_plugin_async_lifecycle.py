"""Integration at the SDK boundary; fake catalogue and send, no real QQ."""
from pathlib import Path
from types import SimpleNamespace
import asyncio
import importlib.util
import logging
import subprocess
import sys
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
    monkeypatch.setattr(instance, '_build_music_client', FakeCatalogue)
    monkeypatch.setattr(instance, '_restore_music_logins', no_login)
    return instance, sends


def command(stream='stream-1', msg='platform-message-1', *, user='user-1', text='/翻唱 Song - Artist', groups=None):
    return {'stream_id': stream, 'platform': 'qq', 'user_id': user, 'text': text,
            'matched_groups': groups or {'query': 'Song - Artist'},
            'message': {'message_id': msg, 'platform': 'qq', 'session_id': stream,
                        'processed_plain_text': text, 'is_command': True,
                        'message_info': {'user_info': {'user_id': user}}}}


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
    assert before.state == 'needs_selection' and before.consent_event == 'platform-message-1'
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
        assert (await instance.handle_cover_status(**command(msg='platform-message-3', text='/翻唱状态 '+job_id,
            groups={'job_id': job_id})))[0]
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
        with pytest.raises(plugin_module.OwnershipBusy):
            await instance._start_durable_services()
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
async def test_missing_demucs_bundle_fails_before_scheduler_or_qq(plugin):
    instance, _ = plugin
    repo = Path(instance.config.local.demucs_repo_path)
    (repo / 'htdemucs.yaml').unlink()
    from sing_async_test.services.asset_inventory import InventoryError
    with pytest.raises(InventoryError, match='Demucs'):
        await instance.on_load()
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
