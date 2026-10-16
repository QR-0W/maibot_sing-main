"""Host-side bounded report contract; never launches a media subprocess.

Production probing is JobService's durable source_probe StageCoordinator step.
A test-injected probe callable is responsible for supplying an equivalent report;
there is intentionally no naked ffprobe/ffmpeg fallback here.
"""
from pathlib import Path
from typing import Any, Dict
import json
import math
import hashlib
import re

from ..runtime.context_window import validate_context_window
from ..runtime.render_plan import Step
from ..runtime.stage_receipts import StageReceipts, CheckpointError
from .catalogue_service import CatalogueError
from .source_offer import CatalogueItem

_REPORT_LIMIT = 65536
_REPORT_KEYS_V1 = frozenset(('duration_s', 'container_duration_s', 'frames',
                             'sample_rate', 'audio_streams', 'source_id', 'provider'))
_REPORT_KEYS_V2 = _REPORT_KEYS_V1 | {'source_frames', 'context_window', 'render_mode'}
_PROBE_V1 = 'sing-source-probe-v1'
_PROBE_V2 = 'sing-source-probe-v2'
_CONTEXT_POLICY = 'centered-source-v1'


def source_hash(source: Path, max_bytes: int) -> str:
    """Stream bounded frozen bytes; do not import or launch media code."""
    StageReceipts._safe(source)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > max_bytes:
        raise CatalogueError('source_invalid', 'Source probe input is unsafe or oversized')
    digest = hashlib.sha256()
    amount = 0
    with source.open('rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            amount += len(block)
            if amount > max_bytes:
                raise CatalogueError('source_invalid', 'Source probe input grew beyond bound')
            digest.update(block)
    return digest.hexdigest()


def probe_key(request: dict) -> str:
    raw = json.dumps(request, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _bounded_json(path: Path) -> dict:
    StageReceipts._safe(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _REPORT_LIMIT:
        raise ValueError('Frozen source probe document is unsafe')
    with path.open('rb') as stream:
        raw = stream.read(_REPORT_LIMIT+1)
    if len(raw) > _REPORT_LIMIT:
        raise ValueError('Frozen source probe document is oversized')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Frozen source probe document is invalid')
    return value


def probe_step(workspace: Path, request: dict) -> Step:
    return Step('source_probe', (request['worker_python'], request['worker_script'],
        'probe', '--source', str(workspace/'source.audio'),
        '--request', str(workspace/'source-probe-request.json'),
        '--report', str(workspace/'source-probe-report.json')), 150,
        ('source.audio', 'source-probe-request.json'), ('source-probe-report.json',))


def read_probe_plan(workspace: Path, selected: CatalogueItem):
    """Verify frozen request/plan without inspecting changed runtime assets.

    Claimed-unit recovery calls this before reconciliation. Actual byte checks
    belong to verify_source_preflight, after ownership is settled.
    """
    try:
        if not workspace.is_absolute() or not workspace.is_dir():
            raise ValueError('Probe workspace must be an existing absolute directory')
        StageReceipts._safe(workspace)
        request = _bounded_json(workspace/'source-probe-request.json')
        plan = _bounded_json(workspace/'source-probe-plan.json')
        schema = request.get('schema')
        common = {'schema', 'selected', 'max_bytes', 'source_sha256',
                  'worker_sha256', 'worker_python', 'worker_script', 'inference_lock'}
        if schema == _PROBE_V1:
            valid_schema = (set(request) == common | {'max_seconds'}
                and type(request.get('max_seconds')) is int
                and 30 <= request['max_seconds'] <= 300)
            digest_names = ('source_sha256', 'worker_sha256')
            path_names = ('worker_python', 'worker_script', 'inference_lock')
        elif schema == _PROBE_V2:
            valid_schema = (set(request) == common | {
                    'render_mode', 'source_max_seconds', 'context_max_seconds',
                    'context_policy', 'context_helper_sha256'}
                and request.get('render_mode') in ('full', 'excerpt')
                and type(request.get('source_max_seconds')) is int
                and ((request['render_mode'] == 'full'
                      and 30 <= request['source_max_seconds'] <= 300)
                     or (request['render_mode'] == 'excerpt'
                         and request['source_max_seconds'] == 900))
                and type(request.get('context_max_seconds')) is int
                and request['context_max_seconds'] == 90
                and request.get('context_policy') == _CONTEXT_POLICY)
            digest_names = ('source_sha256', 'worker_sha256', 'context_helper_sha256')
            path_names = ('worker_python', 'worker_script', 'inference_lock')
        else:
            valid_schema = False
            digest_names = ()
            path_names = ()
        if (not valid_schema
                or request['selected'] != {'provider': selected.provider,
                    'track_id': selected.track_id, 'duration_s': selected.duration_s}
                or type(request['max_bytes']) is not int
                or not 1024 <= request['max_bytes'] <= 64*1024*1024
                or any(not isinstance(request[name], str)
                    or not re.fullmatch('[0-9a-f]{64}', request[name])
                    for name in digest_names)
                or any(not isinstance(request[name], str)
                    or not Path(request[name]).is_absolute()
                    for name in path_names)):
            raise ValueError('Invalid frozen source request')
        step = probe_step(workspace, request)
        key = probe_key(request)
        expected = {'schema': 1, 'workspace': str(workspace), 'recipe': key,
            'inference_lock': request['inference_lock'], 'steps': [
                {'name': step.name, 'argv': list(step.argv), 'timeout_s': step.timeout_s,
                 'inputs': list(step.inputs), 'outputs': list(step.outputs)}]}
        if type(plan.get('schema')) is not int or plan != expected:
            raise ValueError('Source probe plan differs from frozen request')
    except (KeyError, TypeError, ValueError, OSError, CheckpointError, RecursionError) as exc:
        raise CatalogueError('source_probe_plan_invalid', 'Frozen source probe binding changed') from exc
    return request, step, workspace/'source-probe-plan.json', key


def verify_probe_receipt(workspace: Path, step: Step, key: str) -> None:
    try:
        report = workspace/'source-probe-report.json'
        if report.is_symlink() or not report.is_file() or report.stat().st_size > _REPORT_LIMIT:
            raise ValueError('Source probe report missing or oversized')
        receipts = StageReceipts(workspace, key)
        inputs = receipts._files(list(step.inputs))
        value = receipts.verify(step.name, {name: record['sha256']
                                           for name, record in inputs.items()})
        if value is None or set(value['outputs']) != set(step.outputs):
            raise ValueError('Source probe receipt missing')
    except (CheckpointError, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise CatalogueError('source_probe_receipt_invalid',
                             'Source report has no verified bounded-stage receipt') from exc


def verify_source_preflight(workspace: Path, selected: CatalogueItem, recipe: dict):
    """Verify independent probe evidence; never add it to the audio recipe/cache."""
    request, step, _, key = read_probe_plan(workspace, selected)
    if (request['source_sha256'] != recipe['hashes']['source']
            or request['worker_sha256'] != recipe['hashes']['worker']):
        raise CatalogueError('source_probe_plan_invalid', 'Probe and audio recipe inputs differ')
    if request['schema'] == _PROBE_V2:
        if (recipe.get('schema') != 'sing-render-v4'
                or request['render_mode'] != recipe.get('render_mode')
                or request['context_helper_sha256'] != recipe['hashes'].get('context_window')):
            raise CatalogueError('source_probe_plan_invalid',
                                 'Probe mode/context policy differs from audio recipe')
    elif recipe.get('schema') not in ('sing-render-v2', 'sing-render-v3'):
        raise CatalogueError('source_probe_plan_invalid',
                             'Legacy probe cannot authorize a new recipe schema')
    # The bounded worker checked its own bytes at execution. Its SHA is frozen
    # in the request, whose exact bytes the independent receipt binds. Current
    # external worker/model assets are a future-launch concern, not evidence of
    # a completed immutable publication.
    if source_hash(workspace/'source.audio', request['max_bytes']) != request['source_sha256']:
        raise CatalogueError('source_probe_plan_invalid', 'Source probe frozen input bytes changed')
    verify_probe_receipt(workspace, step, key)
    report = read_probe_report(workspace/'source-probe-report.json', selected,
                             max_seconds=(request['max_seconds'] if request['schema'] == _PROBE_V1
                                           else request['source_max_seconds']),
                              schema=request['schema'], render_mode=request.get('render_mode'))
    if request['schema'] == _PROBE_V2 and (
            report['source_frames'] != recipe['source_frames']
            or report['context_window'] != recipe['context_window']):
        raise CatalogueError('source_probe_invalid', 'Probe context differs from frozen recipe')
    return report


async def probe_download(path: Path, selected: CatalogueItem,
                         *, max_seconds: int = 300) -> Dict[str, Any]:
    """Reject unbounded legacy callers instead of silently running on the Host."""
    raise CatalogueError('source_probe_requires_budget',
                         '来源媒体检测必须通过受预算约束的持久计算单元。')


def read_probe_report(path: Path, selected: CatalogueItem,
                      *, max_seconds: int = 300, schema: str = _PROBE_V1,
                       render_mode=None) -> Dict[str, Any]:
    """Read <=64KiB, only AFTER the caller verifies the source_probe receipt.

    This is schema validation, not a second decoding/duration-agreement algorithm.
    A report alone (including a leftover .part file) never proves stage completion.
    """
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.stat().st_size > _REPORT_LIMIT):
        raise CatalogueError('source_probe_invalid', '来源检测报告缺失或无效。')
    with path.open('rb') as stream:
        raw = stream.read(_REPORT_LIMIT+1)
    if len(raw) > _REPORT_LIMIT:
        raise CatalogueError('source_probe_invalid', '来源检测报告过大。')
    try:
        report = json.loads(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        raise CatalogueError('source_probe_invalid', '来源检测报告JSON无效。') from exc
    return validate_probe_report(report, selected, max_seconds=max_seconds,
                                 schema=schema, render_mode=render_mode)


def validate_probe_report(report, selected, *, max_seconds=300,
                          schema=_PROBE_V1, render_mode=None):
    """One pure field validator for both sealed reports and explicit test DI."""
    try:
        expected_keys = _REPORT_KEYS_V1 if schema == _PROBE_V1 else _REPORT_KEYS_V2
        if (schema not in (_PROBE_V1, _PROBE_V2) or not isinstance(report, dict)
                or set(report) != expected_keys):
            raise ValueError('Unexpected report schema')
        frames, rate = report['frames'], report['sample_rate']
        duration, container = report['duration_s'], report['container_duration_s']
        if (type(frames) is not int or type(rate) is not int or rate != 44100
                or not 30*rate <= frames <= max_seconds*rate
                or type(report['audio_streams']) is not int or report['audio_streams'] < 1
                or isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or duration != frames/rate
                or (container is not None and (isinstance(container, bool)
                    or not isinstance(container, (int, float))
                    or not math.isfinite(container) or container < 0))
                or (report['provider'], report['source_id']) !=
                   (selected.provider, selected.track_id)):
            raise ValueError('Invalid normalized report fields')
        if schema == _PROBE_V2:
            if (type(report['source_frames']) is not int
                    or report['source_frames'] != frames or report['render_mode'] != render_mode
                    or render_mode not in ('full', 'excerpt')):
                raise ValueError('Probe report mode/source binding changed')
            validate_context_window(report['context_window'], frames,
                                    frozen=render_mode == 'excerpt')
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise CatalogueError('source_probe_invalid', '来源检测报告格式或曲目绑定无效。') from exc
    return report
