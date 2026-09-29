"""Bounded, persistent Linux cover backend. No model imports in MaiBot."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
import unicodedata
import uuid

if __package__:
    from .library_catalog import index_cover
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from library_catalog import index_cover
    sys.path.pop(0)


class CoverStageError(RuntimeError):
    """Explicit pre-delivery failure; never represent this as a QQ send failure."""
    def __init__(self, code: str, message: str, candidates: list | None = None):
        super().__init__(message)
        self.code = code
        self.candidates = candidates or []


def worker_failure(scratch: Path, log_path: Path) -> CoverStageError:
    path = scratch / 'error.json'
    allowed = {'source_ambiguous', 'source_unavailable', 'source_invalid_selection', 'worker_failed'}
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            raise ValueError('Missing or oversized worker error record')
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('version') != 1 or record.get('code') not in allowed:
            raise ValueError('Invalid worker error record')
        message = record['message']
        if not isinstance(message, str) or len(message) > 500:
            raise ValueError('Invalid worker error message')
        choices = record.get('candidates', [])
        if not isinstance(choices, list):
            raise ValueError('Invalid candidate list')
        safe_choices = []
        for choice in choices[:10]:
            if not isinstance(choice, dict):
                raise ValueError('Invalid candidate')
            safe_choices.append({k: str(choice.get(k, ''))[:180] for k in ('source', 'identifier', 'album', 'duration_s')})
        return CoverStageError(record['code'], message, safe_choices)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        logging.getLogger(__name__).error('Worker failed; see %s', log_path)
        return CoverStageError('worker_failed', '本地处理失败，尚未进入发送；请管理员查看任务日志。')


# Never assume an administrator's home directory exists in another installation.
# The plugin config must supply paths to licensed weights and isolated runtimes.
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


def normalized_catalogue_text(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


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
    public_path: Path | None = None
    catalog_warning: str = ""
    album: str = ""
    source_id: str = ""


class LocalBackend:
    def __init__(self, output_dir: Path, *, model: Path | None = None, index: Path | None = None,
                 worker_python: Path | None = None, musicdl_python: Path | None = None,
                 rvc_script: Path | None = None, inference_lock: Path | None = None,
                 max_queue: int = 2, timeout_s: int = 900, max_duration_s: int = 300,
                 max_download_bytes: int = 64 * 1024 * 1024, allowlist: tuple[Path, ...] = ()) -> None:
        if not output_dir.is_absolute() or max_queue < 0 or max_queue > 8 or not 60 <= timeout_s <= 900:
            raise ValueError('无效的本地后端配置')
        if not 30 <= max_duration_s <= 300 or not 1024 * 1024 <= max_download_bytes <= 64 * 1024 * 1024:
            raise ValueError('歌曲时长或下载大小超过安全上限')
        self.output = output_dir
        self.model, self.index = model, index
        self.worker_python, self.musicdl_python = worker_python, musicdl_python
        self.rvc_script, self.inference_lock = rvc_script, inference_lock
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

    async def _catalog_result(self, result: CoverResult) -> CoverResult:
        try:
            entry = await asyncio.to_thread(index_cover, self.output, result.key)
        except (OSError, ValueError, RuntimeError) as exc:
            # A browse-view failure must never revoke a committed song or report
            # that inference failed. The file is still safe to send by cache path.
            logging.getLogger(__name__).warning('Cover saved, catalog update failed: %s', exc)
            return CoverResult(result.path, result.title, result.artist, result.key,
                               catalog_warning='成品已保存，但可读目录索引更新失败',
                               album=result.album, source_id=result.source_id)
        return CoverResult(result.path, result.title, result.artist, result.key,
                           public_path=self.output / entry['file'],
                           album=result.album, source_id=result.source_id)

    async def cover(self, title: str, artist: str, *, instrumental: bool = False,
                    source_file: Path | None = None, album: str | None = None,
                    source_id: str | None = None) -> CoverResult:
        title, artist = safe_text(title), safe_text(artist)
        if album is not None:
            album = safe_text(album)
        if source_id is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', source_id):
            raise ValueError('无效的来源曲目 ID')
        if source_file is not None and (album is not None or source_id is not None):
            raise ValueError('本地回归输入不能混用远程曲目选择参数')
        await self.start()
        if source_file is not None:
            resolved = source_file.resolve(strict=True)
            if not any(resolved == allowed.resolve(strict=True) for allowed in self.allowlist):
                raise ValueError('本地音频未列入回归 allowlist')
        source = {'type': 'local', 'sha256': await asyncio.to_thread(digest_file, source_file)} if source_file else {
            'type': 'musicdl-native', 'title': normalized_catalogue_text(title),
            'artist': normalized_catalogue_text(artist)}
        if source_file is not None:
            source['name'] = source_file.name
        else:
            if album is not None:
                source['album'] = normalized_catalogue_text(album)
            if source_id is not None:
                source['source_id'] = source_id
        if self.model is None or self.index is None or not self.model.is_file() or not self.index.is_file():
            raise RuntimeError('请配置有效的模型与 index；拒绝使用旧 sidecar 兜底')
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
                selection = data.get('release_selection') or data.get('verified_source', {}).get('selection', {})
                if isinstance(selection, dict) and 'tolerance_s' in selection:
                    return None  # Never silently reuse the invalid duration-cluster choice.
                result_file = final / 'cover.mp3'
                if (data.get('status') == 'completed' and data.get('key') == actual
                        and result_file.is_file() and data.get('sha256') == digest_file(result_file)):
                    stored = data.get('verified_source', {})
                    return CoverResult(result_file, title, artist, actual,
                        album=str(stored.get('album', '')) if isinstance(stored, dict) else '',
                        source_id=str(stored.get('identifier', '')) if isinstance(stored, dict) else '')
            except (OSError, ValueError, KeyError):
                pass
            return None
        existing = await asyncio.to_thread(completed)
        if existing is not None:
            return await self._catalog_result(existing)
        key = request_key
        async with self._lock:
            if self._closed:
                raise RuntimeError('后端已关闭')
            task = self._runs.get(key)
            if task is None:
                if len(self._runs) >= self.max_queue + 1:
                    raise RuntimeError('翻唱队列已满，请稍后再试')
                task = asyncio.create_task(self._tracked_execute(key, title, artist, source, source_file,
                                                                 model_hash, index_hash, instrumental, album, source_id))
                self._runs[key] = task
                task.add_done_callback(lambda finished, name=key: self._runs.pop(name, None))
        return await asyncio.shield(task)

    async def _tracked_execute(self, key: str, title: str, artist: str, source: dict[str, str],
                               source_file: Path | None, model_hash: str, index_hash: str,
                               instrumental: bool, album: str | None = None,
                               source_id: str | None = None) -> CoverResult:
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
                                         model_hash, index_hash, instrumental, unit, album, source_id)
            await asyncio.to_thread(status, 'completed')
            return await self._catalog_result(result)
        except asyncio.CancelledError:
            await asyncio.to_thread(status, 'cancelled')
            raise
        except asyncio.TimeoutError as exc:
            await asyncio.to_thread(status, 'failed', 'processing_timeout')
            raise CoverStageError('processing_timeout', '翻唱处理超时；没有进入语音发送，不会自动重试。') from exc
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
                       instrumental: bool, unit: str, album: str | None = None,
                       source_id: str | None = None) -> CoverResult:
        async with self._semaphore:
            runtime_paths = (self.worker_python, self.musicdl_python, self.rvc_script, self.inference_lock)
            if any(path is None or not path.is_absolute() for path in runtime_paths):
                raise RuntimeError('请配置绝对路径：local.worker_python、musicdl_python、rvc_script、inference_lock')
            worker_python, musicdl_python, rvc_script, inference_lock = runtime_paths
            if not all(path.is_file() for path in (worker_python, musicdl_python, rvc_script)):
                raise RuntimeError('本地隔离 Python 或 RVC 脚本缺失；不会回退到未受限环境')
            if not inference_lock.parent.is_dir() or inference_lock.is_symlink():
                raise RuntimeError('inference_lock 父目录不存在或锁是符号链接')
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
                       str(worker_python), str(script),
                       '--worker-python', str(worker_python), '--musicdl-python', str(musicdl_python),
                       '--rvc-script', str(rvc_script), '--inference-lock', str(inference_lock),
                       '--scratch', str(scratch), '--title', title, '--artist', artist,
                       '--model', str(self.model), '--index', str(self.index),
                       '--max-seconds', str(self.max_duration_s), '--max-bytes', str(self.max_download_bytes)]
            if album is not None:
                command.extend(['--album', album])
            if source_id is not None:
                command.extend(['--source-id', source_id])
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
                    raise await asyncio.to_thread(worker_failure, scratch, log_path)
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
                        # Preserve the actual selected release in the cache key;
                        # albums can share a recording or contain different takes.
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
                        old_selection = previous.get('release_selection', {})
                        if isinstance(old_selection, dict) and 'tolerance_s' in old_selection:
                            # Never reuse the deprecated duration-cluster decision.
                            # A new namespace avoids overwriting immutable historic audio.
                            identity = {**identity, 'selection_policy': 'explicit-source-v1'}
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
                        return CoverResult(final / 'cover.mp3', title, artist, actual_key,
                            album=str(verified.get('album', '')), source_id=str(verified.get('identifier', '')))
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
                    return CoverResult(final / 'cover.mp3', title, artist, actual_key,
                            album=str(verified.get('album', '')), source_id=str(verified.get('identifier', '')))
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
