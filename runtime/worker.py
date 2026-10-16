#!/usr/bin/env python3
"""One bounded systemd service: locked orchestration, separate Demucs and RVC children."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import MethodType
import argparse
import asyncio
from types import SimpleNamespace
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
    from .context_window import freeze_context, full_context
    from .source_selection import SourceCandidate, SourceSelectionError, select_source
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from context_window import freeze_context, full_context
    from source_selection import SourceCandidate, SourceSelectionError, select_source
    sys.path.pop(0)


def _budget_value(name: str) -> int:
    """Use the launcher's frozen budget; a missing value must not use defaults."""
    value = os.environ.get('MAIBOT_SING_BUDGET_' + name, '')
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise RuntimeError('媒体单元缺少有效预算字段：' + name)
    return int(value)


def _unit_cgroup() -> Path:
    rel = Path('/proc/self/cgroup').read_text().strip().split('0::', 1)[1]
    if not rel.startswith('/') or '..' in Path(rel).parts:
        raise RuntimeError('媒体单元 cgroup 路径无效')
    return Path('/sys/fs/cgroup') / rel.lstrip('/')


def verify_limits() -> None:
    """Verify the actual service against the single launch-time budget."""
    if not os.environ.get('INVOCATION_ID'):
        raise RuntimeError('必须在 systemd user service 内运行')
    base = _unit_cgroup()
    expected = {
        'memory.max': str(_budget_value('MEMORY_MAX')),
        'memory.high': str(_budget_value('MEMORY_HIGH')),
        'memory.swap.max': '0',
        'memory.oom.group': '1',
        'pids.max': str(_budget_value('TASKS_MAX')),
    }
    for name, value in expected.items():
        if (base / name).read_text().strip() != value:
            raise RuntimeError('未应用隔离配置: ' + name)
    quota, period = (base / 'cpu.max').read_text().split()
    if quota == 'max' or int(quota) * 100 > _budget_value('CPU_PERCENT') * int(period):
        raise RuntimeError('CPU 未受限')


def verify_host_headroom() -> None:
    """Recheck finite ancestors after acquiring the shared inference lock.

    This tiny Python-3.9-compatible check uses the frozen launcher budget. The
    unit leaf is excluded: only shared ancestors carry the competing workloads.
    """
    required = _budget_value('MEMORY_MAX') + _budget_value('HEADROOM')
    root, current = Path('/sys/fs/cgroup'), _unit_cgroup().parent
    measured = False
    while current == root or root in current.parents:
        try:
            maximum = (current / 'memory.max').read_text().strip()
            usage = (current / 'memory.current').read_text().strip()
            if not usage.isascii() or not usage.isdecimal():
                raise ValueError('invalid usage')
            if maximum != 'max':
                if not maximum.isascii() or not maximum.isdecimal():
                    raise ValueError('invalid capacity')
                measured = True
                if int(maximum) - int(usage) < required:
                    error = RuntimeError('共享祖先内存余量不足，未启动媒体子进程')
                    error.code = 'resource_budget_exhausted'
                    raise error
        except (OSError, ValueError) as exc:
            error = RuntimeError('无法读取共享祖先内存限制，未启动媒体子进程')
            error.code = 'cgroup_unavailable'
            raise error from exc
        if current == root:
            break
        current = current.parent
    if not measured:
        error = RuntimeError('没有有限共享祖先内存容量，未启动媒体子进程')
        error.code = 'cgroup_unavailable'
        raise error


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
                '--scratch', str(args.scratch), '--title=' + args.title, '--artist=' + args.artist,
                '--model', str(args.model), '--index', str(args.index),
                '--max-seconds', str(args.max_seconds), '--max-bytes', str(args.max_bytes)]
        if args.album:
            opts.append('--album=' + args.album)
        if args.source_id:
            opts.append('--source-id=' + args.source_id)
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


