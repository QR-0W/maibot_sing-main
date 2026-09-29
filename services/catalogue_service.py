"""One source boundary: inherited search/login client and exact-ID playback resolution.

This layer does not render, send messages or infer rights from a search result.
The same injected client is used before and after selection; callers own its life.
"""
from typing import List, Optional
import asyncio
import re

from ..music.search import AudioSource, MusicSearchClient, MusicSearchError
from .source_offer import CatalogueItem


class CatalogueError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def explicit_version(title: str) -> str:
    # Only explicit release tags, not album popularity, duration or shortest take.
    match = re.search(r'(?:[\[(（]|\s-\s)\s*(live|acoustic|instrumental|remix|现场|不插电|伴奏)(?:\b|[）)\]])', title, re.I)
    if not match:
        return 'unspecified'
    label = match[1].lower()
    return {'现场':'live','不插电':'acoustic','伴奏':'instrumental'}.get(label,label)


class CatalogueService:
    def __init__(self, client: MusicSearchClient, *, timeout_s: float = 20, max_duration_s: int = 300):
        if not 1 <= timeout_s <= 60 or not 30 <= max_duration_s <= 300:
            raise ValueError('Invalid source limits')
        self.client = client
        self.timeout_s = timeout_s
        self.max_duration_s = max_duration_s

    async def search(self, query: str, provider: str = '163', limit: int = 5) -> List[CatalogueItem]:
        try:
            songs = await asyncio.wait_for(self.client.search(query,provider,limit=limit),self.timeout_s)
        except asyncio.TimeoutError as exc:
            raise CatalogueError('source_search_timeout','音乐搜索超时；没有启动翻唱。') from exc
        except MusicSearchError as exc:
            raise CatalogueError(exc.code,'音乐平台搜索失败；不能据此判断歌曲不存在。') from exc
        result = []
        by_id = {}
        for song in songs:
            if song.platform != provider:
                raise CatalogueError('source_protocol_error','音乐平台返回了不匹配的来源。')
            item = CatalogueItem(provider=provider,track_id=song.song_id,title=song.name,
                artist=song.artists or '艺人未标注',album=song.album,duration_s=song.duration_s,
                availability='over_limit' if song.duration_s and song.duration_s > self.max_duration_s else 'unknown',
                version=explicit_version(song.name),media_id=song.media_id)
            identity = (provider,item.track_id)
            if identity in by_id:
                if by_id[identity] != item:
                    raise CatalogueError('source_identity_conflict','同一个曲目 ID 返回了互相矛盾的信息。')
                continue
            by_id[identity] = item
            result.append(item)
        # Empty is different from inaccessible: search never calls playback API.
        return result[:limit]

    async def resolve(self, chosen: CatalogueItem) -> AudioSource:
        if chosen.availability == 'over_limit':
            raise CatalogueError('source_duration_limit','所选曲目超过本机时长上限，未换用其他版本。')
        try:
            audio = await asyncio.wait_for(
                self.client.resolve_audio(chosen.track_id,chosen.provider,chosen.media_id),self.timeout_s)
        except asyncio.TimeoutError as exc:
            raise CatalogueError('source_resolution_timeout','所选曲目取链超时，未换用其他版本。') from exc
        except MusicSearchError as exc:
            raise CatalogueError(exc.code,'所选曲目取链失败；请检查插件内音乐登录状态或平台状态。') from exc
        if (audio.platform,audio.song_id) != (chosen.provider,chosen.track_id):
            raise CatalogueError('source_identity_mismatch','音频来源与选定曲目不符，已拒绝。')
        if audio.is_preview:
            raise CatalogueError('source_preview','所选曲目仅返回试听片段；未下载，也未换成另一版。')
        if not audio.url:
            raise CatalogueError('source_unavailable','歌曲已找到，但平台未为插件当前登录态返回音频链接；请登录或明确改选。')
        return audio  # Ephemeral only; never place this URL in a candidate snapshot.
