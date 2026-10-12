import asyncio
from typing import Protocol


class AsyncLock(Protocol):
    async def __aenter__(self) -> None: ...
    async def __aexit__(self, *exc) -> None: ...


class NullLock:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc) -> None:
        return None


class AsyncioLock:
    """Single-process lock backed by `asyncio.Lock`."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> None:
        await self._lock.acquire()

    async def __aexit__(self, *exc) -> None:
        self._lock.release()
