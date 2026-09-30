"""Durable selected-source orchestration that outlives the host RPC call.

The host persists a preselected catalogue row and returns immediately.  This
service later resolves that exact provider+track ID, inventories real bytes and
runtime versions, executes the frozen finite plan, and publishes an immutable
artifact.  It never performs a replacement keyword search and never persists a
signed playback URL.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import stat
import time
import uuid

from ..runtime.recipe_identity import SCHEMA, fingerprint, recipe_document, validate_document
from ..runtime.render_plan import Step, build_plan
from .artifact_store import ArtifactError, ArtifactStore, publish_job
from .asset_inventory import (AssetInventory, InventoryError, RuntimeInventory,
                              RUNTIME_HASH_NAMES, runtime_generation)
from .catalogue_service import CatalogueError, CatalogueService
from .job_store import (TRUSTED_QQ_INGRESS, JOB_PROGRESS_STAGES, Job, JobConflict,
                        JobStore, request_modes)
from .media_probe import probe_download
from .source_download import DownloadError, download_selected
from .source_offer import CatalogueItem
from .stage_coordinator import StageCoordinator
from .unit_runner import UnitError, save_plan


LOGGER = logging.getLogger(__name__)
RENDER_PARAMETER_POLICY = {
    'schema': 'sing-render-policy-v1',
    'source_binding': 'selected-provider-track-id',
    'source_frame_policy': 'ffmpeg-map0-a0-44100-stereo-pcm-f32le-stream-count-v1',
    'sample_rate_hz': 44100,
    'chunk_seconds': 20,
    'tail_fold_seconds': 5,
    'pitch_semitones': 0,
    'f0_method': 'harvest',
    'index_rate': 0.5,
    'filter_radius': 3,
    'rms_mix_rate': 0.25,
    'protect': 0.33,
    'seed': 20260928,
    'resample_hz': 44100,
    'encode': {'codec': 'libmp3lame', 'bitrate': '192k'},
    'instrumental_modes': [False, True],
    'render_modes': ['full', 'excerpt'],
    'excerpt_policy': 'sing-excerpt-selection-v1',
}

_COORDINATOR_MESSAGES = {
    'unit_status_unknown': '无法确认当前计算单元是否已停止；任务槽位保留，未启动新阶段。',
    'launch_unknown': '无法确认当前阶段是否已完成启动或退出；任务槽位保留，未启动新阶段。',
    'scheduler_ownership_lost': '后台调度所有权不可用；任务槽位保留，未启动新阶段。',
    'job_scan_failed': '后台任务扫描暂时失败；未启动新阶段。',
    'job_ownership_conflict': '后台任务所有权冲突；未启动新阶段。',
}


class JobServiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RenderRuntime:
    work_root: Path
    worker_python: Path
    worker_script: Path
    rvc_script: Path
    model: Path
    index: Path
    hubert: Path
    demucs_repo: Path
    inference_lock: Path
    max_download_bytes: int = 64 * 1024 * 1024
    max_duration_s: int = 300
    poll_interval_s: float = 1.0

    def __post_init__(self):
        paths = (self.work_root, self.worker_python, self.worker_script,
                 self.rvc_script, self.model, self.index, self.hubert,
                 self.demucs_repo, self.inference_lock)
        if any(not Path(path).is_absolute() for path in paths):
            raise ValueError('Job runtime paths must be absolute')
        if (type(self.max_download_bytes) is not int
                or not 1024 <= self.max_download_bytes <= 64 * 1024 * 1024
                or type(self.max_duration_s) is not int
                or not 30 <= self.max_duration_s <= 300
                or isinstance(self.poll_interval_s, bool)
                or not 0.05 <= self.poll_interval_s <= 60):
            raise ValueError('Invalid job runtime bounds')


class SqliteJobScanner:
    """Read-only adapter for the public scan operation JobStore does not expose."""
    _STATES = frozenset(('running', 'cancel_requested'))

    def __call__(self, store: JobStore, states: Iterable[str]) -> list[Job]:
        wanted = tuple(states)
        if not wanted or not set(wanted) <= self._STATES:
            raise ValueError('Invalid recovery scan states')
        placeholders = ','.join('?' for _ in wanted)
        try:
            with sqlite3.connect(store.path, timeout=5) as database:
                rows = database.execute(
                    'SELECT id,stream_id FROM jobs WHERE state IN (' + placeholders + ') '
                    'ORDER BY created_at,id', wanted).fetchall()
        except sqlite3.Error as exc:
            raise JobServiceError('job_scan_failed', 'Durable job recovery scan failed') from exc
        return [store.get(job_id, stream_id) for job_id, stream_id in rows]


def _reject_credentials(value, path='request') -> None:
    forbidden = ('url', 'cookie', 'authorization', 'password', 'secret', 'signed', 'header')
    if isinstance(value, dict):
        for key, child in value.items():
            label = str(key).casefold()
            if any(word in label for word in forbidden):
                raise ValueError('Request metadata must not contain playback credentials')
            _reject_credentials(child, path + '.' + str(key))
    elif isinstance(value, list):
        for child in value:
            _reject_credentials(child, path)


def _atomic_json(path: Path, document: dict) -> None:
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > 65536:
        raise JobServiceError('recipe_oversized', 'Frozen render recipe exceeds 64KiB')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
    try:
        with temporary.open('xb') as output:
            os.chmod(temporary, 0o600)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
                raise JobServiceError('recipe_conflict', 'A different frozen recipe already exists')
        directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_status(path: Path, document: dict) -> None:
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > 4096:
        raise JobServiceError('coordinator_status_oversized',
                              'Coordinator status exceeds 4KiB')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
    try:
        with temporary.open('xb') as output:
            os.chmod(temporary, 0o600)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary,path)
        directory=os.open(path.parent,os.O_DIRECTORY|os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _freeze_runtime_context(context: Mapping[str, Any]) -> dict:
    try:
        execution = {str(name): os.fspath(path)
                     for name, path in dict(context['execution_paths']).items()}
        limits = dict(context['limits'])
        policy = json.loads(json.dumps(context['parameter_policy'], ensure_ascii=False,
                                       allow_nan=False))
        return {'execution_paths': execution,
                'artifact_root': os.fspath(context['artifact_root']),
                'limits': limits, 'parameter_policy': policy}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('Invalid immutable runtime context') from exc


def _delivery_candidates(store: JobStore, limit: int) -> list[tuple[str, str]]:
    if type(limit) is not int or not 1 <= limit <= 32:
        raise ValueError('Invalid delivery scan limit')
    try:
        with sqlite3.connect(store.path, timeout=5) as database:
            return [(str(job_id), str(stream_id)) for job_id, stream_id in database.execute(
                "SELECT id,stream_id FROM jobs WHERE state='ready' "
                "AND delivery_state='pending' AND consent_event IS NOT NULL "
                "AND json_extract(request_json,'$.platform')='qq' "
                "AND json_extract(request_json,'$.auto_reply')=1 "
                "AND json_extract(request_json,'$.ingress_proof')=? "
                "ORDER BY created_at,id LIMIT ?", (TRUSTED_QQ_INGRESS,limit))]
    except sqlite3.Error as exc:
        raise JobServiceError('delivery_scan_failed',
                              'Durable delivery scan failed') from exc


def _read_status(path: Path):
    try:
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file() or path.stat().st_size>4096:
            raise ValueError('unsafe status path')
        value=json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(value,dict)
                or set(value)!={'schema','code','message','retry_after_s','updated_at'}
                or value.get('schema')!=1
                or not isinstance(value.get('code'),str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value['code'])
                or not isinstance(value.get('message'),str)
                or not 1<=len(value['message'])<=300
                or isinstance(value.get('retry_after_s'),bool)
                or not isinstance(value.get('retry_after_s'),(int,float))
                or not math.isfinite(value['retry_after_s'])
                or not .05<=value['retry_after_s']<=60
                or isinstance(value.get('updated_at'),bool)
                or not isinstance(value.get('updated_at'),(int,float))
                or not math.isfinite(value['updated_at'])):
            raise ValueError('invalid status document')
        return value
    except (OSError,ValueError,TypeError,json.JSONDecodeError) as exc:
        raise JobServiceError('coordinator_status_invalid',
                              'Coordinator status is invalid') from exc


class JobService:
    """One lightweight scheduler for durable selected-source jobs.

    ``submit_selected`` performs only local ledger writes.  Network resolution,
    download, probing, hashing, subprocess stages, and publication occur in the
    background loop or an explicit ``run_once`` call used by tests/hosts.
    """
    _TERMINAL = frozenset(('ready', 'failed', 'cancelled', 'interrupted'))
    _PROGRESS = JOB_PROGRESS_STAGES

    def __init__(self, store: JobStore, catalogue: CatalogueService,
                 coordinator: StageCoordinator, artifacts: Any,
                 inventory: AssetInventory, runtime: RenderRuntime, *,
                 runtime_context: Optional[Mapping[str, Any]] = None,
                 downloader: Callable = download_selected,
                 probe: Callable = probe_download,
                 plan_builder: Callable = build_plan,
                 recipe_builder: Callable = recipe_document,
                 publisher: Callable = publish_job,
                 scanner: Optional[Callable[[JobStore, Iterable[str]], list[Job]]] = None,
                 ownership_fd: Optional[int] = None, close_drain_s: float = 1.0):
        self.store = store
        self.catalogue = catalogue
        self.coordinator = coordinator
        self.artifacts = None if isinstance(artifacts, Path) else artifacts
        if isinstance(artifacts, Path):
            self._artifact_root = Path(artifacts)
        elif isinstance(artifacts, ArtifactStore):
            self._artifact_root = Path(artifacts.root)
        else:
            self._artifact_root = None
        self.inventory = inventory
        self.runtime = runtime
        self._runtime_context = (_freeze_runtime_context(runtime_context)
                                 if runtime_context is not None else None)
        self._generation: Optional[str] = None
        self._runtime_inventory: Optional[RuntimeInventory] = None
        self._context_task: Optional[asyncio.Task] = None
        self.downloader = downloader
        self.probe = probe
        self.plan_builder = plan_builder
        self.recipe_builder = recipe_builder
        self.publisher = publisher
        self.scanner = scanner or SqliteJobScanner()
        if ownership_fd is not None:
            if type(ownership_fd) is not int:
                raise ValueError('Scheduler ownership fd must be an integer')
            try:
                if not stat.S_ISREG(os.fstat(ownership_fd).st_mode):
                    raise ValueError('Scheduler ownership fd must reference a regular file')
            except OSError as exc:
                raise ValueError('Scheduler ownership fd is not open') from exc
        if (isinstance(close_drain_s, bool) or not isinstance(close_drain_s, (int, float))
                or not 0 <= close_drain_s <= 10):
            raise ValueError('Invalid finite close drain')
        self._ownership_fd = ownership_fd
        self._close_drain_s = float(close_drain_s)
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='sing-job-io')
        self._offloads: set[asyncio.Future] = set()
        self._late_offload_errors: list[BaseException] = []
        self._coordinator_failures: list[BaseException] = []
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._loop_failures = 0
        self._closing = False
        self._closed = False

    def _finish_offload(self, future: asyncio.Future) -> None:
        """Retrieve and retain late failures after an awaiting task is cancelled."""
        self._offloads.discard(future)
        if future.cancelled():
            return
        try:
            error = future.exception()
        except BaseException as exc:
            error = exc
        if error is not None:
            self._late_offload_errors.append(error)
            del self._late_offload_errors[:-16]

    async def _offload(self, function: Callable, /, *args, **kwargs):
        """Run synchronous work while a duplicate scheduler lease remains open."""
        if self._closing:
            raise RuntimeError('Job service is closing')
        lease_fd = None
        if self._ownership_fd is not None:
            try:
                lease_fd = os.dup(self._ownership_fd)
            except OSError as exc:
                raise JobServiceError('scheduler_ownership_lost',
                                      'Scheduler ownership is no longer available') from exc
        operation = partial(function, *args, **kwargs)

        def owned_call():
            try:
                return operation()
            finally:
                if lease_fd is not None:
                    os.close(lease_fd)

        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(self._executor, owned_call)
        except BaseException:
            if lease_fd is not None:
                os.close(lease_fd)
            raise
        self._offloads.add(future)
        future.add_done_callback(self._finish_offload)
        return await asyncio.shield(future)

    def _prepare_context_sync(self) -> str:
        if self._runtime_context is None or self._artifact_root is None:
            raise JobServiceError('runtime_context_missing',
                                  'Durable job service requires a runtime generation context')
        self._ensure_work_root()
        root = Path(self._runtime_context['artifact_root'])
        if root != self._artifact_root or not root.is_absolute():
            raise JobServiceError('artifact_root_mismatch',
                                  'Runtime context and artifact store root differ')
        if any(path.is_symlink() for path in (root, *root.parents)):
            raise JobServiceError('artifact_root_invalid',
                                  'Symlinked artifact storage root refused')
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not root.is_dir() or root.is_symlink():
            raise JobServiceError('artifact_root_invalid',
                                  'Artifact storage root is invalid')
        os.chmod(root, 0o700)
        if self.artifacts is None:
            self.artifacts = ArtifactStore(root)
        elif not isinstance(self.artifacts, ArtifactStore) or Path(self.artifacts.root) != root:
            raise JobServiceError('artifact_root_mismatch',
                                  'Artifact validator is not bound to the configured root')
        self.store.bind_artifact_root(str(root))
        runtime_inventory = self.inventory.build_runtime()
        generation = runtime_generation(runtime_inventory, self._runtime_context)
        self._runtime_inventory = runtime_inventory
        self._generation = generation
        return generation

    def _finish_context_task(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            error=task.exception()
        except BaseException as exc:
            error=exc
        if error is not None:
            self._coordinator_failures.append(error)
            del self._coordinator_failures[:-16]

    async def prepare(self) -> str:
        """Bind storage and freeze one real runtime generation without starting loops."""
        if self._closed:
            raise RuntimeError('Job service is closed')
        if self._generation is not None:
            return self._generation
        if self._context_task is None:
            task=asyncio.create_task(self._offload(self._prepare_context_sync),
                                     name='sing-runtime-context')
            task.add_done_callback(self._finish_context_task)
            self._context_task=task
        generation=await asyncio.shield(self._context_task)
        if self._generation is None or generation!=self._generation:
            raise JobServiceError('runtime_generation_missing',
                                  'Runtime generation preparation did not complete')
        return self._generation

    @property
    def generation(self) -> str:
        if self._generation is None:
            raise RuntimeError('Job service runtime context is not prepared')
        return self._generation

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError('Job service is closed')
        await self.prepare()
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name='sing-durable-job-service')
            self._wake.set()

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Do not stall SDK unload indefinitely. Pending worker calls retain their
        # duplicated flock lease until their own finally block, so the entrypoint
        # may close its fd after this bounded drain without allowing a new host in.
        pending = tuple(self._offloads)
        if pending and self._close_drain_s:
            await asyncio.wait(pending, timeout=self._close_drain_s)
        self._executor.shutdown(wait=False, cancel_futures=False)

    def _ensure_work_root(self) -> None:
        root = self.runtime.work_root
        if any(path.is_symlink() for path in (root, *root.parents)):
            raise ValueError('Symlinked job workspace root refused')
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not root.is_dir():
            raise ValueError('Job workspace root is not a directory')
        os.chmod(root, 0o700)

    def _request_document(self, request: dict, *, auto_reply: bool) -> dict:
        if not isinstance(request,dict):
            raise ValueError('Request metadata must be an object')
        if 'service_generation' in request:
            raise ValueError('Service generation is reserved for the durable coordinator')
        _reject_credentials(request)
        request_modes(request)
        declared_auto_reply=request.get('auto_reply',False)
        if type(auto_reply) is not bool or type(declared_auto_reply) is not bool:
            raise ValueError('Auto reply must be explicit bool')
        if declared_auto_reply!=auto_reply:
            raise ValueError('Request auto reply selector differs from durable consent')
        try:
            document=json.loads(json.dumps(request,ensure_ascii=False,allow_nan=False))
        except (TypeError,ValueError) as exc:
            raise ValueError('Request metadata must be finite JSON') from exc
        document['service_generation']=self.generation
        return document

    async def find_request(self, stream_id: str, request_token: str, request: dict, *,
                           auto_reply: bool = False,
                           consent_event: Optional[str] = None) -> Optional[Job]:
        await self.prepare()
        document=self._request_document(request,auto_reply=auto_reply)
        await self._offload(self.store.expire_candidates)
        job=await self._offload(self.store.find_request,stream_id,request_token)
        if job is not None and (job.request!=document or job.consent_event!=consent_event):
            raise JobConflict('Same request token has different contents, consent, or generation')
        return job

    async def admit_offer(self, stream_id: str, request_token: str, request: dict,
                          candidates: list, *, auto_reply: bool = False,
                          consent_event: Optional[str] = None,
                          select_single: bool = True) -> tuple[Job,bool]:
        await self.prepare()
        document=self._request_document(request,auto_reply=auto_reply)
        await self._offload(self.store.expire_candidates)
        job,created=await self._offload(
            self.store.admit_offer,stream_id,request_token,document,candidates,
            auto_reply=auto_reply,consent_event=consent_event,
            select_single=select_single)
        if job.state=='queued':
            self._wake.set()
        return job,created

    async def submit_selected(self, stream_id: str, request_token: str, request: dict,
                              selected: CatalogueItem, *, auto_reply: bool = False,
                              consent_event: Optional[str] = None) -> tuple[Job, bool]:
        """Atomically persist and select one exact displayed provider row."""
        if not isinstance(selected,CatalogueItem):
            raise ValueError('A typed selected catalogue item is required')
        job,created=await self.admit_offer(
            stream_id,request_token,request,[selected],auto_reply=auto_reply,
            consent_event=consent_event,select_single=True)
        if (job.state not in (frozenset(('queued','running','cancel_requested'))|self._TERMINAL)
                or not job.selected_source
                or (job.selected_source['provider'],job.selected_source['track_id']) !=
                   (selected.provider,selected.track_id)):
            raise JobConflict('Job is not bound to the requested provider track')
        return job,created

    async def get_job(self, job_id: str, stream_id: str) -> Job:
        await self.prepare()
        return await self._offload(self.store.get,job_id,stream_id)

    async def choices(self, job_id: str, stream_id: str) -> dict:
        await self.prepare()
        return await self._offload(self.store.choices,job_id,stream_id)

    async def select(self, job_id: str, stream_id: str, offer_id: str,
                     number: int) -> Job:
        await self.prepare()
        current=await self._offload(self.store.get,job_id,stream_id)
        if current.request.get('service_generation')!=self.generation:
            if current.state=='needs_selection':
                await self._offload(self.store.cancel,job_id,stream_id)
            raise JobServiceError('configuration_changed',
                                  '任务配置已变化；旧候选未启动，请重新发起请求。')
        result=await self._offload(self.store.select,job_id,stream_id,offer_id,number)
        self._wake.set()
        return result

    async def cancel(self, job_id: str, stream_id: str) -> Job:
        await self.prepare()
        result = await self._offload(self.store.cancel, job_id, stream_id)
        self._wake.set()
        return result

    async def expire_candidates(self) -> int:
        await self.prepare()
        return await self._offload(self.store.expire_candidates)

    async def delivery_candidates(self, limit: int = 8) -> list[tuple[str,str]]:
        await self.prepare()
        return await self._offload(_delivery_candidates,self.store,limit)

    @staticmethod
    def _coordinator_document(exc: Exception, retry_after_s: float) -> dict:
        code=str(getattr(exc,'code','coordinator_error'))
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',code):
            code='coordinator_error'
        message=_COORDINATOR_MESSAGES.get(
            code,'后台任务协调暂时失败；任务槽位保留，未启动新阶段。')
        return {'schema':1,'code':code,'message':message,
                'retry_after_s':round(min(60,max(.05,retry_after_s)),3),
                'updated_at':round(time.time(),3)}

    async def _record_coordinator_error(self, exc: Exception, *, job: Optional[Job],
                                        retry_after_s: float) -> None:
        document=self._coordinator_document(exc,retry_after_s)
        path=(self._workspace(job) if job is not None else self.runtime.work_root)/'coordinator-error.json'
        await self._offload(_replace_status,path,document)
        LOGGER.error('%s code=%s job=%s',document['message'],document['code'],
                     job.id if job is not None else 'scheduler')

    async def _clear_coordinator_error(self, job: Optional[Job]) -> None:
        path=(self._workspace(job) if job is not None else self.runtime.work_root)/'coordinator-error.json'
        await self._offload(path.unlink,missing_ok=True)

    async def coordinator_error(self, job_id: Optional[str] = None,
                                stream_id: Optional[str] = None):
        """Return a bounded public-safe nonterminal coordinator status."""
        await self.prepare()
        if (job_id is None)!=(stream_id is None):
            raise ValueError('Job and stream identities must be supplied together')
        if job_id is None:
            path=self.runtime.work_root/'coordinator-error.json'
        else:
            job=await self._offload(self.store.get,job_id,stream_id)
            path=self._workspace(job)/'coordinator-error.json'
        return await self._offload(_read_status,path)

    async def run_once(self) -> Optional[Job]:
        """Reconcile one owned run, or atomically claim and run one queued job."""
        await self.prepare()
        await self._offload(self.store.expire_candidates)
        active = await self._offload(self.scanner, self.store,
                                         ('running', 'cancel_requested'))
        if len(active) > 1:
            raise JobServiceError('job_ownership_conflict', 'More than one media job owns the slot')
        job = active[0] if active else await self._offload(self.store.claim_next)
        if job is None:
            return None
        try:
            await self._drive(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not await self._preserve_failure(job, exc):
                raise
            await self._clear_coordinator_error(job)
        else:
            await self._clear_coordinator_error(job)
        return await self._offload(self.store.get, job.id, job.stream_id)

    async def _loop(self) -> None:
        while not self._closed:
            try:
                result = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._loop_failures=min(7,self._loop_failures+1)
                timeout=min(60,max(self.runtime.poll_interval_s,
                                   float(2**(self._loop_failures-1))))
                self._coordinator_failures.append(exc)
                del self._coordinator_failures[:-16]
                try:
                    await self._record_coordinator_error(
                        exc,job=None,retry_after_s=timeout)
                except Exception as status_exc:
                    self._coordinator_failures.append(status_exc)
                    del self._coordinator_failures[:-16]
                    LOGGER.error('无法写入后台协调器受限错误状态；将在有限退避后重试。')
                result=None
            else:
                self._loop_failures=0
                timeout=(self.runtime.poll_interval_s if result is not None else
                         min(60,self.runtime.poll_interval_s*5))
                try:
                    await self._clear_coordinator_error(None)
                except Exception as clear_exc:
                    self._coordinator_failures.append(clear_exc)
                    del self._coordinator_failures[:-16]
                    LOGGER.error('无法清理后台协调器受限错误状态。')
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(),timeout)
            except asyncio.TimeoutError:
                pass

    def _workspace(self, job: Job) -> Path:
        if not re.fullmatch(r'[0-9a-f]{32}', job.id):
            raise JobServiceError('job_identity_invalid', 'Durable job identity is invalid')
        return self.runtime.work_root / job.id

    def _ensure_workspace(self, workspace: Path) -> None:
        self._ensure_work_root()
        if workspace.is_symlink():
            raise JobServiceError('workspace_symlink', 'Symlinked job workspace refused')
        workspace.mkdir(mode=0o700, exist_ok=True)
        if not workspace.is_dir():
            raise JobServiceError('workspace_invalid', 'Job workspace is invalid')
        os.chmod(workspace, 0o700)

    @staticmethod
    def _selected(job: Job) -> CatalogueItem:
        try:
            item = CatalogueItem(**job.selected_source)
        except (TypeError, ValueError) as exc:
            raise JobServiceError('source_snapshot_invalid', 'Selected source snapshot is invalid') from exc
        if (item.provider, item.track_id) != (job.selected_source['provider'],
                                               job.selected_source['track_id']):
            raise JobServiceError('source_snapshot_invalid', 'Selected source identity is invalid')
        return item

    async def _progress(self, job: Job, stage: str, done: int, total: int) -> Job:
        current = await self._offload(self.store.get, job.id, job.stream_id)
        if current.state == 'cancel_requested':
            return current
        if current.state != 'running':
            return current
        current_index = self._PROGRESS.index(current.stage) if current.stage in self._PROGRESS else 0
        target_index = self._PROGRESS.index(stage)
        if target_index < current_index or done < current.chunk_done:
            return current
        if current.chunk_total and current.chunk_total != total:
            raise JobServiceError('plan_progress_conflict', 'Frozen stage count changed after recovery')
        return await self._offload(self.store.progress, current.id, current.run_token,
                                       stage=stage, done=done, total=total)

    async def _cancel_without_unit(self, job: Job) -> bool:
        current = await self._offload(self.store.get, job.id, job.stream_id)
        if current.state != 'cancel_requested':
            return False
        attempts = await self._offload(self.store.stage_attempts, current.id, current.stream_id)
        if any(item['status'] == 'claimed' for item in attempts):
            return False
        await self._offload(self.store.finish_failure, current.id, current.run_token,
            {'code': 'cancelled', 'message': '用户取消；未启动新的媒体阶段，已有证据保留。'},
            expected_unit=current.unit_name, expected_revision=current.revision)
        return True

    async def _source(self, job: Job, workspace: Path, selected: CatalogueItem) -> tuple[Path, dict]:
        source = workspace / 'source.audio'
        current = await self._progress(job, 'resolving', 0, 0)
        if await self._cancel_without_unit(current):
            raise JobServiceError('cancelled', 'Job cancelled before source resolution')
        if not source.exists():
            current = await self._progress(current, 'downloading', 0, 0)
            download_options={'max_bytes':self.runtime.max_download_bytes}
            if self.downloader is download_selected:
                download_options['offload']=self._offload
            result = await self.downloader(self.catalogue,selected,workspace,
                                           **download_options)
            downloaded = Path(result[0])
            if downloaded != source or not downloaded.is_file() or downloaded.is_symlink():
                raise JobServiceError('source_download_contract', 'Downloader returned an unexpected source path')
        elif not source.is_file() or source.is_symlink():
            raise JobServiceError('source_invalid', 'Persisted selected source is not a regular file')
        report = await self.probe(source, selected, max_seconds=self.runtime.max_duration_s)
        if (report.get('provider'), report.get('source_id')) != (selected.provider, selected.track_id):
            raise JobServiceError('source_identity_mismatch', 'Media probe is not bound to the selected provider track')
        return source, report

    def _runtime_args(self, workspace: Path) -> dict:
        return {'workspace': workspace, 'worker_python': self.runtime.worker_python,
                'worker_script': self.runtime.worker_script,
                'rvc_script': self.runtime.rvc_script, 'model': self.runtime.model,
                'index': self.runtime.index, 'hubert': self.runtime.hubert,
                'demucs_repo': self.runtime.demucs_repo}

    def _read_frozen(self, workspace: Path, selected: CatalogueItem):
        recipe_path, plan_path = workspace / 'recipe.json', workspace / 'plan.json'
        if not recipe_path.exists() or not plan_path.exists():
            return None
        if (recipe_path.is_symlink() or plan_path.is_symlink() or not recipe_path.is_file()
                or not plan_path.is_file() or recipe_path.stat().st_size > 65536
                or plan_path.stat().st_size > 65536):
            raise JobServiceError('frozen_plan_invalid', 'Frozen recipe or plan is invalid')
        try:
            recipe = json.loads(recipe_path.read_text(encoding='utf-8'))
            plan = json.loads(plan_path.read_text(encoding='utf-8'))
            validate_document(recipe)
            if recipe['schema'] != SCHEMA:
                raise ValueError('Legacy recipes are read-only committed artifacts')
            if (plan.get('schema') != 1 or plan.get('workspace') != str(workspace)
                    or plan.get('inference_lock') != str(self.runtime.inference_lock)
                    or plan.get('recipe') != fingerprint(recipe)):
                raise ValueError('plan binding changed')
            steps = tuple(Step(item['name'], tuple(item['argv']), item['timeout_s'],
                               tuple(item['inputs']), tuple(item['outputs']))
                          for item in plan['steps'])
            rebuilt = self.recipe_builder(provider=selected.provider, track_id=selected.track_id,
                hashes=recipe['hashes'], versions=recipe['versions'], steps=steps,
                render_mode=recipe['render_mode'], **self._runtime_args(workspace))
            if rebuilt != recipe:
                raise ValueError('private plan differs from portable recipe')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise JobServiceError('frozen_plan_invalid', 'Frozen recipe or plan is invalid') from exc
        return recipe, steps, plan_path

    def _inventory_generation(self, hashes: Mapping[str,str],
                              versions: Mapping[str,str]) -> str:
        if self._runtime_context is None:
            raise JobServiceError('runtime_context_missing',
                                  'Runtime generation context is unavailable')
        runtime_inventory=RuntimeInventory(
            hashes={name:hashes[name] for name in RUNTIME_HASH_NAMES},
            versions=dict(versions))
        return runtime_generation(runtime_inventory,self._runtime_context)

    async def _verify_live_generation(self) -> None:
        current=await self._offload(self.inventory.build_runtime)
        if runtime_generation(current,self._runtime_context)!=self.generation:
            raise JobServiceError('configuration_changed',
                                  '任务配置资产已变化；未启动下载或媒体阶段，请重新加载配置。')

    async def _prepare(self, job: Job, workspace: Path, selected: CatalogueItem,
                       frozen=None):
        if frozen is None:
            await self._verify_live_generation()
        else:
            return (*frozen, True)
        source, report = await self._source(job, workspace, selected)
        inventory = await self._offload(self.inventory.build, source)
        if self._inventory_generation(inventory.hashes,inventory.versions)!=self.generation:
            raise JobServiceError('configuration_changed',
                                  '任务配置资产在冻结前发生变化；未启动媒体阶段。')
        frames = report.get('frames')
        sample_rate = report.get('sample_rate')
        if (type(frames) is not int or type(sample_rate) is not int
                or sample_rate != 44100
                or not 30*sample_rate <= frames <= self.runtime.max_duration_s*sample_rate):
            raise JobServiceError('source_probe_invalid',
                                  'Media probe omitted exact normalized source frames')
        _, render_mode, instrumental = request_modes(job.request)
        steps = self.plan_builder(**self._runtime_args(workspace), frames=frames,
                                  instrumental=instrumental, render_mode=render_mode)
        recipe = self.recipe_builder(provider=selected.provider, track_id=selected.track_id,
            hashes=inventory.hashes, versions=inventory.versions, steps=steps,
            render_mode=render_mode, **self._runtime_args(workspace))
        recipe_path, plan_path = workspace / 'recipe.json', workspace / 'plan.json'
        await self._offload(_atomic_json, recipe_path, recipe)
        # If recipe survived a crash before plan creation, require byte-identical
        # reconstruction from the current real assets before completing the plan.
        stored = json.loads(await self._offload(recipe_path.read_text, encoding='utf-8'))
        if stored != recipe:
            raise JobServiceError('recipe_conflict', 'Real render assets changed during plan freeze')
        await self._offload(save_plan, plan_path, workspace=workspace,
                                recipe=fingerprint(recipe),
                                inference_lock=self.runtime.inference_lock, steps=steps)
        return recipe, tuple(steps), plan_path, False

    async def _verify_resume_inventory(self, workspace: Path, recipe: dict) -> None:
        """Fail closed if a frozen job would launch with different real assets."""
        source = workspace / 'source.audio'
        current = await self._offload(self.inventory.build, source)
        changed_hashes = sorted(name for name, digest in recipe['hashes'].items()
                                if current.hashes.get(name) != digest)
        changed_versions = sorted(name for name, version in recipe['versions'].items()
                                  if current.versions.get(name) != version)
        if changed_hashes or changed_versions:
            # Names are recipe schema labels, never private paths or credentials.
            labels = ','.join(changed_hashes + changed_versions)
            raise JobServiceError('asset_inventory_changed',
                'Frozen render inventory changed before resume (' + labels + '); '
                'no new media stage was launched.')

    @staticmethod
    def _stage_progress(step: Step) -> str:
        if step.name in ('decode', 'separate'):
            return 'separating'
        if step.name == 'excerpt':
            return 'excerpt'
        if step.name.startswith('convert_') or step.name == 'mix':
            return 'converting'
        if step.name == 'encode':
            return 'encoding'
        if step.name == 'validate':
            return 'validating'
        raise JobServiceError('plan_stage_unknown', 'Frozen plan contains an unknown stage')

    @staticmethod
    def _attempt_prefix(steps: Sequence[Step], attempts: list) -> int:
        if len(attempts) > len(steps):
            raise JobServiceError('attempt_plan_conflict', 'Durable stage attempts exceed the frozen plan')
        names = [step.name for step in steps]
        for position, attempt in enumerate(attempts):
            if attempt['step'] != names[position]:
                raise JobServiceError('attempt_plan_conflict', 'Durable stage attempts differ from the frozen plan')
            if attempt['status'] == 'completed':
                continue
            if attempt['status'] == 'claimed' and position == len(attempts) - 1:
                return position
            raise JobServiceError('attempt_state_invalid', 'Durable stage attempt requires explicit reconciliation')
        return len(attempts)

    async def _drive(self, job: Job) -> None:
        selected = self._selected(job)
        workspace = self._workspace(job)
        await self._offload(self._ensure_workspace, workspace)
        if await self._cancel_without_unit(job):
            return
        _, render_mode, instrumental = request_modes(job.request)
        frozen_plan=await self._offload(self._read_frozen,workspace,selected)
        if frozen_plan is not None:
            frozen_recipe = frozen_plan[0]
            mix = next((step for step in frozen_recipe['steps'] if step['name']=='mix'), None)
            if (frozen_recipe['render_mode'] != render_mode or mix is None
                    or ('--instrumental' in mix['argv']) != instrumental):
                raise JobServiceError('frozen_plan_invalid', 'Frozen plan differs from requested render options')
        if (frozen_plan is None
                and job.request.get('service_generation')!=self.generation):
            raise JobServiceError('configuration_changed',
                                  '任务配置代际已变化；未启动下载或媒体阶段，请重新发起。')
        recipe, steps, plan_path, frozen = await self._prepare(
            job,workspace,selected,frozen_plan)
        recipe_key = fingerprint(recipe)
        attempts = await self._offload(self.store.stage_attempts, job.id, job.stream_id)
        start = self._attempt_prefix(steps, attempts)
        total = len(steps)
        # Existing durable ownership is reconciled before hashing changed assets
        # or considering another claim. This path can neither claim nor launch.
        if attempts and attempts[-1]['status']=='claimed':
            current=await self._offload(self.store.get,job.id,job.stream_id)
            step=steps[start]
            result=await self.coordinator.reconcile_step(
                current.id,current.stream_id,current.run_token,
                step,workspace,recipe_key)
            if result.get('state') in ('running','owned'):
                return
            if result.get('state')=='cancelled':
                return
            if result.get('state')!='completed':
                raise JobServiceError('stage_result_invalid',
                                      'Stage reconciler returned an invalid state')
            attempts=await self._offload(self.store.stage_attempts,job.id,job.stream_id)
            start=self._attempt_prefix(steps,attempts)
            await self._progress(current,self._stage_progress(step),start,total)
        # A completed plan can be published from its immutable bytes and receipts
        # even if an administrator later updates the configured model.  Any job
        # with an uncompleted stage must instead prove that source, weights, code,
        # and runtime versions still match the recipe before coordinator launch.
        if frozen and start < total:
            await self._verify_resume_inventory(workspace, recipe)
        for position in range(start, total):
            current = await self._offload(self.store.get, job.id, job.stream_id)
            if current.state == 'cancel_requested' and not any(
                    item['status'] == 'claimed' for item in attempts):
                if await self._cancel_without_unit(current):
                    return
            step = steps[position]
            await self._progress(current, self._stage_progress(step), position, total)
            result = await self.coordinator.run_step(
                current.id, current.stream_id, current.run_token,
                step, workspace, plan_path, recipe_key)
            if result.get('state') in ('running', 'owned'):
                return
            if result.get('state') == 'cancelled':
                return
            if result.get('state') != 'completed':
                raise JobServiceError('stage_result_invalid', 'Stage coordinator returned an invalid state')
            attempts = await self._offload(self.store.stage_attempts, job.id, job.stream_id)
            await self._progress(current, self._stage_progress(step), position + 1, total)
        current = await self._offload(self.store.get, job.id, job.stream_id)
        await self._progress(current, 'publishing', total, total)
        await self._offload(self.publisher, self.store, self.artifacts,
            job_id=current.id, stream_id=current.stream_id, run_token=current.run_token,
            workspace=workspace, recipe=recipe)

    @staticmethod
    def _error(exc: Exception) -> dict:
        typed = (CatalogueError, DownloadError, InventoryError, ArtifactError,
                 UnitError, JobServiceError)
        if isinstance(exc, typed):
            code = getattr(exc, 'code', 'job_failed')
            message = str(exc)
        elif isinstance(exc, (ValueError, JobConflict)):
            code, message = 'orchestration_conflict', str(exc)
        else:
            code = 'job_service_error'
            message = '后台媒体协调失败；具体诊断已保留在本机任务证据中。'
        code = str(code) if re.fullmatch(r'[A-Za-z0-9_-]{1,256}', str(code)) else 'job_failed'
        message = str(message).strip()[:1500] or 'Background media job failed'
        return {'code': code, 'message': message}

    async def _preserve_failure(self, original: Job, exc: Exception) -> bool:
        current = await self._offload(self.store.get, original.id, original.stream_id)
        if current.state in self._TERMINAL:
            return True
        if current.state not in ('running', 'cancel_requested'):
            return True
        attempts = await self._offload(self.store.stage_attempts, current.id, current.stream_id)
        # A claimed unit may still be active or unknown. Retain its slot, expose a
        # bounded public-safe coordinator fault, and make the loop back off.
        if any(item['status'] == 'claimed' for item in attempts):
            await self._record_coordinator_error(
                exc,job=current,retry_after_s=max(1,self.runtime.poll_interval_s))
            return False
        error = self._error(exc)
        await self._offload(self.store.finish_failure, current.id, current.run_token, error,
            expected_unit=current.unit_name, expected_revision=current.revision,
            interrupted=error['code'] in ('stage_timeout', 'unit_timeout', 'unit_signal',
                                          'stage_terminated', 'launch_unknown', 'unit_wait_unknown'))
        return True
