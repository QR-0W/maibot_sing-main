"""Actual SDK/plugin tests; all outbound transport is replaced, never sent."""
from pathlib import Path
from types import SimpleNamespace
import asyncio
import importlib.util
import logging
import re
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sing_sdk_test', ROOT / 'plugin.py', submodule_search_locations=[str(ROOT)])
plugin_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin_module
spec.loader.exec_module(plugin_module)


@pytest.fixture
def plugin(tmp_path):
    instance = plugin_module.create_plugin()
    instance.set_plugin_config(tomllib.loads((ROOT / 'config.example.toml').read_text()))
    instance._ctx = SimpleNamespace(logger=logging.getLogger('sing-test'), paths=SimpleNamespace(
        data_dir=str(tmp_path / 'data'), runtime_dir=str(tmp_path / 'runtime')))
    return instance


@pytest.mark.asyncio
async def test_real_lifecycle_without_chat_or_sidecar(plugin, monkeypatch):
    # Isolate this no-network lifecycle check from the harness proxy variables;
    # httpx rejects this harness's bracketed IPv6 NO_PROXY entry.
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
        monkeypatch.delenv(name, raising=False)
    await plugin.on_load()
    first = plugin._local
    assert first is not None and first._owner is not None
    assert plugin._sidecar_proc is None
    assert any(component['name'] == 'cover_song' for component in plugin.get_components())
    await plugin.on_config_update(plugin_module.CONFIG_RELOAD_SCOPE_SELF, {}, '0.3.0')
    assert first._closed
    assert plugin._local is not first
    await plugin.on_unload()
    assert plugin._local is None
    assert plugin._cache_cleanup_task is None


@pytest.mark.asyncio
async def test_startup_failure_releases_library_and_tasks(plugin, monkeypatch):
    def fail_client():
        raise RuntimeError('injected client initialization failure')
    monkeypatch.setattr(plugin, '_build_music_client', fail_client)
    with pytest.raises(RuntimeError, match='injected'):
        await plugin.on_load()
    assert plugin._local is None
    assert plugin._cache_cleanup_task is None
    manager = plugin._make_local_backend()
    await manager.start()
    await manager.close()


def test_sdk_config_and_backend_defaults(plugin):
    assert plugin.config.plugin.enabled is False
    assert plugin.config.rvc.auto_start is False
    manager = plugin._make_local_backend()
    assert manager.output == Path(plugin.ctx.paths.data_dir) / 'covers'
    assert manager.timeout_s == 900
    assert manager.model.name == 'NatsumeIroha.pth'


def test_english_title_is_not_consumed_as_model():
    import ast
    tree = ast.parse((ROOT / 'plugin.py').read_text())
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'handle_cover_command')
    pattern = next(k.value.value for d in method.decorator_list for k in d.keywords if k.arg == 'pattern')
    match = re.fullmatch(pattern, '/翻唱 In the Aeroplane Over the Sea - Neutral Milk Hotel')
    assert match['query'] == 'In the Aeroplane Over the Sea - Neutral Milk Hotel'
    assert match['model'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome, expected_calls', [('sent', 1), ('unknown', 1), ('failed', 2)])
async def test_delivery_retry_policy_preserves_song(plugin, tmp_path, monkeypatch, outcome, expected_calls):
    audio = tmp_path / 'cover.mp3'
    audio.write_bytes(b'permanent cover')
    song = plugin_module.SongInfo('id', 'Song', 'Artist', '', 'local')
    calls = []
    async def render(*args, **kwargs):
        return song, audio
    async def send(custom_type, data, stream):
        calls.append((custom_type, data, stream))
        return outcome
    monkeypatch.setattr(plugin, '_run_cover', render)
    monkeypatch.setattr(plugin, '_send_custom_voice', send)
    first = await plugin._run_cover_dedup('Song - Artist', 'model', 'test-stream')
    second = await plugin._run_cover_dedup('Song - Artist', 'model', 'test-stream')
    assert first[2] == second[2] == outcome
    assert len(calls) == expected_calls
    assert calls[0] == ('voiceurl', {'url': audio.as_uri()}, 'test-stream')
    assert audio.read_bytes() == b'permanent cover'


@pytest.mark.asyncio
async def test_concurrent_request_and_cancelled_caller_send_once(plugin, tmp_path, monkeypatch):
    entered, gate = asyncio.Event(), asyncio.Event()
    audio = tmp_path / 'cover.mp3'
    song = plugin_module.SongInfo('id', 'Song', 'Artist', '', 'local')
    calls = []
    async def render(*args, **kwargs):
        entered.set()
        await gate.wait()
        return song, audio
    async def send(*args):
        calls.append(args)
        return 'sent'
    monkeypatch.setattr(plugin, '_run_cover', render)
    monkeypatch.setattr(plugin, '_send_custom_voice', send)
    first = asyncio.create_task(plugin._run_cover_dedup('Song - Artist', 'model', 'test-stream'))
    await entered.wait()
    second = asyncio.create_task(plugin._run_cover_dedup('Song - Artist', 'model', 'test-stream'))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    result = await second
    assert result[2:] == ('sent', True)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unknown_stream_rejected_before_render(plugin):
    with pytest.raises(ValueError, match='缺少当前会话'):
        await plugin._run_cover_dedup('Song - Artist', 'model', '')


@pytest.mark.asyncio
async def test_permanent_mp3_is_not_deleted_on_real_send_failure(plugin, tmp_path):
    audio = tmp_path / 'cover.mp3'
    audio.write_bytes(b'permanent cover')
    async def failed_transport(*args, **kwargs):
        return False
    plugin._ctx.send = SimpleNamespace(custom=failed_transport)
    assert await plugin._send_voice(audio, 'test-stream') is False
    plugin._cleanup_voice_cache()
    assert audio.read_bytes() == b'permanent cover'
