from __future__ import annotations

import concurrent.futures
from typing import Callable, TypeVar

T = TypeVar("T")

# A dedicated pool, not a plain `threading.Thread`, so that a call which
# never returns (see below) doesn't leak an unbounded number of live
# threads over time - the pool caps concurrent in-flight (possibly stuck)
# calls instead.
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="http-call")


def call_with_timeout(fn: Callable[[], T], timeout: float) -> T:
    """Runs fn() with a hard wall-clock timeout that also bounds DNS
    resolution, unlike requests' own `timeout=` argument.

    `requests`/urllib3's `timeout` only covers the socket connect/read
    phases; the DNS lookup (getaddrinfo) that happens before a socket
    even exists is a blocking OS call with no timeout of its own. If that
    lookup stalls - which is exactly what was observed hanging the
    background collector indefinitely on Render, well past any of the
    30s-with-retries budget the API clients already set - `requests.get`
    can block forever regardless of the timeout passed to it.

    Running the call on a helper thread and bounding it with
    `Future.result(timeout=...)` fixes that: if it doesn't finish in time
    we raise and move on. The stuck OS thread itself leaks (Python can't
    force-kill a thread), but that's an acceptable trade for turning a
    silent, permanent hang into a fast, visible failure the periodic
    collector retry can recover from.
    """
    future = _executor.submit(fn)
    return future.result(timeout=timeout)
