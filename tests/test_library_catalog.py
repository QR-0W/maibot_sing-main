from pathlib import Path
import hashlib
import importlib
import importlib.util
import json
import os
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sing_library_catalog_pkg', ROOT / '__init__.py',
                                            submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
catalog = importlib.import_module(spec.name + '.services.library_catalog')
manifest = importlib.import_module(spec.name + '.runtime.artifact_manifest')
IROHA_MODEL_SHA256 = catalog.IROHA_MODEL_SHA256
index_cover = catalog.index_cover
rebuild_library = catalog.rebuild_library


def make_cover(root, *, title='Aoi', artist='サカナクション', model=IROHA_MODEL_SHA256,
               instrumental=True, source=None, extra=None, payload=b'unchanged cover bytes'):
    source = source if source is not None else {'type': 'local', 'sha256': 'a' * 64, 'name': 'clip.wav'}
    identity = {'source': source, 'model_sha256': model, 'index_sha256': 'b' * 64,
                'parameters': {'seed': 20260928}, 'instrumental': instrumental}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    folder = root / key
    folder.mkdir()
    data = dict(identity, key=key, status='completed', sha256=hashlib.sha256(payload).hexdigest(),
                title=title, artist=artist, duration_s=30.0)
    if extra:
        data.update(extra)
    (folder / 'cover.mp3').write_bytes(payload)
    (folder / 'metadata.json').write_text(json.dumps(data, ensure_ascii=False))
    return key, folder


def test_rebuild_is_idempotent_and_clips_are_honest(tmp_path):
    key, folder = make_cover(tmp_path, extra={'excerpt_provenance': {'clip': {
        'start_s': 12, 'end_s': 42, 'file': '/private/audio', 'sha256': 'f' * 64}},
        'verified_source': {'url': 'https://example.test/stream?token=secret'}})
    unknown, _ = make_cover(tmp_path, title='Unknown local', source={'type': 'local', 'sha256': 'c' * 64},
                            instrumental=False, model='d' * 64)
    entries = rebuild_library(tmp_path)
    assert len(entries) == 2
    first = next(entry for entry in entries if entry['key'] == key)
    second = next(entry for entry in entries if entry['key'] == unknown)
    assert first['excerpt'] == '00分12秒至00分42秒'
    assert first['voice'] == '枣伊吕波'
    assert second['excerpt'] == '00分30秒片段-起点未标注'
    assert second['voice'] == '音色-' + 'd' * 12
    assert second['accompaniment'] == '纯人声'
    song = tmp_path / first['file']
    assert os.path.samefile(folder / 'cover.mp3', song)
    assert song.read_bytes() == b'unchanged cover bytes'
    previous = [(tmp_path / name).read_bytes() for name in ('README.md', 'library-index.json')]
    assert rebuild_library(tmp_path) == entries
    assert [(tmp_path / name).read_bytes() for name in ('README.md', 'library-index.json')] == previous
    assert index_cover(tmp_path, key) == first
    public = b' '.join(previous).decode()
    assert 'token=secret' not in public and '/private/audio' not in public
    assert 'source' not in json.loads((tmp_path / 'library-index.json').read_text())['entries'][0]


def test_versioned_metadata_always_uses_shared_manifest_validator(tmp_path, monkeypatch):
    # This checks routing only. ArtifactStore tests exercise full real-schema
    # metadata, receipt and provenance validation during publication/indexing.
    key, folder = make_cover(tmp_path, extra={'recipe_schema': 'test-new-schema'})
    calls = []
    def validate(data, expected_key, size):
        calls.append((data['recipe_schema'], expected_key, size))
        return data
    monkeypatch.setattr(manifest, 'validate_manifest', validate)
    assert index_cover(tmp_path, key)['key'] == key
    assert calls and set(calls) == {('test-new-schema', key, (folder / 'cover.mp3').stat().st_size)}


def test_unknown_recipe_cannot_fall_back_to_valid_legacy_identity(tmp_path):
    key, folder = make_cover(tmp_path, extra={'recipe_schema': 'unknown-schema', 'recipe': {}})
    before = (folder / 'cover.mp3').read_bytes()
    with pytest.raises(ValueError, match='Invalid cache identity'):
        index_cover(tmp_path, key)
    assert (folder / 'cover.mp3').read_bytes() == before
    assert not (tmp_path / 'library-index.json').exists()


def test_remote_does_not_claim_verified_full_song(tmp_path):
    key, _ = make_cover(tmp_path, source={'type': 'musicdl-native', 'platform': 'NeteaseMusicClient',
                                        'identifier': '1', 'title': 'Aoi', 'artist': 'X'})
    assert index_cover(tmp_path, key)['excerpt'] == '源音频-范围未核验'


