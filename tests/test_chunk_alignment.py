"""RVC chunk alignment: no silent 20 ms gap, bounded drift, safe merge of short tails."""
from pathlib import Path
import importlib.util
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
spec = importlib.util.spec_from_file_location('chunk_worker', ROOT / 'runtime/worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)

RATE = 44100


def ramp(count):
    return np.linspace(-.5, .5, count, dtype=np.float32)


def test_identical_length_is_returned_unmodified():
    chunk = ramp(1000)
    assert worker.align_converted_chunk(chunk, 1000) is chunk


def test_short_output_is_stretched_not_zero_padded():
    """The real defect: RVC returns 882 fewer samples per 20 s call."""
    chunk = ramp(882000 - 882)
    aligned = worker.align_converted_chunk(chunk, 882000)
    assert len(aligned) == 882000
    assert np.isfinite(aligned).all()
    # A silent pad would zero the preceding 20 ms; interpolation must not.
    assert np.max(np.abs(aligned[-882:])) > 0
    assert np.allclose(aligned[0], chunk[0], atol=1e-6)
    assert np.allclose(aligned[-1], chunk[-1], atol=1e-6)
    # Sub-0.2% timing correction, so pitch drift stays far below a semitone.
    assert abs(len(chunk) - 882000) / 882000 < .005


def test_drift_beyond_half_a_percent_is_refused():
    with pytest.raises(RuntimeError, match='0.5%'):
        worker.align_converted_chunk(ramp(5000), 6000)
    with pytest.raises(ValueError, match='无效'):
        worker.align_converted_chunk(np.array([], dtype=np.float32), 100)
    with pytest.raises(ValueError, match='无效'):
        worker.align_converted_chunk(np.array([np.nan], dtype=np.float32), 1)


@pytest.mark.parametrize('seconds, expected', [
    # Splits always land on 20 s steps, so no call exceeds the 25 s CLI limit.
    (30, [(0, 20 * RATE), (20 * RATE, 30 * RATE)]),
    (40, [(0, 20 * RATE), (20 * RATE, 40 * RATE)]),
    (41, [(0, 20 * RATE), (20 * RATE, 41 * RATE)]),
    # A 4 s tail would lose RVC context, so it merges back into a 24 s call.
    (44, [(0, 20 * RATE), (20 * RATE, 44 * RATE)]),
    # Exactly 5 s is kept as its own call.
    (65, [(0, 20 * RATE), (20 * RATE, 40 * RATE), (40 * RATE, 60 * RATE), (60 * RATE, 65 * RATE)]),
    (300, [(start, start + 20 * RATE) for start in range(0, 300 * RATE, 20 * RATE)]),
])
def test_chunk_bounds_never_leave_a_short_tail_or_exceed_25s(seconds, expected):
    bounds = worker.chunk_bounds(seconds * RATE, RATE)
    assert bounds == expected
    for start, end in bounds:
        assert 0 < end - start <= 25 * RATE
        # A sub-5 s final call would lose RVC context.
        assert end - start >= 5 * RATE
    assert bounds[0][0] == 0 and bounds[-1][1] == seconds * RATE
    assert all(bounds[i][1] == bounds[i + 1][0] for i in range(len(bounds) - 1))


def test_chunk_bounds_reject_short_or_invalid_input():
    with pytest.raises(ValueError):
        worker.chunk_bounds(29 * RATE, RATE)
    with pytest.raises(ValueError):
        worker.chunk_bounds(60 * RATE, 0)
