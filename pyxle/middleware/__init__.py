"""Reusable framework middleware.

Currently ships :class:`RateLimitMiddleware`, a dependency-free token-bucket
rate limiter. Enable it from ``pyxle.config.json`` via the ``rateLimit`` block,
or apply it yourself in a custom ASGI stack.
"""

from __future__ import annotations

from pyxle.middleware.rate_limit import RateLimitMiddleware

__all__ = ["RateLimitMiddleware"]
