"""Token-bucket rate-limiting ASGI middleware.

A pure-ASGI middleware (no third-party deps) that limits requests per client
using the **token-bucket** algorithm: each client gets a bucket that holds up to
``requests`` tokens and refills at ``requests / window`` tokens per second. A
request consumes one token; when the bucket is empty the request is rejected
with ``429 Too Many Requests`` and a ``Retry-After`` header. This allows a burst
up to the capacity while bounding the sustained rate — friendlier than a hard
fixed window, and it never blocks the event loop.

The bucket store is **in-memory and per-process** and bounded by an LRU cap, so
under ``pyxle serve --workers N`` each worker enforces the limit independently
(the effective global limit is ``N × requests``). For a shared limit across
workers/hosts, put a rate limiter in your reverse proxy or use a Redis-backed
store — see the middleware guide.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, MutableMapping, Sequence

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Cap the number of tracked client buckets so a flood of unique clients can't
# grow memory without bound (CLAUDE.md rule 17). Least-recently-seen buckets are
# evicted first; an evicted client simply starts with a full bucket again.
DEFAULT_MAX_BUCKETS = 10_000


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


def _canonical_path(path: str) -> str:
    """Collapse ``//``, resolve ``.``/``..``, for boundary-safe matching."""
    segments: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return "/" + "/".join(segments)


class RateLimitMiddleware:
    """Limit requests per client with a token bucket.

    ``requests`` is the bucket capacity (max burst); ``window_seconds`` is the
    period over which a full bucket refills, so the sustained rate is
    ``requests / window_seconds`` per second. Paths under ``exempt_paths`` (matched
    on segment boundaries) skip the limit. The client key is the connection's
    remote IP, or the first hop of ``X-Forwarded-For`` when ``trust_forwarded_for``
    is set (only enable that behind a trusted proxy).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        requests: int,
        window_seconds: float,
        exempt_paths: Sequence[str] = (),
        trust_forwarded_for: bool = False,
        max_buckets: int = DEFAULT_MAX_BUCKETS,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if requests < 1:
            raise ValueError("RateLimitMiddleware requires requests >= 1")
        if window_seconds <= 0:
            raise ValueError("RateLimitMiddleware requires window_seconds > 0")
        self.app = app
        self._capacity = float(requests)
        self._refill_per_second = requests / window_seconds
        self._exempt = tuple(
            _canonical_path(p).rstrip("/") for p in exempt_paths if p and p != "/"
        )
        self._trust_forwarded_for = trust_forwarded_for
        self._max_buckets = max(1, max_buckets)
        self._time = time_fn
        self._buckets: "OrderedDict[str, _Bucket]" = OrderedDict()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or self._is_exempt(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        allowed, retry_after = self._consume(self._client_key(scope))
        if not allowed:
            await self._send_429(send, retry_after)
            return
        await self.app(scope, receive, send)

    # -- token bucket -------------------------------------------------------

    def _consume(self, key: str) -> tuple[bool, int]:
        now = self._time()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self._capacity, updated_at=now)
            self._buckets[key] = bucket
            if len(self._buckets) > self._max_buckets:
                self._buckets.popitem(last=False)  # evict least-recently-seen
        else:
            elapsed = now - bucket.updated_at
            bucket.tokens = min(
                self._capacity, bucket.tokens + elapsed * self._refill_per_second
            )
            bucket.updated_at = now
            self._buckets.move_to_end(key)

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, 0
        # Seconds until one token is available again, rounded up to >= 1.
        deficit = 1.0 - bucket.tokens
        retry_after = max(1, int(deficit / self._refill_per_second) + 1)
        return False, retry_after

    def _client_key(self, scope: Scope) -> str:
        if self._trust_forwarded_for:
            for name, value in scope.get("headers", ()):
                if name == b"x-forwarded-for":
                    first = value.split(b",")[0].strip()
                    if first:
                        return first.decode("latin-1")
        client = scope.get("client")
        if client:
            return str(client[0])
        return "unknown"

    def _is_exempt(self, path: str) -> bool:
        norm = _canonical_path(path)
        return any(norm == base or norm.startswith(base + "/") for base in self._exempt)

    async def _send_429(self, send: Send, retry_after: int) -> None:
        body = b'{"ok": false, "error": "Too Many Requests"}'
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                    (b"retry-after", str(retry_after).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


__all__ = ["RateLimitMiddleware"]
