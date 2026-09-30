"""Validate bounded downloaded audio without treating duration as recording identity."""
from pathlib import Path
from typing import Any, Dict
import asyncio
import json
import math

from .catalogue_service import CatalogueError
from .source_offer import CatalogueItem


_SAMPLE_RATE = 44100
_CHANNELS = 2
_SAMPLE_BYTES = 4
_FRAME_BYTES = _CHANNELS * _SAMPLE_BYTES
_FFPROBE_TIMEOUT_S = 20
_FFMPEG_TIMEOUT_S = 120
_STDOUT_LIMIT = 65536
_STDERR_LIMIT = 8192
_PROCESS_CLEANUP_S = 3


class _OutputLimit(RuntimeError):
    pass


class _DecodedTooLong(RuntimeError):
    pass


async def _read_limited(stream, limit):
    output = bytearray()
    while True:
        block = await stream.read(min(8192,limit-len(output)+1))
        if not block:
            return bytes(output)
        output.extend(block)
        if len(output)>limit:
            raise _OutputLimit


async def _count_pcm(stream, max_bytes):
    amount = 0
    while True:
        block = await stream.read(256*1024)
        if not block:
            return amount
        amount += len(block)
        if amount>max_bytes:
            raise _DecodedTooLong


async def _drain_pipe(stream):
    try:
        while await stream.read(256*1024):
            pass
    except (BrokenPipeError,ConnectionError,RuntimeError):
        pass


def _close_pipe_transport(stream):
    transport=getattr(stream,'_transport',None)
    if transport is not None:
        try:
            transport.close()
        except BaseException:
            pass


async def _cleanup_owned(process, readers, waited):
    """Kill, drain and reap our one child without leaving reader/wait tasks."""
    async def settle_reader(task,stream):
        await asyncio.gather(task,return_exceptions=True)
        at_eof=getattr(stream,'at_eof',None)
        if at_eof is None or not at_eof():
            await _drain_pipe(stream)

    async def finish():
        if process.returncode is None:
            try:
                process.kill()
            except OSError:
                pass
        output,errors=readers
        settling=[asyncio.create_task(settle_reader(output,process.stdout),
                                      name='media-probe-clean-stdout'),
                  asyncio.create_task(settle_reader(errors,process.stderr),
                                      name='media-probe-clean-stderr')]
        try:
            await asyncio.wait_for(
                asyncio.gather(*settling,waited,return_exceptions=True),
                _PROCESS_CLEANUP_S)
        except asyncio.TimeoutError:
            # A killed child can still have a paused pipe transport. Close both
            # transports before cancelling our tasks so no protocol task remains.
            _close_pipe_transport(process.stdout)
            _close_pipe_transport(process.stderr)
            transport=getattr(process,'_transport',None)
            if transport is not None:
                try:
                    transport.close()
                except BaseException:
                    pass
            for task in (*settling,*readers,waited):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*settling,*readers,waited,return_exceptions=True)

    cleanup=asyncio.create_task(finish(),name='media-probe-cleanup')
    # A second caller cancellation must not interrupt ownership cleanup. The
    # cleanup coroutine itself is bounded and leaves no background reaper.
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
    await asyncio.gather(cleanup,return_exceptions=True)


