"""Fan one run's progress out to whoever is watching it.

A test run is a subprocess that reports each node as it happens (see
`core/progress.py` in the package template). That report has to reach however
many browser tabs have the canvas open, and those subscribers come and go
independently of the run — so the run pushes into here and never knows or waits
for who is reading.

Deliberately in memory, and deliberately small:

* One `asyncio.Queue` per subscriber rather than one shared queue, because a
  shared one would let the first reader consume a step the second never sees.
* Bounded queues, dropped-oldest on overflow. A subscriber that has stopped
  reading — a closed laptop, a dead connection — must not be able to stall the
  run that is feeding it.
* A short replay buffer, so a canvas that subscribes a moment after the run
  starts still sees the nodes that already finished rather than a blank canvas
  until the next one.

Nothing here survives a restart or reaches a second backend instance. That is
the right trade for the Test panel, which watches a run it started itself; a
deployment running several API instances would need Redis pub/sub in this
module's place, and nothing outside it would change.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

# Enough to cover a slow subscriber without letting one hold memory forever.
QUEUE_SIZE = 256

# What a late subscriber gets to catch up on.
REPLAY_SIZE = 200

# How long a finished run stays readable, so a subscriber that arrives at the
# very end still sees the outcome rather than an empty stream.
KEEP_AFTER_FINISH = 60.0


class _Run:
    def __init__(self) -> None:
        self.history: deque[dict[str, Any]] = deque(maxlen=REPLAY_SIZE)
        self.subscribers: set[asyncio.Queue] = set()
        self.finished = False


_runs: dict[str, _Run] = {}


def publish(execution_id: str, step: dict[str, Any]) -> None:
    """Report one step. Safe to call from anywhere, including a sync callback."""
    run = _runs.setdefault(execution_id, _Run())
    run.history.append(step)
    for queue in list(run.subscribers):
        if queue.full():
            # Drop the oldest rather than block: the run must not be held up by
            # a subscriber that stopped reading.
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(step)
        except asyncio.QueueFull:  # pragma: no cover - just lost the race above
            pass


def finish(execution_id: str, **fields: Any) -> None:
    """Mark the run over and tell every subscriber to stop waiting."""
    run = _runs.setdefault(execution_id, _Run())
    run.finished = True
    publish(execution_id, {"e": "finished", **fields})

    async def forget() -> None:
        await asyncio.sleep(KEEP_AFTER_FINISH)
        _runs.pop(execution_id, None)

    try:
        asyncio.get_running_loop().create_task(forget())
    except RuntimeError:  # pragma: no cover - no loop in a sync test
        _runs.pop(execution_id, None)


def is_live(execution_id: str) -> bool:
    """Whether a run is being tracked right now and has not reported finishing.

    A run this process never started, or one whose record has already expired,
    is not live — and a subscriber must be told so rather than left holding an
    open connection that will never produce a frame.
    """
    run = _runs.get(execution_id)
    return run is not None and not run.finished


async def subscribe(execution_id: str):
    """Yield every step of this run, starting with the ones already past.

    Ends when the run reports it is finished. A caller that goes away simply
    stops iterating; the `finally` takes its queue out of the fan-out.
    """
    run = _runs.setdefault(execution_id, _Run())
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
    replay = list(run.history)
    run.subscribers.add(queue)

    try:
        for step in replay:
            yield step
            if step.get("e") == "finished":
                return
        while True:
            step = await queue.get()
            yield step
            if step.get("e") == "finished":
                return
    finally:
        run.subscribers.discard(queue)
