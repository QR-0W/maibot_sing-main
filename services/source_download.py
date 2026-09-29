"""Stream an explicitly selected provider ID into a private stage workspace.

This transport never searches, replaces a recording, downloads a full body into
RAM or accepts an arbitrary redirect. Caller must ffprobe/decode the result,
check expected duration and validate recording identity before rendering.
"""
import asyncio
import hashlib
import os
from pathlib import Path
import re
import stat
import threading
from urllib.parse import urljoin, urlsplit
import uuid

import httpx

from .catalogue_service import CatalogueError, CatalogueService
from .source_offer import CatalogueItem


class DownloadError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


_HOST_SUFFIXES = {
    '163': ('.music.126.net', '.music.163.com'),
    'qq': ('.qqmusic.qq.com',),
}


def _safe_url(raw, provider):
    """Reject credentials, private hosts, non-TLS and non-provider redirects."""
    if provider not in _HOST_SUFFIXES or not isinstance(raw, str) or len(raw) > 4096:
        raise DownloadError('source_url_rejected', 'Untrusted playback location')
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        if (parsed.scheme != 'https' or not host or parsed.username or parsed.password or
                parsed.port not in (None, 443) or not any(host.endswith(suffix) and host != suffix[1:]
                for suffix in _HOST_SUFFIXES[provider]) or parsed.fragment):
            raise DownloadError('source_url_rejected', 'Playback host is outside the configured provider')
    except ValueError as exc:
        raise DownloadError('source_url_rejected','Invalid playback location') from exc
    return raw


def _prepare_destination(workspace, target, max_bytes):
    """Validate/create the private workspace before resolving provider media."""
    if (not workspace.is_absolute() or
            any(p.is_symlink() for p in workspace.parents)):
        raise ValueError('Download workspace must be private absolute non-symlink directory')
    workspace.mkdir(mode=0o700,exist_ok=True)
    if not workspace.is_dir() or workspace.is_symlink():
        raise ValueError('Download workspace must be private absolute non-symlink directory')
    disk = os.statvfs(workspace)
    if disk.f_bavail * disk.f_frsize < max_bytes + 256 * 1024 * 1024:
        raise DownloadError('source_disk_budget', 'Not enough free space to safely stage the source')
    if target.exists() or target.is_symlink():
        raise DownloadError('source_exists', 'Source already present; verify its receipt before reuse')


def _reserve_part(temporary):
    fd = os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    os.close(fd)


