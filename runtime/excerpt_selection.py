"""Deterministic short singing excerpt selection and synchronized mixing.

The selector operates on the already separated 44.1 kHz stems.  It never
changes pitch, predicts melody, adds reverb, or falls back to a whole song.
RMS and spectral flatness are acoustic heuristics, NOT evidence of a chorus,
singing quality, or a semantic phrase ending. Endpoints are low-energy cuts.

Contract: selection.json is canonical UTF-8 JSON (sorted keys, compact separators,
no NaN, no trailing newline), schema sing-excerpt-selection-v1. All ranges use
integer 44.1 kHz frames and half-open [start, end) bounds. source_ranges contains
one or two chronological, nonoverlapping ranges, each >=1.2 s; their contiguous
output_start_frame/output_end_frame bounds partition a 12–18 s output. Importing
this module and validate_selection(document) needs only the standard library.
"""
from pathlib import Path
import json
import math
import os
import uuid


SCHEMA = 'sing-excerpt-selection-v1'
SAMPLE_RATE = 44100
HOP_FRAMES = 2205  # 50 ms
MIN_EXCERPT_FRAMES = 12 * SAMPLE_RATE
TARGET_EXCERPT_FRAMES = 15 * SAMPLE_RATE
MAX_EXCERPT_FRAMES = 18 * SAMPLE_RATE
MAX_BREATH_GAP_FRAMES = int(.6 * SAMPLE_RATE)
MIN_FRAGMENT_FRAMES = int(1.2 * SAMPLE_RATE)
ENDPOINT_SEARCH_FRAMES = int(.3 * SAMPLE_RATE)
FADE_IN_FRAMES = int(.05 * SAMPLE_RATE)
FADE_OUT_FRAMES = int(.12 * SAMPLE_RATE)
TARGET_VOCAL_RMS = .12


class ExcerptSelectionError(RuntimeError):
    pass


def _analysis(vocal):
    import numpy as np
    rms=[]
    flatness=[]
    for start in range(0,len(vocal),HOP_FRAMES):
        block=vocal[start:min(len(vocal),start+HOP_FRAMES)]
        if len(block)<HOP_FRAMES:
            block=np.pad(block,(0,HOP_FRAMES-len(block)))
        value=float(np.sqrt(np.mean(block.astype(np.float64)**2)))
        window=block*np.hanning(len(block))
        power=np.abs(np.fft.rfft(window.astype(np.float64)))**2
        epsilon=1e-15
        flat=float(math.exp(float(np.mean(np.log(power+epsilon)))) /
                   float(np.mean(power+epsilon)))
        rms.append(value)
        flatness.append(flat)
    return np.asarray(rms,dtype=np.float64),np.asarray(flatness,dtype=np.float64)


def _runs(mask):
    result=[]
    start=None
    for index,active in enumerate(mask):
        if active and start is None:
            start=index
        elif not active and start is not None:
            result.append((start,index))
            start=None
    if start is not None:
        result.append((start,len(mask)))
    return result


def _merge_breaths(intervals,source_frames):
    merged=[]
    maximum_gap=math.ceil(MAX_BREATH_GAP_FRAMES/HOP_FRAMES)
    for start,end in intervals:
        if merged and start-merged[-1][1]<=maximum_gap:
            merged[-1]=(merged[-1][0],end)
        else:
            merged.append((start,end))
    result=[]
    for start,end in merged:
        first=start*HOP_FRAMES
        last=min(source_frames,end*HOP_FRAMES)
        if last-first>=MIN_FRAGMENT_FRAMES:
            result.append((first,last))
    return result