_SAMPLE_RATE = 44100
_CHANNELS = 2
_FRAME_BYTES = 8
_STDOUT_LIMIT = 65536
_STDERR_LIMIT = 8192
_FFPROBE_TIMEOUT_S = 20
_FFMPEG_TIMEOUT_S = 120
_PROCESS_CLEANUP_S = 3


class ProbeError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class _OutputLimit(RuntimeError):
    pass


class _DecodedTooLong(RuntimeError):
    pass


async def _read_limited(stream, limit):
    output = bytearray()
    while True:
        block = await stream.read(min(8192, limit-len(output)+1))
        if not block:
            return bytes(output)
        output.extend(block)
        if len(output) > limit:
            raise _OutputLimit


async def _count_pcm(stream, max_bytes):
    amount = 0
    while True:
        block = await stream.read(256*1024)
        if not block:
            return amount
        amount += len(block)
        if amount > max_bytes:
            raise _DecodedTooLong


async def _drain_pipe(stream):
    try:
        while await stream.read(256*1024):
            pass
    except (BrokenPipeError, ConnectionError, RuntimeError):
        pass


def _close_pipe_transport(stream):
    transport = getattr(stream, '_transport', None)
    if transport is not None:
        try:
            transport.close()
        except BaseException:
            pass


async def _cleanup_owned(process, readers, waited):
    """Bound kill/drain/reap, including cancellation during ownership cleanup."""
    async def settle_reader(task, stream):
        await asyncio.gather(task, return_exceptions=True)
        at_eof = getattr(stream, 'at_eof', None)
        if at_eof is None or not at_eof():
            await _drain_pipe(stream)

    async def finish():
        if process.returncode is None:
            try:
                process.kill()
            except OSError:
                pass
        output, errors = readers
        settling = [asyncio.create_task(settle_reader(output, process.stdout),
                                        name='media-probe-clean-stdout'),
                    asyncio.create_task(settle_reader(errors, process.stderr),
                                        name='media-probe-clean-stderr')]
        try:
            await asyncio.wait_for(
                asyncio.gather(*settling, waited, return_exceptions=True),
                _PROCESS_CLEANUP_S)
        except asyncio.TimeoutError:
            _close_pipe_transport(process.stdout)
            _close_pipe_transport(process.stderr)
            transport = getattr(process, '_transport', None)
            if transport is not None:
                try:
                    transport.close()
                except BaseException:
                    pass
            for task in (*settling, *readers, waited):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*settling, *readers, waited, return_exceptions=True)

    cleanup = asyncio.create_task(finish(), name='media-probe-cleanup')
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
    await asyncio.gather(cleanup, return_exceptions=True)


