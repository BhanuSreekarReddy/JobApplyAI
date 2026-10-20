from __future__ import annotations

import asyncio
from collections import defaultdict

_queues: dict[str, list[asyncio.Queue]] = defaultdict(list)
_history: dict[str, list[dict]] = defaultdict(list)


async def publish(run_id: str, event: dict) -> None:
    _history[run_id].append(event)
    for q in list(_queues[run_id]):
        await q.put(event)


def history(run_id: str) -> list[dict]:
    return list(_history[run_id])


def subscribe(run_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _queues[run_id].append(q)
    return q


def unsubscribe(run_id: str, q: asyncio.Queue) -> None:
    if q in _queues[run_id]:
        _queues[run_id].remove(q)
