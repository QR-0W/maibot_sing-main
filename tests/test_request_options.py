"""Pure request grammar tests: no SDK runtime, network, delivery or inference."""
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


@pytest.mark.parametrize('text,kind,render,instrumental,delivery', [
    ('/翻唱 Song - Artist', 'command', 'full', True, 'file'),
    ('唱一下《Song - Artist》', 'natural', 'excerpt', False, 'voice'),
    ('唱一段《Song - Artist》', 'natural', 'excerpt', False, 'voice'),
    ('唱《Song - Artist》', 'natural', 'excerpt', False, 'voice'),
])
def test_defaults_and_exact_shared_pattern(text, kind, render, instrumental, delivery):
    assert re.match(options.COVER_COMMAND_PATTERN, text)
    result = parse(text)
    assert (result.entry_kind, result.render_mode, result.instrumental, result.delivery_mode) == (
        kind, render, instrumental, delivery)
    assert result.query == 'Song - Artist'
    assert result.auto_reply is False
    assert result.album == result.source_id == result.model_selector == ''


@pytest.mark.parametrize('base', ['/翻唱 Song - Artist', '唱一下《Song - Artist》'])
@pytest.mark.parametrize('render', ['full', 'excerpt'])
@pytest.mark.parametrize('instrumental', [True, False])
@pytest.mark.parametrize('delivery', ['file', 'voice'])
def test_explicit_axes_are_independent(base, render, instrumental, delivery):
    backing = '--with-instrumental' if instrumental else '--without-instrumental'
    result = parse(f'{base} --{delivery} {backing} --{render}')
    assert (result.render_mode, result.instrumental, result.delivery_mode) == (render, instrumental, delivery)
    assert result.auto_reply is False


@pytest.mark.parametrize('base', ['/翻唱 Song - Artist', '唱一段《Song - Artist》'])
def test_selectors_in_any_order_and_literal_final_consent(base):
    result = parse(base + ' --voice --source-id id-1 --album Album Live - 2026 -v model.pth --full --auto-reply\t ')
    assert result.album == 'Album Live - 2026'
    assert result.source_id == 'id-1' and result.model_selector == 'model.pth'
    assert result.auto_reply is True
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
@pytest.mark.parametrize('base', ['/翻唱 Song - Artist', '唱《Song - Artist》'])
def test_bad_or_contradictory_options_rejected(base, suffix):
    with pytest.raises(ValueError):
        parse(base + ' ' + suffix)


@pytest.mark.parametrize('text', [
    '请唱一下《Song - Artist》', '我想听你唱《Song - Artist》', '唱一下Song - Artist',
    '唱一首《Song - Artist》', '唱《Song》', '唱《Song - Artist》好吗',
    '唱《Song - Artist》然后发送', '/唱《Song - Artist》', '翻唱 Song - Artist',
    'x翻唱 Song - Artist', '唱《《Song - Artist》》', '唱《Song - Artist》\n--auto-reply',
    '唱《Song - Artist》\n', '普通聊天里说唱《Song - Artist》',
])
def test_natural_grammar_does_not_capture_prose(text):
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


def test_title_and_artist_punctuation_are_not_shell_syntax():
    result = parse("/翻唱 Don't Stop - Artist's Band --album Year's Release")
    assert result.query == "Don't Stop - Artist's Band"
    assert result.album == "Year's Release" and result.auto_reply is False
