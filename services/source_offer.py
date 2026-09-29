"""Serializable catalogue choices; availability is distinct from search visibility."""
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional
import math
import unicodedata


@dataclass(frozen=True)
class CatalogueItem:
    provider: str
    track_id: str
    title: str
    artist: str
    album: str
    duration_s: Optional[float] = None
    availability: str = 'unknown'
    version: str = 'unspecified'
    media_id: str = ''  # QQ Music resource identifier, not an access credential.

    def __post_init__(self):
        for value in (self.provider, self.track_id, self.title, self.artist):
            if not isinstance(value, str) or not value.strip() or len(value) > 300 or any(ord(c) < 32 for c in value):
                raise ValueError('Invalid catalogue text')
        if not isinstance(self.album, str) or len(self.album) > 300 or any(ord(c) < 32 for c in self.album):
            raise ValueError('Invalid album')
        if not isinstance(self.media_id, str) or len(self.media_id) > 100 or any(ord(c) < 32 for c in self.media_id):
            raise ValueError('Invalid media resource ID')
        if self.availability not in ('unknown','available','requires_login','unavailable','over_limit'):
            raise ValueError('Invalid availability')
        if self.version not in ('unspecified','studio','live','acoustic','instrumental','remix'):
            raise ValueError('Invalid version')
        if self.duration_s is not None and (isinstance(self.duration_s,bool) or not isinstance(self.duration_s,(int,float)) or not math.isfinite(self.duration_s) or self.duration_s <= 0):
            raise ValueError('Invalid duration')

    def document(self) -> Dict:
        # No raw provider response, playback URL, cookie or headers are persisted.
        return asdict(self)


def normalized(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC',value).casefold().split())


def default_row(items: List[CatalogueItem], *, mode: str,
                requested_artist: Optional[str] = None, requested_version: Optional[str] = None) -> Optional[int]:
    """Return a one-based row, or require selection. Never skip first to a playable substitute.

    Provider ranking is preserved. First result is a usability default, not proof
    of studio provenance. An unavailable first item stays selected so the caller
    can explain its actual access problem instead of changing the recording.
    """
    if mode not in ('first','manual'):
        raise ValueError('Unknown selection mode')
    if not items or (mode == 'manual' and len(items) > 1):
        return None
    first = items[0]
    if requested_artist and normalized(first.artist) != normalized(requested_artist):
        return None
    if requested_version and first.version != requested_version:
        return None
    return 1


def snapshot(items: List[CatalogueItem]) -> List[Dict]:
    if not 1 <= len(items) <= 25:
        raise ValueError('Offer must contain 1–25 results')
    seen = set()
    result = []
    for item in items:
        identity = (item.provider,item.track_id)
        if identity in seen:
            raise ValueError('Duplicate catalogue identity in offer')
        seen.add(identity)
        result.append(item.document())
    return result
