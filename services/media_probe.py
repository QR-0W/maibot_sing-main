"""Validate bounded downloaded audio without treating duration as recording identity."""
from pathlib import Path
from typing import Any, Dict
import asyncio
import json
import math

from .catalogue_service import CatalogueError
from .source_offer import CatalogueItem


async def probe_download(path: Path, selected: CatalogueItem,
                         *, max_seconds: int = 300) -> Dict[str, Any]:
    """Inspect the locally downloaded bytes after exact-ID resolution.

    Matching duration rules out some previews, not a wrong live/studio take.
    The selected provider+track ID remains the sole claimed release identity.
    """
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise CatalogueError('source_missing','已选曲目的下载文件不存在。')
    if type(max_seconds) is not int or not 30 <= max_seconds <= 300:
        raise ValueError('Invalid media duration limit')
    process=await asyncio.create_subprocess_exec('ffprobe','-v','error',
        '-show_format','-show_streams','-of','json',str(path),
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        raw,errors=await asyncio.wait_for(process.communicate(),20)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise CatalogueError('source_probe_timeout','已选曲目的媒体检测超时，没有改选其他版本。') from exc
    if process.returncode or not raw or len(raw)>65536 or len(errors)>8192:
        raise CatalogueError('source_invalid_media','已选曲目的下载内容不是可验证的完整音频。')
    try:
        record=json.loads(raw)
        streams=record['streams']
        audio=[stream for stream in streams if isinstance(stream,dict) and stream.get('codec_type')=='audio']
        duration=float(record['format']['duration'])
    except (ValueError,KeyError,TypeError) as exc:
        raise CatalogueError('source_invalid_media','已选曲目的媒体格式或时长无效。') from exc
    if not audio or not math.isfinite(duration) or not 30 <= duration <= max_seconds:
        raise CatalogueError('source_duration_limit','媒体无有效完整音轨，或超出本机时长范围。')
    expected=selected.duration_s
    if expected is not None and abs(duration-expected)>max(3.,expected*.02):
        raise CatalogueError('source_duration_mismatch','下载时长与所选曲目列表不符；不能据此判断录音身份。')
    return {'duration_s':duration,'audio_streams':len(audio),'source_id':selected.track_id,
            'provider':selected.provider}
