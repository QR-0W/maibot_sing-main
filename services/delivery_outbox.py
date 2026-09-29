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
        return DeliveryReceipt('failed')
    if not isinstance(result, Mapping):
        return DeliveryReceipt('unknown')
    sent = result.get('sent')
    message_id = result.get('message_id')
    valid_id = isinstance(message_id, str) and bool(message_id.strip())
    if sent is True and valid_id:
        return DeliveryReceipt('sent', message_id.strip())
    if sent is False and not valid_id:
        return DeliveryReceipt('failed')
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

    async def recover(self) -> int:
        """On startup, fence every pre-restart dispatch as unknown."""
        return await asyncio.to_thread(self._store.recover_dispatches)

    async def _record(self, job: Job, receipt: DeliveryReceipt) -> Job:
        write_task = asyncio.create_task(
            asyncio.to_thread(
                self._store.delivery_result,
                job.id,
                job.delivery_token,
                receipt.outcome,
                message_id=receipt.message_id,
            )
        )
        try:
            return await asyncio.shield(write_task)
        except asyncio.CancelledError:
            # Once a platform result is known, do not let host cancellation
            # strand the durable row in dispatching while sqlite still writes.
            await asyncio.shield(write_task)
            raise

    async def _mark_unknown(self, job: Job) -> Job:
        return await self._record(job, DeliveryReceipt('unknown'))

    async def _observe_late(self, job: Job, send_task: asyncio.Task[Any]) -> None:
        """A late result may settle the same RPC, but never starts another one."""
        try:
            result = await send_task
            receipt = classify_ack(result)
            if receipt.outcome != 'unknown':
                await self._record(job, receipt)
        except (asyncio.CancelledError, JobConflict):
            return
        except Exception:
            # The durable unknown written before this observer started remains
            # the only honest result for a transport exception.
            return

    def _watch_late(self, job: Job, send_task: asyncio.Task[Any]) -> None:
        watcher = asyncio.create_task(self._observe_late(job, send_task))
        self._late_ack_tasks.add(watcher)
        watcher.add_done_callback(self._late_ack_tasks.discard)

    async def drain_late_acknowledgements(self) -> None:
        """Test/shutdown hook: wait for currently known late observers."""
        tasks = tuple(self._late_ack_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
            # sqlite may already have committed the claim in its worker thread.
            claimed = await asyncio.shield(claim_task)
            if claimed is not None:
                await self._mark_unknown(claimed)
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
        try:
            async with asyncio.timeout(self._acknowledgement_timeout_s):
                result = await asyncio.shield(send_task)
        except TimeoutError:
            unknown = await self._mark_unknown(claimed)
            self._watch_late(claimed, send_task)
            return unknown
        except asyncio.CancelledError:
            await self._mark_unknown(claimed)
            self._watch_late(claimed, send_task)
            raise
        except Exception:
            return await self._mark_unknown(claimed)

        return await self._record(claimed, classify_ack(result))
