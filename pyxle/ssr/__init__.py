"""Server-side rendering utilities for Pyxle."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import-time helpers
    from .renderer import (
        BROWSER_ONLY_GLOBALS,
        BrowserGlobalRenderError,
        ComponentRenderer,
        ComponentRenderError,
        detect_browser_only_global,
        pool_render_factory,
    )
    from .template import render_document, render_error_document
    from .view import build_page_response
    from .worker_pool import SsrWorkerPool, WorkerPoolError

__all__ = [
    "BROWSER_ONLY_GLOBALS",
    "BrowserGlobalRenderError",
    "ComponentRenderError",
    "ComponentRenderer",
    "InlineStyleFragment",
    "RenderResult",
    "detect_browser_only_global",
    "pool_render_factory",
    "SsrWorkerPool",
    "WorkerPoolError",
    "render_document",
    "render_error_document",
    "build_page_response",
    "build_page_navigation_response",
    "build_not_found_response",
]


def __getattr__(name: str) -> Any:  # pragma: no cover - module-level indirection
    if name in {
        "BROWSER_ONLY_GLOBALS",
        "BrowserGlobalRenderError",
        "ComponentRenderer",
        "ComponentRenderError",
        "InlineStyleFragment",
        "RenderResult",
        "detect_browser_only_global",
        "pool_render_factory",
    }:
        module = import_module(".renderer", __name__)
        return getattr(module, name)
    if name in {"SsrWorkerPool", "WorkerPoolError"}:
        module = import_module(".worker_pool", __name__)
        return getattr(module, name)
    if name in {"render_document", "render_error_document"}:
        module = import_module(".template", __name__)
        return getattr(module, name)
    if name in {"build_page_response", "build_page_navigation_response", "build_not_found_response"}:
        module = import_module(".view", __name__)
        return getattr(module, name)
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
