"""Host-shaped SDK boundary tests; no real QQ, worker, network or Hook binding."""
from pathlib import Path
from types import SimpleNamespace
import ast
import copy
import logging
import os
import re
import sqlite3
import sys
import types

import pytest

from test_plugin_async_lifecycle import ROOT, command, plugin, plugin_module


def cover_component(instance):
    commands = [item for item in instance.get_components() if item['type'] == 'COMMAND']
    covers = [item for item in commands if item['name'] == '翻唱']
    assert len(covers) == 1
    assert not any(item['type'] == 'HOOK_HANDLER' for item in instance.get_components())
    return covers[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('text,kind,render,backing,delivery', [
    ('/翻唱 Song - Artist', 'command', 'full', True, 'file'),
    ('/翻唱 Song - Artist --excerpt --without-instrumental --voice', 'command', 'excerpt', False, 'voice'),
])
async def test_same_sdk_command_uses_raw_host_intent_only(plugin, text, kind, render, backing, delivery):
    instance, sends = plugin
    component = cover_component(instance)
    assert component['metadata']['handler_name'] == 'handle_cover_command'
    assert re.match(component['metadata']['command_pattern'], text)
    await instance.on_load()
    try:
        payload = command(text=text, groups={
            'query': 'Forged - Attacker', 'album': 'Fake Album', 'source_id': 'fake-id',
            'model': 'evil.pth', 'instrumental': '--with-instrumental',
            'auto_reply': '--auto-reply', 'render_mode': 'full', 'delivery_mode': 'file',
        })
        # Top-level model-controlled selectors also cannot replace raw intent.
        payload.update(auto_reply=True, consent_event='fake-consent', album='Wrong',
                       source_id='fake-id', render_mode='full', delivery_mode='file')
        assert (await instance.handle_cover_command(**payload))[0]
        token = instance._request_token(instance._command_identity('stream-1', payload))
        saved = instance._jobs.store.find_request('stream-1', token)
        assert saved.state == 'needs_selection'
        assert (saved.request['entry_kind'], saved.request['render_mode'],
                saved.request['instrumental'], saved.request['delivery_mode']) == (kind, render, backing, delivery)
        assert saved.request['query'] == ('生如夏花 - 朴树' if '朴树' in text else 'Song - Artist')
        assert saved.request['album'] == saved.request['source_id'] == saved.request['model_selector'] == ''
        assert saved.request['auto_reply'] is True and saved.consent_event == payload['message']['message_id']
        assert saved.delivery_state == 'pending'
        assert await instance._jobs.delivery_candidates() == []
        assert len(instance._music.search_calls) == 1
        assert (await instance.handle_cover_command(**payload))[0]
        again = instance._jobs.store.find_request('stream-1', token)
        assert again.id == saved.id and again.request == saved.request
        assert len(instance._music.search_calls) == 1
        assert all(stream == 'stream-1' for _, stream in sends)
        assert instance._jobs.inventory.paths.excerpt_selection == ROOT / 'runtime/excerpt_selection.py'
        assert instance._jobs._runtime_context['execution_paths']['excerpt_selection'] == str(ROOT / 'runtime/excerpt_selection.py')
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
async def test_command_consent_is_original_message_bound_and_replay_fenced(plugin):
    instance, _ = plugin
    await instance.on_load()
    try:
        original = command(text='/翻唱 Song - Artist --excerpt --without-instrumental --voice --auto-reply')
        assert (await instance.handle_cover_command(**original))[0]
        token = instance._request_token(instance._command_identity('stream-1', original))
        saved = instance._jobs.store.find_request('stream-1', token)
        assert saved.consent_event == original['message']['message_id']
        assert saved.request['auto_reply'] is True and saved.delivery_state == 'pending'
        # Changing raw intent under the same command/message ID must not upgrade
        # delivery, source selection, or produce a second admission/search.
        for changed in (
            '/翻唱 Different - Artist',
            '/翻唱 Song - Artist --full --auto-reply',
            '/翻唱 Song - Artist --file --auto-reply',
            '/翻唱 Song - Artist --with-instrumental --auto-reply',
            '/翻唱 Song - Artist --album Album --auto-reply',
            '/翻唱 Song - Artist --excerpt --without-instrumental --voice',
        ):
            assert not (await instance.handle_cover_command(**command(text=changed)))[0]
        assert instance._jobs.store.find_request('stream-1', token).request == saved.request
        assert len(instance._music.search_calls) == 1
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['/翻唱 Song - Artist',
                                  '/翻唱 Song - Artist --excerpt --without-instrumental --voice'])
