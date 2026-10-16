#!/usr/bin/env python3
"""Owned CPU RVC v1/v2 adapter, executed only by the bounded StageExecutor.

Third-party RVC sources belong in sibling upstream/ and retain their own license.
The reference revision below is provenance, not verification of that checkout;
AssetInventory binds the actual source tree to each frozen rendering recipe.
"""
import argparse
import collections
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import pickle
import random
import resource
import sys
import time
import types

ROOT = Path(__file__).resolve().parent
HUBERT_SHA256 = 'f54b40fd2802423a5643779c4861af1e9ee9c1564dc9d32f54f20b5ffba7db96'
UPSTREAM_COMMIT = '9f2f0559e6932c10c48642d404e7d2e771d9db43'

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1048576), b''):
            h.update(block)
    return h.hexdigest()

def cgroup_metrics():
    result = {}
    try:
        rel = Path('/proc/self/cgroup').read_text().strip().split('0::', 1)[1]
        base = Path('/sys/fs/cgroup') / rel.lstrip('/')
        for name in ('memory.peak', 'memory.current', 'memory.events', 'memory.swap.peak', 'memory.max', 'memory.high', 'memory.swap.max', 'cpu.max', 'pids.max', 'cpu.stat'):
            p = base / name
            if p.exists():
                result[name] = p.read_text().strip()
    except Exception as e:
        result['error'] = str(e)
    return result