def _window_score(scores,start,end):
    first=max(0,start//HOP_FRAMES)
    last=min(len(scores),math.ceil(end/HOP_FRAMES))
    return float(scores[first:last].sum())


def _best_window(interval,scores,length):
    start,end=interval
    if end-start<=length:
        return interval
    hop_length=max(1,length//HOP_FRAMES)
    first=start//HOP_FRAMES
    last=end//HOP_FRAMES
    best=None
    for position in range(first,max(first+1,last-hop_length+1)):
        candidate=(position*HOP_FRAMES,min(end,position*HOP_FRAMES+length))
        rank=(_window_score(scores,*candidate),-candidate[0])
        if best is None or rank>best[0]:
            best=(rank,candidate)
    return best[1]


def _endpoint_candidates(frame,minimum,maximum):
    low=max(minimum,frame-ENDPOINT_SEARCH_FRAMES)
    high=min(maximum,frame+ENDPOINT_SEARCH_FRAMES)
    choices=set(range(math.ceil(low/HOP_FRAMES)*HOP_FRAMES,high+1,HOP_FRAMES))
    # Retain the exact original cut, including non-hop-aligned source tails.
    if low<=frame<=high:
        choices.add(frame)
    return sorted(choices)


def _adjust_endpoints(interval,rms,source_frames,*,minimum_length=MIN_FRAGMENT_FRAMES,
                      maximum_length=MAX_EXCERPT_FRAMES,minimum_start=0,maximum_end=None):
    """Jointly minimize endpoint energy subject to the actual duration budget.

    Independent minima can shrink an exactly 12 s selection below the contract.
    Keep the original feasible pair in the search rather than trim-only repair.
    """
    start,end=interval
    maximum_end=source_frames if maximum_end is None else min(source_frames,maximum_end)
    starts=_endpoint_candidates(start,minimum_start,maximum_end-minimum_length)
    ends=_endpoint_candidates(end,minimum_start+minimum_length,maximum_end)
    candidates=[(left,right) for left in starts for right in ends
                if minimum_length<=right-left<=maximum_length]
    if not candidates:
        raise ExcerptSelectionError('No duration-preserving low-energy endpoints')
    def rank(pair):
        left,right=pair
        energy=float(rms[min(left//HOP_FRAMES,len(rms)-1)])+float(
            rms[min(right//HOP_FRAMES,len(rms)-1)])
        return energy,abs(left-start)+abs(right-end),left,right
    return min(candidates,key=rank)


def _select_ranges(vocal):
    import numpy as np
    rms,flatness=_analysis(vocal)
    peak=float(rms.max(initial=0.))
    if peak<.003:
        raise ExcerptSelectionError('Separated vocal is below the acoustic activity threshold')
    threshold=max(.003,peak*.08)
    active=(rms>=threshold)&(flatness<=.50)
    intervals=_merge_breaths(_runs(active),len(vocal))
    if not intervals:
        raise ExcerptSelectionError('No tonal vocal fragment survived selection')
    scores=(rms/(peak+1e-12))*np.clip(1.-flatness,0.,1.)
    singles=[]
    for interval in intervals:
        if interval[1]-interval[0]>=MIN_EXCERPT_FRAMES:
            length=min(TARGET_EXCERPT_FRAMES,interval[1]-interval[0])
            candidate=_best_window(interval,scores,length)
            singles.append((_window_score(scores,*candidate),candidate))
    if singles:
        chosen=[max(singles,key=lambda item:(item[0],-item[1][0]))[1]]
    else:
        pairs=[]
        for left in range(len(intervals)):
            for right in range(left+1,len(intervals)):
                total=(intervals[left][1]-intervals[left][0] +
                       intervals[right][1]-intervals[right][0])
                if total>=MIN_EXCERPT_FRAMES:
                    rank=(_window_score(scores,*intervals[left])+
                          _window_score(scores,*intervals[right]),-left,-right)
                    pairs.append((rank,intervals[left],intervals[right]))
        if not pairs:
            raise ExcerptSelectionError('Fewer than 12 seconds of usable vocal fragments')
        _,first,second=max(pairs,key=lambda item:item[0])
        total=(first[1]-first[0])+(second[1]-second[0])
        target=min(TARGET_EXCERPT_FRAMES,total)
        first_length=first[1]-first[0]
        second_length=second[1]-second[0]
        allocation=max(MIN_FRAGMENT_FRAMES,min(first_length,
            round(target*first_length/total)))
        other=target-allocation
        if other<MIN_FRAGMENT_FRAMES:
            other=MIN_FRAGMENT_FRAMES
            allocation=target-other
        chosen=[_best_window(first,scores,allocation),
                _best_window(second,scores,min(second_length,other))]
    adjusted=[]
    total=0
    for number,interval in enumerate(chosen):
        remaining=sum(end-start for start,end in chosen[number+1:])
        cut=_adjust_endpoints(interval,rms,len(vocal),
            minimum_length=max(MIN_FRAGMENT_FRAMES,MIN_EXCERPT_FRAMES-total-remaining),
            maximum_length=MAX_EXCERPT_FRAMES-total-remaining,
            minimum_start=adjusted[-1][1] if adjusted else 0,
            maximum_end=chosen[number+1][0] if number+1<len(chosen) else len(vocal))
        adjusted.append(cut)
        total+=cut[1]-cut[0]
    if not MIN_EXCERPT_FRAMES<=total<=MAX_EXCERPT_FRAMES:
        raise ExcerptSelectionError('Selected excerpt duration is outside 12–18 seconds')
    return adjusted,rms,flatness,threshold


def _fade(segment):
    import numpy as np
    result=np.asarray(segment,dtype=np.float32).copy()
    length=len(result)
    fade_in=min(FADE_IN_FRAMES,length)
    fade_out=min(FADE_OUT_FRAMES,length)
    if fade_in:
        result[:fade_in]*=np.linspace(0.,1.,fade_in,dtype=np.float32).reshape(
            (fade_in,)+(1,)*(result.ndim-1))
    if fade_out:
        result[-fade_out:]*=np.linspace(1.,0.,fade_out,dtype=np.float32).reshape(
            (fade_out,)+(1,)*(result.ndim-1))
    return result


def _read_stems(scratch):
    import numpy as np
    import soundfile as sf
    vocal_info=sf.info(scratch/'vocals.wav')
    backing_info=sf.info(scratch/'backing.wav')
    if (vocal_info.samplerate!=SAMPLE_RATE or backing_info.samplerate!=SAMPLE_RATE or
            vocal_info.channels!=1 or backing_info.channels!=2 or
            not 30*SAMPLE_RATE<=vocal_info.frames<=300*SAMPLE_RATE or
            backing_info.frames!=vocal_info.frames):
        raise ExcerptSelectionError('Separated stems have an invalid shared timeline')
    vocal,rate=sf.read(scratch/'vocals.wav',dtype='float32',always_2d=False)
    backing,backing_rate=sf.read(scratch/'backing.wav',dtype='float32',always_2d=True)
    if (rate!=SAMPLE_RATE or backing_rate!=rate or vocal.ndim!=1 or backing.shape[1]!=2 or
            len(vocal)!=len(backing) or not 30*rate<=len(vocal)<=300*rate or
            not np.isfinite(vocal).all() or not np.isfinite(backing).all()):
        raise ExcerptSelectionError('Separated stems have an invalid shared timeline')
    return vocal,backing


def select_excerpt(args):
    """Write one fixed vocal chunk, synchronized backing and canonical evidence."""
    import numpy as np
    import soundfile as sf
    scratch=Path(args.scratch)
    vocal,backing=_read_stems(scratch)
    ranges,rms,flatness,threshold=_select_ranges(vocal)
    vocal_parts=[]
    backing_parts=[]
    records=[]
    output_start=0
    for start,end in ranges:
        vocal_part=_fade(vocal[start:end])
        backing_part=_fade(backing[start:end])
        vocal_parts.append(vocal_part)
        backing_parts.append(backing_part)
        output_end=output_start+(end-start)
        records.append({'start_frame':int(start),'end_frame':int(end),
                        'output_start_frame':int(output_start),
                        'output_end_frame':int(output_end)})
        output_start=output_end
    selected=np.concatenate(vocal_parts)
    selected_backing=np.concatenate(backing_parts,axis=0)
    mask=np.abs(selected)>max(float(np.max(np.abs(selected)))*.06,1e-5)
    if not mask.any():
        raise ExcerptSelectionError('Selected vocal is silent after endpoint fades')
    source_rms=float(np.sqrt(np.mean(selected[mask].astype(np.float64)**2)))
    gain=min(2.,max(.5,TARGET_VOCAL_RMS/source_rms))
    peak=float(np.max(np.abs(selected)))
    if peak*gain>.95:
        gain=.95/peak
    selected=(selected*gain).astype(np.float32)
    if len(selected)!=output_start or len(selected_backing)!=output_start:
        raise ExcerptSelectionError('Synchronized excerpt assembly failed')
    sf.write(scratch/'vocal_000.wav',selected,SAMPLE_RATE,subtype='FLOAT')
    sf.write(scratch/'excerpt_backing.wav',selected_backing,SAMPLE_RATE,subtype='FLOAT')
    evidence={'schema':SCHEMA,'sample_rate':SAMPLE_RATE,
        'source_frames':len(vocal),'output_frames':len(selected),
        'source_ranges':records,
        'analysis':{'hop_frames':HOP_FRAMES,'rms_threshold':threshold,
                    'flatness_max':.50,'breath_gap_frames':MAX_BREATH_GAP_FRAMES,
                    'minimum_fragment_frames':MIN_FRAGMENT_FRAMES,
                    'analyzed_hops':len(rms)},
        'fades':{'in_frames':FADE_IN_FRAMES,'out_frames':FADE_OUT_FRAMES},
        'normalization':{'target_rms':TARGET_VOCAL_RMS,'applied_gain':gain}}
    temporary=scratch/('selection.json.'+uuid.uuid4().hex+'.part')
    try:
        with temporary.open('x',encoding='utf-8') as stream:
            os.chmod(temporary,0o600)
            stream.write(canonical_selection_json(evidence))
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,scratch/'selection.json')
    finally:
        temporary.unlink(missing_ok=True)
    return evidence


def _finite_number(value):
    if type(value) not in (int,float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_selection(document):
    """Validate persisted selection evidence using only the standard library."""
    required={'schema','sample_rate','source_frames','output_frames','source_ranges',
              'analysis','fades','normalization'}
    if not isinstance(document,dict) or set(document)!=required:
        raise ExcerptSelectionError('Invalid excerpt selection evidence schema')
    source=document['source_frames']
    output=document['output_frames']
    if (document['schema']!=SCHEMA or type(document['sample_rate']) is not int or
            document['sample_rate']!=SAMPLE_RATE or
            type(source) is not int or not 30*SAMPLE_RATE<=source<=300*SAMPLE_RATE or
            type(output) is not int or
            not MIN_EXCERPT_FRAMES<=output<=MAX_EXCERPT_FRAMES):
        raise ExcerptSelectionError('Invalid excerpt frame bounds')
    ranges=document['source_ranges']
    if not isinstance(ranges,list) or not 1<=len(ranges)<=2:
        raise ExcerptSelectionError('Excerpt needs one or two source ranges')
    expected_output=0
    previous_source_end=0
    for number,record in enumerate(ranges):
        if (not isinstance(record,dict) or set(record)!=
                {'start_frame','end_frame','output_start_frame','output_end_frame'} or
                any(type(record[name]) is not int for name in record)):
            raise ExcerptSelectionError('Invalid excerpt source range')
        start,end=record['start_frame'],record['end_frame']
        output_start,output_end=record['output_start_frame'],record['output_end_frame']
        if (not 0<=start<end<=source or end-start<MIN_FRAGMENT_FRAMES or
                (number and start<previous_source_end) or
                output_start!=expected_output or output_end-output_start!=end-start):
            raise ExcerptSelectionError('Excerpt ranges do not preserve the source timeline')
        previous_source_end=end
        expected_output=output_end
    if expected_output!=output:
        raise ExcerptSelectionError('Excerpt output frame total mismatch')
    analysis=document['analysis']
    if (not isinstance(analysis,dict) or set(analysis)!=
            {'hop_frames','rms_threshold','flatness_max','breath_gap_frames',
             'minimum_fragment_frames','analyzed_hops'} or
            any(type(analysis[key]) is not int for key in
                ('hop_frames','breath_gap_frames','minimum_fragment_frames','analyzed_hops')) or
            analysis['hop_frames']!=HOP_FRAMES or
            analysis['flatness_max']!=.50 or
            analysis['breath_gap_frames']!=MAX_BREATH_GAP_FRAMES or
            analysis['minimum_fragment_frames']!=MIN_FRAGMENT_FRAMES or
            type(analysis['analyzed_hops']) is not int or
            analysis['analyzed_hops']!=math.ceil(source/HOP_FRAMES) or
            not _finite_number(analysis['rms_threshold']) or
            analysis['rms_threshold']<=0):
        raise ExcerptSelectionError('Invalid excerpt analysis evidence')
    fades=document['fades']
    if (not isinstance(fades,dict) or set(fades)!= {'in_frames','out_frames'} or
            any(type(value) is not int for value in fades.values()) or
            fades['in_frames']!=FADE_IN_FRAMES or fades['out_frames']!=FADE_OUT_FRAMES):
        raise ExcerptSelectionError('Invalid excerpt fade evidence')
    normalization=document['normalization']
    gain=normalization.get('applied_gain') if isinstance(normalization,dict) else None
    if (not isinstance(normalization,dict) or set(normalization)!=
            {'target_rms','applied_gain'} or normalization['target_rms']!=TARGET_VOCAL_RMS or
            not _finite_number(gain) or not 0<gain<=2):
        raise ExcerptSelectionError('Invalid excerpt normalization evidence')
    return document


def canonical_selection_json(document):
    """Serialize validated evidence identically for stage receipts and consumers."""
    validate_selection(document)
    return json.dumps(document,sort_keys=True,separators=(',',':'),
                      ensure_ascii=False,allow_nan=False)


def _load_selection(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_size>65536:
        raise ExcerptSelectionError('Missing or unsafe excerpt selection evidence')
    try:
        text=path.read_text(encoding='utf-8')
        document=json.loads(text)
    except (OSError,ValueError) as exc:
        raise ExcerptSelectionError('Unreadable excerpt selection evidence') from exc
    if text!=canonical_selection_json(document):
        raise ExcerptSelectionError('Excerpt selection evidence is not canonical JSON')
    return document


def _align(converted,length):
    import numpy as np
    if not converted.size or not np.isfinite(converted).all():
        raise ExcerptSelectionError('Converted excerpt is empty or non-finite')
    if abs(len(converted)-length)>length*.005:
        raise ExcerptSelectionError('Converted excerpt timeline drift exceeds 0.5%')
    if len(converted)==length:
        return converted
    positions=np.linspace(0,len(converted)-1,length,dtype=np.float64)
    return np.interp(positions,np.arange(len(converted),dtype=np.float64),converted).astype(np.float32)


def mix_excerpt(args):
    """Mix from selection.json's actual frame contract, never full-song frames."""
    import numpy as np
    import soundfile as sf
    scratch=Path(args.scratch)
    selection=_load_selection(scratch/'selection.json')
    length=selection['output_frames']
    original,rate=sf.read(scratch/'vocal_000.wav',dtype='float32',always_2d=False)
    converted,converted_rate=sf.read(scratch/'vocal_000_converted.wav',dtype='float32',always_2d=False)
    backing,backing_rate=sf.read(scratch/'excerpt_backing.wav',dtype='float32',always_2d=True)
    if (rate!=SAMPLE_RATE or converted_rate!=rate or backing_rate!=rate or
            original.ndim!=1 or converted.ndim!=1 or len(original)!=length or
            len(backing)!=length or backing.shape[1]!=2 or not np.isfinite(original).all() or
            not np.isfinite(backing).all()):
        raise ExcerptSelectionError('Excerpt mix inputs disagree with selection evidence')
    converted=_align(converted,length)
    mask=np.abs(original)>max(float(np.max(np.abs(original)))*.06,1e-5)
    if not mask.any():
        raise ExcerptSelectionError('Excerpt mix has no vocal reference')
    source_rms=float(np.sqrt(np.mean(original[mask].astype(np.float64)**2)))
    target_rms=float(np.sqrt(np.mean(converted[mask].astype(np.float64)**2)))
    if target_rms<1e-6:
        raise ExcerptSelectionError('Converted excerpt is silent')
    converted=converted*min(2.,max(.5,source_rms/target_rms))
    result=backing+converted[:,None] if args.instrumental else converted
    # RVC may regenerate nonzero endpoints despite pre-conversion fades. Reapply
    # the envelope on the aligned output timeline, including an internal join.
    for record in selection['source_ranges']:
        start,end=record['output_start_frame'],record['output_end_frame']
        result[start:end]=_fade(result[start:end])
    peak=float(np.max(np.abs(result)))
    result=result*min(1.,.95/max(peak,1e-9))
    sf.write(scratch/'mixed.wav',result,SAMPLE_RATE,subtype='PCM_24')