async def test_one_exact_candidate_queues_without_flag_or_guessing(plugin, monkeypatch, text):
    instance, sends = plugin
    await instance.on_load()
    try:
        async def idle():
            return None
        monkeypatch.setattr(instance._jobs, 'run_once', idle)
        search = instance._jobs.catalogue.search
        async def single(*args, **kwargs):
            return (await search(*args, **kwargs))[:1]
        monkeypatch.setattr(instance._jobs.catalogue, 'search', single)
        raw = command(text=text)
        assert (await instance.handle_cover_command(**raw))[0]
        token = instance._request_token(instance._command_identity('stream-1', raw))
        job = instance._jobs.store.find_request('stream-1', token)
        assert job.state == 'queued' and job.selected_source['track_id'] == 'id-1'
        assert job.request['auto_reply'] is True and job.consent_event == raw['message']['message_id']
        assert job.delivery_state == 'pending' and sends[-1][1] == 'stream-1'
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
async def test_old_non_delivery_job_cannot_be_upgraded_by_new_default(plugin):
    instance, _ = plugin
    await instance.on_load()
    try:
        seed = command(msg='seed-message')
        assert (await instance.handle_cover_command(**seed))[0]
        seed_token = instance._request_token(instance._command_identity('stream-1', seed))
        old_request = dict(instance._jobs.store.find_request('stream-1', seed_token).request)
        old_request['auto_reply'] = False
        raw = command()  # identical text but a different original Host message ID
        token = instance._request_token(instance._command_identity('stream-1', raw))
        candidates = await instance._jobs.catalogue.search('Song - Artist', '163', limit=5)
        old, created = instance._jobs.store.admit_offer(
            'stream-1', token, old_request, candidates, auto_reply=False, consent_event=None)
        assert created and old.delivery_state == 'not_requested' and old.consent_event is None
        searches = len(instance._music.search_calls)
        assert not (await instance.handle_cover_command(**raw))[0]
        unchanged = instance._jobs.store.find_request('stream-1', token)
        assert unchanged.id == old.id and unchanged.request == old.request
        assert unchanged.delivery_state == 'not_requested' and unchanged.consent_event is None
        assert len(instance._music.search_calls) == searches
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['message', 'text', 'processed_plain_text', 'both_texts', 'is_command'])
async def test_missing_host_original_fails_before_any_send(plugin, missing):
    instance, sends = plugin
    raw = command(text='/翻唱 Song - Artist --auto-reply')
    if missing in ('message', 'text'):
        raw.pop(missing)
    elif missing == 'both_texts':
        raw.pop('text')
        raw['message'].pop('processed_plain_text')
    else:
        raw['message'].pop(missing)
    assert not (await instance.handle_cover_command(**raw))[0]
    assert sends == [] and instance._jobs is None


