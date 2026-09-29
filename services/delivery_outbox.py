"""Consent-gated, at-most-once delivery adapter for committed cover artifacts.

This module is deliberately transport-agnostic except for ``CustomVoiceSender``.
The durable truth remains in :class:`JobStore`; an in-memory task is never used
as proof that a delivery did or did not happen.
"""
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol
import asyncio

from .job_store import Job, JobConflict, JobStore


class CommittedArtifact(Protocol):
    """The part of ``ArtifactStore.verify``'s result needed for delivery."""

    path: Path


class Sender(Protocol):
    async def send(self, job: Job, artifact: CommittedArtifact) -> Any:
        """Start one platform send and return its acknowledgement payload."""


@dataclass(frozen=True)
class DeliveryReceipt:
    """Normalized platform acknowledgement.

    ``sent`` is intentionally stricter than a truthy SDK result: a platform
    message id is required. ``failed`` means the platform explicitly reported
    that it did not accept the message. Everything else is ``unknown``.
    """

    outcome: str
    message_id: Optional[str] = None


def classify_ack(result: Any) -> DeliveryReceipt:
    """Normalize the SDK detailed-send contract without optimistic guessing."""
    if result is False:
        return DeliveryReceipt('unknown')
    # The Host can turn a post-platform hook/store exception into sent=False.
    # Neither that result nor bare False proves the platform rejected the send.
    if not isinstance(result, Mapping):
        return DeliveryReceipt('unknown')
    sent = result.get('sent')
    message_id = result.get('message_id')
    valid_id = isinstance(message_id, str) and bool(message_id.strip())
    if sent is True and valid_id:
        return DeliveryReceipt('sent', message_id.strip())
    return DeliveryReceipt('unknown')


class CustomVoiceSender:
    """Adapter for ``ctx.send.custom`` using the SDK's detailed receipt mode."""

    def __init__(
        self,
        send_custom: Callable[..., Awaitable[Any]],
        *,
        rpc_timeout_ms: int = 75_000,
    ) -> None:
        if not callable(send_custom) or type(rpc_timeout_ms) is not int or rpc_timeout_ms <= 0:
            raise ValueError('Invalid custom sender configuration')
        self._send_custom = send_custom
        self._rpc_timeout_ms = rpc_timeout_ms

    async def send(self, job: Job, artifact: CommittedArtifact) -> Any:
        path = Path(artifact.path)
        if not path.is_absolute():
            raise ValueError('Committed artifact path must be absolute')
        return await self._send_custom(
            'voiceurl',
            {'url': path.as_uri()},
            job.stream_id,
            return_details=True,
            timeout_ms=self._rpc_timeout_ms,
        )


