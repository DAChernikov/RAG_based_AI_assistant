from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class BoundedThreadAdapter:
    """Runs blocking boundary calls without growing the default executor queue."""

    def __init__(self, max_workers: int = 8, max_pending: int = 32):
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rag-io")
        self._slots = asyncio.Semaphore(max_pending)

    async def call(self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        async with self._slots:
            loop = asyncio.get_running_loop()
            bound = functools.partial(function, *args, **kwargs)
            return await loop.run_in_executor(self._executor, bound)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


api_blocking_io = BoundedThreadAdapter()
