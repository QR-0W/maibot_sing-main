"""Catalogue punctuation is label data; explicit paths remain invalid."""
import pytest

from runtime.catalogue_text import normalized_catalogue_text, validate_catalogue_text


@pytest.mark.parametrize('value', [
    'Weird Fishes/Arpeggi', 'Weird Fishes / Arpeggi', 'AC/DC', 'AC / DC',
    "Don't Stop", "Artist's Band", '...Like Clockwork', '-Song', '-h',
    '--voice', '~Song', 'D:ream', 'Song (Live)', '歌名・名前', 'ＡＣ／ＤＣ',
    '"Heroes"', 'Love; Hate', 'name $(literal)', 'A & B',
])
def test_legal_labels_remain_literal(value):
    assert validate_catalogue_text('  ' + value + '  ') == value


@pytest.mark.parametrize('value', [
    None, {}, '', ' ', '/etc/passwd', '//server/share', r'C:\music\song',
    'C:/music/song', r'\\server\share', r'a\b', '~/music', '../song',
    './song', 'a/../b', 'a / .. / b', 'a/./b',
    'https://example.invalid/song', 'file:///song', '／etc/passwd',
    'Ｃ：／music', '．．／song', 'a\x00b', 'a\nb', 'a\x7fb', 'a\x85b',
    'a\u2028b', 'a\ud800b', 'S' * 181,
])
def test_paths_and_invalid_text_are_rejected(value):
    with pytest.raises(ValueError):
        validate_catalogue_text(value)


@pytest.mark.parametrize('left,right', [
    ('Weird Fishes/Arpeggi', 'Weird Fishes / Arpeggi'),
    ('AC/DC', 'AC / DC'), ('ＡＣ／ＤＣ', 'ac/dc'),
    (' Album / Part  II ', 'album/part ii'),
])
def test_harmless_catalogue_formatting_is_equivalent(left, right):
    assert normalized_catalogue_text(left) == normalized_catalogue_text(right)


@pytest.mark.parametrize('left,right', [
    ('A/B', 'A B'), ('A/B', 'A-B'), ('Song', 'Song (Live)'),
    ('Song', 'Song Remastered'), ('Song', 'Songbird'),
    ("Don't Stop", 'Dont Stop'), ('Artist A', 'Artist B'),
    ('Album (Deluxe)', 'Album'), ('"Heroes"', 'Heroes'),
])
def test_identity_punctuation_and_qualifiers_are_not_removed(left, right):
    assert normalized_catalogue_text(left) != normalized_catalogue_text(right)
