"""Create a verified, human-browsable hardlink view of committed cover cache entries.

Synchronous disk I/O: callers in an event loop must run these functions in a thread.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import unicodedata
import uuid

KEY = re.compile(r'[0-9a-f]{64}\Z')
IROHA_MODEL_SHA256 = '01f2ee572103e0770b896c8a73acce9ed477fcf74d804bb441420c1d8e049319'
MARKER = '<!-- maibot-sing-library-catalog-v1 -->'


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _read_metadata(path: Path) -> dict[str, Any]:
    if not _regular(path) or path.stat().st_size > 1024 * 1024:
        raise ValueError(f'Unsafe or oversized metadata: {path}')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except RecursionError as exc:
        raise ValueError('Excessively nested metadata') from exc
    if not isinstance(data, dict):
        raise ValueError(f'Invalid metadata: {path}')
    return data


def _verify(root: Path, key: str, *, max_bytes: int = 64*1024**2) -> tuple[dict[str, Any], Path]:
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise ValueError('Invalid cache key')
    folder = root / key
    if not _directory(folder):
        raise ValueError(f'Unsafe or missing cache directory: {key}')
    data = _read_metadata(folder / 'metadata.json')
    mp3 = folder / 'cover.mp3'
    if not _regular(mp3):
        raise ValueError(f'Unsafe or missing cover: {key}')
    size = mp3.stat().st_size
    if type(max_bytes) is not int or not 0 < size <= max_bytes <= 64*1024**2:
        raise ValueError('Cover exceeds the bounded verification size')
    sha = data.get('sha256')
    if data.get('status') != 'completed' or data.get('key') != key or not isinstance(sha, str) or not KEY.fullmatch(sha):
        raise ValueError(f'Uncommitted or invalid cover: {key}')
    if _sha256(mp3) != sha:
        raise ValueError(f'Cover digest mismatch: {key}')
    try:
        source = data['source']
        model = data['model_sha256']
        index = data['index_sha256']
        parameters = data['parameters']
        instrumental = data['instrumental']
        if (not isinstance(source, dict) or not isinstance(parameters, dict)
                or not isinstance(instrumental, bool) or not all(isinstance(v, str) and KEY.fullmatch(v)
                    for v in (model, index))):
            raise ValueError('Invalid cache identity fields')
        if 'recipe_schema' in data:
            # One shared validator owns the current recipe schema and full
            # provenance checks. Unknown schemas must never fall through to
            # the legacy cache identity path or a second hardcoded version.
            from ..runtime.artifact_manifest import validate_manifest
            validate_manifest(data, key, size)
            return data, mp3
        else:
            identity = {'source': source, 'model_sha256': model, 'index_sha256': index,
                        'parameters': parameters, 'instrumental': instrumental}
            serialized = json.dumps(identity, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if hashlib.sha256(serialized.encode('utf-8')).hexdigest() != key:
            raise ValueError('Cache identity mismatch')
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'Invalid cache identity: {key}') from exc
    return data, mp3


def _label(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError('Missing artist or title')
    if re.search(r'(?i)(?:https?://|www\.|(?:token|signature|authorization|credential)\s*[=:])', value):
        raise ValueError('URL or credential-like artist/title refused in public catalog')
    value = unicodedata.normalize('NFKC', value)
    # Remove controls, bidi overrides, format characters and filesystem metacharacters;
    # do not allow a metadata value to create another path component.
    value = ''.join(' ' if (unicodedata.category(ch).startswith('C') or ch in '/\\:*?"<>|`'
                         or unicodedata.category(ch) in ('Zl', 'Zp')) else ch for ch in value)
    value = ' '.join(value.split()).strip(' .-')
    if not value:
        raise ValueError('Empty safe artist or title')
    while len(value.encode('utf-8')) > 90:
        value = value[:-1]
    return value.strip(' .-')


def _seconds(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('Invalid clip duration or boundary')
    minutes, seconds = divmod(int(round(value)), 60)
    return f'{minutes:02d}分{seconds:02d}秒'


def _excerpt(data: dict[str, Any]) -> str:
    recipe = data.get('recipe')
    if isinstance(recipe, dict) and recipe.get('render_mode') == 'excerpt':
        from ..runtime.excerpt_selection import validate_selection, ExcerptSelectionError
        try:
            selection = validate_selection(data.get('selection'))
        except ExcerptSelectionError as exc:
            raise ValueError('Invalid catalog selection evidence') from exc
        ranges = selection['source_ranges']
        rate = selection['sample_rate']
        def timestamp(frame):
            milliseconds = (frame*1000 + rate//2)//rate
            minutes, remainder = divmod(milliseconds, 60000)
            seconds, fraction = divmod(remainder, 1000)
            return f'{minutes:02d}分{seconds:02d}.{fraction:03d}秒'
        bounds = '+'.join(f'{timestamp(item["start_frame"])}至{timestamp(item["end_frame"])}'
                          for item in ranges)
        return bounds + ('拼接' if len(ranges) > 1 else '片段')
    source = data['source']
    if source.get('type') == 'local':
        provenance = data.get('excerpt_provenance')
        clip = provenance.get('clip') if isinstance(provenance, dict) else None
        if isinstance(clip, dict) and 'start_s' in clip and 'end_s' in clip:
            start, end = clip['start_s'], clip['end_s']
            if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, (int, float))
                    or not isinstance(end, (int, float)) or not math.isfinite(start) or not math.isfinite(end)
                    or start < 0 or end <= start):
                raise ValueError('Invalid excerpt boundaries')
            return f'{_seconds(start)}至{_seconds(end)}'
        if 'duration_s' in data:
            return f'{_seconds(data["duration_s"])}片段-起点未标注'
        return '本地片段-范围未标注'
    if source.get('type') == 'musicdl-native':
        return '源音频-范围未核验'
    return '源音频-范围未知'


def _entry(data: dict[str, Any]) -> dict[str, str]:
    key = data['key']
    raw_title = data['title']
    provenance = data.get('excerpt_provenance')
    if isinstance(raw_title, str) and isinstance(provenance, dict) and isinstance(provenance.get('clip'), dict):
        raw_title = re.sub(r'\s+\[\d{2}:\d{2}-\d{2}:\d{2}\]$', '', raw_title)
    artist, title = _label(data['artist']), _label(raw_title)
    voice = '枣伊吕波' if data['model_sha256'] == IROHA_MODEL_SHA256 else '音色-' + data['model_sha256'][:12]
    excerpt = _excerpt(data)
    accompaniment = '带伴奏' if data['instrumental'] else '纯人声'
    return {'key': key, 'sha256': data['sha256'], 'artist': artist, 'title': title,
            'voice': voice, 'excerpt': excerpt, 'accompaniment': accompaniment}


def _name(entry: dict[str, str], suffix: str) -> str:
    fixed = f' - {entry["voice"]} - {entry["excerpt"]} - {entry["accompaniment"]} - {suffix}.mp3'
    prefix = f'{entry["artist"]} - {entry["title"]}'
    while len((prefix + fixed).encode('utf-8')) > 240:
        prefix = prefix[:-1]
    return prefix.strip(' .-') + fixed


def _managed_text(path: Path, contents: str, *, index: bool = False) -> None:
    if path.exists() or path.is_symlink():
        if not _regular(path):
            raise ValueError(f'Unsafe catalog file: {path}')
        current = path.read_text(encoding='utf-8')
        if index:
            try:
                valid = json.loads(current).get('format') == 'maibot-sing-library-v1'
            except (ValueError, AttributeError):
                valid = False
        else:
            valid = current.startswith(MARKER)
        if not valid:
            raise ValueError(f'Refusing to overwrite unrelated file: {path}')
        if current == contents:
            return
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _locked(root: Path) -> Iterator[Path]:
    if not root.is_absolute() or not _directory(root):
        raise ValueError('Library root must be an existing absolute real directory')
    # Check ancestors too: a symlinked library parent must not redirect writes.
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError('Symlinked library path refused')
    lock = root / '.catalog.lock'
    if lock.is_symlink() or (lock.exists() and not _regular(lock)):
        raise ValueError('Unsafe catalog lock')
    with lock.open('a+b') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield root


def _rebuild_locked(root: Path, *, skip_corrupt: bool = False,
                    warnings: list[str] | None = None) -> list[dict[str, str]]:
    songs = root / 'songs'
    if songs.is_symlink() or (songs.exists() and not _directory(songs)):
        raise ValueError('Unsafe songs directory')
    songs.mkdir(exist_ok=True)
    entries: list[dict[str, str]] = []
    occupied = {p.name.casefold(): p.name for p in songs.iterdir()}
    for folder in sorted(root.iterdir()):
        if not KEY.fullmatch(folder.name):
            continue
        try:
            data, source = _verify(root, folder.name)
            entry = _entry(data)
        except (ValueError, KeyError, TypeError) as exc:
            if not skip_corrupt:
                raise
            if warnings is not None:
                warnings.append('跳过损坏或不兼容的成品 ' + folder.name[:16] + ': ' + type(exc).__name__)
            continue
        name = _name(entry, entry['key'][:16])
        if name.casefold() in occupied and occupied[name.casefold()] != name:
            name = _name(entry, entry['key'])
        target = songs / name
        if name.casefold() in occupied and occupied[name.casefold()] != name:
            raise ValueError(f'Casefold filename collision: {name}')
        if target.exists() or target.is_symlink():
            if not _regular(target) or not os.path.samefile(source, target) or _sha256(target) != entry['sha256']:
                raise ValueError(f'Refusing to overwrite unrelated or corrupted file: {target}')
        else:
            # Hardlinks are exclusive and byte-identical; EXDEV is deliberately fatal,
            # rather than risking an interrupted cross-filesystem copy.
            os.link(source, target, follow_symlinks=False)
        if (not _regular(source) or not _regular(target) or not os.path.samefile(source, target)
                or _sha256(source) != entry['sha256'] or _sha256(target) != entry['sha256']):
            raise ValueError(f'Cover changed during indexing: {entry["key"]}')
        occupied[name.casefold()] = name
        entry['file'] = 'songs/' + name
        entries.append(entry)
    index = {'format': 'maibot-sing-library-v1', 'entries': entries}
    _managed_text(root / 'library-index.json', json.dumps(index, ensure_ascii=False, indent=2) + '\n', index=True)
    lines = [MARKER, '# 翻唱成品库', '', '在 songs 目录按歌手、歌名、音色、片段和伴奏模式找文件；末尾短 ID 用于区分版本。',
             '这里是原成品的硬链接，不重新编码、不额外复制音频数据；删除一个名称不会删除另一个，原地改写或改权限则会影响两者。',
             '新选段源时间显示到毫秒（舍入），+ 表示按顺序拼接；metadata.json 保留阶段回执与精确源帧半开区间 [起点,终点)。旧来源范围未独立核验，未记录起点的片段不冒充整曲。', '',
             '| 歌手 | 歌名 | 音色 | 范围 | 模式 | 音频 |', '| --- | --- | --- | --- | --- | --- |']
    for entry in entries:
        fields = [entry[field].replace('|', '\\|').replace('[', '\\[').replace(']', '\\]')
                  for field in ('artist', 'title', 'voice', 'excerpt', 'accompaniment')]
        lines.append('| ' + ' | '.join(fields) + f' | [打开音频](<{entry["file"]}>) |')
    _managed_text(root / 'README.md', '\n'.join(lines) + '\n')
    return entries


def rebuild_library(root: Path, *, skip_corrupt: bool = False,
                    warnings: list[str] | None = None) -> list[dict[str, str]]:
    """Reconcile verified covers into songs/ without overwriting unrelated files.

    Legacy default stays strict. With skip_corrupt=True, individually invalid
    artifact metadata is reported and skipped; unsafe songs/ or summary files
    still fail closed. Run from async code via asyncio.to_thread.
    """
    with _locked(Path(root)) as library:
        return _rebuild_locked(library, skip_corrupt=skip_corrupt, warnings=warnings)


def index_cover(root: Path, key: str) -> dict[str, str]:
    """After a completed publish, verify *key* and reconcile the library under a lock.

    Presently scans all committed entries so an interrupted prior run is recovered.
    """
    with _locked(Path(root)) as library:
        _verify(library, key)
        for entry in _rebuild_locked(library):
            if entry['key'] == key:
                return entry
    raise RuntimeError('Verified key disappeared during catalog rebuild')