@pytest.mark.asyncio
@pytest.mark.parametrize('suffix', [
    '--full --excerpt', '--with-instrumental --without-instrumental', '--file --voice',
    '--auto-reply --file', '--auto-reply --auto-reply', '--auto-reply yes',
])
async def test_contradictions_never_search_or_admit(plugin, suffix):
    instance, _ = plugin
    await instance.on_load()
    try:
        raw = command(text='/翻唱 Song - Artist ' + suffix)
        assert not (await instance.handle_cover_command(**raw))[0]
        token = instance._request_token(instance._command_identity('stream-1', raw))
        assert instance._jobs.store.find_request('stream-1', token) is None
        assert instance._music.search_calls == []
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
async def test_command_raw_selectors_override_forged_groups_without_running_worker(plugin, monkeypatch):
    instance, _ = plugin
    await instance.on_load()
    try:
        async def idle():
            return None
        monkeypatch.setattr(instance._jobs, 'run_once', idle)
        raw = command(text='/翻唱 Song - Artist --source-id id-1 --album Album --full --without-instrumental --file',
                      groups={'source_id': 'id-2', 'album': 'Album Live', 'auto_reply': '--auto-reply'})
        assert (await instance.handle_cover_command(**raw))[0]
        token = instance._request_token(instance._command_identity('stream-1', raw))
        saved = instance._jobs.store.find_request('stream-1', token)
        assert saved.state == 'queued' and saved.selected_source['track_id'] == 'id-1'
        assert saved.request['album'] == 'Album' and saved.request['source_id'] == 'id-1'
        assert saved.request['instrumental'] is False and saved.consent_event == raw['message']['message_id']
        assert saved.delivery_state == 'pending'
    finally:
        await instance.on_unload()


@pytest.mark.parametrize('text', [
    '唱一下《Song - Artist》', '唱一段《Song - Artist》', '唱《Song - Artist》',
    '你能唱一下朴树的生如夏花吗？', '你能唱一下朴树的生如夏花吗?',
    '请翻唱朴树的生如夏花',
])
def test_natural_requests_are_not_intercepted_by_command(plugin, text):
    instance, sends = plugin
    component = cover_component(instance)
    assert re.match(component['metadata']['command_pattern'], text) is None
    assert instance._jobs is None and sends == []


def test_choice_feedback_keeps_ordinals_and_marks_blocked_versions(plugin):
    instance,_ = plugin
    items = [dict(title='Song',artist='Artist',album='Album',track_id='long',
                  availability='over_limit'),
             dict(title='Song (Live)',artist='Artist',album='Live',track_id='live',
                  availability='unknown')]
    lines = instance._source_choice_lines('a' * 32,items)
    assert '请选择标为可选' in lines[0]
    assert lines[1].startswith('1.') and 'ID long' in lines[1] and '不可选' in lines[1]
    assert lines[2].startswith('2.') and 'ID live' in lines[2] and '取链确认' in lines[2]
    blocked = instance._source_choice_lines('a' * 32,items[:1])
    assert '无需发送选择命令' in blocked[0] and '/翻唱选择' not in blocked[0]
    assert items[0]['availability'] == 'over_limit'


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['command', 'tool'])
async def test_known_long_song_returns_refusal_not_selection_or_queue(plugin, monkeypatch, entry):
    instance,sends = plugin
    await instance.on_load()
    try:
        async def idle():
            return None
        async def long_song(query, provider, *, limit):
            return [plugin_module.SongInfo('long-id','Song','Artist','Album',provider,
                                         duration_s=901 if entry=='tool' else 500)]
        async def forbidden_resolution(*args, **kwargs):
            raise AssertionError('Known duration refusal must not resolve media')
        monkeypatch.setattr(instance._jobs,'run_once',idle)
        monkeypatch.setattr(instance._music,'search',long_song)
        monkeypatch.setattr(instance._jobs.catalogue,'resolve',forbidden_resolution)
        if entry == 'tool':
            result = await instance.handle_cover_tool(title='Song',artist='Artist',**tool_call())
            assert result['success'] is False and result['state'] == 'not_admitted'
            assert result['job_id'] is None and result['code'] == 'source_duration_limit'
            text = result['content']
            assert sends == []
        else:
            result = await instance.handle_cover_command(**command(text='/翻唱 Song - Artist'))
            assert result[0] is False and result[2] is True
            assert len(sends) == 1
            text = sends[0][0]
        assert ('900 秒' if entry=='tool' else '300 秒') in text
        assert '未入队' in text and '无需选择序号' in text
        assert job_rows(instance) == 0
    finally:
        await instance.on_unload()


def tool_call(**overrides):
    """Host-shaped Tool payload: session fields come from the execution context."""
    payload = {'stream_id': 'stream-1', 'chat_id': 'stream-1', 'platform': 'qq',
               'user_id': 'user-1'}
    payload.update(overrides)
    return payload


