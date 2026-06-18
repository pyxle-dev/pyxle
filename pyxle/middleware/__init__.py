"""Reusable framework middleware.

Ships :class:`RateLimitMiddleware`, a dependency-free token-bucket rate limiter
(enable it from ``pyxle.config.json`` via the ``rateLimit`` block), and
:class:`StreamingGZipMiddleware`, a streaming-aware gzip compressor used in
production so gzip doesn't buffer streaming-SSR responses.
"""

from __future__ import annotations

from pyxle.middleware.gzip import StreamingGZipMiddleware
from pyxle.middleware.rate_limit import RateLimitMiddleware

__all__ = ["RateLimitMiddleware", "StreamingGZipMiddleware"]