def _append_block(temporary, block):
    """Append one bounded network block while owning and closing its descriptor."""
    fd = os.open(temporary,os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DownloadError('source_part_invalid','Private source part identity changed')
        view = memoryview(block)
        while view:
            written = os.write(fd,view)
            if written <= 0:
                raise OSError('Short source part write')
            view = view[written:]
    finally:
        os.close(fd)


def _fsync_directory(workspace):
    fd = os.open(workspace,os.O_DIRECTORY|os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_owned_target(target, identity, workspace):
    """Remove only the exact inode published by this attempt."""
    if identity is None:
        return False
    try:
        current = os.stat(target,follow_symlinks=False)
    except FileNotFoundError:
        return False
    if (current.st_dev,current.st_ino) != identity or not stat.S_ISREG(current.st_mode):
        return False
    os.unlink(target)
    _fsync_directory(workspace)
    return True


def _publish_part(temporary, target, workspace, cancelled):
    """Fsync and exclusively publish; cancellation never leaves our target."""
    if cancelled.is_set():
        return None
    fd = os.open(temporary,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DownloadError('source_part_invalid','Private source part identity changed')
        os.fsync(fd)
    finally:
        os.close(fd)
    if cancelled.is_set():
        return None
    identity = None
    try:
        try:
            os.link(temporary,target,follow_symlinks=False)
        except FileExistsError as exc:
            raise DownloadError('source_exists',
                'Source already present; verify its receipt before reuse') from exc
        published = os.stat(target,follow_symlinks=False)
        identity = (published.st_dev,published.st_ino)
        if cancelled.is_set():
            _remove_owned_target(target,identity,workspace)
            return None
        _fsync_directory(workspace)
        if cancelled.is_set():
            _remove_owned_target(target,identity,workspace)
            return None
        os.unlink(temporary)
        return identity
    except BaseException:
        if identity is not None:
            try:
                _remove_owned_target(target,identity,workspace)
            except OSError:
                pass
        raise


async def _offload_result(offload, function, /, *args, cancel_signal=None, **kwargs):
    """Drain one sync operation after cancellation so its handles cannot escape."""
    runner = asyncio.to_thread if offload is None else offload
    operation = asyncio.ensure_future(runner(function,*args,**kwargs))
    cancellation = None
    while True:
        try:
            result = await asyncio.shield(operation)
            return result,cancellation
        except asyncio.CancelledError as exc:
            if operation.done() and operation.cancelled() and cancellation is None:
                raise
            if cancellation is None:
                cancellation = exc
                if cancel_signal is not None:
                    cancel_signal()
            if operation.done():
                try:
                    return operation.result(),cancellation
                except BaseException as operation_error:
                    raise cancellation from operation_error
        except BaseException as exc:
            if cancellation is not None:
                raise cancellation from exc
            raise


async def _offload_io(offload, function, /, *args, **kwargs):
    result,cancellation = await _offload_result(offload,function,*args,**kwargs)
    if cancellation is not None:
        raise cancellation
    return result


async def download_selected(catalogue: CatalogueService, chosen: CatalogueItem, workspace: Path,
                            *, max_bytes: int = 64*1024*1024, max_redirects: int = 3,
                            timeout_s: int = 100, offload=None):
    if type(timeout_s) is not int or not 5 <= timeout_s <= 120:
        raise ValueError('Invalid total download deadline')
    try:
        return await asyncio.wait_for(_download_inner(catalogue, chosen, workspace,
            max_bytes=max_bytes, max_redirects=max_redirects, offload=offload),timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise DownloadError('source_download_timeout',
            'Selected source exceeded total download deadline; no alternate recording used') from exc


async def _download_inner(catalogue: CatalogueService, chosen: CatalogueItem, workspace: Path,
                          *, max_bytes: int, max_redirects: int, offload):
    """Return (immutable downloaded file, sha256, bytes); preserve failed .part files.

    Same authenticated provider client for search/resolve and stream. The signed
    URL is ephemeral and never placed into a metadata/receipt/log message.
    """
    if type(max_bytes) is not int or not 1024 <= max_bytes <= 64*1024*1024:
        raise ValueError('Invalid download byte budget')
    if type(max_redirects) is not int or not 0 <= max_redirects <= 5:
        raise ValueError('Invalid redirect budget')
    target = workspace/'source.audio'
    temporary = workspace/('source-'+uuid.uuid4().hex+'.part')
    await _offload_io(offload,_prepare_destination,workspace,target,max_bytes)
    audio = await catalogue.resolve(chosen)
    url = _safe_url(audio.url, chosen.provider)
    if audio.size_bytes is not None and audio.size_bytes > max_bytes:
        raise DownloadError('source_too_large','Selected source exceeds download byte limit')
    await _offload_io(offload,_reserve_part,temporary)
    client = (catalogue.client._netease_client if chosen.provider=='163' else catalogue.client._qq_client)
    amount = 0
    digest = hashlib.sha256()
    for hop in range(max_redirects+1):
        try:
            async with client.stream('GET',url,follow_redirects=False) as response:
                if response.status_code in (301,302,303,307,308):
                    location = response.headers.get('location','')
                    if not location:
                        raise DownloadError('source_redirect_invalid','Playback redirect omitted destination')
                    if hop == max_redirects:
                        raise DownloadError('source_redirect_limit','Playback redirected too many times')
                    url = _safe_url(urljoin(url,location),chosen.provider)
                    continue
                if response.status_code != 200:
                    raise DownloadError('source_http_error',
                        'Selected source refused playback (HTTP %d)' % response.status_code)
                claimed=response.headers.get('content-length')
                if claimed:
                    if not re.fullmatch('[0-9]{1,20}',claimed) or int(claimed)>max_bytes:
                        raise DownloadError('source_too_large','Claimed media size exceeds byte limit')
                async for block in response.aiter_bytes(256*1024):
                    amount += len(block)
                    if amount > max_bytes:
                        raise DownloadError('source_too_large','Media stream exceeds byte limit')
                    await _offload_io(offload,_append_block,temporary,block)
                    digest.update(block)
                break
        except (httpx.TimeoutException,httpx.NetworkError,httpx.ProtocolError) as exc:
            raise DownloadError('source_network_error','Selected source stream interrupted') from exc
    if amount < 1024:
        raise DownloadError('source_incomplete','Media stream was empty or implausibly short')
    cancel_signal = threading.Event()
    identity,cancellation = await _offload_result(offload,_publish_part,temporary,target,workspace,
                                                   cancel_signal,cancel_signal=cancel_signal.set)
    if cancellation is not None:
        if identity is not None:
            _,cleanup_cancellation = await _offload_result(
                offload,_remove_owned_target,target,identity,workspace)
            if cleanup_cancellation is not None:
                cancellation = cleanup_cancellation
        raise cancellation
    if identity is None:
        raise asyncio.CancelledError
    return target,digest.hexdigest(),amount