def job_rows(instance):
    with sqlite3.connect(instance._jobs.store.path) as database:
        return database.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]


@pytest.mark.asyncio
async def test_tool_formatted_params_admit_cover_request_and_replay_once(plugin, monkeypatch):
    instance, sends = plugin
    await instance.on_load()
    try:
        async def idle():
            return None
        monkeypatch.setattr(instance._jobs, 'run_once', idle)
        first = await instance.handle_cover_tool(title='生如夏花', artist='朴树', **tool_call())
        # Tool binds the first exact provider candidate without manual selection.
        assert '无需选择序号' in first['content']
        assert first['success'] and first['state'] == 'queued'
        job_id = first['job_id']
        saved = instance._jobs.store.get(job_id, 'stream-1')
        assert saved.request['entry_kind'] == 'tool'
        assert saved.request['query'] == '生如夏花 - 朴树'
        assert saved.request['auto_reply'] is True
        assert saved.request['ingress_proof'] == 'napcat-tool-session-v1'
        assert saved.state == 'queued' and saved.delivery_state == 'pending'
        assert saved.selected_source['track_id'] == 'id-1'
        assert instance._music.search_calls == [('生如夏花 - 朴树', saved.request['provider'], 5)]
        # The Tool itself never pushes chat text; the model composes the reply.
        assert sends == []
        again = await instance.handle_cover_tool(title='生如夏花', artist='朴树', **tool_call())
        assert job_id in again['content'] and len(instance._music.search_calls) == 1
        assert job_rows(instance) == 1
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
async def test_tool_single_exact_candidate_is_queued_with_requested_format(plugin, monkeypatch):
    instance, sends = plugin
    await instance.on_load()
    try:
        async def idle():
            return None
        monkeypatch.setattr(instance._jobs, 'run_once', idle)
        async def single(query, provider, *, limit):
            return [plugin_module.SongInfo('id-9', '生如夏花', '朴树', 'Album', provider, duration_s=120)]
        monkeypatch.setattr(instance._music, 'search', single)
        reply = await instance.handle_cover_tool(
            title='生如夏花', artist='朴树', render_mode='full', instrumental=True,
            delivery_mode='file', **tool_call())
        assert reply['success'] and reply['state'] == 'queued'
        job_id = reply['job_id']
        saved = instance._jobs.store.get(job_id, 'stream-1')
        assert (saved.request['render_mode'], saved.request['instrumental'],
                saved.request['delivery_mode']) == ('full', True, 'file')
        assert saved.state == 'queued' and saved.delivery_state == 'pending'
        assert saved.consent_event and saved.request['auto_reply'] is True
        assert sends == []
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize('overrides,reason', [
    ({'chat_id': None}, '缺少 chat_id'),
    ({'chat_id': ''}, '空 chat_id'),
    ({'stream_id': 'other-stream'}, '模型覆写了 stream_id'),
    ({'stream_id': ''}, '空 stream_id'),
    ({'platform': 'webui'}, '非 QQ 平台'),
    ({'user_id': ''}, '缺用户'),
    ({'user_id': None}, '用户非字符串'),
    ({'group_id': 'webui_virtual_group_12345'}, '虚拟 QQ 群'),
    ({'stream_id': 'webui_virtual_group_1', 'chat_id': 'webui_virtual_group_1'}, '虚拟 QQ 会话'),
])
async def test_tool_without_host_session_context_has_zero_effects(plugin, overrides, reason):
    instance, sends = plugin
    await instance.on_load()
    try:
        reply = await instance.handle_cover_tool(title='Song', artist='Artist',
                                                 **tool_call(**overrides))
        assert '未入队' in reply['content'], reason
        assert instance._music.search_calls == [] and sends == []
        assert job_rows(instance) == 0
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
async def test_tool_rejects_placeholder_or_conflicting_params(plugin):
    instance, sends = plugin
    await instance.on_load()
    try:
        for kwargs in ({'title': '', 'artist': 'Artist'},
                       {'title': 'Song', 'artist': ''},
                       {'title': None, 'artist': 'Artist'},
                       {'title': '../etc/passwd', 'artist': 'Artist'},
                       {'title': 'Song', 'artist': 'Artist', 'render_mode': 'clip'},
                       {'title': 'Song', 'artist': 'Artist', 'delivery_mode': 'text'},
                       {'title': 'Song', 'artist': 'Artist', 'instrumental': 'yes'},
                       {'title': 'Song', 'artist': 'Artist', 'source_id': 'bad id!'}):
            reply = await instance.handle_cover_tool(**kwargs, **tool_call())
            assert '未入队' in reply['content'], kwargs
        assert instance._music.search_calls == [] and sends == []
        assert job_rows(instance) == 0
    finally:
        await instance.on_unload()



