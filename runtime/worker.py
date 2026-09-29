#!/usr/bin/env python3
"""One bounded systemd service: locked orchestration, separate Demucs and RVC children."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import MethodType
import argparse
import fcntl
import hashlib
import json
import math
import os
import resource
import subprocess
import sys
import time
import unicodedata

# Support direct CLI execution and isolated import-based tests without importing
# the plugin entry point or any model library here.
if __package__:
    from .source_selection import SourceCandidate, SourceSelectionError, select_source
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from source_selection import SourceCandidate, SourceSelectionError, select_source
    sys.path.pop(0)


def verify_limits() -> None:
    if not os.environ.get('INVOCATION_ID'):
        raise RuntimeError('必须在 systemd user service 内运行')
    rel = Path('/proc/self/cgroup').read_text().strip().split('0::', 1)[1]
    base = Path('/sys/fs/cgroup') / rel.lstrip('/')
    expected = {'memory.max': '4294967296', 'memory.high': '3221225472',
                'memory.swap.max': '0', 'pids.max': '64'}
    for name, value in expected.items():
        if (base / name).read_text().strip() != value:
            raise RuntimeError('未应用隔离配置: ' + name)
    quota, period = (base / 'cpu.max').read_text().split()
    if quota == 'max' or int(quota) / int(period) > 1.5:
        raise RuntimeError('CPU 未受限')


def run(*args: str, timeout: int = 300, capture: bool = False) -> subprocess.CompletedProcess:
    started = time.monotonic()
    print(json.dumps({'event': 'stage_start', 'command': list(args), 'time': time.time()}), flush=True)
    result = subprocess.run(list(args), check=True, timeout=timeout, capture_output=capture, text=capture)
    print(json.dumps({'event': 'stage_end', 'seconds': round(time.monotonic() - started, 2)}), flush=True)
    return result


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def norm(value: object) -> str:
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())


def select_release(matches: list, *, title: str, artist: str, max_seconds: int,
                   max_bytes: int, album: str | None = None,
                   source_id: str | None = None) -> tuple[object, dict]:
    """Require a unique stable ID or explicit release choice; runtime is not identity."""
    candidates = [SourceCandidate('NeteaseMusicClient', str(song.identifier),
        str(song.song_name), str(song.singers), str(song.album), float(song.duration_s or 0),
        int(song.file_size_bytes) if song.file_size_bytes else None) for song in matches]
    chosen = select_source(candidates, title=title, artist=artist, max_seconds=max_seconds,
        max_bytes=max_bytes, album=album, source_id=source_id)
    original = next(song for song in matches if str(song.identifier) == chosen.identifier)
    return original, {'policy': 'explicit-source-v1',
        'method': 'source_id' if source_id else 'album' if album else 'unique_id',
        'chosen_id': chosen.identifier, 'chosen_album': chosen.album,
        'notice': 'A selected catalogue ID is not proof of studio provenance or rights'}


def write_failure(args, exc: Exception) -> None:
    """Persist a bounded error envelope for the parent, separate from worker logs."""
    path = args.scratch / 'error.json'
    if path.exists():
        return  # Keep the original child-stage failure, not CalledProcessError.
    messages = {
        'ambiguous': '找到多个同名发行，尚未开始翻唱与发送；请明确选择专辑或来源曲目 ID。',
        'unavailable': '当前来源未返回符合条件的音频候选；尚未生成或发送翻唱。',
        'invalid_selection': '所选专辑或曲目 ID 与当前候选不匹配，请重新确认。',
    }
    if isinstance(exc, SourceSelectionError):
        record = {'version': 1, 'stage': 'source_selection', 'code': 'source_'+exc.code,
                  'message': messages.get(exc.code, '歌曲选择失败；尚未生成或发送翻唱。'),
                  'candidates': [asdict(c) for c in exc.candidates[:10]]}
    else:
        record = {'version': 1, 'stage': args.stage, 'code': 'worker_failed',
                  'message': '本地处理未完成，尚未进入语音发送；诊断日志已保留。'}
    path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')


def download(args: argparse.Namespace) -> None:
    # This subprocess runs in the same bounded service, behind its parent's flock.
    resource.setrlimit(resource.RLIMIT_FSIZE, (args.max_bytes, args.max_bytes))
    from musicdl.musicdl import MusicClient
    from musicdl.modules.utils import SongInfo

    def native_only(self, search_result, request_overrides=None):
        return SongInfo(source=self.source, raw_data={'quality': 'standard'})

    source = 'NeteaseMusicClient'
    cfg = {'work_dir': str(args.scratch / 'musicdl'), 'search_size_per_source': 25,
           'search_size_per_page': 25, 'max_retries': 1, 'auto_set_proxies': False,
           'maintain_session': True, 'disable_print': True}
    client = MusicClient(music_sources=[source], init_music_clients_cfg={source: cfg},
                         clients_threadings={source: 1}, requests_overrides={source: {'timeout': (8, 15)}})
    native = client.music_clients[source]
    native._parsewiththirdpartapis = MethodType(native_only, native)
    results = client.search(args.title + ' ' + args.artist).get(source, [])
    chosen, selection = select_release(results, title=args.title, artist=args.artist,
        max_seconds=args.max_seconds, max_bytes=args.max_bytes,
        album=args.album, source_id=args.source_id)
    chosen.chunk_size = 64 * 1024
    downloaded = native.download([chosen], num_threadings=1,
                                 request_overrides={'timeout': (8, 20)}, auto_supplement_song=False)
    if len(downloaded) != 1:
        raise RuntimeError('所选来源下载未完成；请检查来源可用性，不自动替换其他发行版本')
    path = Path(downloaded[0].save_path).resolve(strict=True)
    if not path.is_relative_to((args.scratch / 'musicdl').resolve()) or path.stat().st_size > args.max_bytes:
        raise RuntimeError('下载结果越界或过大')
    (args.scratch / 'source.json').write_text(json.dumps({'source': source,
        'identifier': str(chosen.identifier), 'title': str(chosen.song_name),
        'artist': str(chosen.singers), 'album': str(chosen.album),
        'expected_duration_s': float(chosen.duration_s), 'selection': selection,
        'file': str(path)}, ensure_ascii=False))


def chunk_bounds(frames: int, rate: int) -> list[tuple[int, int]]:
    """Bound each RVC call to <=25 s and never leave a sub-5 s final chunk."""
    if frames < 30 * rate or rate <= 0:
        raise ValueError('无效或过短的原始音频')
    starts = list(range(0, frames, 20 * rate))
    if len(starts) > 1 and frames - starts[-1] < 5 * rate:
        starts.pop()
    return [(start, starts[i+1] if i+1 < len(starts) else frames)
            for i, start in enumerate(starts)]


def separate(args: argparse.Namespace) -> None:
    # The dedicated durable CLI requires a local repo; the old worker CLI
    # retains its legacy behavior only for explicitly invoked old workflows.
    repo = getattr(args, 'demucs_repo', None)
    if repo is not None:
        repo = Path(repo)
        if not repo.is_absolute() or not repo.is_dir() or repo.is_symlink():
            raise ValueError('显式 Demucs repo 缺失或无效，拒绝默认缓存/联网')
    import numpy as np
    import soundfile as sf
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    data, rate = sf.read(args.scratch / 'original.wav', dtype='float32', always_2d=True)
    if rate != 44100 or data.shape[1] != 2 or not np.isfinite(data).all():
        raise ValueError('无效的输入音频')
    wave = torch.from_numpy(data.T.copy())
    model = get_model('htdemucs', repo=repo).cpu().eval()
    ref = wave.mean(0)
    scale, center = ref.std(), ref.mean()
    if float(scale) < 1e-6:
        raise ValueError('音频无声音')
    with torch.inference_mode():
        stems = apply_model(model, ((wave - center) / scale)[None], device='cpu',
                            shifts=0, split=True, segment=5, overlap=0.25,
                            num_workers=0, progress=False)[0] * scale + center
    voice = stems[model.sources.index('vocals')].T.numpy()
    backing = sum(stems[i] for i, name in enumerate(model.sources) if name != 'vocals').T.numpy()
    mono = voice.mean(axis=1)
    sf.write(args.scratch / 'vocals.wav', mono, rate, subtype='FLOAT')
    for i, (start, end) in enumerate(chunk_bounds(len(mono), rate)):
        sf.write(args.scratch / f'vocal_{i:03d}.wav', mono[start:end], rate, subtype='FLOAT')
    sf.write(args.scratch / 'backing.wav', backing, rate, subtype='FLOAT')


def align_converted_chunk(chunk, reference_length: int):
    """Match the input timeline without injecting a silent gap at every 20s seam.

    RVC currently returns 882 fewer samples per call (20 ms at 44.1 kHz).
    Linear interpolation distributes that sub-0.2% timing correction over the
    chunk instead of adding 20 ms of silence next to the following vocal.
    This causes at most a few cents of pitch drift, not a key transposition.
    """
    import numpy as np
    if reference_length <= 0 or not np.isfinite(chunk).all() or not chunk.size:
        raise ValueError('无效的 RVC 分块输出')
    if len(chunk) == reference_length:
        return chunk
    if abs(len(chunk) - reference_length) > reference_length * .005:
        raise RuntimeError('RVC 分块时轴漂移超过 0.5%，拒绝擅自拉伸')
    xp = np.arange(len(chunk), dtype=np.float64)
    samples = np.linspace(0, len(chunk) - 1, reference_length, dtype=np.float64)
    return np.interp(samples, xp, chunk).astype(chunk.dtype, copy=False)


def mix(args: argparse.Namespace) -> None:
    import numpy as np
    import soundfile as sf
    before, sr = sf.read(args.scratch / 'vocals.wav', dtype='float32')
    chunks = []
    for original in sorted(args.scratch.glob('vocal_???.wav')):
        converted = original.with_name(original.stem + '_converted.wav')
        reference, rr = sf.read(original, dtype='float32')
        chunk, ar = sf.read(converted, dtype='float32')
        if ar != sr or rr != sr or abs(len(chunk) - len(reference)) > sr * .1 or not np.isfinite(chunk).all():
            raise RuntimeError('RVC 片段采样率、时长或数值异常')
        chunks.append(align_converted_chunk(chunk, len(reference)))
    if not chunks:
        raise RuntimeError('没有 RVC 转换片段')
    after = np.concatenate(chunks)
    if len(after) != len(before):
        raise RuntimeError('RVC 片段拼接时长不一致')
    mask = np.abs(before) > max(float(np.max(np.abs(before))) * .06, 1e-5)
    if not mask.any():
        raise RuntimeError('没有有效人声')
    source_rms = float(np.sqrt(np.mean(before[mask] ** 2)))
    target_rms = float(np.sqrt(np.mean(after[mask] ** 2)))
    if target_rms < 1e-6:
        raise RuntimeError('转换结果无声')
    gain = min(2., max(.5, source_rms / target_rms))
    result = after * gain
    if args.instrumental:
        backing, br = sf.read(args.scratch / 'backing.wav', dtype='float32', always_2d=True)
        if br != sr or len(backing) != len(before):
            raise RuntimeError('伴奏时间轴不匹配')
        result = backing + result[:, None]
    result *= min(1., .95 / max(float(np.max(np.abs(result))), 1e-9))
    sf.write(args.scratch / 'mixed.wav', result, sr, subtype='PCM_24')


def orchestrate(args: argparse.Namespace) -> None:
    verify_limits()
    with args.inference_lock.open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        verify_limits()
        script = str(Path(__file__).resolve())
        python = str(args.worker_python)
        opts = ['--worker-python', str(args.worker_python), '--musicdl-python', str(args.musicdl_python),
                '--rvc-script', str(args.rvc_script), '--inference-lock', str(args.inference_lock),
                '--scratch', str(args.scratch), '--title', args.title, '--artist', args.artist,
                '--model', str(args.model), '--index', str(args.index),
                '--max-seconds', str(args.max_seconds), '--max-bytes', str(args.max_bytes)]
        if args.album:
            opts.extend(['--album', args.album])
        if args.source_id:
            opts.extend(['--source-id', args.source_id])
        if args.instrumental:
            opts.append('--instrumental')
        if args.source_file:
            src = args.source_file.resolve(strict=True)
            if not src.is_file() or src.stat().st_size > args.max_bytes:
                raise ValueError('本地输入不存在或过大')
            (args.scratch / 'source.json').write_text(json.dumps({'source': 'allowlist',
                'title': args.title, 'artist': args.artist, 'sha256': digest_file(src),
                'file': str(src)}, ensure_ascii=False))
        else:
            run(str(args.musicdl_python), script, 'download', *opts, timeout=100)
        info = json.loads((args.scratch / 'source.json').read_text())
        src = Path(info['file'])
        probe = json.loads(run('ffprobe', '-v', 'error', '-show_format', '-of', 'json', str(src),
                               timeout=20, capture=True).stdout)
        duration = float(probe['format']['duration'])
        if not math.isfinite(duration) or not 30 <= duration <= args.max_seconds:
            raise RuntimeError('拒绝试听或超长歌曲')
        expected = info.get('expected_duration_s', duration)
        if abs(duration - expected) > max(3., expected * .02):
            raise RuntimeError('下载音频与来源标注时长不符，拒绝使用；此检查不能判定录音身份')
        info['sha256'] = digest_file(src)
        if src.stat().st_size > args.max_bytes:
            raise RuntimeError('下载大小超限')
        run('ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-threads', '1', '-i', str(src),
            '-map', '0:a:0', '-ar', '44100', '-ac', '2', '-c:a', 'pcm_f32le',
            str(args.scratch / 'original.wav'), timeout=120)
        run(python, script, 'separate', *opts, timeout=360)
        for chunk in sorted(args.scratch.glob('vocal_???.wav')):
            run(python, str(args.rvc_script), '--model', str(args.model), '--index', str(args.index),
                '--input', str(chunk), '--output', str(chunk.with_name(chunk.stem + '_converted.wav')),
                '--limit-seconds', '25', '--pitch', '0', '--f0-method', 'harvest',
                '--index-rate', '0.5', '--filter-radius', '3', '--rms-mix-rate', '0.25',
                '--protect', '0.33', '--seed', '20260928', '--resample-sr', '44100', timeout=300)
        run(python, script, 'mix', *opts, timeout=60)
        run('ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-threads', '1', '-i',
            str(args.scratch / 'mixed.wav'), '-c:a', 'libmp3lame', '-b:a', '192k',
            str(args.scratch / 'cover.mp3'), timeout=90)
        run('ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-threads', '1', '-i',
            str(args.scratch / 'cover.mp3'), '-f', 'null', '-', timeout=60)
        info.pop('file', None)
        rel = Path('/proc/self/cgroup').read_text().strip().split('0::', 1)[1]
        cgroup = Path('/sys/fs/cgroup') / rel.lstrip('/')
        resources = {name: (cgroup / name).read_text().strip() for name in (
            'memory.peak', 'memory.max', 'memory.high', 'memory.swap.max', 'memory.events', 'cpu.max')}
        (args.scratch / 'result.json').write_text(json.dumps({'verified_source': info,
            'duration_s': duration, 'input_bytes': src.stat().st_size,
            'resources': resources}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', nargs='?', default='run', choices=['run', 'download', 'separate', 'mix'])
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--artist', required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--index', type=Path, required=True)
    parser.add_argument('--worker-python', type=Path, required=True)
    parser.add_argument('--musicdl-python', type=Path, required=True)
    parser.add_argument('--rvc-script', type=Path, required=True)
    parser.add_argument('--inference-lock', type=Path, required=True)
    parser.add_argument('--source-file', type=Path)
    parser.add_argument('--album')
    parser.add_argument('--source-id')
    parser.add_argument('--max-seconds', type=int, required=True)
    parser.add_argument('--max-bytes', type=int, required=True)
    parser.add_argument('--instrumental', action='store_true')
    args = parser.parse_args()
    verify_limits()
    try:
        {'run': orchestrate, 'download': download, 'separate': separate, 'mix': mix}[args.stage](args)
    except Exception as exc:
        write_failure(args, exc)
        raise


if __name__ == '__main__':
    main()
