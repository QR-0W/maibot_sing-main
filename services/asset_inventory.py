"""Hash the real render inputs and record the isolated runtime versions.

The inventory is deliberately built only after the exact selected source has
been downloaded.  It never accepts declared digests and never stores playback
URLs, credentials, chat identities, or private filesystem paths in the recipe.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping
import hashlib
import json
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
    demucs_weights: Path
    rvc_script: Path
    rvc_upstream: Path
    media_stage: Path
    worker: Path
    render_plan: Path
    stage_executor: Path

    def documents(self) -> Dict[str, Path]:
        return {name: Path(getattr(self, name)) for name in HASH_NAMES if name != 'source'}


@dataclass(frozen=True)
class Inventory:
    hashes: Dict[str, str]
    versions: Dict[str, str]


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


def sha256_asset(path: Path, *, max_files: int = 20000) -> str:
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
            if item.is_symlink():
                raise InventoryError('asset_symlink', 'Symlinked render assets are refused')
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
 "fairseq": ("fairseq",),
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

    def build(self, source: Path) -> Inventory:
        assets = {'source': Path(source), **self.paths.documents()}
        if set(assets) != set(HASH_NAMES):
            raise InventoryError('asset_inventory_invalid', 'Render asset inventory is incomplete')
        hashes = {name: sha256_asset(path) for name, path in assets.items()}
        try:
            supplied = dict(self.version_provider())
        except InventoryError:
            raise
        except Exception as exc:
            raise InventoryError('runtime_probe_failed', 'Runtime version inventory failed') from exc
        if set(supplied) != set(VERSION_NAMES):
            raise InventoryError('runtime_version_missing', 'Runtime inventory is incomplete')
        versions = {name: _version_token(supplied[name]) for name in VERSION_NAMES}
        return Inventory(hashes=hashes, versions=versions)