class DeliveryOutbox:
    """Claim and dispatch one durable delivery transition at most once.

    Candidate discovery belongs to the coordinator because ``JobStore`` does
    not expose a scan API. ``dispatch`` atomically rechecks ready+pending and
    persisted consent via ``claim_delivery`` before invoking any transport.
    """

    def __init__(
        self,
        store: JobStore,
        sender: Sender,
        validate_artifact: Callable[[str], CommittedArtifact],
        *,
        acknowledgement_timeout_s: float = 60.0,
    ) -> None:
        if not isinstance(store, JobStore) or not callable(validate_artifact):
            raise ValueError('Invalid delivery outbox dependencies')
        if not isinstance(acknowledgement_timeout_s, (int, float)) or acknowledgement_timeout_s <= 0:
            raise ValueError('Invalid acknowledgement timeout')
        self._store = store
        self._sender = sender
        self._validate_artifact = validate_artifact
        self._acknowledgement_timeout_s = float(acknowledgement_timeout_s)
        self._late_ack_tasks: set[asyncio.Task[None]] = set()
        self._write_tasks: set[asyncio.Task[Job]] = set()
        self._send_tasks: set[asyncio.Task[Any]] = set()

    async def recover(self) -> int:
        """On startup, fence every pre-restart dispatch as unknown."""
        return await asyncio.to_thread(self._store.recover_dispatches)

    def _consume_background_write(self, task: asyncio.Task[Any]) -> None:
        self._write_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _record(self, job: Job, receipt: DeliveryReceipt,
                      done: Optional[asyncio.Event] = None) -> Job:
        write_task = asyncio.create_task(
            asyncio.to_thread(
                self._store.delivery_result,
                job.id,
                job.delivery_token,
                receipt.outcome,
                message_id=receipt.message_id,
            )
        )
        self._write_tasks.add(write_task)
        def finished(task: asyncio.Task[Job]) -> None:
            self._write_tasks.discard(task)
            if not task.cancelled():
                task.exception()  # Retrieve detached SQLite failures.
            if done is not None:
                done.set()
        write_task.add_done_callback(finished)
        return await asyncio.shield(write_task)

    async def _mark_unknown(self, job: Job,
                            done: Optional[asyncio.Event] = None) -> Job:
        return await self._record(job, DeliveryReceipt('unknown'), done)

    async def _observe_late(self, job: Job, send_task: asyncio.Task[Any],
                            unknown_done: asyncio.Event) -> None:
        """A late result may settle the same RPC, but never starts another one."""
        try:
            result = await send_task
            await unknown_done.wait()
            receipt = classify_ack(result)
            if receipt.outcome != 'unknown':
                await self._record(job, receipt)
        except (asyncio.CancelledError, JobConflict):
            return
        except Exception:
            # The durable unknown written before this observer started remains
            # the only honest result for a transport exception.
            return

    def _watch_late(self, job: Job, send_task: asyncio.Task[Any],
                    unknown_done: asyncio.Event) -> None:
        watcher = asyncio.create_task(self._observe_late(job, send_task, unknown_done))
        self._late_ack_tasks.add(watcher)
        watcher.add_done_callback(self._late_ack_tasks.discard)

    async def drain_late_acknowledgements(self) -> None:
        """Test/shutdown hook: wait for currently known late observers."""
        tasks = tuple(self._late_ack_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self, *, timeout_s: float = 1.0) -> None:
        """Bound unload time; leave ambiguous attempts unknown, never retry.

        A running SQLite thread cannot be killed safely; its tracked write will
        complete independently. Cancelling observers does not undo platform work.
        """
        if timeout_s < 0:
            raise ValueError('Invalid shutdown timeout')
        tasks = tuple(self._late_ack_tasks | self._send_tasks)
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout_s)
            for task in pending:
                task.cancel()
            # Retrieve exceptions from already completed tasks.
            for task in done:
                if not task.cancelled():
                    task.exception()

    async def dispatch(self, job_id: str, stream_id: str) -> Optional[Job]:
        """Attempt one eligible delivery, returning ``None`` when not claimable.

        Cancellation is propagated to the host only after a claimed transition
        is durably marked unknown. The transport task is shielded so a later
        acknowledgement from that same RPC can still settle the ledger.
        """
        claim_task = asyncio.create_task(
            asyncio.to_thread(self._store.claim_delivery, job_id, stream_id)
        )
        claimed: Optional[Job] = None
        try:
            claimed = await asyncio.shield(claim_task)
        except asyncio.CancelledError:
            # A SQLite thread cannot be interrupted. Reconcile its eventual
            # claim without holding the unloading coordinator hostage.
            def reconcile(task: asyncio.Task[Optional[Job]]) -> None:
                try:
                    row = task.result()
                except Exception:
                    return
                if row is not None:
                    watcher = asyncio.create_task(self._mark_unknown(row))
                    self._write_tasks.add(watcher)
                    watcher.add_done_callback(self._consume_background_write)
            claim_task.add_done_callback(reconcile)
            raise
        if claimed is None:
            return None

        try:
            if not claimed.artifact_key:
                raise ValueError('Ready delivery has no committed artifact key')
            artifact = await asyncio.to_thread(
                self._validate_artifact,
                claimed.artifact_key,
            )
        except asyncio.CancelledError:
            await self._mark_unknown(claimed)
            raise
        except Exception:
            return await self._mark_unknown(claimed)

        send_task = asyncio.create_task(self._sender.send(claimed, artifact))
        self._send_tasks.add(send_task)
        send_task.add_done_callback(self._send_tasks.discard)
        try:
            async with asyncio.timeout(self._acknowledgement_timeout_s):
                result = await asyncio.shield(send_task)
        except (TimeoutError, asyncio.CancelledError) as interruption:
            unknown_done = asyncio.Event()
            self._watch_late(claimed, send_task, unknown_done)
            unknown = await self._mark_unknown(claimed, unknown_done)
            if isinstance(interruption, asyncio.CancelledError):
                raise interruption
            return unknown
        except Exception:
            return await self._mark_unknown(claimed)

        return await self._record(claimed, classify_ack(result))
