"""Bounded, persistent Linux cover backend. No model imports in MaiBot."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import time
import uuid

MODEL = Path('/home/qr0w/audio-lab/models/natsume-iroha/extracted/NatsumeIroha/NatsumeIroha.pth')
INDEX = MODEL.with_name('added_IVF186_Flat_nprobe_1_v1.index')
PARAMETERS = {'pitch': 0, 'f0_method': 'harvest', 'index_rate': 0.5, 'filter_radius': 3,
              'rms_mix_rate': 0.25, 'protect': 0.33, 'seed': 20260928,
              'separator': 'htdemucs', 'segment': 5, 'shifts': 0, 'resample_sr': 44100,
              'rvc_chunk_seconds': 20}


def safe_text(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 180 or any(ord(ch) < 32 for ch in value):
        raise ValueError('歌名/艺人为空、过长或含控制字符')
    if '/' in value or '\\' in value or '..' in value or value.startswith(('-', '~')):
        raise ValueError('不接受文件路径或命令参数作为歌名')
    return value


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def cache_key(source: dict[str, str], model_hash: str, index_hash: str, instrumental: bool) -> str:
    data = {'source': source, 'model_sha256': model_hash, 'index_sha256': index_hash,
            'parameters': PARAMETERS, 'instrumental': instrumental}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('x', encoding='utf-8') as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@dataclass(frozen=True)
class CoverResult:
    path: Path
    title: str
    artist: str
    key: str


class LocalBackend:
    def __init__(self, output_dir: Path, *, model: Path = MODEL, index: Path = INDEX,
                 max_queue: int = 2, timeout_s: int = 900, max_duration_s: int = 300,
                 max_download_bytes: int = 64 * 1024 * 1024, allowlist: tuple[Path, ...] = ()) -> None:
        if not output_dir.is_absolute() or max_queue < 0 or max_queue > 8 or not 60 <= timeout_s <= 900:
            raise ValueError('无效的本地后端配置')
        if not 30 <= max_duration_s <= 300 or not 1024 * 1024 <= max_download_bytes <= 64 * 1024 * 1024:
            raise ValueError('歌曲时长或下载大小超过安全上限')
        self.output = output_dir
        self.model, self.index = model, index
        self.max_queue, self.timeout_s = max_queue, timeout_s
        self.max_duration_s, self.max_download_bytes = max_duration_s, max_download_bytes
        self.allowlist = allowlist
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(1)
        self._runs: dict[str, asyncio.Task[CoverResult]] = {}
        self._units: set[str] = set()
        self._closed = False
        self._owner = None

    async def start(self) -> None:
        """Own one library; never clean a scratch directory used by a surviving worker."""
        async with self._lock:
            if self._closed:
                raise RuntimeError('后端已关闭')
            if self._owner is not None:
                return
            await asyncio.to_thread(self.output.mkdir, parents=True, exist_ok=True)
            owner = (self.output / '.backend.lock').open('a+b')
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                owner.close()
                raise RuntimeError('另一个本地后端正在使用此成品库') from None
            try:
                await self._recover()
            except BaseException:
                owner.close()
                raise
            self._owner = owner

    async def _recover(self) -> None:
        def read_jobs() -> list[tuple[Path, dict[str, Any]]]:
            return [(path, json.loads(path.read_text(encoding='utf-8')))
                    for path in (self.output / 'jobs').glob('*.json')]
        records = await asyncio.to_thread(read_jobs)
        stale = []
        for path, data in records:
            if data.get('status') not in ('queued', 'processing', 'failed'):
                continue
            key, unit, scratch = data.get('request_key', ''), data.get('unit', ''), data.get('scratch', '')
            if not re.fullmatch('[0-9a-f]{64}', key) or path.stem != key:
                raise RuntimeError('任务记录损坏，拒绝自动清理')
            if not unit:
                # Pre-0.3 records have no proven worker ownership. Preserve them
                # for explicit administrator recovery, rather than guessing a PID.
                raise RuntimeError(f'旧任务缺少 unit 身份，需管理员确认已停止: {path}')
            if not re.fullmatch(r'maibot-sing-[0-9a-f]{32}', unit):
                raise RuntimeError('任务 unit 身份无效，拒绝自动清理')
            proc = await asyncio.create_subprocess_exec('systemctl', '--user', 'is-active', unit + '.service',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(proc.communicate(), 10)
            if proc.returncode not in (3, 4) or stdout.decode().strip() not in ('inactive', 'failed', 'unknown'):
                raise RuntimeError(f'遗留 worker 尚未停止或 systemd 状态未知，暂不接收任务: {unit}')
            if scratch and not re.fullmatch(key + '-[0-9a-f]{32}', scratch):
                raise RuntimeError('任务 scratch 身份无效，拒绝自动清理')
            stale.append((path, data, scratch))
        def finalize() -> None:
            for path, data, scratch in stale:
                if scratch:
                    candidate = self.output / '.scratch' / scratch
                    if candidate.is_symlink():
                        raise RuntimeError('拒绝清理符号链接 scratch')
                    if candidate.exists():
                        shutil.rmtree(candidate)
                data.update(status='interrupted', error='宿主中断；已确认原 worker 不再运行', updated_at=time.time())
                atomic_json(path, data)
        await asyncio.to_thread(finalize)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            tasks = list(self._runs.values())
            for task in tasks:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for unit in tuple(self._units):
            await self._stop_unit(unit)
        if self._owner is not None:
            self._owner.close()
            self._owner = None

    async def cover(self, title: str, artist: str, *, instrumental: bool = False,
                    source_file: Path | None = None) -> CoverResult:
        title, artist = safe_text(title), safe_text(artist)
        await self.start()
        if source_file is not None:
            resolved = source_file.resolve(strict=True)
            if not any(resolved == allowed.resolve(strict=True) for allowed in self.allowlist):
                raise ValueError('本地音频未列入回归 allowlist')
        source = {'type': 'local', 'sha256': await asyncio.to_thread(digest_file, source_file)} if source_file else {
            'type': 'musicdl-native', 'title': title.casefold(), 'artist': artist.casefold()}
        if source_file is not None:
            source['name'] = source_file.name
        if not self.model.is_file() or not self.index.is_file():
            raise RuntimeError('模型或 index 不存在；拒绝使用旧 sidecar 兜底')
        model_hash, index_hash = await asyncio.gather(asyncio.to_thread(digest_file, self.model),
                                                       asyncio.to_thread(digest_file, self.index))
        request_key = cache_key(source, model_hash, index_hash, instrumental)
        def completed() -> CoverResult | None:
            try:
                pointer = self.output / 'requests' / (request_key + '.json')
                actual = json.loads(pointer.read_text(encoding='utf-8'))['key']
                if not re.fullmatch('[0-9a-f]{64}', actual):
                    return None
                final = self.output / actual
                data = json.loads((final / 'metadata.json').read_text(encoding='utf-8'))
                result_file = final / 'cover.mp3'
                if (data.get('status') == 'completed' and data.get('key') == actual
                        and result_file.is_file() and data.get('sha256') == digest_file(result_file)):
                    return CoverResult(result_file, title, artist, actual)
            except (OSError, ValueError, KeyError):
                pass
            return None
        existing = await asyncio.to_thread(completed)
        if existing is not None:
            return existing
        key = request_key
        async with self._lock:
            if self._closed:
                raise RuntimeError('后端已关闭')
            task = self._runs.get(key)
            if task is None:
                if len(self._runs) >= self.max_queue + 1:
                    raise RuntimeError('翻唱队列已满，请稍后再试')
                task = asyncio.create_task(self._tracked_execute(key, title, artist, source, source_file,
                                                                 model_hash, index_hash, instrumental))
                self._runs[key] = task
                task.add_done_callback(lambda finished, name=key: self._runs.pop(name, None))
        return await asyncio.shield(task)

    async def _tracked_execute(self, key: str, title: str, artist: str, source: dict[str, str],
                               source_file: Path | None, model_hash: str, index_hash: str,
                               instrumental: bool) -> CoverResult:
        jobs = self.output / 'jobs'
        unit = 'maibot-sing-' + uuid.uuid4().hex
        scratch_name = key + '-' + unit.removeprefix('maibot-sing-')
        def status(value: str, error: str = '') -> None:
            jobs.mkdir(parents=True, exist_ok=True)
            atomic_json(jobs / (key + '.json'), {'request_key': key, 'status': value,
                         'unit': unit, 'scratch': scratch_name,
                         'title': title, 'artist': artist, 'error': error[:300], 'updated_at': time.time()})
        await asyncio.to_thread(status, 'queued')
        try:
            result = await self._execute(key, title, artist, source, source_file,
                                         model_hash, index_hash, instrumental, unit)
            await asyncio.to_thread(status, 'completed')
            return result
        except asyncio.CancelledError:
            await asyncio.to_thread(status, 'cancelled')
            raise
        except Exception as exc:
            await asyncio.to_thread(status, 'failed', str(exc))
            raise

    async def _stop_unit(self, unit: str) -> None:
        if unit not in self._units or not re.fullmatch(r'maibot-sing-[0-9a-f]{32}', unit):
            return
        proc = await asyncio.create_subprocess_exec('systemctl', '--user', 'stop', unit + '.service',
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), 30)
        if proc.returncode != 0:
            check = await asyncio.create_subprocess_exec('systemctl', '--user', 'is-active', unit + '.service',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(check.communicate(), 10)
            if check.returncode not in (3, 4) or stdout.decode().strip() not in ('inactive', 'failed', 'unknown'):
                raise RuntimeError(f'无法确认 worker 已停止，保留 scratch 与库锁: {unit}')
        self._units.discard(unit)

    async def _execute(self, key: str, title: str, artist: str, source: dict[str, str],
                       source_file: Path | None, model_hash: str, index_hash: str,
                       instrumental: bool, unit: str) -> CoverResult:
        async with self._semaphore:
            scratch_name = key + '-' + unit.removeprefix('maibot-sing-')
            def mark_processing() -> None:
                jobs = self.output / 'jobs'
                jobs.mkdir(parents=True, exist_ok=True)
                atomic_json(jobs / (key + '.json'), {'request_key': key, 'status': 'processing',
                            'unit': unit, 'scratch': scratch_name,
                            'title': title, 'artist': artist, 'updated_at': time.time()})
            await asyncio.to_thread(mark_processing)
            root = self.output
            await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
            scratch = root / '.scratch' / scratch_name
            await asyncio.to_thread(scratch.mkdir, parents=True, exist_ok=False)
            script = Path(__file__).resolve().parent.parent / 'runtime' / 'worker.py'
            command = ['systemd-run', '--user', '--wait', '--collect', '--pipe', '--unit', unit,
                       '-p', 'MemoryMax=4G', '-p', 'MemoryHigh=3G', '-p', 'MemorySwapMax=0',
                       '-p', 'CPUQuota=150%', '-p', 'TasksMax=64', '-p', 'TimeoutStopSec=15',
                       '-p', f'RuntimeMaxSec={self.timeout_s}',
                       '/home/qr0w/svc-bench/.venv39/bin/python', str(script),
                       '--scratch', str(scratch), '--title', title, '--artist', artist,
                       '--model', str(self.model), '--index', str(self.index),
                       '--max-seconds', str(self.max_duration_s), '--max-bytes', str(self.max_download_bytes)]
            if instrumental:
                command.append('--instrumental')
            if source_file:
                command.extend(['--source-file', str(source_file)])
            self._units.add(unit)
            log_path = root / 'jobs' / (key + '.log')
            log = log_path.open('ab')
            clean_scratch = True
            try:
                proc = await asyncio.create_subprocess_exec(*command, stdout=log,
                                                            stderr=asyncio.subprocess.STDOUT)
                try:
                    await asyncio.wait_for(proc.communicate(), self.timeout_s + 20)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    clean_scratch = False
                    await self._stop_unit(unit)
                    clean_scratch = True
                    if proc.returncode is None:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                    await proc.communicate()
                    raise
                if proc.returncode != 0:
                    clean_scratch = False
                    await self._stop_unit(unit)
                    clean_scratch = True
                    raise RuntimeError(f'受限 worker 失败 (exit={proc.returncode})，日志: {log_path}')
                mp3 = scratch / 'cover.mp3'
                report = scratch / 'result.json'
                def publish() -> CoverResult:
                    data = json.loads(report.read_text(encoding='utf-8'))
                    if not mp3.is_file() or mp3.stat().st_size < 1024:
                        raise RuntimeError('worker 未生成完整 MP3')
                    verified = data['verified_source']
                    if source_file is None:
                        if verified.get('source') != 'NeteaseMusicClient' or not verified.get('identifier'):
                            raise RuntimeError('下载缺少可追溯的官方源身份')
                        # Different albums are different recordings, so album belongs
                        # in the cache identity instead of being flattened away.
                        identity = {'type': 'musicdl-native', 'platform': verified['source'],
                                    'identifier': str(verified['identifier']),
                                    'title': str(verified['title']), 'artist': str(verified['artist']),
                                    'album': str(verified.get('album', ''))}
                    else:
                        identity = source
                    actual_key = cache_key(identity, model_hash, index_hash, instrumental)
                    final = root / actual_key
                    if final.exists():
                        previous = json.loads((final / 'metadata.json').read_text(encoding='utf-8'))
                        if (previous.get('status') != 'completed' or previous.get('key') != actual_key
                                or previous.get('sha256') != digest_file(final / 'cover.mp3')):
                            raise RuntimeError('既有成品校验失败，拒绝覆盖或清理')
                        pointer_dir = root / 'requests'
                        pointer_dir.mkdir(parents=True, exist_ok=True)
                        atomic_json(pointer_dir / (key + '.json'), {'key': actual_key})
                        return CoverResult(final / 'cover.mp3', title, artist, actual_key)
                    data.update({'status': 'completed', 'key': actual_key, 'title': title, 'artist': artist,
                                 'source': identity, 'model_sha256': model_hash,
                                 'index_sha256': index_hash, 'parameters': PARAMETERS,
                                 'instrumental': instrumental, 'sha256': digest_file(mp3),
                                 'completed_at': time.time()})
                    # Keep an auditable record of which official release was picked.
                    if source_file is None and data.get('verified_source', {}).get('selection'):
                        data['release_selection'] = data['verified_source']['selection']
                    # Publish only the final song and metadata. Original downloads,
                    # separated stems and diagnostic WAVs remain disposable scratch.
                    publishing = root / '.publishing' / (actual_key + '-' + uuid.uuid4().hex)
                    publishing.parent.mkdir(parents=True, exist_ok=True)
                    publishing.mkdir(exist_ok=False)
                    try:
                        shutil.copyfile(mp3, publishing / 'cover.mp3')
                        with (publishing / 'cover.mp3').open('rb') as output:
                            os.fsync(output.fileno())
                        atomic_json(publishing / 'metadata.json', data)
                        if final.exists():
                            raise RuntimeError('成品已存在，拒绝覆盖或清理')
                        os.replace(publishing, final)
                    finally:
                        if publishing.exists():
                            shutil.rmtree(publishing)
                    pointer_dir = root / 'requests'
                    pointer_dir.mkdir(parents=True, exist_ok=True)
                    atomic_json(pointer_dir / (key + '.json'), {'key': actual_key})
                    return CoverResult(final / 'cover.mp3', title, artist, actual_key)
                publishing_task = asyncio.create_task(asyncio.to_thread(publish))
                try:
                    return await asyncio.shield(publishing_task)
                except asyncio.CancelledError:
                    # A thread cannot be cancelled. Finish its atomic commit
                    # before deleting scratch or releasing library ownership.
                    await publishing_task
                    raise
            finally:
                log.close()
                if clean_scratch:
                    self._units.discard(unit)
                    await asyncio.to_thread(shutil.rmtree, scratch, ignore_errors=True)
