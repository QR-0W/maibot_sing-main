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


async def download_selected(catalogue: CatalogueService, chosen: CatalogueItem, workspace: Path,
                            *, max_bytes: int = 64*1024*1024, max_redirects: int = 3,
                            timeout_s: int = 100):
    if type(timeout_s) is not int or not 5 <= timeout_s <= 120:
        raise ValueError('Invalid total download deadline')
    try:
        return await asyncio.wait_for(_download_inner(catalogue, chosen, workspace,
            max_bytes=max_bytes, max_redirects=max_redirects), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise DownloadError('source_download_timeout',
            'Selected source exceeded total download deadline; no alternate recording used') from exc


async def _download_inner(catalogue: CatalogueService, chosen: CatalogueItem, workspace: Path,
                          *, max_bytes: int, max_redirects: int):
    """Return (immutable downloaded file, sha256, bytes); preserve failed .part files.

    Same authenticated provider client for search/resolve and stream. The signed
    URL is ephemeral and never placed into a metadata/receipt/log message.
    """
    if type(max_bytes) is not int or not 1024 <= max_bytes <= 64*1024*1024:
        raise ValueError('Invalid download byte budget')
    if type(max_redirects) is not int or not 0 <= max_redirects <= 5:
        raise ValueError('Invalid redirect budget')
    if not workspace.is_absolute() or not workspace.is_dir() or any(p.is_symlink() for p in (workspace,*workspace.parents)):
        raise ValueError('Download workspace must be private absolute non-symlink directory')
    disk = os.statvfs(workspace)
    if disk.f_bavail * disk.f_frsize < max_bytes + 256 * 1024 * 1024:
        raise DownloadError('source_disk_budget', 'Not enough free space to safely stage the source')
    target = workspace/'source.audio'
    if target.exists() or target.is_symlink():
        raise DownloadError('source_exists', 'Source already present; verify its receipt before reuse')
    audio = await catalogue.resolve(chosen)
    url = _safe_url(audio.url, chosen.provider)
    if audio.size_bytes is not None and audio.size_bytes > max_bytes:
        raise DownloadError('source_too_large','Selected source exceeds download byte limit')
    temporary = workspace/('source-'+uuid.uuid4().hex+'.part')
    client = (catalogue.client._netease_client if chosen.provider=='163' else catalogue.client._qq_client)
    amount = 0
    digest = hashlib.sha256()
    fd = os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb',buffering=0) as output:
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
                            raise DownloadError('source_http_error', 'Selected source refused playback (HTTP %d)' % response.status_code)
                        claimed=response.headers.get('content-length')
                        if claimed:
                            if not re.fullmatch('[0-9]{1,20}',claimed) or int(claimed)>max_bytes:
                                raise DownloadError('source_too_large','Claimed media size exceeds byte limit')
                        async for block in response.aiter_bytes(256*1024):
                            amount += len(block)
                            if amount > max_bytes:
                                raise DownloadError('source_too_large','Media stream exceeds byte limit')
                            digest.update(block)
                            output.write(block)
                        break
                except (httpx.TimeoutException,httpx.NetworkError,httpx.ProtocolError) as exc:
                    raise DownloadError('source_network_error','Selected source stream interrupted') from exc
            if amount < 1024:
                raise DownloadError('source_incomplete','Media stream was empty or implausibly short')
            output.flush()
            os.fsync(output.fileno())
        # Never overwrite a file published by another process. Caller provides
        # per-job ownership; the link still closes the accidental overwrite case.
        os.link(temporary,target)
        dirfd=os.open(workspace,os.O_DIRECTORY|os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        temporary.unlink()
        return target,digest.hexdigest(),amount
    except BaseException:
        # The partial file is diagnostic evidence, not a resumable complete song.
        # Preserve it; the scheduler tracks its private attempt for expiry cleanup.
        raise
