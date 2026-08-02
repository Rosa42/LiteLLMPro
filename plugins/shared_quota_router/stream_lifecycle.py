"""Stream lifecycle: first-byte gate, lease renew (R1), absolute cap, release on end.

Wraps upstream async iterators returned to clients. Do **not** use
``async_post_call_streaming_hook`` (corrupts SSE per callbacks.py note).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Callable

from shared_quota_router.lease import LeaseManager, lease_ttl_seconds
from shared_quota_router.stream_wire import (
    StreamWireProtocol,
    chunk_is_public_stream_event,
    terminal_stream_chunks,
)

logger = logging.getLogger(__name__)

DEFAULT_RENEW_FLOOR_SECONDS = 30
DEFAULT_ABSOLUTE_MAX_SECONDS = 900


@dataclass(slots=True)
class StreamLifecycleConfig:
    """R1 defaults (staging); override via env/config later if needed."""

    request_timeout_seconds: float = 300
    renew_floor_seconds: int = DEFAULT_RENEW_FLOOR_SECONDS
    absolute_max_seconds: int = DEFAULT_ABSOLUTE_MAX_SECONDS

    @property
    def lease_ttl_seconds(self) -> int:
        return lease_ttl_seconds(self.request_timeout_seconds)

    @property
    def renew_interval_seconds(self) -> int:
        ttl = self.lease_ttl_seconds
        return max(self.renew_floor_seconds, ttl // 3)


@dataclass(slots=True)
class StreamLifecycleContext:
    quota_group_id: str
    request_id: str
    config: StreamLifecycleConfig = field(default_factory=StreamLifecycleConfig)
    lease_manager: LeaseManager | None = None
    on_first_byte: Callable[[], None] | None = None
    on_stream_complete: Callable[[], None] | None = None
    wire_protocol: StreamWireProtocol | None = None


def is_async_iterator(obj: Any) -> bool:
    return hasattr(obj, "__aiter__") and callable(getattr(obj, "__aiter__", None))


class ManagedStream:
    """Async iterator wrapper: renew lease, enforce absolute cap, release on exit."""

    def __init__(
        self,
        source: AsyncIterator[Any],
        ctx: StreamLifecycleContext,
    ) -> None:
        self._source = source
        self._ctx = ctx
        self._first_byte = False
        self._released = False
        self._finalized = False
        self._started_at = time.monotonic()
        self._renew_task: asyncio.Task[None] | None = None
        self._terminal_queue: list[Any] = []
        self._finished = False
        self._upstream_aclose_unpatched: Any = None

    def __aiter__(self) -> ManagedStream:
        return self

    async def __anext__(self) -> Any:
        if self._terminal_queue:
            item = self._terminal_queue.pop(0)
            if not self._terminal_queue:
                self._finished = True
                await self._finalize()
                if item is _STOP:
                    raise StopAsyncIteration
            return item

        if self._finished:
            raise StopAsyncIteration

        if self._renew_task is None:
            self._start_renew_loop()
        try:
            self._check_absolute_cap()
        except StreamAbsoluteCapError as exc:
            await self._enqueue_terminal_error(str(exc))
            return await self.__anext__()
        try:
            item = await self._source.__anext__()
        except StopAsyncIteration:
            await self._finalize()
            raise
        except asyncio.CancelledError:
            await self._finalize(cancelled=True)
            raise
        except Exception as exc:
            await self._enqueue_terminal_error(str(exc))
            return await self.__anext__()
        self._on_chunk(item)
        return item

    def _on_chunk(self, item: Any) -> None:
        if self._first_byte:
            return
        if not chunk_is_public_stream_event(item):
            return
        self._first_byte = True
        if self._ctx.on_first_byte:
            try:
                self._ctx.on_first_byte()
            except Exception as exc:  # noqa: BLE001
                logger.warning("stream on_first_byte failed: %s", exc)

    def _check_absolute_cap(self) -> None:
        elapsed = time.monotonic() - self._started_at
        if elapsed >= self._ctx.config.absolute_max_seconds:
            raise StreamAbsoluteCapError(
                f"stream exceeded absolute max {self._ctx.config.absolute_max_seconds}s"
            )

    async def _enqueue_terminal_error(self, message: str) -> None:
        if self._first_byte and self._ctx.wire_protocol is not None:
            self._terminal_queue.extend(
                terminal_stream_chunks(self._ctx.wire_protocol, message)
            )
        self._terminal_queue.append(_STOP)
        if self._renew_task is not None:
            self._renew_task.cancel()

    def _start_renew_loop(self) -> None:
        if self._ctx.lease_manager is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._renew_task = loop.create_task(self._renew_loop())

    async def _renew_loop(self) -> None:
        lm = self._ctx.lease_manager
        if lm is None:
            return
        interval = self._ctx.config.renew_interval_seconds
        ttl = self._ctx.config.lease_ttl_seconds
        qg = self._ctx.quota_group_id
        rid = self._ctx.request_id
        while True:
            await asyncio.sleep(interval)
            if self._released or self._finished:
                return
            if not lm.renew(
                quota_group_id=qg,
                request_id=rid,
                ttl_seconds=ttl,
            ):
                logger.error(
                    "lease renew failed request_id=%s qg=%s — terminating stream",
                    rid,
                    qg,
                )
                await self._enqueue_terminal_error("lease renew failed")
                return

    async def _finalize(self, *, cancelled: bool = False) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self._renew_task is not None:
            self._renew_task.cancel()
            try:
                await self._renew_task
            except asyncio.CancelledError:
                pass
            self._renew_task = None
        self._release_lease()
        if self._ctx.on_stream_complete:
            try:
                self._ctx.on_stream_complete()
            except Exception as exc:  # noqa: BLE001
                logger.warning("stream on_stream_complete failed: %s", exc)
        if cancelled:
            logger.info("stream cancelled request_id=%s", self._ctx.request_id)

    def _release_lease(self) -> None:
        if self._released:
            return
        self._released = True
        lm = self._ctx.lease_manager
        if lm is None:
            return
        try:
            lm.release(
                quota_group_id=self._ctx.quota_group_id,
                request_id=self._ctx.request_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("stream lease release failed: %s", exc)

    async def aclose(self) -> None:
        """Client disconnect / generator close."""
        await self._finalize(cancelled=True)
        closer = self._upstream_aclose_unpatched
        if closer is None:
            closer = getattr(self._source, "aclose", None)
        if callable(closer):
            try:
                result = closer()
            except TypeError:
                result = closer
            if asyncio.iscoroutine(result):
                await result


_STOP = object()


class StreamAbsoluteCapError(Exception):
    """Wall-clock stream duration exceeded."""


class StreamRenewFailedError(Exception):
    """Lease renew failed; stream must terminate."""


def wrap_async_stream(
    source: Any,
    ctx: StreamLifecycleContext,
) -> Any:
    """Wrap async iterator with lifecycle management; passthrough non-stream."""
    if not is_async_iterator(source):
        return source
    managed = ManagedStream(source, ctx)
    bind_upstream_aclose(managed, source)
    return managed


def bind_upstream_aclose(managed: ManagedStream, upstream: Any) -> None:
    """Patch upstream ``aclose`` so LiteLLM proxy cleanup releases the lease.

    ``async_streaming_data_generator`` finally calls ``response.aclose()`` on the
    original upstream iterator (e.g. CustomStreamWrapper), not on ManagedStream.
    """
    original = getattr(upstream, "aclose", None)
    managed._upstream_aclose_unpatched = original

    async def patched_aclose() -> None:
        await managed.aclose()

    try:
        upstream.aclose = patched_aclose  # type: ignore[method-assign]
    except (AttributeError, TypeError):
        logger.debug("could not patch upstream aclose for stream lifecycle")


async def drain_managed_stream(stream: ManagedStream) -> list[Any]:
    """Test helper: consume entire managed stream."""
    out: list[Any] = []
    async for chunk in stream:
        out.append(chunk)
    return out