async def _execute(argv, output_reader, *, timeout_s, pipe_limit):
    process=await asyncio.create_subprocess_exec(*argv,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,limit=pipe_limit)
    output=asyncio.create_task(output_reader(process.stdout),
                               name='media-probe-stdout')
    errors=asyncio.create_task(_read_limited(process.stderr,_STDERR_LIMIT),
                               name='media-probe-stderr')
    waited=asyncio.create_task(process.wait(),name='media-probe-wait')
    owned=(output,errors,waited)
    try:
        async with asyncio.timeout(timeout_s):
            done,_=await asyncio.wait(owned,return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exception=task.exception()
                if exception is not None:
                    raise exception
            values=await asyncio.gather(*owned)
            return values[0],values[1],values[2]
    except BaseException:
        await _cleanup_owned(process,(output,errors),waited)
        raise


async def _metadata(path):
    try:
        raw,errors,returncode=await _execute([
            'ffprobe','-v','error','-show_entries',
            'format=duration:stream=codec_type','-of','json',str(path)],
            lambda stream:_read_limited(stream,_STDOUT_LIMIT),
            timeout_s=_FFPROBE_TIMEOUT_S,pipe_limit=_STDOUT_LIMIT)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        raise CatalogueError('source_probe_timeout',
            '已选曲目的媒体检测超时，没有改选其他版本。') from exc
    except (OSError,_OutputLimit) as exc:
        raise CatalogueError('source_invalid_media',
            '已选曲目的媒体元数据无法安全读取。') from exc
    if returncode or not raw or errors:
        raise CatalogueError('source_invalid_media','已选曲目的下载内容不是可验证的完整音频。')
    try:
        record=json.loads(raw)
        streams=record['streams']
        audio=[stream for stream in streams
               if isinstance(stream,dict) and stream.get('codec_type')=='audio']
    except (ValueError,KeyError,TypeError) as exc:
        raise CatalogueError('source_invalid_media','已选曲目的媒体格式无效。') from exc
    container_duration=None
    try:
        candidate=float(record.get('format',{}).get('duration'))
        if math.isfinite(candidate) and candidate>=0:
            container_duration=candidate
    except (ValueError,TypeError):
        pass
    if not audio:
        raise CatalogueError('source_duration_limit','媒体无有效完整音轨，或超出本机时长范围。')
    return len(audio),container_duration


async def _decoded_frames(path, max_seconds):
    max_bytes=max_seconds*_SAMPLE_RATE*_FRAME_BYTES
    try:
        amount,errors,returncode=await _execute([
            'ffmpeg','-nostdin','-v','error','-xerror','-threads','1',
            '-max_alloc',str(64*1024*1024),'-i',str(path),'-map','0:a:0',
            '-ar',str(_SAMPLE_RATE),'-ac',str(_CHANNELS),'-c:a','pcm_f32le',
            '-f','f32le','pipe:1'],
            lambda stream:_count_pcm(stream,max_bytes),
            timeout_s=_FFMPEG_TIMEOUT_S,pipe_limit=256*1024)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        raise CatalogueError('source_probe_timeout',
            '已选曲目的受控解码超时，没有改选其他版本。') from exc
    except _DecodedTooLong as exc:
        raise CatalogueError('source_duration_limit',
            '媒体无有效完整音轨，或超出本机时长范围。') from exc
    except (OSError,_OutputLimit) as exc:
        raise CatalogueError('source_invalid_media',
            '已选曲目的受控解码输出无效。') from exc
    if returncode or errors or amount<=0 or amount%_FRAME_BYTES:
        raise CatalogueError('source_invalid_media','已选曲目的下载内容不能完整解码。')
    return amount//_FRAME_BYTES


async def probe_download(path: Path, selected: CatalogueItem,
                         *, max_seconds: int = 300) -> Dict[str, Any]:
    """Count normalized decoded frames before planning any model work.

    Container duration remains diagnostic only. Limits and catalogue-duration
    agreement use the streamed 44.1 kHz stereo float PCM frame count.
    """
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise CatalogueError('source_missing','已选曲目的下载文件不存在。')
    if type(max_seconds) is not int or not 30 <= max_seconds <= 300:
        raise ValueError('Invalid media duration limit')
    audio_streams,container_duration=await _metadata(path)
    frames=await _decoded_frames(path,max_seconds)
    if not 30*_SAMPLE_RATE<=frames<=max_seconds*_SAMPLE_RATE:
        raise CatalogueError('source_duration_limit','媒体无有效完整音轨，或超出本机时长范围。')
    duration=frames/_SAMPLE_RATE
    expected=selected.duration_s
    if expected is not None and abs(duration-expected)>max(3.,expected*.02):
        raise CatalogueError('source_duration_mismatch',
            '下载时长与所选曲目列表不符；不能据此判断录音身份。')
    return {'duration_s':duration,'container_duration_s':container_duration,
            'frames':frames,'sample_rate':_SAMPLE_RATE,'audio_streams':audio_streams,
            'source_id':selected.track_id,'provider':selected.provider}
