"""Opaque catalogue label validation and conservative comparison keys.

Song, artist and album labels are data, not filesystem paths or shell commands.
Keep their spelling for display/search and normalize only harmless formatting for
comparison. Recording qualifiers and punctuation remain significant.
"""
import re
import unicodedata


_PATH_SEGMENT = re.compile(r'(?:^|/)\.{1,2}(?:/|$)')
_DRIVE_PATH = re.compile(r'^[A-Za-z]:(?:[/\\]|$)')
_URI_PREFIX = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*://')


def _has_controls(value: str) -> bool:
    return any(unicodedata.category(character) in ('Cc', 'Cs', 'Zl', 'Zp')
               for character in value)


def validate_catalogue_text(value: str) -> str:
    """Return trimmed literal text while rejecting explicit path-like input."""
    if not isinstance(value, str):
        raise ValueError('歌名/艺人/专辑必须是文字')
    if _has_controls(value):
        raise ValueError('歌名/艺人/专辑含控制字符或换行')
    clean = value.strip()
    if not clean or len(clean) > 180:
        raise ValueError('歌名/艺人/专辑为空或过长')
    view = unicodedata.normalize('NFKC', clean).strip()
    path_view = re.sub(r'\s*/\s*', '/', view)
    if (not view or _has_controls(view) or view.startswith(('/', '~/', '~\\'))
            or '\\' in view or _DRIVE_PATH.match(view) or _URI_PREFIX.match(view)
            or _PATH_SEGMENT.search(path_view)):
        raise ValueError('不接受文件路径或URL作为歌名/艺人/专辑')
    return clean


def normalized_catalogue_text(value: str) -> str:
    """Normalize case/spacing and slash spacing without removing identity data."""
    folded = ' '.join(unicodedata.normalize('NFKC', value).casefold().split())
    return re.sub(r'\s*/\s*', '/', folded)
