"""Opt-in real Host AST contract, with attribute-only objects and no Host imports."""
from datetime import datetime, timezone
from types import SimpleNamespace
import logging

import pytest

from test_request_binding import HOST_ROOT, cover_component, host_function
from test_plugin_async_lifecycle import plugin


@pytest.fixture
def host_serializer():
    # Only data-holder types are fake: all serialization decisions run Host AST.
    names = ('TextComponent', 'ImageComponent', 'EmojiComponent', 'VoiceComponent',
             'FileComponent', 'AtComponent', 'ReplyComponent', 'ForwardNodeComponent')
    namespace = {name: type(name, (SimpleNamespace,), {}) for name in names}
    namespace.update({name: dict for name in (
        'UserInfoDict', 'GroupInfoDict', 'MessageInfoDict', 'MessageDict')})
    utils = SimpleNamespace()
    namespace['PluginMessageUtils'] = utils
    for name in ('_component_to_dict', '_message_sequence_to_dict',
                 '_message_info_to_dict', '_session_message_to_dict'):
        setattr(utils, name, host_function('src/plugin_runtime/host/message_utils.py', name, namespace))
    return utils, namespace


def host_message(namespace, route, text):
    message = SimpleNamespace()
    # Execute the actual base initializer rather than guessing safety defaults.
    initialize = host_function('src/common/data_models/mai_message_data_model.py', '__init__', {})
    initialize(message, 'platform-message-1', datetime.now(timezone.utc), 'qq')
    assert message.is_notify is message.is_at is message.is_mentioned is False
    assert message.reply_to is None
    extra = {'self_id': 'test-bot', 'platform_io_account_id': 'test-bot',
             'napcat_message_type': route}
    group = None
    if route == 'group':
        group = SimpleNamespace(group_id='trusted-group', group_name='test')
        extra['platform_io_target_group_id'] = group.group_id
    else:
        extra['platform_io_target_user_id'] = 'user-1'
    message.message_info = SimpleNamespace(
        user_info=SimpleNamespace(user_id='user-1', user_nickname='test', user_cardname=None),
        group_info=group, additional_config=extra)
    message.session_id = 'stream-1'
    message.is_command = True  # Host marks matched commands before RPC.
    message.processed_plain_text = text
    message.raw_message = SimpleNamespace(components=[namespace['TextComponent'](text=text)])
    return message


@pytest.mark.skipif(not HOST_ROOT, reason='未显式设置 MAIBOT_TEST_HOST_ROOT')
@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['private', 'group'])
@pytest.mark.parametrize('text', ['/翻唱 Song - Artist', '唱一下《Song - Artist》 --auto-reply'])
@pytest.mark.parametrize('case', ['direct', 'reply_to', 'reply_component', 'derived_text'])
async def test_real_host_serializer_command_contract(plugin, monkeypatch, host_serializer, route, text, case):
    instance, sends = plugin
    utils, namespace = host_serializer
    message = host_message(namespace, route, text)
    if case == 'reply_to':
        message.reply_to = 'quoted-message'
    elif case == 'reply_component':
        message.raw_message.components = [namespace['ReplyComponent'](
            target_message_id='quoted-message', target_message_content=text,
            target_message_sender_id='user-2', target_message_sender_nickname='other',
            target_message_sender_cardname=None)]
    elif case == 'derived_text':
        message.raw_message.components = [namespace['TextComponent'](text='unrelated direct text')]

    captured = []
    class Supervisor:
        async def invoke_plugin(self, **rpc):
            captured.append(rpc['args'])
            result = await instance.handle_cover_command(**rpc['args'])
            return SimpleNamespace(payload={'success': result[0], 'result': result})

    component = cover_component(instance)
    executor = host_function('src/plugin_runtime/component_query.py', '_build_command_executor', {
        'resolve_component_rpc_timeout_ms': lambda value: value,
        'is_local_operator': lambda *args: False,
        'PluginMessageUtils': utils,
        'logger': logging.getLogger(__name__),
    })(Supervisor(), 'qr0w.maibot-sing', component['name'], component['metadata'], 45000)

    if case == 'direct':
        await instance.on_load()
    else:
        def forbidden_lookup():
            pytest.fail('derived/replied message reached service lookup/admission')
        monkeypatch.setattr(instance, '_require_active_cover', forbidden_lookup)
    try:
        result = await executor(message=message, matched_groups={})
        assert len(captured) == 1
        payload = captured[0]
        serialized = payload['message']
        assert serialized['is_notify'] is serialized['is_at'] is serialized['is_mentioned'] is False
        assert serialized['message_info']['additional_config']['napcat_message_type'] == route
        assert (serialized['message_info']['group_info'] is None) == (route == 'private')
        if case == 'reply_to':
            assert serialized['reply_to'] == 'quoted-message'
        else:
            assert 'reply_to' not in serialized
        if case == 'direct':
            assert serialized['raw_message'] == [{'type': 'text', 'data': text}]
            assert result[0] is True
            identity = instance._command_identity('stream-1', payload)
            job = instance._jobs.store.find_request('stream-1', instance._request_token(identity))
            assert job.state == 'needs_selection'
            assert job.request['ingress_proof'] == 'napcat-direct-text-v2'
            assert job.request['auto_reply'] == text.endswith('--auto-reply')
            assert len(instance._music.search_calls) == 1 and sends
        else:
            assert result[0] is False
            assert sends == [] and instance._jobs is None and instance._music is None
    finally:
        if case == 'direct':
            await instance.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', ['omitted', None, 'quoted-message', '', False, 0, {}])
async def test_reply_key_optional_but_non_none_rejected(plugin, monkeypatch, reply):
    from test_plugin_async_lifecycle import command
    instance, sends = plugin
    payload = command()
    assert 'reply_to' not in payload['message']
    if reply != 'omitted':
        payload['message']['reply_to'] = reply
    if reply == 'omitted' or reply is None:
        identity = instance._command_identity('stream-1', payload)
        assert identity['user_id'] == 'user-1'
    else:
        def forbidden_lookup():
            pytest.fail('non-None reply reached service lookup')
        monkeypatch.setattr(instance, '_require_active_cover', forbidden_lookup)
        assert not (await instance.handle_cover_command(**payload))[0]
        assert sends == [] and instance._jobs is None
