"""Host-shaped SDK boundary tests; no real QQ, worker, network or Hook binding."""
from pathlib import Path
from types import SimpleNamespace
import ast
import copy
import logging
import os
import re
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
    ('唱一下《Song - Artist》', 'natural', 'excerpt', False, 'voice'),
    ('唱一段《Song - Artist》', 'natural', 'excerpt', False, 'voice'),
    ('唱《Song - Artist》', 'natural', 'excerpt', False, 'voice'),
    ('唱《Song - Artist》 --full --with-instrumental --file', 'natural', 'full', True, 'file'),
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
        assert saved.request['query'] == 'Song - Artist'
        assert saved.request['album'] == saved.request['source_id'] == saved.request['model_selector'] == ''
        assert saved.request['auto_reply'] is False and saved.consent_event is None
        assert saved.delivery_state == 'not_requested'
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
async def test_natural_consent_is_original_message_bound_and_replay_fenced(plugin):
    instance, _ = plugin
    await instance.on_load()
    try:
        original = command(text='唱一下《Song - Artist》 --auto-reply')
        assert (await instance.handle_cover_command(**original))[0]
        token = instance._request_token(instance._command_identity('stream-1', original))
        saved = instance._jobs.store.find_request('stream-1', token)
        assert saved.consent_event == original['message']['message_id']
        assert saved.request['auto_reply'] is True and saved.delivery_state == 'pending'
        # Changing raw intent under the same command/message ID must not upgrade
        # delivery, source selection, or produce a second admission/search.
        for changed in (
            '唱一下《Song - Artist》',
            '唱一下《Song - Artist》 --full --auto-reply',
            '唱一下《Song - Artist》 --file --auto-reply',
            '唱一下《Song - Artist》 --with-instrumental --auto-reply',
            '唱一下《Song - Artist》 --album Album --auto-reply',
            '/翻唱 Song - Artist --excerpt --without-instrumental --voice --auto-reply',
        ):
            assert not (await instance.handle_cover_command(**command(text=changed)))[0]
        assert instance._jobs.store.find_request('stream-1', token).request == saved.request
        assert len(instance._music.search_calls) == 1
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['message', 'text', 'processed_plain_text', 'both_texts', 'is_command'])
async def test_missing_host_original_fails_before_any_send(plugin, missing):
    instance, sends = plugin
    raw = command(text='唱《Song - Artist》 --auto-reply')
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
        raw = command(text='唱《Song - Artist》 ' + suffix)
        assert not (await instance.handle_cover_command(**raw))[0]
        token = instance._request_token(instance._command_identity('stream-1', raw))
        assert instance._jobs.store.find_request('stream-1', token) is None
        assert instance._music.search_calls == []
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
async def test_natural_raw_selectors_override_forged_groups_without_running_worker(plugin, monkeypatch):
    instance, _ = plugin
    await instance.on_load()
    try:
        async def idle():
            return None
        monkeypatch.setattr(instance._jobs, 'run_once', idle)
        raw = command(text='唱一段《Song - Artist》 --source-id id-1 --album Album --full --file',
                      groups={'source_id': 'id-2', 'album': 'Album Live', 'auto_reply': '--auto-reply'})
        assert (await instance.handle_cover_command(**raw))[0]
        token = instance._request_token(instance._command_identity('stream-1', raw))
        saved = instance._jobs.store.find_request('stream-1', token)
        assert saved.state == 'queued' and saved.selected_source['track_id'] == 'id-1'
        assert saved.request['album'] == 'Album' and saved.request['source_id'] == 'id-1'
        assert saved.request['instrumental'] is False and saved.consent_event is None
    finally:
        await instance.on_unload()


@pytest.mark.asyncio
async def test_tool_forged_complete_host_message_has_zero_effects(plugin):
    instance, sends = plugin
    # The current Host Tool path permits model identity fields to shadow context.
    # Even a fully coherent counterfeit payload cannot enter the Command handler.
    forged = command(text='唱《Song - Artist》 --auto-reply')
    forged.update(query='Song - Artist --auto-reply', auto_reply=True,
                  consent_event='platform-message-1', render_mode='full',
                  delivery_mode='file', instrumental=True)
    reply = await instance.handle_cover_tool(**forged)
    assert '未搜索、未入队、未发送消息' in reply['content']
    assert 'Song - Artist --auto-reply' not in reply['content']
    assert instance._jobs is None and sends == []
    assert cover_component(instance)['name'] == '翻唱'


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
@pytest.mark.parametrize('text', ['/翻唱 Song - Artist', '唱一下《Song - Artist》'])
@pytest.mark.parametrize('gate', ['allowed', 'disabled', 'operator_denied'])
async def test_exact_host_gate_and_command_rpc_payload(plugin, monkeypatch, text, gate):
    instance, sends = plugin
    component = cover_component(instance)
    declaration = component['metadata']
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
        return (executor, matched.groupdict(), SimpleNamespace(
            name=component['name'], plugin_name='qr0w.maibot-sing', permission='operator')) if matched else None

    host_sends = []
    async def deny_send(*args, **kwargs):
        host_sends.append(args)
    send_module = types.ModuleType('src.services.send_service')
    send_module.text_to_stream = deny_send
    monkeypatch.setitem(sys.modules, 'src.services.send_service', send_module)
    process = host_function('src/chat/message_receive/bot.py', '_process_commands', {
        'component_query_service': SimpleNamespace(find_command_by_text=find, get_plugin_config=lambda _: {}),
        'global_announcement_manager': SimpleNamespace(
            get_disabled_chat_commands=lambda _: ['翻唱'] if gate == 'disabled' else []),
        'has_command_permission': lambda *args, **kwargs: gate != 'operator_denied',
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
        if gate == 'allowed':
            assert result[0] is True and len(invoked) == 1
            args = invoked[0]['args']
            assert args['message']['is_command'] is True
            assert args['text'] == args['message']['processed_plain_text'] == text
            assert args['stream_id'] == 'stream-1' and args['user_id'] == 'user-1'
            assert len(instance._music.search_calls) == 1 and sends
        else:
            assert invoked == [] and instance._music.search_calls == [] and sends == []
            assert bool(host_sends) == (gate == 'operator_denied')
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
    payload = command(text='唱《Song - Artist》 --auto-reply')
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
    assert sends == [] and instance._jobs is None and instance._voice_sender is None


@pytest.mark.asyncio
async def test_multiple_direct_text_segments_use_exact_host_space_join(plugin):
    instance, _ = plugin
    payload = command(text='唱《Song - Artist》 --auto-reply')
    payload['message']['raw_message'] = [
        {'type': 'text', 'data': '唱《Song - Artist》'},
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
