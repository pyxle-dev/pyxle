"""HTTP proxy utilities that forward asset requests to the local Vite dev server."""

from __future__ import annotations

from typing import AsyncIterator, Iterable, Sequence

import httpx
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse

from pyxle.cli.logger import ConsoleLogger

from .settings import DevServerSettings

_ASSET_SUFFIXES: tuple[str, ...] = (
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".css",
    ".map",
)
_HOT_MODULE_PREFIXES: tuple[str, ...] = ("/@vite", "/@react-refresh")
# Framework-internal namespaces (Pyxle Studio, the overlay WebSocket) serve
# their own assets from the wheel; their ``.js``/``.css`` URLs must never be
# forwarded to the user's Vite instance, which knows nothing about them.
_RESERVED_INTERNAL_PREFIXES: tuple[str, ...] = ("/__pyxle",)
_HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
_SKIP_REQUEST_HEADERS: frozenset[str] = frozenset({"host", "content-length"})


class ViteProxy:
    """Forward HTTP requests for client assets to the Vite development server."""

    def __init__(
        self,
        settings: DevServerSettings,
        *,
        logger: ConsoleLogger | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        asset_suffixes: Sequence[str] = _ASSET_SUFFIXES,
        asset_prefixes: Sequence[str] = _HOT_MODULE_PREFIXES,
    ) -> None:
        self._settings = settings
        self._logger = logger or ConsoleLogger()
        self._asset_suffixes = tuple(asset_suffixes)
        self._asset_prefixes = tuple(asset_prefixes)
        base_url = f"http://{settings.vite_host}:{settings.vite_port}"
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._owns_client = client is None

    def should_proxy(self, request: Request) -> bool:
        """Return ``True`` when the request should be forwarded to Vite."""

        if request.method.upper() not in {"GET", "HEAD"}:
            return False

        path = request.url.path
        if path.startswith(_RESERVED_INTERNAL_PREFIXES):
            return False
        if any(path.startswith(prefix) for prefix in self._asset_prefixes):
            return True

        return path.endswith(self._asset_suffixes)

    async def handle(self, request: Request) -> Response:
        """Forward the given request to Vite and stream the response back."""
        import posixpath

        if not self.should_proxy(request):
            return await self._fallback_response(request)

        # Normalise the path to prevent directory-traversal attacks that
        # could reach Vite's @fs endpoint or other sensitive paths.
        raw_path = request.url.path
        normalised = posixpath.normpath(raw_path)
        if not normalised.startswith("/"):
            normalised = "/" + normalised
        if ".." in normalised.split("/"):
            return PlainTextResponse("Invalid path", status_code=400)

        headers = self._prepare_request_headers(
            request,
            upstream_host=f"{self._settings.vite_host}:{self._settings.vite_port}",
        )
        body = await request.body()
        params: Iterable[tuple[str, str]] = list(request.query_params.multi_items())

        stream_cm = self._client.stream(
                request.method,
                f"http://{self._settings.vite_host}:{self._settings.vite_port}{normalised}",
                params=params,
                headers=headers,
                content=body if body else None,
        )

        try:
            upstream = await stream_cm.__aenter__()
        except httpx.RequestError as exc:
            await stream_cm.__aexit__(None, None, None)
            self._logger.error(
                f"Failed to proxy {request.url.path} to Vite ({exc.__class__.__name__}: {exc})"
            )
            return PlainTextResponse(
                "Vite development server is not reachable",
                status_code=502,
            )

        status_code = upstream.status_code
        raw_headers = self._prepare_response_headers(upstream.headers)

        if status_code >= 500:
            self._logger.error(
                f"[vite-proxy] Upstream responded {status_code} for {request.url.path}"
            )
        elif status_code >= 400:
            self._logger.warning(
                f"[vite-proxy] Upstream responded {status_code} for {request.url.path}"
            )

        async def iterator() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await stream_cm.__aexit__(None, None, None)

        response = StreamingResponse(iterator(), status_code=status_code)
        for key, value in raw_headers:
            response.headers.append(key, value)
        return response

    async def close(self) -> None:
        """Close the shared HTTP client if this proxy owns it."""

        if self._owns_client:
            await self._client.aclose()

    async def _fallback_response(self, request: Request) -> Response:
        return PlainTextResponse(
            f"No Vite proxy route for {request.url.path}", status_code=404
        )

    @staticmethod
    def _prepare_request_headers(
        request: Request, *, upstream_host: str = ""
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in request.headers.items():
            if key.lower() in _SKIP_REQUEST_HEADERS:
                continue
            headers[key] = value
        # Set the correct Host header for the upstream Vite server so that
        # Vite plugins that inspect Host get a trustworthy value.
        if upstream_host:
            headers["host"] = upstream_host
        return headers

    @staticmethod
    def _prepare_response_headers(headers: httpx.Headers) -> list[tuple[str, str]]:
        forwarded: list[tuple[str, str]] = []
        for key, value in headers.multi_items():
            if key.lower() in _HOP_BY_HOP_HEADERS:
                continue
            forwarded.append((key, value))
        return forwarded


__all__ = ["ViteProxy"]
