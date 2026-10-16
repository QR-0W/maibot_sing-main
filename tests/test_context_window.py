import importlib.util
from pathlib import Path

import pytest

MODULE=Path(__file__).resolve().parents[1]/'runtime/context_window.py'
spec=importlib.util.spec_from_file_location('context_window',MODULE)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
RATE=m.SAMPLE_RATE


@pytest.mark.parametrize('seconds,expected',[(30,(0,30)),(90,(0,90)),
                                              (91,(.5,90.5)),(900,(405,495))])
def test_freeze_context_is_one_centered_half_open_window(seconds,expected):
    context=m.freeze_context(seconds*RATE)
    assert context=={'start_frame':int(expected[0]*RATE),
                     'end_frame':int(expected[1]*RATE)}
    assert context['end_frame']-context['start_frame']==min(seconds,90)*RATE


def test_odd_remainder_is_floored_deterministically():
    source=90*RATE+1
    assert m.freeze_context(source)=={'start_frame':0,'end_frame':90*RATE}


@pytest.mark.parametrize('value',[None,True,False,0,-1,30*RATE-1,900*RATE+1,
                                  30*RATE+.5,float(30*RATE),'1323000',[],{}])
def test_source_bounds_are_exact_integers(value):
    with pytest.raises(m.ContextWindowError):m.freeze_context(value)


def test_validator_distinguishes_full_and_frozen_contexts():
    source=300*RATE
    assert m.validate_context_window(m.full_context(source),source)==m.full_context(source)
    assert m.validate_context_window(m.freeze_context(source),source,frozen=True)==m.freeze_context(source)
    with pytest.raises(m.ContextWindowError):
        m.validate_context_window(m.full_context(source),source,frozen=True)


@pytest.mark.parametrize('source',[30*RATE,90*RATE,90*RATE+1,90*RATE+3,900*RATE-1,900*RATE])
def test_integer_center_is_exact_half_open_and_does_not_mutate(source):
    expected_length=min(source,90*RATE)
    window=m.freeze_context(source);before=dict(window)
    assert window['start_frame']==(source-expected_length)//2
    assert window['end_frame']==window['start_frame']+expected_length
    assert all(type(value) is int for value in window.values())
    assert abs(window['start_frame']-(source-window['end_frame']))<=1
    assert m.validate_context_window(window,source,frozen=True) is window
    assert window==before


@pytest.mark.parametrize('window',[
    None,[],{}, {'start_frame':0}, {'start_frame':0,'end_frame':90*RATE,'extra':True},
    {'start_frame':True,'end_frame':90*RATE},
    {'start_frame':0,'end_frame':float(90*RATE)},
    {'start_frame':-1,'end_frame':90*RATE},
    {'start_frame':0,'end_frame':0},
    {'start_frame':0,'end_frame':90*RATE+1},
    {'start_frame':1,'end_frame':90*RATE+1}])
def test_context_schema_and_noncanonical_windows_are_rejected(window):
    with pytest.raises(m.ContextWindowError):
        m.validate_context_window(window,90*RATE,frozen=True)


def test_long_context_cannot_relabel_full_as_frozen_or_shift_window():
    source=900*RATE;frozen=m.freeze_context(source)
    with pytest.raises(m.ContextWindowError):m.validate_context_window(frozen,source)
    with pytest.raises(m.ContextWindowError):
        m.validate_context_window({'start_frame':frozen['start_frame']+1,
            'end_frame':frozen['end_frame']+1},source,frozen=True)
