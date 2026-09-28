"""Release selection must pick a studio master and refuse ambiguous catalogues."""
from pathlib import Path
from types import SimpleNamespace
import importlib.util

ROOT = Path(__file__).resolve().parents[1]


def load_worker():
    spec = importlib.util.spec_from_file_location('release_worker', ROOT / 'runtime/worker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def song(identifier, duration, album=''):
    return SimpleNamespace(identifier=identifier, duration_s=duration, album=album)


def test_creep_studio_cluster_beats_live_versions():
    """Real catalogue shape: the studio master repeats, live takes do not."""
    worker = load_worker()
    matches = [song(3375291324, 235.891, 'RFM 90'), song(26928500, 239.0, '40 Jaar Pinkpop'),
               song(27141620, 238.64, 'Greatest Hits of Modern Rock'), song(2158167564, 274.373, 'Summer Sonic'),
               song(22558968, 237.923, 'The Best Of'), song(2725592755, 281.04, 'Dijon'),
               song(2699873607, 280.386, "Glastonbury '97"), song(2715069705, 289.546, 'South Park'),
               song(27011845, 241.76, 'Now British')]
    chosen, selection = worker.select_release(matches)
    assert selection['cluster_size'] == 5
    assert selection['runner_up_size'] == 2
    assert chosen.duration_s < 250  # Studio length, not a live take.
    assert selection['chosen_album']


def test_single_candidate_is_accepted():
    worker = load_worker()
    chosen, selection = worker.select_release([song(1, 202.3, 'Album')])
    assert chosen.identifier == 1
    assert selection['candidates'] == 1


def test_all_distinct_durations_refused():
    """Every 15 Step candidate had its own duration, so nothing may be assumed."""
    worker = load_worker()
    matches = [song(i, d) for i, d in enumerate([272.5, 225.9, 261.0, 290.4, 283.1, 232.2, 249.3])]
    try:
        worker.select_release(matches)
    except RuntimeError as exc:
        assert '录音室版本' in str(exc)
    else:
        raise AssertionError('ambiguous live-only catalogue must be refused')


def test_empty_candidates_refused():
    worker = load_worker()
    try:
        worker.select_release([])
    except RuntimeError as exc:
        assert '没有可免费下载' in str(exc)
    else:
        raise AssertionError('empty candidate list must be refused')