@pytest.mark.asyncio
async def test_structured_hyphenated_title_is_not_resplit(plugin, monkeypatch):
    instance, sends = plugin
    await instance.on_load()
    try:
        async def idle():
            return None
        queries = []
        async def single(query, provider, *, limit):
            queries.append(query)
            return [plugin_module.SongInfo('id-9', 'Part - II', 'Artist', 'Album', provider, duration_s=120)]
        monkeypatch.setattr(instance._jobs, 'run_once', idle)
        monkeypatch.setattr(instance._music, 'search', single)
        reply = await instance.handle_cover_tool(title='Part - II', artist='Artist', **tool_call())
        assert reply['success'] and reply['state'] == 'queued'
        saved = instance._jobs.store.get(reply['job_id'], 'stream-1')
        assert saved.request['title'] == 'Part - II' and saved.request['artist'] == 'Artist'
        assert saved.selected_source['title'] == 'Part - II'
        assert queries == ['Part - II - Artist'] and sends == []
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize('title,artist,album,catalogue_title,catalogue_artist,catalogue_album', [
    ('Weird Fishes/Arpeggi', 'Radiohead', 'Parts/I',
     'Weird Fishes / Arpeggi', 'Radiohead', 'Parts / I'),
    ('Weird Fishes / Arpeggi', 'Radiohead', '',
     'Weird Fishes/Arpeggi', 'Radiohead', 'Album'),
    ('Song', 'AC/DC', '', 'Song', 'AC / DC', 'Album'),
    ('...Like Clockwork', 'D:ream', '', '...Like Clockwork', 'D:ream', 'Album'),
    ('-Song', '-h', '', '-Song', '-h', 'Album'),
    ('歌曲', 'アーティスト', '專輯/上', '歌曲', 'アーティスト', '專輯 / 上'),
])
async def test_structured_catalogue_punctuation_admits_once(
        plugin, monkeypatch, title, artist, album,
        catalogue_title, catalogue_artist, catalogue_album):
    instance, sends = plugin
    await instance.on_load()
    try:
        async def idle():
            return None
        queries = []
        async def single(query, provider, *, limit):
            queries.append(query)
            return [plugin_module.SongInfo('punctuation-id', catalogue_title,
                catalogue_artist, catalogue_album, provider, duration_s=120)]
        monkeypatch.setattr(instance._jobs, 'run_once', idle)
        monkeypatch.setattr(instance._music, 'search', single)
        reply = await instance.handle_cover_tool(
            title=title, artist=artist, album=album, **tool_call())
        assert reply['success'] and reply['state'] == 'queued'
        saved = instance._jobs.store.get(reply['job_id'], 'stream-1')
        assert saved.request['title'] == title and saved.request['artist'] == artist
        assert saved.selected_source['title'] == catalogue_title
        assert saved.selected_source['artist'] == catalogue_artist
        assert saved.selected_source['album'] == catalogue_album
        again = await instance.handle_cover_tool(
            title=title, artist=artist, album=album, **tool_call())
        assert again['job_id'] == reply['job_id']
        assert queries == [title + ' - ' + artist]
        assert job_rows(instance) == 1 and sends == []
    finally:
        await instance.on_unload()


