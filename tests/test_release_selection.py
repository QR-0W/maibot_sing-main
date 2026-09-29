"""Do not label similarly timed releases as the same recording or a studio master."""
from pathlib import Path
from types import SimpleNamespace
import importlib.util
import json
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_worker', ROOT / 'runtime/worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def song(identifier, duration, album):
    return SimpleNamespace(identifier=str(identifier), song_name='Creep', singers='Radiohead',
                           duration_s=duration, album=album, file_size_bytes=4000000)


def choose(matches, **kwargs):
    return worker.select_release(matches, title='Creep', artist='Radiohead',
                                 max_seconds=300, max_bytes=67108864, **kwargs)


def test_similar_duration_studio_and_live_remain_ambiguous():
    matches = [song(1, 237.9, 'The Best Of'), song(2, 239., '40 Jaar Pinkpop'), song(3, 238.6, 'Compilation')]
    with pytest.raises(worker.SourceSelectionError) as error:
        choose(matches)
    assert error.value.code == 'ambiguous'
    assert len(error.value.candidates) == 3


def test_chained_durations_never_prove_recording_identity():
    with pytest.raises(worker.SourceSelectionError) as error:
        choose([song(i, 220 + i*3, f'Album {i}') for i in range(6)])
    assert error.value.code == 'ambiguous'


def test_explicit_source_id_or_unique_album_selects_without_studio_claim():
    matches = [song(1, 237.9, 'The Best Of'), song(2, 239., '40 Jaar Pinkpop')]
    chosen, selection = choose(matches, source_id='2')
    assert chosen.identifier == '2'
    assert selection['method'] == 'source_id'
    assert 'cluster_size' not in selection
    chosen, selection = choose(matches, album='The Best Of')
    assert chosen.identifier == '1'
    assert selection['method'] == 'album'


def test_single_id_duplicate_is_not_false_ambiguity():
    chosen, selection = choose([song(1, 237.9, 'Album'), song(1, 237.9, 'Album')])
    assert chosen.identifier == '1'
    assert selection['method'] == 'unique_id'


def test_missing_candidate_is_unavailable_not_a_payment_claim():
    with pytest.raises(worker.SourceSelectionError) as error:
        choose([])
    assert error.value.code == 'unavailable'


def test_failure_envelope_preserves_original_selection_error(tmp_path):
    args = SimpleNamespace(scratch=tmp_path, stage='download')
    try:
        choose([song(1, 237.9, 'Album A'), song(2, 237.9, 'Album B')])
    except worker.SourceSelectionError as error:
        worker.write_failure(args, error)
    path = tmp_path / 'error.json'
    before = path.read_bytes()
    record = json.loads(before)
    assert record['code'] == 'source_ambiguous'
    assert record['candidates'][0]['identifier'] == '1'
    worker.write_failure(args, subprocess.CalledProcessError(1, ['download']))
    assert path.read_bytes() == before
