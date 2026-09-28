#!/usr/bin/env python3
"""One bounded systemd service: locked orchestration, separate Demucs and RVC children."""
from __future__ import annotations

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

LOCK = Path('/home/qr0w/audio-lab/inference.lock')
RVC = Path('/home/qr0w/audio-lab/tools/rvc/rvc_infer.py')


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


def download(args: argparse.Namespace) -> None:
    # This subprocess runs in the same bounded service, behind its parent's flock.
    resource.setrlimit(resource.RLIMIT_FSIZE, (args.max_bytes, args.max_bytes))
    from musicdl.musicdl import MusicClient
    from musicdl.modules.utils import SongInfo

    def native_only(self, search_result, request_overrides=None):
        return SongInfo(source=self.source, raw_data={'quality': 'standard'})

    source = 'NeteaseMusicClient'
    cfg = {'work_dir': str(args.scratch / 'musicdl'), 'search_size_per_source': 8,
           'search_size_per_page': 8, 'max_retries': 1, 'auto_set_proxies': False,
           'maintain_session': True, 'disable_print': True}
    client = MusicClient(music_sources=[source], init_music_clients_cfg={source: cfg},
                         clients_threadings={source: 1}, requests_overrides={source: {'timeout': (8, 15)}})
    native = client.music_clients[source]
    native._parsewiththirdpartapis = MethodType(native_only, native)
    results = client.search(args.title + ' ' + args.artist).get(source, [])
    matches = [song for song in results if norm(song.song_name) == norm(args.title)
               and norm(args.artist) == norm(song.singers)
               and 30 <= float(song.duration_s or 0) <= args.max_seconds
               and not any(tag in norm(song.song_name + ' ' + str(song.album))
                           for tag in ('试听', 'preview', 'live', '现场', '伴奏', 'karaoke'))
               and (not song.file_size_bytes or int(song.file_size_bytes) <= args.max_bytes)]
    identities = {(norm(song.identifier), norm(song.singers), norm(song.album)) for song in matches}
    if len(identities) != 1:
        raise RuntimeError('没有唯一匹配的完整歌曲；请指定准确标题与艺人，或由管理员配置本地 allowlist')
    chosen = matches[0]
    chosen.chunk_size = 64 * 1024
    downloaded = native.download([chosen], num_threadings=1,
                                 request_overrides={'timeout': (8, 20)}, auto_supplement_song=False)
    if len(downloaded) != 1:
        raise RuntimeError('官方源没有可下载的完整歌曲；不使用第三方解锁')
    path = Path(downloaded[0].save_path).resolve(strict=True)
    if not path.is_relative_to((args.scratch / 'musicdl').resolve()) or path.stat().st_size > args.max_bytes:
        raise RuntimeError('下载结果越界或过大')
    (args.scratch / 'source.json').write_text(json.dumps({'source': source,
        'identifier': str(chosen.identifier), 'title': str(chosen.song_name),
        'artist': str(chosen.singers), 'album': str(chosen.album),
        'expected_duration_s': float(chosen.duration_s),
        'file': str(path)}, ensure_ascii=False))


def separate(args: argparse.Namespace) -> None:
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
    model = get_model('htdemucs').cpu().eval()
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
    for i, start in enumerate(range(0, len(mono), 20 * rate)):
        sf.write(args.scratch / f'vocal_{i:03d}.wav', mono[start:start + 20 * rate], rate, subtype='FLOAT')
    sf.write(args.scratch / 'backing.wav', backing, rate, subtype='FLOAT')


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
        chunks.append(np.pad(chunk[:len(reference)], (0, max(0, len(reference)-len(chunk)))))
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
    with LOCK.open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        verify_limits()
        script = str(Path(__file__).resolve())
        python = '/home/qr0w/svc-bench/.venv39/bin/python'
        opts = ['--scratch', str(args.scratch), '--title', args.title, '--artist', args.artist,
                '--model', str(args.model), '--index', str(args.index),
                '--max-seconds', str(args.max_seconds), '--max-bytes', str(args.max_bytes)]
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
            run('/home/qr0w/musicdl-run/.venv/bin/python', script, 'download', *opts, timeout=100)
        info = json.loads((args.scratch / 'source.json').read_text())
        src = Path(info['file'])
        probe = json.loads(run('ffprobe', '-v', 'error', '-show_format', '-of', 'json', str(src),
                               timeout=20, capture=True).stdout)
        duration = float(probe['format']['duration'])
        if not math.isfinite(duration) or not 30 <= duration <= args.max_seconds:
            raise RuntimeError('拒绝试听或超长歌曲')
        expected = info.get('expected_duration_s', duration)
        if abs(duration - expected) > max(3., expected * .02):
            raise RuntimeError('下载音频与官方歌曲时长不符，拒绝试听截断或错曲')
        info['sha256'] = digest_file(src)
        if src.stat().st_size > args.max_bytes:
            raise RuntimeError('下载大小超限')
        run('ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-threads', '1', '-i', str(src),
            '-map', '0:a:0', '-ar', '44100', '-ac', '2', '-c:a', 'pcm_f32le',
            str(args.scratch / 'original.wav'), timeout=120)
        run(python, script, 'separate', *opts, timeout=360)
        for chunk in sorted(args.scratch.glob('vocal_???.wav')):
            run(python, str(RVC), '--model', str(args.model), '--index', str(args.index),
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
    parser.add_argument('--source-file', type=Path)
    parser.add_argument('--max-seconds', type=int, required=True)
    parser.add_argument('--max-bytes', type=int, required=True)
    parser.add_argument('--instrumental', action='store_true')
    args = parser.parse_args()
    verify_limits()
    {'run': orchestrate, 'download': download, 'separate': separate, 'mix': mix}[args.stage](args)


if __name__ == '__main__':
    main()