@pytest.mark.parametrize('bad', [
    {'status': 'processing'}, {'sha256': '0' * 64}, {'key': '0' * 64},
    {'parameters': {'seed': 1}}, {'model_sha256': '0' * 64},
])
def test_rejects_invalid_commit_or_identity(tmp_path, bad):
    key, folder = make_cover(tmp_path)
    path = folder / 'metadata.json'
    data = json.loads(path.read_text())
    data.update(bad)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        index_cover(tmp_path, key)
    assert not (tmp_path / 'library-index.json').exists()


def test_rejects_symlink_source_and_unrelated_destination(tmp_path):
    key, folder = make_cover(tmp_path)
    source = folder / 'cover.mp3'
    source.rename(folder / 'original.mp3')
    source.symlink_to(folder / 'original.mp3')
    with pytest.raises(ValueError):
        index_cover(tmp_path, key)
    source.unlink()
    (folder / 'original.mp3').rename(source)
    entry = index_cover(tmp_path, key)
    dest = tmp_path / entry['file']
    dest.unlink()
    dest.write_bytes(b'unrelated file')
    with pytest.raises(ValueError, match='unrelated'):
        rebuild_library(tmp_path)
    assert dest.read_bytes() == b'unrelated file'


def test_rejects_symlink_metadata_and_invalid_key(tmp_path):
    key, folder = make_cover(tmp_path)
    with pytest.raises(ValueError, match='cache key'):
        index_cover(tmp_path, '../escape')
    metadata = folder / 'metadata.json'
    metadata.rename(folder / 'original.json')
    metadata.symlink_to(folder / 'original.json')
    with pytest.raises(ValueError, match='metadata'):
        index_cover(tmp_path, key)


def test_rejects_symlink_root_and_songs(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        rebuild_library(alias)
    (real / 'songs').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        rebuild_library(real)


def test_unsafe_names_and_duplicate_titles_have_distinct_entries(tmp_path):
    one, _ = make_cover(tmp_path, title='../\u202e\nA/B: title', artist='\u2066Artist')
    two, _ = make_cover(tmp_path, title='../\u202e\nA/B: title', artist='\u2066Artist',
                        instrumental=False)
    entries = rebuild_library(tmp_path)
    assert {entry['key'] for entry in entries} == {one, two}
    assert len({entry['file'].casefold() for entry in entries}) == 2
    assert all('..' not in entry['file'] and '\n' not in entry['file'] and '\u202e' not in entry['file']
               for entry in entries)
    assert all(len(Path(entry['file']).name.encode()) <= 255 for entry in entries)


def test_public_labels_refuse_url_or_credential(tmp_path):
    make_cover(tmp_path, title='https://host/path?token=private')
    with pytest.raises(ValueError, match='credential'):
        rebuild_library(tmp_path)
    assert not (tmp_path / 'library-index.json').exists()


def test_unmanaged_index_is_not_overwritten(tmp_path):
    make_cover(tmp_path)
    (tmp_path / 'library-index.json').write_text('{"other": true}')
    with pytest.raises(ValueError, match='unrelated'):
        rebuild_library(tmp_path)
    assert (tmp_path / 'library-index.json').read_text() == '{"other": true}'


def test_opt_in_corruption_isolation_preserves_four_legacy_covers(tmp_path):
    originals={}
    keys=[]
    for number in range(4):
        key,folder=make_cover(tmp_path,title='Legacy '+str(number),
            source={'type':'local','sha256':str(number)*64},payload=('old '+str(number)).encode())
        keys.append(key)
        for path in (folder/'cover.mp3',folder/'metadata.json'):
            originals[path]=(path.read_bytes(),path.stat().st_ino,path.stat().st_mode)
    broken=tmp_path/('f'*64);broken.mkdir()
    (broken/'metadata.json').write_text('{')
    with pytest.raises(ValueError):rebuild_library(tmp_path)
    warnings=[]
    entries=rebuild_library(tmp_path,skip_corrupt=True,warnings=warnings)
    assert {entry['key'] for entry in entries}==set(keys)
    assert len(warnings)==1 and 'ffffffffffffffff' in warnings[0]
    for path,expected in originals.items():
        assert (path.read_bytes(),path.stat().st_ino,path.stat().st_mode)==expected
    assert (broken/'metadata.json').read_text()=='{'
    assert rebuild_library(tmp_path,skip_corrupt=True)==entries


def test_unknown_duration_and_invalid_ranges(tmp_path):
    key, folder = make_cover(tmp_path, extra={'excerpt_provenance': {'clip': {'start_s': 20, 'end_s': 10}}})
    with pytest.raises(ValueError, match='boundaries'):
        rebuild_library(tmp_path)
    metadata = folder / 'metadata.json'
    data = json.loads(metadata.read_text())
    data.pop('duration_s')
    data.pop('excerpt_provenance')
    metadata.write_text(json.dumps(data))
    assert index_cover(tmp_path, key)['excerpt'] == '本地片段-范围未标注'
