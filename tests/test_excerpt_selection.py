"""Deterministic synthetic audio only: no torch, checkpoints, network or inference."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

MODULE = Path(__file__).resolve().parents[1] / 'runtime/excerpt_selection.py'
spec = importlib.util.spec_from_file_location('excerpt_selection_test', MODULE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
RATE = m.SAMPLE_RATE


def tone(ranges, seconds=30, amplitude=.15):
    signal = np.zeros(round(seconds * RATE), dtype=np.float32)
    for start, end in ranges:
        left, right = round(start * RATE), round(end * RATE)
        signal[left:right] = amplitude * np.sin(2 * np.pi * 100 * np.arange(right-left) / RATE)
    return signal


def stems(tmp_path, ranges=((3, 19),), seconds=30):
    vocal = tone(ranges, seconds)
    position = np.arange(len(vocal), dtype=np.float32) / len(vocal)
    backing = np.column_stack((.02 + .03 * position, -.03 - .02 * position))
    sf.write(tmp_path / 'vocals.wav', vocal, RATE, subtype='FLOAT')
    sf.write(tmp_path / 'backing.wav', backing, RATE, subtype='FLOAT')
    return vocal, backing


def evidence(ranges=((2, 8), (16, 22))):
    records = []
    position = 0
    for start, end in ranges:
        start, end = round(start * RATE), round(end * RATE)
        records.append(dict(start_frame=start, end_frame=end, output_start_frame=position,
                            output_end_frame=position + end - start))
        position += end - start
    return dict(schema=m.SCHEMA, sample_rate=RATE, source_frames=30*RATE,
                output_frames=position, source_ranges=records,
                analysis=dict(hop_frames=m.HOP_FRAMES, rms_threshold=.01, flatness_max=.5,
                              breath_gap_frames=m.MAX_BREATH_GAP_FRAMES,
                              minimum_fragment_frames=m.MIN_FRAGMENT_FRAMES, analyzed_hops=600),
                fades=dict(in_frames=m.FADE_IN_FRAMES, out_frames=m.FADE_OUT_FRAMES),
                normalization=dict(target_rms=m.TARGET_VOCAL_RMS, applied_gain=1.0))


def test_analysis_uses_50ms_rms_and_flatness():
    sinusoid = tone(((0, 1),), seconds=1)
    rms, flatness = m._analysis(sinusoid)
    assert len(rms) == len(flatness) == 20
    np.testing.assert_allclose(rms, .15 / np.sqrt(2), rtol=1e-5)
    assert np.max(flatness) < .01
    _, noise_flatness = m._analysis(np.random.default_rng(42).normal(0, .1, RATE).astype('float32'))
    assert np.median(noise_flatness) > .5


def test_breaths_up_to_point_six_and_drop_fragments_below_one_point_two():
    # 50 ms hops: keep .6 s breathing, do not join .65 s; drop the isolated 1.15 s run.
    intervals = [(0, 24), (36, 60), (73, 96), (109, 133)]
    assert m._merge_breaths(intervals, 200*m.HOP_FRAMES) == [
        (0, 60*m.HOP_FRAMES), (109*m.HOP_FRAMES, 133*m.HOP_FRAMES)]


@pytest.mark.parametrize('ranges,count', [(((3, 19),), 1), (((2, 9), (16, 23)), 2),
                                          (((1, 7), (9, 15), (17, 29)), 1)])
def test_single_preferred_else_two_chronological_ranges(ranges, count):
    signal = tone(ranges)
    selected, *_ = m._select_ranges(signal)
    assert len(selected) == count
    assert m.MIN_EXCERPT_FRAMES <= sum(end-start for start, end in selected) <= m.MAX_EXCERPT_FRAMES
    assert all(end-start >= m.MIN_FRAGMENT_FRAMES for start, end in selected)
    assert all(left[1] <= right[0] for left, right in zip(selected, selected[1:]))
    assert selected == m._select_ranges(signal)[0]
    if len(ranges) == 3:
        assert selected[0][0] >= round(16.7*RATE)  # The available 12 s single wins.


@pytest.mark.parametrize('ranges', [((3, 15),), ((2, 8), (16, 22))])
def test_exact_twelve_seconds_survive_inward_low_energy_endpoints(monkeypatch, ranges):
    # Synthetic acoustic features with quieter minima inside each tonal interval.
    rms = np.full(600, .3)
    flatness = np.ones(600)
    for start, end in ranges:
        first, last = start*20, end*20
        rms[first:last] = .15
        flatness[first:last] = .1
        rms[first+6] = rms[last-6] = .05
    monkeypatch.setattr(m, '_analysis', lambda _: (rms, flatness))
    selected, *_ = m._select_ranges(np.zeros(30*RATE, dtype='float32'))
    assert sum(end-start for start, end in selected) >= 12*RATE
    assert len(selected) == len(ranges)
    for (start, end), (original_start, original_end) in zip(selected, ranges):
        assert abs(start-original_start*RATE) <= m.ENDPOINT_SEARCH_FRAMES
        assert abs(end-original_end*RATE) <= m.ENDPOINT_SEARCH_FRAMES


def test_endpoint_search_stays_within_bounds_and_preserves_fragment_minimum():
    rms = np.ones(601)
    rms[60] = 0
    rms[90] = 0
    assert m._adjust_endpoints((3*RATE, int(4.5*RATE)), rms, 30*RATE) == (3*RATE, int(4.5*RATE))
    start, end = m._adjust_endpoints((29*RATE, 30*RATE+1), rms, 30*RATE+1,
                                   minimum_length=RATE)
    assert 0 <= start < end <= 30*RATE+1
    assert end-start >= RATE
    assert abs(start-29*RATE) <= m.ENDPOINT_SEARCH_FRAMES
    assert abs(end-(30*RATE+1)) <= m.ENDPOINT_SEARCH_FRAMES


@pytest.mark.parametrize('signal', [np.zeros(30*RATE, dtype='float32'),
                                    tone(((1, 6), (15, 20))), tone(((1, 2), (5, 6)))])
def test_silent_short_or_fragmented_sources_fail_closed(signal):
    with pytest.raises(m.ExcerptSelectionError):
        m._select_ranges(signal)


def test_selected_assets_and_canonical_contract_preserve_shared_timeline(tmp_path):
    vocal, backing = stems(tmp_path, ((2, 9), (16, 23)))
    document = m.select_excerpt(SimpleNamespace(scratch=tmp_path))
    assert m.validate_selection(document) is document
    text = (tmp_path/'selection.json').read_text()
    assert text == m.canonical_selection_json(document)
    assert json.loads(text) == document
    assert not text.endswith('\n')
    selected, rate = sf.read(tmp_path/'vocal_000.wav', dtype='float32')
    selected_backing, backing_rate = sf.read(tmp_path/'excerpt_backing.wav', dtype='float32')
    assert rate == backing_rate == RATE
    assert selected_backing.shape == (len(selected), 2)
    assert len(selected) == document['output_frames']
    gain = document['normalization']['applied_gain']
    for record in document['source_ranges']:
        start, end = record['start_frame'], record['end_frame']
        left, right = record['output_start_frame'], record['output_end_frame']
        np.testing.assert_allclose(selected[left:right], m._fade(vocal[start:end])*gain, atol=1e-7)
        np.testing.assert_array_equal(selected_backing[left:right], m._fade(backing[start:end]))
        assert selected[left] == selected[right-1] == 0
    assert max(abs(selected)) <= .95
    assert not (tmp_path/'vocal_001.wav').exists()


def test_normalization_changes_gain_not_pitch_or_interior_waveform(tmp_path):
    vocal, _ = stems(tmp_path)
    document = m.select_excerpt(SimpleNamespace(scratch=tmp_path))
    selected, _ = sf.read(tmp_path/'vocal_000.wav', dtype='float32')
    record = document['source_ranges'][0]
    gain = document['normalization']['applied_gain']
    interior = slice(m.FADE_IN_FRAMES, len(selected)-m.FADE_OUT_FRAMES)
    source = vocal[record['start_frame']:record['end_frame']]
    np.testing.assert_allclose(selected[interior], source[interior]*gain, atol=1e-7)
    assert .5 <= gain <= 2
    assert .03*RATE <= m.FADE_IN_FRAMES <= .06*RATE
    assert .08*RATE <= m.FADE_OUT_FRAMES <= .15*RATE


@pytest.mark.parametrize('frames,channels,rate', [(30*RATE-1, 2, RATE), (300*RATE+1, 2, RATE),
                                                 (30*RATE, 1, RATE), (30*RATE, 3, RATE),
                                                 (30*RATE, 2, 48000)])
def test_stem_metadata_rejected_before_loading_samples(monkeypatch, tmp_path, frames, channels, rate):
    def info(path):
        return SimpleNamespace(frames=frames, samplerate=rate,
                               channels=1 if path.name == 'vocals.wav' else channels)
    monkeypatch.setattr(sf, 'info', info)
    monkeypatch.setattr(sf, 'read', lambda *a, **k: pytest.fail('unsafe metadata must fail before allocation'))
    with pytest.raises(m.ExcerptSelectionError):
        m._read_stems(tmp_path)


def test_nonfinite_stems_rejected(tmp_path):
    vocal, _ = stems(tmp_path)
    vocal[0] = np.nan
    sf.write(tmp_path/'vocals.wav', vocal, RATE, subtype='FLOAT')
    with pytest.raises(m.ExcerptSelectionError):
        m._read_stems(tmp_path)


@pytest.mark.parametrize('path,value', [
    (('schema',), 'other'), (('sample_rate',), 44100.0), (('source_frames',), 300*RATE+1),
    (('output_frames',), 12*RATE-1), (('output_frames',), 18*RATE+1),
    (('source_ranges', 0, 'start_frame'), True), (('source_ranges', 0, 'end_frame'), 30*RATE+1),
    (('source_ranges', 1, 'start_frame'), RATE),
    (('source_ranges', 1, 'output_start_frame'), 0),
    (('analysis', 'hop_frames'), 2205.0), (('analysis', 'rms_threshold'), float('nan')),
    (('analysis', 'rms_threshold'), True), (('analysis', 'analyzed_hops'), 599),
    (('fades', 'in_frames'), 2205.0), (('normalization', 'applied_gain'), float('inf')),
    (('normalization', 'applied_gain'), True), (('normalization', 'applied_gain'), 0),
    (('analysis', 'rms_threshold'), 10**400), (('normalization', 'applied_gain'), 10**400),
])
def test_validator_rejects_invalid_evidence(path, value):
    document = evidence()
    parent = document
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    with pytest.raises(m.ExcerptSelectionError):
        m.validate_selection(document)


def test_validator_rejects_sub_one_point_two_second_range_even_with_valid_total():
    with pytest.raises(m.ExcerptSelectionError):
        m.validate_selection(evidence(((1, 2.15), (5, 16))))
    assert m.validate_selection(evidence(((1, 2.2), (5, 15.8))))


@pytest.mark.parametrize('alter', [lambda d: d.update(extra=True),
                                  lambda d: d['source_ranges'][0].update(extra=True),
                                  lambda d: d.update(source_ranges=[]),
                                  lambda d: d.update(source_ranges=d['source_ranges']*2)])
def test_validator_rejects_unknown_fields_or_range_count(alter):
    document = evidence()
    alter(document)
    with pytest.raises(m.ExcerptSelectionError):
        m.validate_selection(document)


def test_validator_and_canonical_serializer_import_without_site_packages():
    script = '''import importlib.util, json, sys
spec=importlib.util.spec_from_file_location('selection_stdlib',sys.argv[1])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
doc=json.loads(sys.stdin.read())
assert m.validate_selection(doc) is doc
assert 'numpy' not in sys.modules and 'soundfile' not in sys.modules and 'torch' not in sys.modules
print(m.canonical_selection_json(doc),end='')
'''
    run = subprocess.run([sys.executable, '-S', '-c', script, str(MODULE)],
                         input=json.dumps(evidence()), text=True, capture_output=True, timeout=5)
    assert run.returncode == 0, run.stderr
    assert run.stdout == m.canonical_selection_json(evidence())


@pytest.mark.parametrize('instrumental', [False, True])
def test_mix_refades_rvc_regenerated_boundaries_and_internal_join(tmp_path, instrumental):
    stems(tmp_path, ((2, 9), (16, 23)))
    document = m.select_excerpt(SimpleNamespace(scratch=tmp_path))
    length = document['output_frames']
    # A stand-in converter deliberately ignores the input fades and returns DC.
    sf.write(tmp_path/'vocal_000_converted.wav', np.full(length-882, .2), RATE, subtype='FLOAT')
    # Stale full-song artifacts must not enter the short-excerpt mix.
    sf.write(tmp_path/'vocal_001_converted.wav', np.full(100, np.nan), RATE, subtype='FLOAT')
    m.mix_excerpt(SimpleNamespace(scratch=tmp_path, instrumental=instrumental))
    result, rate = sf.read(tmp_path/'mixed.wav', dtype='float32', always_2d=True)
    assert rate == RATE and result.shape == (length, 2 if instrumental else 1)
    assert np.isfinite(result).all() and np.max(np.abs(result)) <= .950001
    assert np.max(np.abs(result)) > .01
    for record in document['source_ranges']:
        start, end = record['output_start_frame'], record['output_end_frame']
        assert np.max(abs(result[start])) < 1e-6
        assert np.max(abs(result[end-1])) < 1e-6
        assert np.max(abs(result[start+1]-result[start])) < .001
        assert np.max(abs(result[end-1]-result[end-2])) < .001


@pytest.mark.parametrize('kind', ['drift', 'nan', 'silent', 'backing_mono', 'wrong_rate'])
def test_mix_rejects_invalid_conversion_or_backing(tmp_path, kind):
    stems(tmp_path)
    document = m.select_excerpt(SimpleNamespace(scratch=tmp_path))
    length = document['output_frames']
    converted = np.full(length + (round(.006*length) if kind == 'drift' else 0), .2)
    if kind == 'nan':
        converted[10] = np.nan
    if kind == 'silent':
        converted[:] = 0
    sf.write(tmp_path/'vocal_000_converted.wav', converted, 48000 if kind == 'wrong_rate' else RATE,
             subtype='FLOAT')
    if kind == 'backing_mono':
        sf.write(tmp_path/'excerpt_backing.wav', np.zeros(length), RATE, subtype='FLOAT')
    with pytest.raises(m.ExcerptSelectionError):
        m.mix_excerpt(SimpleNamespace(scratch=tmp_path, instrumental=False))


def test_noncanonical_selection_is_not_consumed(tmp_path):
    (tmp_path/'selection.json').write_text(json.dumps(evidence(), indent=2))
    with pytest.raises(m.ExcerptSelectionError, match='canonical'):
        m._load_selection(tmp_path/'selection.json')