# Optional exact Host bridge coverage: execute only these two read-only function
# ASTs, with transport/serialization/send stubs, never importing or starting Host.
# Like test_manifest, require an explicitly supplied checkout instead of embedding
# a developer-specific location. The plugin itself has no Host source dependency.
HOST_ROOT = os.environ.get('MAIBOT_TEST_HOST_ROOT', '').strip()


def host_function(relative_path, name, namespace):
    path = Path(HOST_ROOT) / relative_path
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    method.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module='__future__',
        names=[ast.alias(name='annotations')], level=0), method], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
    return namespace[name]


@pytest.mark.skipif(not HOST_ROOT, reason='未显式设置 MAIBOT_TEST_HOST_ROOT')
@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['/翻唱 Song - Artist',
                                  '/翻唱 Song - Artist --excerpt --without-instrumental --voice'])
@pytest.mark.parametrize('gate', ['allowed', 'disabled', 'public_permission_probe'])
async def test_exact_host_gate_and_command_rpc_payload(plugin, monkeypatch, text, gate):
    instance, sends = plugin
    component = cover_component(instance)
    declaration = component['metadata']
    # Execute the actual Host metadata-to-CommandInfo method: this public
    # declaration has no explicit operator permission (unlike login commands).
    build_info = host_function('src/plugin_runtime/component_query.py', '_build_command_info', {
        'CommandInfo': lambda **fields: SimpleNamespace(**fields),
    })
    actual_info = build_info(SimpleNamespace(
        name=component['name'], plugin_id='qr0w.maibot-sing', enabled=True,
        metadata=declaration))
    assert actual_info.permission == 'public'
    raw = command(text=text)
    info = raw['message']['message_info']
    message = SimpleNamespace(**{**raw['message'], 'is_command': False, 'message_info': SimpleNamespace(
        user_info=SimpleNamespace(**info['user_info']), group_info=SimpleNamespace(**info['group_info']),
        additional_config=info['additional_config'])})
    invoked = []

    class Supervisor:
        async def invoke_plugin(self, **rpc):
            invoked.append(rpc)
            assert rpc['method'] == 'plugin.invoke_command' and rpc['component_name'] == '翻唱'
            result = await instance.handle_cover_command(**rpc['args'])
            return SimpleNamespace(payload={'success': result[0], 'result': result})

    def serialize(msg, *, include_binary_data):
        assert include_binary_data is False
        payload = copy.deepcopy(raw['message'])
        payload['is_command'] = msg.is_command
        return payload

    executor = host_function('src/plugin_runtime/component_query.py', '_build_command_executor', {
        'resolve_component_rpc_timeout_ms': lambda value: value,
        'is_local_operator': lambda *args: False,
        'PluginMessageUtils': SimpleNamespace(_session_message_to_dict=serialize),
        'logger': logging.getLogger(__name__),
    })(Supervisor(), 'qr0w.maibot-sing', component['name'], declaration, 45000)

    def find(text):
        matched = re.match(declaration['command_pattern'], text)
        return (executor, matched.groupdict(), actual_info) if matched else None

    host_sends = []
    async def deny_send(*args, **kwargs):
        host_sends.append(args)
    send_module = types.ModuleType('src.services.send_service')
    send_module.text_to_stream = deny_send
    monkeypatch.setitem(sys.modules, 'src.services.send_service', send_module)
    permission_checks = []
    def denied_operator(*args, **kwargs):
        permission_checks.append((args, kwargs))
        return False
    process = host_function('src/chat/message_receive/bot.py', '_process_commands', {
        'component_query_service': SimpleNamespace(find_command_by_text=find, get_plugin_config=lambda _: {}),
        'global_announcement_manager': SimpleNamespace(
            get_disabled_chat_commands=lambda _: ['翻唱'] if gate == 'disabled' else []),
        'has_command_permission': denied_operator,
        'is_local_operator': lambda *args: False,
        'global_config': SimpleNamespace(plugin=SimpleNamespace(permission={}, command_permissions={})),
        'logger': logging.getLogger(__name__),
    })
    async def hook(name, msg, **kwargs):
        return SimpleNamespace(aborted=False, kwargs=kwargs), msg
    async def store(msg):
        pass
    host = SimpleNamespace(_invoke_message_hook=hook, _store_intercepted_command_message=store,
        _mark_command_message=lambda *args, **kwargs: None, _coerce_int=lambda value, default: int(value))
    await instance.on_load()
    try:
        result = await process(host, message)
        assert permission_checks == [] and host_sends == []
        if gate != 'disabled':
            assert result[0] is True and len(invoked) == 1
            args = invoked[0]['args']
            assert args['message']['is_command'] is True
            assert args['text'] == args['message']['processed_plain_text'] == text
            assert args['stream_id'] == 'stream-1' and args['user_id'] == 'user-1'
            assert len(instance._music.search_calls) == 1 and sends
        else:
            assert invoked == [] and instance._music.search_calls == [] and sends == []
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', [
    'reply', 'forward', 'voice', 'image', 'card', 'dict', 'at',
    'missing', 'non_list', 'empty', 'nested', 'dict_data', 'missing_data',
    'non_component', 'mismatch', 'oversized_count', 'oversized_text',
    'notify', 'quoted', 'missing_notify', 'is_at', 'is_mentioned',
])
async def test_derived_or_malformed_raw_text_has_zero_effects(plugin, monkeypatch, case):
    instance, sends = plugin
    payload = command(text='/翻唱 Song - Artist --auto-reply')
    message = payload['message']
    text = payload['text']
    if case in ('reply', 'forward', 'voice', 'image', 'card', 'dict', 'at'):
        message['raw_message'] = [{'type': case, 'data': text}]
    elif case == 'missing':
        message.pop('raw_message')
    elif case == 'non_list':
        message['raw_message'] = {'type': 'text', 'data': text}
    elif case == 'empty':
        message['raw_message'] = []
    elif case == 'nested':
        message['raw_message'] = [{'type': 'text', 'data': [{'type': 'text', 'data': text}]}]
    elif case == 'dict_data':
        message['raw_message'] = [{'type': 'text', 'data': {'text': text}}]
    elif case == 'missing_data':
        message['raw_message'] = [{'type': 'text'}]
    elif case == 'non_component':
        message['raw_message'] = [text]
    elif case == 'mismatch':
        message['raw_message'][0]['data'] = text.removesuffix(' --auto-reply')
    elif case == 'oversized_count':
        message['raw_message'] = [{'type': 'text', 'data': text}] * 33
    elif case == 'oversized_text':
        message['raw_message'][0]['data'] = 'x' * 2049
    elif case == 'missing_notify':
        message.pop('is_notify')
    else:
        message[{'notify': 'is_notify', 'quoted': 'reply_to'}.get(case, case)] = (
            'quoted-message' if case == 'quoted' else True)
    # The gate must run even before looking up the active service or any job.
    def forbidden_lookup():
        raise AssertionError('untrusted raw message reached service lookup')
    monkeypatch.setattr(instance, '_require_active_cover', forbidden_lookup)
    assert not (await instance.handle_cover_command(**payload))[0]
    assert sends == [] and instance._jobs is None
    assert not hasattr(instance, '_voice_sender')


@pytest.mark.asyncio
async def test_multiple_direct_text_segments_use_exact_host_space_join(plugin):
    instance, _ = plugin
    payload = command(text='/翻唱 Song - Artist --auto-reply')
    payload['message']['raw_message'] = [
        {'type': 'text', 'data': '/翻唱 Song - Artist'},
        {'type': 'text', 'data': '--auto-reply'},
    ]
    await instance.on_load()
    try:
        assert (await instance.handle_cover_command(**payload))[0]
        token = instance._request_token(instance._command_identity('stream-1', payload))
        job = instance._jobs.store.find_request('stream-1', token)
        assert job.request['ingress_proof'] == 'napcat-direct-text-v2'
        assert job.request['auto_reply'] is True and job.delivery_state == 'pending'
        assert len(instance._music.search_calls) == 1
    finally:
        await instance.on_unload()
