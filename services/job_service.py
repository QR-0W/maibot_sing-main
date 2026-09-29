"""Durable selected-source orchestration that outlives the host RPC call.

The host persists a preselected catalogue row and returns immediately.  This
service later resolves that exact provider+track ID, inventories real bytes and
runtime versions, executes the frozen finite plan, and publishes an immutable
artifact.  It never performs a replacement keyword search and never persists a
signed playback URL.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence
import asyncio
import json
import os
import re
import sqlite3
import uuid

from ..runtime.recipe_identity import fingerprint, recipe_document, validate_document
from ..runtime.render_plan import Step, build_plan
from .artifact_store import ArtifactError, ArtifactStore, publish_job
from .asset_inventory import AssetInventory, InventoryError
from .catalogue_service import CatalogueError, CatalogueService
from .job_store import Job, JobConflict, JobStore
from .media_probe import probe_download
from .source_download import DownloadError, download_selected
from .source_offer import CatalogueItem
from .stage_coordinator import StageCoordinator
from .unit_runner import UnitError, save_plan


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
    inference_lock: Path
    max_download_bytes: int = 64 * 1024 * 1024
    max_duration_s: int = 300
    poll_interval_s: float = 1.0

    def __post_init__(self):
        paths = (self.work_root, self.worker_python, self.worker_script,
                 self.rvc_script, self.model, self.index, self.hubert,
                 self.inference_lock)
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


class JobService:
    """One lightweight scheduler for durable selected-source jobs.

    ``submit_selected`` performs only local ledger writes.  Network resolution,
    download, probing, hashing, subprocess stages, and publication occur in the
    background loop or an explicit ``run_once`` call used by tests/hosts.
    """
    _TERMINAL = frozenset(('ready', 'failed', 'cancelled', 'interrupted'))
    _PROGRESS = ('starting', 'resolving', 'downloading', 'separating',
                 'converting', 'encoding', 'validating', 'publishing')

    def __init__(self, store: JobStore, catalogue: CatalogueService,
                 coordinator: StageCoordinator, artifacts: ArtifactStore,
                 inventory: AssetInventory, runtime: RenderRuntime, *,
                 downloader: Callable = download_selected,
                 probe: Callable = probe_download,
                 plan_builder: Callable = build_plan,
                 recipe_builder: Callable = recipe_document,
                 publisher: Callable = publish_job,
                 scanner: Optional[Callable[[JobStore, Iterable[str]], list[Job]]] = None):
        self.store = store
        self.catalogue = catalogue
        self.coordinator = coordinator
        self.artifacts = artifacts
        self.inventory = inventory
        self.runtime = runtime
        self.downloader = downloader
        self.probe = probe
        self.plan_builder = plan_builder
        self.recipe_builder = recipe_builder
        self.publisher = publisher
        self.scanner = scanner or SqliteJobScanner()
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError('Job service is closed')
        await asyncio.to_thread(self._ensure_work_root)
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name='sing-durable-job-service')
            self._wake.set()

    async def close(self) -> None:
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _ensure_work_root(self) -> None:
        root = self.runtime.work_root
        if any(path.is_symlink() for path in (root, *root.parents)):
            raise ValueError('Symlinked job workspace root refused')
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not root.is_dir():
            raise ValueError('Job workspace root is not a directory')
        os.chmod(root, 0o700)

    async def submit_selected(self, stream_id: str, request_token: str, request: dict,
                              selected: CatalogueItem, *, auto_reply: bool = False,
                              consent_event: Optional[str] = None) -> tuple[Job, bool]:
        """Persist one exact displayed row without resolving or downloading it."""
        if not isinstance(selected, CatalogueItem):
            raise ValueError('A typed selected catalogue item is required')
        if not isinstance(request, dict):
            raise ValueError('Request metadata must be an object')
        _reject_credentials(request)
        instrumental = request.get('instrumental', False)
        if type(instrumental) is not bool:
            raise ValueError('Instrumental must be explicit bool')
        job, created = await asyncio.to_thread(
            self.store.submit, stream_id, request_token, request,
            auto_reply=auto_reply, consent_event=consent_event)
        # Retry a host crash between submit/offer/select without a new search.
        if job.state == 'searching':
            job = await asyncio.to_thread(self.store.offer, job.id, stream_id, [selected],
                                          expected_revision=job.revision)
        if job.state == 'needs_selection':
            choices = await asyncio.to_thread(self.store.choices, job.id, stream_id)
            items = choices['items']
            if len(items) != 1 or (items[0]['provider'], items[0]['track_id']) != (
                    selected.provider, selected.track_id):
                raise JobConflict('Persisted selection differs from this exact provider track')
            job = await asyncio.to_thread(self.store.select, job.id, stream_id,
                                          choices['offer_id'], 1)
        if (job.state not in (frozenset(('queued', 'running', 'cancel_requested')) | self._TERMINAL)
                or not job.selected_source
                or (job.selected_source['provider'], job.selected_source['track_id']) !=
                   (selected.provider, selected.track_id)):
            raise JobConflict('Job is not bound to the requested provider track')
        self._wake.set()
        return job, created

    async def cancel(self, job_id: str, stream_id: str) -> Job:
        result = await asyncio.to_thread(self.store.cancel, job_id, stream_id)
        self._wake.set()
        return result

    async def run_once(self) -> Optional[Job]:
        """Reconcile one owned run, or atomically claim and run one queued job."""
        active = await asyncio.to_thread(self.scanner, self.store,
                                         ('running', 'cancel_requested'))
        if len(active) > 1:
            raise JobServiceError('job_ownership_conflict', 'More than one media job owns the slot')
        job = active[0] if active else await asyncio.to_thread(self.store.claim_next)
        if job is None:
            return None
        try:
            await self._drive(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._preserve_failure(job, exc)
        return await asyncio.to_thread(self.store.get, job.id, job.stream_id)

    async def _loop(self) -> None:
        while not self._closed:
            try:
                result = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                result = None
            self._wake.clear()
            timeout = self.runtime.poll_interval_s if result is not None else min(60, self.runtime.poll_interval_s * 5)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
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
        current = await asyncio.to_thread(self.store.get, job.id, job.stream_id)
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
        return await asyncio.to_thread(self.store.progress, current.id, current.run_token,
                                       stage=stage, done=done, total=total)

    async def _cancel_without_unit(self, job: Job) -> bool:
        current = await asyncio.to_thread(self.store.get, job.id, job.stream_id)
        if current.state != 'cancel_requested':
            return False
        attempts = await asyncio.to_thread(self.store.stage_attempts, current.id, current.stream_id)
        if any(item['status'] == 'claimed' for item in attempts):
            return False
        await asyncio.to_thread(self.store.finish_failure, current.id, current.run_token,
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
            result = await self.downloader(self.catalogue, selected, workspace,
                                           max_bytes=self.runtime.max_download_bytes)
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
                'index': self.runtime.index, 'hubert': self.runtime.hubert}

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
            if (plan.get('schema') != 1 or plan.get('workspace') != str(workspace)
                    or plan.get('inference_lock') != str(self.runtime.inference_lock)
                    or plan.get('recipe') != fingerprint(recipe)):
                raise ValueError('plan binding changed')
            steps = tuple(Step(item['name'], tuple(item['argv']), item['timeout_s'],
                               tuple(item['inputs']), tuple(item['outputs']))
                          for item in plan['steps'])
            rebuilt = self.recipe_builder(provider=selected.provider, track_id=selected.track_id,
                hashes=recipe['hashes'], versions=recipe['versions'], steps=steps,
                **self._runtime_args(workspace))
            if rebuilt != recipe:
                raise ValueError('private plan differs from portable recipe')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise JobServiceError('frozen_plan_invalid', 'Frozen recipe or plan is invalid') from exc
        return recipe, steps, plan_path

    async def _prepare(self, job: Job, workspace: Path, selected: CatalogueItem):
        frozen = await asyncio.to_thread(self._read_frozen, workspace, selected)
        if frozen is not None:
            return frozen
        source, report = await self._source(job, workspace, selected)
        inventory = await asyncio.to_thread(self.inventory.build, source)
        duration = report.get('duration_s')
        frames = report.get('frames')
        if type(frames) is not int:
            if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                raise JobServiceError('source_probe_invalid', 'Media probe omitted a usable duration')
            frames = round(float(duration) * 44100)
        request = job.request
        instrumental = request.get('instrumental', False)
        steps = self.plan_builder(**self._runtime_args(workspace), frames=frames,
                                  instrumental=instrumental)
        recipe = self.recipe_builder(provider=selected.provider, track_id=selected.track_id,
            hashes=inventory.hashes, versions=inventory.versions, steps=steps,
            **self._runtime_args(workspace))
        recipe_path, plan_path = workspace / 'recipe.json', workspace / 'plan.json'
        await asyncio.to_thread(_atomic_json, recipe_path, recipe)
        # If recipe survived a crash before plan creation, require byte-identical
        # reconstruction from the current real assets before completing the plan.
        stored = json.loads(await asyncio.to_thread(recipe_path.read_text, encoding='utf-8'))
        if stored != recipe:
            raise JobServiceError('recipe_conflict', 'Real render assets changed during plan freeze')
        await asyncio.to_thread(save_plan, plan_path, workspace=workspace,
                                recipe=fingerprint(recipe),
                                inference_lock=self.runtime.inference_lock, steps=steps)
        return recipe, tuple(steps), plan_path

    @staticmethod
    def _stage_progress(step: Step) -> str:
        if step.name in ('decode', 'separate'):
            return 'separating'
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
        await asyncio.to_thread(self._ensure_workspace, workspace)
        if await self._cancel_without_unit(job):
            return
        recipe, steps, plan_path = await self._prepare(job, workspace, selected)
        recipe_key = fingerprint(recipe)
        attempts = await asyncio.to_thread(self.store.stage_attempts, job.id, job.stream_id)
        start = self._attempt_prefix(steps, attempts)
        total = len(steps)
        for position in range(start, total):
            current = await asyncio.to_thread(self.store.get, job.id, job.stream_id)
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
            attempts = await asyncio.to_thread(self.store.stage_attempts, job.id, job.stream_id)
            await self._progress(current, self._stage_progress(step), position + 1, total)
        current = await asyncio.to_thread(self.store.get, job.id, job.stream_id)
        await self._progress(current, 'publishing', total, total)
        await asyncio.to_thread(self.publisher, self.store, self.artifacts,
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

    async def _preserve_failure(self, original: Job, exc: Exception) -> None:
        current = await asyncio.to_thread(self.store.get, original.id, original.stream_id)
        if current.state in self._TERMINAL:
            return
        if current.state not in ('running', 'cancel_requested'):
            return
        attempts = await asyncio.to_thread(self.store.stage_attempts, current.id, current.stream_id)
        # An active/unknown unit still owns the slot.  StageCoordinator will
        # reconcile it on the next scan; never convert uncertainty into failure.
        if any(item['status'] == 'claimed' for item in attempts):
            return
        error = self._error(exc)
        await asyncio.to_thread(self.store.finish_failure, current.id, current.run_token, error,
            expected_unit=current.unit_name, expected_revision=current.revision,
            interrupted=error['code'] in ('stage_timeout', 'unit_timeout', 'unit_signal',
                                          'stage_terminated', 'launch_unknown', 'unit_wait_unknown'))
