"""Pure slash grammar tests: natural text belongs to Tool, never Command."""
from pathlib import Path
import importlib.util
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sing_request_options_test', ROOT / 'services/request_options.py')
options = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = options
spec.loader.exec_module(options)
parse = options.parse_request_text


def test_slash_defaults_and_exact_shared_pattern():
    text = '/翻唱 Song - Artist'
    assert re.match(options.COVER_COMMAND_PATTERN, text)
    result = parse(text)
    assert (result.entry_kind, result.render_mode, result.instrumental, result.delivery_mode) == (
        'command', 'full', True, 'file')
    assert result.query == 'Song - Artist' and result.auto_reply is True
    assert (result.title,result.artist) == ('Song','Artist')
    assert result.album == result.source_id == result.model_selector == ''


@pytest.mark.parametrize('render', ['full', 'excerpt'])
@pytest.mark.parametrize('instrumental', [True, False])
@pytest.mark.parametrize('delivery', ['file', 'voice'])
def test_explicit_axes_are_independent(render, instrumental, delivery):
    backing = '--with-instrumental' if instrumental else '--without-instrumental'
    result = parse(f'/翻唱 Song - Artist --{delivery} {backing} --{render}')
    assert (result.render_mode, result.instrumental, result.delivery_mode) == (render, instrumental, delivery)
    assert result.auto_reply is True


def test_selectors_in_any_order_and_literal_final_consent():
    result = parse('/翻唱 Song - Artist --voice --source-id id-1 --album Album Live - 2026 -v model.pth --full --auto-reply\t ')
    assert result.album == 'Album Live - 2026'
    assert result.source_id == 'id-1' and result.model_selector == 'model.pth'
    assert result.auto_reply is True and result.legacy_auto_reply_flag is True
    assert result.render_mode == 'full' and result.delivery_mode == 'voice'


@pytest.mark.parametrize('suffix', [
    '--full --excerpt', '--excerpt --full', '--full --full',
    '--with-instrumental --without-instrumental', '--without-instrumental --with-instrumental',
    '--voice --file', '--file --voice', '--file --file',
    '--album One --album Two', '--source-id id1 --source-id id2', '-v one -v two',
    '--auto-reply --voice', '--auto-reply --auto-reply', '--auto-reply yes',
    '--auto-reply=false', '--auto_reply', '--consent yes', '--instrumental', '-x',
    '--full false', '--voice true', '--album', '--source-id', '--source-id a b',
    '--source-id https://example.invalid/track', '-v two models', '-v',
])
def test_bad_or_contradictory_options_rejected(suffix):
    with pytest.raises(ValueError):
        parse('/翻唱 Song - Artist ' + suffix)


@pytest.mark.parametrize('text', [
    '唱一下《Song - Artist》', '唱一段《Song - Artist》', '唱《Song - Artist》',
    '你能唱一下朴树的生如夏花吗？', '你能唱一下朴树的生如夏花吗?',
    '你能唱一下朴树的生如夏花吗', '请翻唱朴树的生如夏花',
    '请翻唱张惠妹的如果你也听说', '唱一下《生如夏花 - 朴树》',
    '唱一下《Song - Artist》 --voice --excerpt',
    '请唱一下《Song - Artist》', '我想听你唱《Song - Artist》', '唱一下Song - Artist',
    '不要唱一下《生如夏花 - 朴树》', '如果你能唱一下朴树的生如夏花吗?',
    '请翻唱朴树的生如夏花不要发送', '请翻唱朴树的生如夏花别发',
    '请翻唱朴树的生如夏花不要唱', '你能唱一下朴树的生如夏花不要发送吗?',
    '请翻唱朴树的生如夏花发送',
    '你能唱一下朴树的生如夏花吗？我只是引用', '请翻唱朴树的生如夏花的现场版',
    '请翻唱朴树的生如夏花吗？', '你能唱一下朴树的生如夏花',
    '唱一首《Song - Artist》', '唱《Song》', '唱《Song - Artist》好吗',
    '唱《Song - Artist》然后发送', '/唱《Song - Artist》', '翻唱 Song - Artist',
    'x翻唱 Song - Artist', '唱《《Song - Artist》》', '唱《Song - Artist》\n--auto-reply',
    '唱《Song - Artist》\n', '普通聊天里说唱《Song - Artist》',
    '/翻唱选择 task 1', '/翻唱状态 task', '/翻唱取消 task',
])
def test_natural_text_and_other_commands_are_not_captured(text):
    assert re.match(options.COVER_COMMAND_PATTERN, text) is None
    with pytest.raises(ValueError):
        parse(text)


@pytest.mark.parametrize('text', [
    None, {}, '', ' ', '/翻唱', '/翻唱 Song', '/翻唱  - Artist', '/翻唱 Song - ',
    '/翻唱 Song - Artist\n--auto-reply', '/翻唱 Song - Artist\x00',
    '唱《Song - Artist --auto-reply》', '唱《Song --full - Artist》',
    '/翻唱 ' + 'S' * 2048 + ' - Artist',
])
def test_invalid_raw_text_cannot_be_replaced_by_inferred_intent(text):
    with pytest.raises(ValueError):
        parse(text)


@pytest.mark.parametrize('query', [
    "Don't Stop - Artist's Band", 'Song-Part - Artist-Name',
    'Weird Fishes / Arpeggi - Radiohead', 'Song - AC/DC',
    '...Like Clockwork - D:ream', '-Song - -h', '歌曲 - アーティスト',
    'Song - Part Two - Artist', '如果你也听说 - 张惠妹', '不要发送 - 艺人',
])
def test_legal_title_punctuation_and_words_are_not_tail_patched(query):
    result = parse('/翻唱 ' + query + " --album Year's Release")
    assert result.query == query
    assert result.album == "Year's Release" and result.auto_reply is True
