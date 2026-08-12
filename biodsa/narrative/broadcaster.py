"""
Thread-safe SSE event broadcaster with per-session asyncio.Queue.

Designed for use from within a thread-pool executor (emit_sync is the
call-in point from the agent stream loop) while the Starlette async
handler reads events via subscribe().
"""

import asyncio
import json
import logging
from typing import Optional

from biodsa.narrative.events import NarrativeEvent

logger = logging.getLogger(__name__)

RING_BUFFER_SIZE = 50
SENTINEL = object()


class EventBroadcaster:
    """Manages per-session asyncio.Queue instances for SSE event delivery."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}
        self._buffers: dict[str, list[NarrativeEvent]] = {}
        self._loops: dict[str, asyncio.AbstractEventLoop] = {}

    def create_run(self, session_id: str) -> None:
        """Create a queue and ring buffer for the given session."""
        if session_id not in self._queues:
            self._queues[session_id] = asyncio.Queue()
            self._buffers[session_id] = []
            # Captured here because create_run runs on the event loop thread,
            # while emit_sync is called from a worker thread.
            try:
                self._loops[session_id] = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop (e.g. a synchronous test); emit_sync then
                # falls back to a direct, same-thread put.
                pass
            logger.info("Broadcaster run created: session_id=%s", session_id)

    def close_run(self, session_id: str) -> None:
        """Push sentinel and clean up the queue for the given session."""
        queue = self._queues.pop(session_id, None)
        loop = self._loops.pop(session_id, None)
        self._buffers.pop(session_id, None)
        if queue is None:
            return
        # The sentinel goes through the loop too. emit_sync only *schedules* its
        # put, so enqueueing the sentinel directly would overtake events that
        # are still pending -- notably the final RunComplete snapshot.
        if loop is not None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, SENTINEL)
                logger.info("Broadcaster run closed: session_id=%s", session_id)
                return
            except RuntimeError:
                pass
        try:
            queue.put_nowait(SENTINEL)
        except asyncio.QueueFull:
            pass
        logger.info("Broadcaster run closed: session_id=%s", session_id)

    def emit_sync(self, session_id: str, event: NarrativeEvent) -> None:
        """
        Push an event from any thread into the session's asyncio queue.

        Called from the agent stream loop, which runs inside a thread-pool
        executor. asyncio.Queue is not thread-safe, so the put is scheduled
        onto the owning event loop. call_soon_threadsafe preserves FIFO order
        for calls originating from the same thread.
        """
        queue = self._queues.get(session_id)
        if queue is None:
            logger.warning("Broadcaster emit for unknown session: %s", session_id)
            return
        # Ring buffer for late subscribers
        buf = self._buffers.get(session_id)
        if buf is not None:
            buf.append(event)
            if len(buf) > RING_BUFFER_SIZE:
                buf.pop(0)
        loop = self._loops.get(session_id)
        if loop is None:
            queue.put_nowait(event)
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, event)
        except RuntimeError:
            # Event loop already closed (run cancelled); dropping is correct.
            logger.warning("Broadcaster loop closed for session: %s", session_id)

    async def subscribe(self, session_id: str):
        """
        Async generator yielding SSE-formatted strings.

        Replays the ring buffer first for late subscribers, then
        yields events as they arrive. Ends when the sentinel is received.
        """
        queue = self._queues.get(session_id)
        if queue is None:
            yield f"event: error\ndata: {json.dumps({'error': 'unknown session_id'})}\n\n"
            return

        # Replay buffer
        buf = self._buffers.get(session_id, [])
        for event in buf:
            yield self._format_sse(event)

        # Live events
        while True:
            event = await queue.get()
            if event is SENTINEL:
                yield "event: done\ndata: {}\n\n"
                return
            yield self._format_sse(event)

    async def subscribe_events(self, session_id: str):
        """
        Async generator yielding raw NarrativeEvent objects.

        Ends when the sentinel is received. For MCP notification delivery.

        No ring-buffer replay: the MCP consumer subscribes at run creation, so
        anything in the buffer is still queued and would be delivered twice.
        """
        queue = self._queues.get(session_id)
        if queue is None:
            return

        while True:
            event = await queue.get()
            if event is SENTINEL:
                return
            yield event

    @staticmethod
    def _format_sse(event: NarrativeEvent) -> str:
        """Format a NarrativeEvent as an SSE event string."""
        data = json.dumps(event.to_dict(), ensure_ascii=False)
        return f"event: graph_event\ndata: {data}\n\n"