async def _execute(argv, output_reader, *, timeout_s, pipe_limit):
    # Private algorithm helper: production enters ONLY through probe_cli, inside
    # StageExecutor's admitted, locked service. No Host-facing adapter calls it.
    process = await asyncio.create_subprocess_exec(*argv,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=pipe_limit)
    output = asyncio.create_task(output_reader(process.stdout), name='media-probe-stdout')
    errors = asyncio.create_task(_read_limited(process.stderr, _STDERR_LIMIT),
                                 name='media-probe-stderr')
    waited = asyncio.create_task(process.wait(), name='media-probe-wait')
    owned = (output, errors, waited)

    async def collect():
        done, _ = await asyncio.wait(owned, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exception = task.exception()
            if exception is not None:
                raise exception
        values = await asyncio.gather(*owned)
        return values[0], values[1], values[2]

    try:
        # asyncio.timeout is unavailable in the frozen Python 3.9 worker.
        return await asyncio.wait_for(collect(), timeout_s)
    except BaseException:
        await _cleanup_owned(process, (output, errors), waited)
        raise


async def _metadata(path):
    try:
        raw, errors, returncode = await _execute([
            'ffprobe', '-v', 'error', '-show_entries',
            'format=duration:stream=codec_type', '-of', 'json', str(path)],
            lambda stream: _read_limited(stream, _STDOUT_LIMIT),
            timeout_s=_FFPROBE_TIMEOUT_S, pipe_limit=_STDOUT_LIMIT)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        raise ProbeError('source_probe_timeout', '已选曲目的媒体检测超时，没有改选其他版本。') from exc
    except (OSError, _OutputLimit) as exc:
        raise ProbeError('source_invalid_media', '已选曲目的媒体元数据无法安全读取。') from exc
    if returncode or not raw or errors:
        raise ProbeError('source_invalid_media', '已选曲目的下载内容不是可验证的完整音频。')
    try:
        record = json.loads(raw)
        streams = record['streams']
        if not isinstance(streams, list):
            raise ValueError('invalid streams')
        audio = [stream for stream in streams
                 if isinstance(stream, dict) and stream.get('codec_type') == 'audio']
    except (ValueError, KeyError, TypeError) as exc:
        raise ProbeError('source_invalid_media', '已选曲目的媒体格式无效。') from exc
    container_duration = None
    try:
        candidate = float(record.get('format', {}).get('duration'))
        if math.isfinite(candidate) and candidate >= 0:
            container_duration = candidate
    except (ValueError, TypeError, AttributeError):
        pass
    if not audio:
        raise ProbeError('source_duration_limit', '媒体无有效完整音轨，或超出本机时长范围。')
    return len(audio), container_duration


async def _decoded_frames(path, max_seconds):
    try:
        amount, errors, returncode = await _execute([
            'ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-threads', '1',
            '-max_alloc', str(64*1024*1024), '-i', str(path), '-map', '0:a:0',
            '-ar', str(_SAMPLE_RATE), '-ac', str(_CHANNELS), '-c:a', 'pcm_f32le',
            '-f', 'f32le', 'pipe:1'],
            lambda stream: _count_pcm(stream, max_seconds*_SAMPLE_RATE*_FRAME_BYTES),
            timeout_s=_FFMPEG_TIMEOUT_S, pipe_limit=256*1024)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        raise ProbeError('source_probe_timeout', '已选曲目的受控解码超时，没有改选其他版本。') from exc
    except _DecodedTooLong as exc:
        raise ProbeError('source_duration_limit', '媒体无有效完整音轨，或超出本机时长范围。') from exc
    except (OSError, _OutputLimit) as exc:
        raise ProbeError('source_invalid_media', '已选曲目的受控解码输出无效。') from exc
    if returncode or errors or amount <= 0 or amount % _FRAME_BYTES:
        raise ProbeError('source_invalid_media', '已选曲目的下载内容不能完整解码。')
    return amount // _FRAME_BYTES


async def probe_download(path, selected, *, max_seconds=300, render_mode=None):
    """Single streamed frame/duration algorithm; CLI supplies bounded execution.

    Direct calls are algorithm-test injection, never a production Host API.
    Container duration is diagnostic, not recording identity or a planning clock.
    """
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ProbeError('source_missing', '已选曲目的下载文件不存在。')
    if (type(max_seconds) is not int or render_mode not in (None, 'full', 'excerpt')
            or (render_mode in (None, 'full') and not 30 <= max_seconds <= 300)
            or (render_mode == 'excerpt' and max_seconds != 900)):
        raise ValueError('Invalid media duration limit')
    audio_streams, container_duration = await _metadata(path)
    frames = await _decoded_frames(path, max_seconds)
    if not 30*_SAMPLE_RATE <= frames <= max_seconds*_SAMPLE_RATE:
        raise ProbeError('source_duration_limit', '媒体无有效完整音轨，或超出本机时长范围。')
    duration = frames / _SAMPLE_RATE
    expected = selected.duration_s
    if expected is not None and abs(duration-expected) > max(3., expected*.02):
        raise ProbeError('source_duration_mismatch',
                         '下载时长与所选曲目列表不符；不能据此判断录音身份。')
    report = {'duration_s': duration, 'container_duration_s': container_duration,
              'frames': frames, 'sample_rate': _SAMPLE_RATE, 'audio_streams': audio_streams,
              'source_id': selected.track_id, 'provider': selected.provider}
    if render_mode is not None:
        report.update(source_frames=frames, render_mode=render_mode,
                      context_window=(freeze_context(frames) if render_mode == 'excerpt'
                                      else full_context(frames)))
    return report


def probe_cli(argv):
    parser = argparse.ArgumentParser(description='Bounded source probe')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    verify_limits()
    verify_host_headroom()  # StageExecutor holds the common inference flock.
    for path in (args.source, args.request, args.report):
        if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError('Unsafe source probe path')
    workspace = args.request.parent
    if (args.source != workspace/'source.audio'
            or args.request != workspace/'source-probe-request.json'
            or args.report != workspace/'source-probe-report.json'):
        raise ValueError('Source probe path binding changed')
    with args.request.open('rb') as stream:
        raw = stream.read(_STDOUT_LIMIT+1)
    if len(raw) > _STDOUT_LIMIT:
        raise ValueError('Oversized source probe request')
    request = json.loads(raw)
    selected = request['selected']
    duration = selected['duration_s']
    common = {'schema', 'selected', 'max_bytes', 'source_sha256', 'worker_sha256',
              'worker_python', 'worker_script', 'inference_lock'}
    if request.get('schema') == 'sing-source-probe-v1':
        schema_valid = (set(request) == common | {'max_seconds'}
                        and type(request.get('max_seconds')) is int
                        and 30 <= request['max_seconds'] <= 300)
        max_seconds = request.get('max_seconds')
        render_mode = None
    elif request.get('schema') == 'sing-source-probe-v2':
        render_mode = request.get('render_mode')
        schema_valid = (set(request) == common | {
                'render_mode', 'source_max_seconds', 'context_max_seconds',
                'context_policy', 'context_helper_sha256'}
            and render_mode in ('full', 'excerpt')
            and type(request.get('source_max_seconds')) is int
            and ((render_mode == 'full' and 30 <= request['source_max_seconds'] <= 300)
                 or (render_mode == 'excerpt' and request['source_max_seconds'] == 900))
            and type(request.get('context_max_seconds')) is int
            and request['context_max_seconds'] == 90
            and request.get('context_policy') == 'centered-source-v1'
            and digest_file(Path(__file__).with_name('context_window.py')) ==
                request.get('context_helper_sha256'))
        max_seconds = request.get('source_max_seconds')
    else:
        schema_valid = False
        max_seconds = None
        render_mode = None
    if (not schema_valid or set(selected) != {'provider', 'track_id', 'duration_s'}
            or any(not isinstance(selected[name], str) or not 1 <= len(selected[name]) <= 512
                   for name in ('provider', 'track_id'))
            or (duration is not None and (isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or duration <= 0))
            or type(request['max_bytes']) is not int
            or not 1024 <= request['max_bytes'] <= 64*1024*1024
            or args.source.stat().st_size > request['max_bytes']
            or digest_file(args.source) != request['source_sha256']
            or digest_file(Path(__file__).resolve()) != request['worker_sha256']):
        raise ValueError('Source probe request or frozen bytes changed')
    temporary = args.report.with_name(args.report.name+'.part')
    if args.report.exists() or temporary.exists() or temporary.is_symlink():
        raise ValueError('Unsealed source probe output preserved; no automatic retry')
    report = asyncio.run(probe_download(args.source, SimpleNamespace(**selected),
                                        max_seconds=max_seconds, render_mode=render_mode))
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(encoded) > _STDOUT_LIMIT:
        raise ValueError('Oversized probe report')
    # Never replace a partial report or an earlier output after interruption.
    with temporary.open('xb') as output:
        os.chmod(temporary, 0o600)
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())
    os.link(temporary, args.report)
    directory = os.open(workspace, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    temporary.unlink()


def main() -> None:
    if sys.argv[1:2] == ['probe']:
        probe_cli(sys.argv[2:])
        return
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
