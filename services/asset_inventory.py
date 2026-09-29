"""Hash the real render inputs and record the isolated runtime versions.

Reusable runtime content can be inventoried before a source exists; the full
inventory adds only the exact downloaded source bytes. It never accepts declared
digests or exposes playback URLs, credentials, chat identities, or private paths.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping
import hashlib
import json
import math
import os
import re
import subprocess

from ..runtime.recipe_identity import HASH_NAMES, VERSION_NAMES


class InventoryError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AssetPaths:
    """Administrator-selected media inputs whose bytes define a render."""
    model: Path
    index: Path
    hubert: Path
    demucs_repo: Path
    rvc_script: Path
    rvc_upstream: Path
    media_stage: Path
    worker: Path
    render_plan: Path
    stage_executor: Path

    def documents(self) -> Dict[str, Path]:
        return {name: Path(getattr(self, name)) for name in HASH_NAMES if name != 'source'}


RUNTIME_HASH_NAMES = tuple(name for name in HASH_NAMES if name != 'source')
RUNTIME_EXECUTION_PATH_NAMES = (
    'model', 'index', 'hubert', 'demucs_repo', 'rvc_script', 'rvc_upstream',
    'media_stage', 'worker', 'render_plan', 'stage_executor', 'worker_python',
    'worker_script', 'inference_lock',
)
RUNTIME_CONTEXT_NAMES = ('execution_paths', 'artifact_root', 'limits', 'parameter_policy')
RUNTIME_LIMIT_NAMES = ('max_duration_s', 'max_download_bytes')


@dataclass(frozen=True)
class RuntimeInventory:
    hashes: Dict[str, str]
    versions: Dict[str, str]


@dataclass(frozen=True)
class Inventory:
    hashes: Dict[str, str]
    versions: Dict[str, str]


def _generation_path(value: object, label: str) -> str:
    try:
        normalized = os.fspath(value)
    except TypeError as exc:
        raise InventoryError('runtime_context_invalid', label + ' must be an absolute path') from exc
    if (not isinstance(normalized, str) or not Path(normalized).is_absolute()
            or not 1 <= len(normalized) <= 4096 or '://' in normalized
            or any(ord(character) < 32 for character in normalized)):
        raise InventoryError('runtime_context_invalid', label + ' must be a bounded absolute path')
    return normalized


def _normalize_policy(value: object, *, depth: int = 0):
    if depth > 8:
        raise InventoryError('runtime_context_invalid', 'Parameter policy nesting is too deep')
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -(2**63) <= value < 2**63:
            raise InventoryError('runtime_context_invalid', 'Parameter policy integer is out of range')
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InventoryError('runtime_context_invalid', 'Parameter policy numbers must be finite')
        return value
    if isinstance(value, str):
        if len(value) > 2048 or any(ord(character) < 32 for character in value):
            raise InventoryError('runtime_context_invalid', 'Parameter policy string is invalid or oversized')
        return value
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise InventoryError('runtime_context_invalid', 'Parameter policy object is oversized')
        normalized = {}
        for key, child in value.items():
            if (not isinstance(key, str) or
                    not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,127}', key)):
                raise InventoryError('runtime_context_invalid', 'Parameter policy key is invalid')
            normalized[key] = _normalize_policy(child, depth=depth + 1)
        return normalized
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise InventoryError('runtime_context_invalid', 'Parameter policy list is oversized')
        return [_normalize_policy(child, depth=depth + 1) for child in value]
    raise InventoryError('runtime_context_invalid', 'Parameter policy contains an unsupported value')


def runtime_generation(runtime_inventory: RuntimeInventory, context: Mapping[str, object]) -> str:
    """Return one canonical generation for reusable non-source render state."""
    if not isinstance(runtime_inventory, RuntimeInventory):
        raise InventoryError('runtime_inventory_invalid', 'A non-source runtime inventory is required')
    if (not isinstance(runtime_inventory.hashes, dict)
            or set(runtime_inventory.hashes) != set(RUNTIME_HASH_NAMES)
            or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)
                   for value in runtime_inventory.hashes.values())):
        raise InventoryError('runtime_inventory_invalid', 'Runtime content inventory is incomplete')
    if (not isinstance(runtime_inventory.versions, dict)
            or set(runtime_inventory.versions) != set(VERSION_NAMES)
            or any(not isinstance(value, str) or not re.fullmatch(r'[0-9A-Za-z.+_~!-]{1,80}', value)
                   for value in runtime_inventory.versions.values())):
        raise InventoryError('runtime_inventory_invalid', 'Runtime version inventory is incomplete')
    if not isinstance(context, Mapping) or set(context) != set(RUNTIME_CONTEXT_NAMES):
        raise InventoryError('runtime_context_invalid', 'Runtime generation context is incomplete')
    execution = context['execution_paths']
    if not isinstance(execution, Mapping) or set(execution) != set(RUNTIME_EXECUTION_PATH_NAMES):
        raise InventoryError('runtime_context_invalid', 'Runtime execution paths are incomplete')
    normalized_execution = {
        name: _generation_path(execution[name], 'execution_paths.' + name)
        for name in RUNTIME_EXECUTION_PATH_NAMES
    }
    limits = context['limits']
    if not isinstance(limits, Mapping) or set(limits) != set(RUNTIME_LIMIT_NAMES):
        raise InventoryError('runtime_context_invalid', 'Runtime limits are incomplete')
    max_duration = limits['max_duration_s']
    max_download = limits['max_download_bytes']
    if (type(max_duration) is not int or not 30 <= max_duration <= 300
            or type(max_download) is not int or not 1024 <= max_download <= 64 * 1024 * 1024):
        raise InventoryError('runtime_context_invalid', 'Runtime duration/download limits are invalid')
    policy = context['parameter_policy']
    if not isinstance(policy, Mapping) or not policy:
        raise InventoryError('runtime_context_invalid', 'Parameter policy must be a non-empty object')
    document = {
        'schema': 'sing-runtime-generation-v1',
        'hashes': {name: runtime_inventory.hashes[name] for name in RUNTIME_HASH_NAMES},
        'versions': {name: runtime_inventory.versions[name] for name in VERSION_NAMES},
        'context': {
            'execution_paths': normalized_execution,
            'artifact_root': _generation_path(context['artifact_root'], 'artifact_root'),
            'limits': {'max_duration_s': max_duration, 'max_download_bytes': max_download},
            'parameter_policy': _normalize_policy(policy),
        },
    }
    raw = json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                     allow_nan=False).encode('utf-8')
    if len(raw) > 65536:
        raise InventoryError('runtime_context_oversized', 'Runtime generation context exceeds 64KiB')
    return hashlib.sha256(raw).hexdigest()


def _safe_path(path: Path) -> None:
    if not path.is_absolute():
        raise InventoryError('asset_path_invalid', 'Render assets must use absolute paths')
    try:
        chain = (path, *path.parents)
        if any(item.is_symlink() for item in chain):
            raise InventoryError('asset_symlink', 'Symlinked render assets are refused')
    except OSError as exc:
        raise InventoryError('asset_unreadable', 'Render asset metadata could not be read') from exc


def _hash_file(path: Path, digest) -> int:
    count = 0
    try:
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                count += len(block)
                digest.update(block)
    except OSError as exc:
        raise InventoryError('asset_unreadable', 'Render asset bytes could not be read') from exc
    return count


def validate_demucs_repo(root: Path) -> Path:
    """Check Demucs 4 LocalRepo+BagOnlyRepo's exact htdemucs lookup.

    Its bag references signature 955717e8 and LocalRepo resolves the signed
    checkpoint filename 955717e8-8726e21a.th.  No torch import or deserialization
    occurs during this bounded structural check; Demucs verifies checksum on load.
    """
    root = Path(root)
    _safe_path(root)
    if not root.is_dir():
        raise InventoryError('demucs_repo_missing', 'Configured local Demucs repo is not a directory')
    expected = {'htdemucs.yaml', '955717e8-8726e21a.th'}
    try:
        entries = list(root.iterdir())
        if {path.name for path in entries} != expected:
            raise InventoryError('demucs_repo_invalid', 'Demucs repo must contain only the htdemucs bag and referenced checkpoint')
        if any(path.is_symlink() or not path.is_file() for path in entries):
            raise InventoryError('demucs_repo_invalid', 'Demucs repo requires regular non-symlink files')
        bag, checkpoint = root / 'htdemucs.yaml', root / '955717e8-8726e21a.th'
        if not 0 < bag.stat().st_size <= 512 or checkpoint.stat().st_size <= 0:
            raise InventoryError('demucs_repo_invalid', 'Demucs bag or checkpoint is empty/oversized')
        document = bag.read_text(encoding='utf-8')
        if not re.fullmatch(r'''\s*models:\s*\[\s*(?:'955717e8'|"955717e8"|955717e8)\s*\]\s*''', document):
            raise InventoryError('demucs_repo_invalid', 'htdemucs bag must reference exactly the local 955717e8 checkpoint')
    except (OSError, UnicodeError) as exc:
        raise InventoryError('demucs_repo_invalid', 'Local Demucs repo could not be inspected') from exc
    return root


def sha256_asset(path: Path, *, max_files: int = 20000, source_tree: bool = False) -> str:
    """Hash one regular file or a deterministic, symlink-free directory tree."""
    path = Path(path)
    _safe_path(path)
    if type(max_files) is not int or not 1 <= max_files <= 100000:
        raise ValueError('Invalid inventory file limit')
    if path.is_file():
        digest = hashlib.sha256()
        _hash_file(path, digest)
        return digest.hexdigest()
    if not path.is_dir():
        raise InventoryError('asset_missing', 'A configured render asset is missing')
    digest = hashlib.sha256(b'maibot-asset-tree-v1\0')
    files = 0
    try:
        entries = sorted(path.rglob('*'), key=lambda item: item.relative_to(path).as_posix())
        for item in entries:
            relative = item.relative_to(path).as_posix()
            # Python imports regenerate bytecode and git updates housekeeping;
            # these are not source dependencies. Never exclude actual .py bytes.
            if item.is_symlink():
                raise InventoryError('asset_symlink', 'Symlinked render assets are refused')
            if source_tree and any(part in ('__pycache__', '.git') for part in item.relative_to(path).parts):
                continue
            encoded = relative.encode('utf-8')
            if item.is_dir():
                digest.update(b'D\0' + encoded + b'\0')
                continue
            if not item.is_file():
                raise InventoryError('asset_type_invalid', 'Only regular render asset files are supported')
            files += 1
            if files > max_files:
                raise InventoryError('asset_tree_limit', 'Render asset tree contains too many files')
            digest.update(b'F\0' + encoded + b'\0')
            content = hashlib.sha256()
            size = _hash_file(item, content)
            digest.update(str(size).encode('ascii') + b'\0' + content.digest())
    except InventoryError:
        raise
    except OSError as exc:
        raise InventoryError('asset_unreadable', 'Render asset tree could not be inventoried') from exc
    return digest.hexdigest()


def _version_token(value: object) -> str:
    token = re.sub(r'[^0-9A-Za-z.+_~!-]', '_', str(value).strip())[:80]
    if not token or not re.fullmatch(r'[0-9A-Za-z.+_~!-]{1,80}', token):
        raise InventoryError('runtime_version_invalid', 'A runtime returned an invalid version identifier')
    return token


class RuntimeVersionProbe:
    """Read package metadata without importing model libraries or running inference."""
    def __init__(self, worker_python: Path, *, ffmpeg: str = 'ffmpeg', timeout_s: int = 20):
        self.worker_python = Path(worker_python)
        self.ffmpeg = ffmpeg
        self.timeout_s = timeout_s
        if (not self.worker_python.is_absolute() or not self.worker_python.is_file()
                or type(timeout_s) is not int or not 1 <= timeout_s <= 60):
            raise ValueError('Invalid isolated runtime probe')

    def _run(self, argv) -> str:
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, timeout=self.timeout_s,
                                    check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InventoryError('runtime_probe_failed', 'Isolated runtime version probe failed') from exc
        if result.returncode or len(result.stdout) > 65536 or len(result.stderr) > 65536:
            raise InventoryError('runtime_probe_failed', 'Isolated runtime version probe failed')
        return result.stdout

    def __call__(self) -> Dict[str, str]:
        script = r'''
import importlib.metadata as m, json, sys
names = {
 "numpy": ("numpy",), "torch": ("torch",), "torchaudio": ("torchaudio",),
 "demucs": ("demucs",), "soundfile": ("soundfile",), "librosa": ("librosa",),
 "pyworld": ("pyworld",), "faiss": ("faiss-cpu", "faiss-gpu", "faiss"),
 "fairseq": ("fairseq",), "scipy": ("scipy",),
  "praat-parselmouth": ("praat-parselmouth",), "torchcrepe": ("torchcrepe",),
  "omegaconf": ("omegaconf",), "numba": ("numba",),
}
out = {"python": ".".join(map(str, sys.version_info[:3]))}
for key, candidates in names.items():
    for candidate in candidates:
        try:
            out[key] = m.version(candidate)
            break
        except m.PackageNotFoundError:
            pass
    else:
        raise SystemExit("missing:" + key)
print(json.dumps(out, sort_keys=True))
'''
        raw = self._run([str(self.worker_python), '-I', '-c', script])
        try:
            versions = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise InventoryError('runtime_probe_invalid', 'Runtime package metadata was invalid') from exc
        ffmpeg_raw = self._run([self.ffmpeg, '-version'])
        first = ffmpeg_raw.splitlines()[0].split()
        if len(first) < 3 or first[0] != 'ffmpeg' or first[1] != 'version':
            raise InventoryError('runtime_probe_invalid', 'FFmpeg version output was invalid')
        ffmpeg_version = _version_token(first[2])
        # Prove the exact configured encoder exists.  FFmpeg does not expose a
        # separate libmp3lame ABI version, so bind its identity to this FFmpeg build.
        encoder = self._run([self.ffmpeg, '-hide_banner', '-h', 'encoder=libmp3lame'])
        if 'libmp3lame' not in encoder:
            raise InventoryError('runtime_encoder_missing', 'FFmpeg lacks the required libmp3lame encoder')
        versions['ffmpeg'] = ffmpeg_version
        versions['libmp3lame'] = _version_token('enabled-' + ffmpeg_version)
        if not isinstance(versions, dict) or set(versions) != set(VERSION_NAMES):
            raise InventoryError('runtime_version_missing', 'Runtime inventory is incomplete')
        return {name: _version_token(versions[name]) for name in VERSION_NAMES}


class AssetInventory:
    def __init__(self, paths: AssetPaths, version_provider: Callable[[], Mapping[str, str]]):
        self.paths = paths
        self.version_provider = version_provider
        if not callable(version_provider):
            raise ValueError('A runtime version provider is required')

    def _versions(self) -> Dict[str, str]:
        try:
            supplied = dict(self.version_provider())
        except InventoryError:
            raise
        except Exception as exc:
            raise InventoryError('runtime_probe_failed', 'Runtime version inventory failed') from exc
        if set(supplied) != set(VERSION_NAMES):
            raise InventoryError('runtime_version_missing', 'Runtime inventory is incomplete')
        return {name: _version_token(supplied[name]) for name in VERSION_NAMES}

    def build_runtime(self) -> RuntimeInventory:
        """Hash every reusable non-source input without inventing a fake source."""
        assets = self.paths.documents()
        if set(assets) != set(RUNTIME_HASH_NAMES):
            raise InventoryError('asset_inventory_invalid', 'Runtime asset inventory is incomplete')
        if self.paths.rvc_upstream != self.paths.rvc_script.parent / 'upstream':
            raise InventoryError('rvc_upstream_mismatch', 'Inventoried RVC upstream is not the wrapper-imported sibling')
        validate_demucs_repo(self.paths.demucs_repo)
        hashes = {name: sha256_asset(assets[name], source_tree=name == 'rvc_upstream')
                  for name in RUNTIME_HASH_NAMES}
        return RuntimeInventory(hashes=hashes, versions=self._versions())

    def build(self, source: Path) -> Inventory:
        runtime = self.build_runtime()
        hashes = {'source': sha256_asset(Path(source)), **runtime.hashes}
        if set(hashes) != set(HASH_NAMES):
            raise InventoryError('asset_inventory_invalid', 'Render asset inventory is incomplete')
        return Inventory(hashes=hashes, versions=dict(runtime.versions))