def load_hubert(path, torch):
    """Only a byte-identical, SHA-pinned official HuBERT can use Dictionary fallback.

    Unknown RVC weights never reach this loader. The fallback rejects every global
    except the five audited names actually present in this official checkpoint.
    No generic pickle loader or network/user-module loading is used.
    """
    digest = sha256(path)
    if digest != HUBERT_SHA256:
        raise ValueError('HuBERT hash differs from pinned lj1995/hubert_base.pt')
    try:
        state = torch.load(path, map_location='cpu', weights_only=True)
        mode = 'weights_only=True'
    except pickle.UnpicklingError:
        from fairseq.data.dictionary import Dictionary
        allowed = {
            ('collections', 'OrderedDict'): collections.OrderedDict,
            ('torch', 'FloatStorage'): torch.FloatStorage,
            ('torch', 'HalfStorage'): torch.HalfStorage,
            ('torch._utils', '_rebuild_tensor_v2'): torch._utils._rebuild_tensor_v2,
            ('fairseq.data.dictionary', 'Dictionary'): Dictionary,
        }
        class RestrictedUnpickler(pickle.Unpickler):
            def find_class(self, module, name):
                if (module, name) not in allowed:
                    raise pickle.UnpicklingError('Forbidden checkpoint global: ' + module + '.' + name)
                return allowed[(module, name)]
        restricted = types.ModuleType('sha_pinned_hubert_restricted_pickle')
        restricted.Unpickler = RestrictedUnpickler
        state = torch.load(path, map_location='cpu', pickle_module=restricted, weights_only=False)
        mode = 'SHA256-pinned restricted 5-global loader (Dictionary metadata)'
    from fairseq import checkpoint_utils
    from omegaconf import OmegaConf
    if state.get('cfg') is not None:
        state['cfg'] = OmegaConf.create(state['cfg'])
        # This official model is built-in; do not allow custom user code in cfg.
        if state['cfg'].get('common') is not None:
            state['cfg'].common.user_dir = None
    models, cfg, task = checkpoint_utils.load_model_ensemble_and_task([str(path)], state=state)
    model = models[0].float().eval().cpu()
    return model, {'sha256': digest, 'load_mode': mode}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--index', default='')
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--hubert', required=True)
    parser.add_argument('--pitch', type=int, default=0)
    parser.add_argument('--f0-method', choices=['harvest', 'pm'], default='harvest')
    parser.add_argument('--index-rate', type=float, default=0.5)
    parser.add_argument('--filter-radius', type=int, choices=[0, 3], default=3)
    parser.add_argument('--rms-mix-rate', type=float, default=0.25)
    parser.add_argument('--protect', type=float, default=0.33)
    parser.add_argument('--speaker-id', type=int, default=0)
    parser.add_argument('--seed', type=int, default=20260928)
    parser.add_argument('--limit-seconds', type=float, default=25.0)
    parser.add_argument('--resample-sr', type=int, default=0)
    args = parser.parse_args()
    if not os.environ.get('INVOCATION_ID'):
        raise SystemExit('Refusing uncontained model load; use the plugin bounded stage executor')
    if not 0 < args.limit_seconds <= 25:
        parser.error('--limit-seconds must be in (0,25]')
    if not 0 <= args.index_rate <= 1 or not 0 <= args.rms_mix_rate <= 1 or not 0 <= args.protect <= 0.5:
        parser.error('index/rms rates require [0,1], protect requires [0,0.5]')
    if args.index_rate and not args.index:
        parser.error('Nonzero --index-rate requires --index; pass --index-rate 0 to disable retrieval')
    # StageExecutor holds the shared inference lock for the complete stage.
    # Reuse its standard-library guard, before importing any model library.
    guard_path = ROOT.parent.parent / 'runtime' / 'worker.py'
    spec = importlib.util.spec_from_file_location('maibot_sing_rvc_budget_guard', guard_path)
    if spec is None or spec.loader is None:
        raise RuntimeError('Packaged media budget guard is unavailable')
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    guard.verify_limits()
    guard.verify_host_headroom()
    print('Verified applied cgroup limits:', json.dumps(cgroup_metrics()), flush=True)
    started = time.perf_counter()
    import numpy as np
    import soundfile as sf
    import torch
    import librosa
    import faiss
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    faiss.omp_set_num_threads(1)
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    sys.path.insert(0, str(ROOT / 'upstream'))
    from infer.lib.infer_pack.models import (
        SynthesizerTrnMs256NSFsid, SynthesizerTrnMs256NSFsid_nono,
        SynthesizerTrnMs768NSFsid, SynthesizerTrnMs768NSFsid_nono,
    )
    from infer.modules.vc.pipeline import Pipeline
    model_path = Path(args.model).resolve()
    print('Loading RVC with weights_only=True:', model_path, flush=True)
    checkpoint = torch.load(model_path, map_location='cpu', weights_only=True)
    if not isinstance(checkpoint, dict) or not {'weight', 'config'} <= checkpoint.keys():
        raise ValueError('Not an exported RVC checkpoint: requires weight/config (SVC incompatible)')
    version, f0 = checkpoint.get('version', 'v1'), checkpoint.get('f0', 1)
    model_config = list(checkpoint['config'])
    model_config[-3] = checkpoint['weight']['emb_g.weight'].shape[0]
    tgt_sr = model_config[-1]
    if not 0 <= args.speaker_id < model_config[-3]:
        raise ValueError('speaker-id outside embedding range')
    synthesizers = {('v1', 1): SynthesizerTrnMs256NSFsid, ('v1', 0): SynthesizerTrnMs256NSFsid_nono,
                    ('v2', 1): SynthesizerTrnMs768NSFsid, ('v2', 0): SynthesizerTrnMs768NSFsid_nono}
    if (version, f0) not in synthesizers:
        raise ValueError('Unsupported RVC architecture')
    net = synthesizers[version, f0](*model_config, is_half=False)
    del net.enc_q
    loaded = net.load_state_dict(checkpoint['weight'], strict=False)
    if loaded.missing_keys or any(not k.startswith('enc_q.') for k in loaded.unexpected_keys):
        raise ValueError('Unexpected weight mismatch: ' + str(loaded))
    net = net.float().eval().cpu()
    metadata = {k: v for k, v in checkpoint.items() if k != 'weight'}
    del checkpoint
    index_meta = None
    if args.index_rate:
        idx = faiss.read_index(args.index)
        if idx.d != (256 if version == 'v1' else 768) or idx.ntotal < 8:
            raise ValueError('FAISS index dimension/count incompatible with RVC checkpoint')
        # Preserve the nprobe serialized in the immutable index, as Pipeline does.
        index_meta = {'sha256': sha256(args.index), 'dimensions': idx.d, 'vectors': idx.ntotal,
                      'nprobe': getattr(idx, 'nprobe', None), 'neighbors': 8}
        del idx
    print('Loading SHA-pinned RVC HuBERT', flush=True)
    hubert, hubert_meta = load_hubert(Path(args.hubert), torch)
    config = types.SimpleNamespace(x_pad=1, x_query=2, x_center=5, x_max=8, is_half=False, device='cpu')
    pipeline = Pipeline(tgt_sr, config)
    audio, original_sr = sf.read(args.input, dtype='float32', always_2d=True)
    input_meta = {'sha256': sha256(args.input), 'sample_rate': original_sr,
                  'channels': audio.shape[1], 'frames': audio.shape[0],
                  'full_seconds': audio.shape[0] / original_sr}
    audio = audio[:round(args.limit_seconds * original_sr)].mean(axis=1)
    used_seconds = len(audio) / original_sr
    if len(audio) < original_sr // 2 or not np.all(np.isfinite(audio)):
        raise ValueError('Input must contain >=0.5 seconds of finite audio')
    audio = librosa.resample(audio, orig_sr=original_sr, target_sr=16000, res_type='kaiser_best')
    scale = max(1.0, float(np.abs(audio).max()) / 0.95)
    audio /= scale
    input_meta.update(used_seconds=used_seconds, normalization_divisor=scale, resampler='librosa kaiser_best -> 16000Hz')
    prep_seconds = time.perf_counter() - started
    times = [0.0, 0.0, 0.0]
    print('Converting', used_seconds, 'seconds with', vars(args), flush=True)
    compute_started = time.perf_counter()
    result = pipeline.pipeline(hubert, net, args.speaker_id, audio, str(Path(args.input).resolve()), times,
                               args.pitch, args.f0_method, args.index, args.index_rate, f0,
                               args.filter_radius, tgt_sr, args.resample_sr, args.rms_mix_rate,
                               version, args.protect)
    compute_seconds = time.perf_counter() - compute_started
    if not result.size or not np.isfinite(result).all() or not np.any(result):
        raise ValueError('Empty/nonfinite/silent conversion result')
    output_sr = args.resample_sr if args.resample_sr >= 16000 else tgt_sr
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output, result, output_sr, subtype='PCM_16')
    y = result.astype(np.float64) / 32768.0
    report = {
        'status': 'converted; human audition still required', 'args': vars(args),
        'backend': {'upstream_reference_commit': UPSTREAM_COMMIT, 'device': 'cpu', 'dtype': 'float32',
                    'threads': 1, 'deterministic_algorithms': True,
                    'chunk_config_seconds': vars(config), 'auto_pitch': False,
                    'highpass_hz': 48, 'highpass_order': 5, 'f0_floor_hz': 50, 'f0_ceiling_hz': 1100,
                    'f0_frame_ms': 10, 'harvest_refinement': 'stonemask', 'harvest_median_kernel': 3 if args.filter_radius > 2 else 0},
        'versions': {n: importlib.metadata.version(n) for n in ['torch', 'torchaudio', 'fairseq', 'numpy', 'scipy', 'librosa', 'pyworld', 'faiss-cpu', 'soundfile', 'numba', 'praat-parselmouth', 'torchcrepe']},
        'python': sys.version, 'model': {'sha256': sha256(model_path), 'metadata': metadata,
        'effective_speakers': model_config[-3], 'missing_keys': loaded.missing_keys, 'unexpected_keys': loaded.unexpected_keys},
        'hubert': hubert_meta, 'index': index_meta, 'input': input_meta,
        'output': {'path': str(output), 'sha256': sha256(output), 'sample_rate': output_sr,
                   'frames': len(result), 'seconds': len(result) / output_sr, 'channels': 1, 'subtype': 'PCM_16',
                   'peak': float(np.abs(y).max()), 'rms': float(np.sqrt(np.mean(y*y))),
                   'clipped_samples': int(np.sum(np.abs(y) >= 0.999))},
        'timing_seconds': {'prepare_and_load': prep_seconds, 'pipeline_wall': compute_seconds,
                           'features_and_retrieval': times[0], 'f0': times[1], 'synthesis': times[2],
                           'total_python': time.perf_counter() - started},
        'max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'cgroup_before_exit': cgroup_metrics(),
    }
    report_path = output.with_suffix('.json')
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    print('WAV:', output, '\nREPORT:', report_path, flush=True)

if __name__ == '__main__':
    main()
