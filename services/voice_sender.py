"""Single-attempt voice send for non-durable speech messages.

Unlike the cover outbox this has no persisted per-message identity; it never
claims cross-request exactly-once behavior. A timeout/cancellation does not
prove failure and must never authorize a transport fallback.
"""
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Optional
import asyncio
import math
import stat

from .delivery_outbox import DeliveryReceipt, classify_ack


class SingleVoiceSender:
    def __init__(self, send_custom: Callable[..., Awaitable[Any]], *,
                 rpc_timeout_ms: int = 75_000, acknowledgement_timeout_s: float = 60.0,
                 late_receipt: Optional[Callable[[DeliveryReceipt], None]] = None) -> None:
        if not callable(send_custom) or type(rpc_timeout_ms) is not int or rpc_timeout_ms <= 0:
            raise ValueError('Invalid voice transport configuration')
        if (type(acknowledgement_timeout_s) not in (float, int)
                or not math.isfinite(acknowledgement_timeout_s)
                or not 0 < acknowledgement_timeout_s <= 120):
            raise ValueError('Invalid acknowledgement timeout')
        self._send_custom = send_custom
        self._rpc_timeout_ms = rpc_timeout_ms
        self._deadline = acknowledgement_timeout_s
        self._late_receipt = late_receipt
        self._tasks: set[asyncio.Task[Any]] = set()
        self._watchers: set[asyncio.Task[None]] = set()
        self._closed = False

    async def _observe(self, send_task: asyncio.Task[Any]) -> None:
        try:
            receipt = classify_ack(await send_task)
            if self._late_receipt is not None:
                self._late_receipt(receipt)
        except asyncio.CancelledError:
            return
        except Exception:
            # The original attempt remains ambiguous; never send another item.
            return

    def _consume_send(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _watch(self, send_task: asyncio.Task[Any]) -> None:
        watcher = asyncio.create_task(self._observe(send_task))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)

    async def send_file(self, path: Path, stream_id: str) -> DeliveryReceipt:
        if self._closed:
            raise RuntimeError('Voice sender is closed')
        path = Path(path)
        if not path.is_absolute():
            raise ValueError('Voice file must exist at an absolute path')
        if not isinstance(stream_id, str) or not stream_id.strip():
            raise ValueError('Invalid stream identity')
        try:
            file_stat = await asyncio.to_thread(path.lstat)
        except OSError as exc:
            raise ValueError('Voice file is unavailable') from exc
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError('Voice file must be regular, not a symlink')
        if self._closed:
            raise RuntimeError('Voice sender is closed')
        send_task = asyncio.create_task(self._send_custom(
            'voiceurl', {'url': path.as_uri()}, stream_id,
            return_details=True, timeout_ms=self._rpc_timeout_ms))
        self._tasks.add(send_task)
        send_task.add_done_callback(self._consume_send)
        try:
            async with asyncio.timeout(self._deadline):
                result = await asyncio.shield(send_task)
        except TimeoutError:
            self._watch(send_task)
            return DeliveryReceipt('unknown')
        except asyncio.CancelledError:
            self._watch(send_task)
            raise
        except Exception:
            return DeliveryReceipt('unknown')
        return classify_ack(result)

    async def shutdown(self, *, timeout_s: float = 1.0) -> None:
        """Give in-flight RPCs a bounded chance to finish, then cancel them."""
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 <= timeout_s <= 10:
            raise ValueError('Invalid shutdown timeout')
        self._closed = True
        tasks = tuple(self._tasks | self._watchers)
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout_s)
            for task in pending:
                task.cancel()
            for task in done:
                if not task.cancelled():
                    task.exception()
