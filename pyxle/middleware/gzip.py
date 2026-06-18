"""Streaming-aware GZip middleware.

Starlette's stock :class:`~starlette.middleware.gzip.GZipMiddleware` buffers a
*streamed* response: it writes each ASGI body chunk into a ``GzipFile`` but never
flushes the underlying zlib compressor between chunks, so the compressed bytes for
the early chunks (e.g. a streaming-SSR shell) stay in the compressor until the
stream closes. The practical effect is that streaming SSR is **defeated** behind
gzip — the shell can't reach the browser until the whole response (including any
slow ``<Suspense>`` boundary) has finished, so the page arrives all at once.

This drop-in variant is byte-for-byte compatible with Starlette's for buffered
responses, but flushes the compressor (``zlib`` ``Z_SYNC_FLUSH``, via
``GzipFile.flush()``) after **every** streamed chunk. Each chunk's compressed
bytes are therefore emitted immediately, preserving both compression *and*
streaming — the shell flushes first, deferred boundaries stream in after.
"""

from __future__ import annotations

import gzip
import io

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class StreamingGZipMiddleware:
    """GZip middleware that streams compressed chunks as they are produced."""

    def __init__(self, app: ASGIApp, minimum_size: int = 500, compresslevel: int = 9) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            if "gzip" in headers.get("Accept-Encoding", ""):
                responder = _StreamingGZipResponder(self.app, self.minimum_size, self.compresslevel)
                await responder(scope, receive, send)
                return
        await self.app(scope, receive, send)


class _StreamingGZipResponder:
    def __init__(self, app: ASGIApp, minimum_size: int, compresslevel: int) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.send: Send = _unattached_send
        self.initial_message: Message = {}
        self.started = False
        self.content_encoding_set = False
        self.gzip_buffer = io.BytesIO()
        self.gzip_file = gzip.GzipFile(mode="wb", fileobj=self.gzip_buffer, compresslevel=compresslevel)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.send = send
        try:
            await self.app(scope, receive, self.send_with_gzip)
        finally:
            # Deterministically release the compressor. A response that never
            # reached a closing branch — too small to compress, already encoded
            # upstream, or a stream that errored mid-flight — would otherwise leave
            # an open ``GzipFile`` for the garbage collector to finalize, and
            # ``GzipFile.close()`` then writes the gzip trailer into a buffer that
            # may already be closed, surfacing as a noisy "Exception ignored while
            # finalizing file ... I/O operation on closed file" on stderr.
            # ``close()`` is idempotent, so the paths that already closed it are
            # unaffected; close the GzipFile (it flushes into the buffer) first,
            # then the buffer.
            self.gzip_file.close()
            self.gzip_buffer.close()

    async def send_with_gzip(self, message: Message) -> None:
        message_type = message["type"]
        if message_type == "http.response.start":
            # Defer the start message until we know how to rewrite the headers.
            self.initial_message = message
            headers = Headers(raw=self.initial_message["headers"])
            self.content_encoding_set = "content-encoding" in headers
        elif message_type == "http.response.body" and self.content_encoding_set:
            # Already encoded upstream — pass through untouched.
            if not self.started:
                self.started = True
                await self.send(self.initial_message)
            await self.send(message)
        elif message_type == "http.response.body" and not self.started:
            self.started = True
            body = message.get("body", b"")
            more_body = message.get("more_body", False)
            if len(body) < self.minimum_size and not more_body:
                # Too small to bother compressing.
                await self.send(self.initial_message)
                await self.send(message)
            elif not more_body:
                # Whole response in one chunk — buffered compression.
                self.gzip_file.write(body)
                self.gzip_file.close()
                body = self.gzip_buffer.getvalue()

                headers = MutableHeaders(raw=self.initial_message["headers"])
                headers["Content-Encoding"] = "gzip"
                headers["Content-Length"] = str(len(body))
                headers.add_vary_header("Accept-Encoding")
                message["body"] = body

                await self.send(self.initial_message)
                await self.send(message)
            else:
                # First chunk of a streamed response. Length is unknown, so drop
                # Content-Length and flush this chunk's compressed bytes now.
                headers = MutableHeaders(raw=self.initial_message["headers"])
                headers["Content-Encoding"] = "gzip"
                headers.add_vary_header("Accept-Encoding")
                del headers["Content-Length"]

                self.gzip_file.write(body)
                self.gzip_file.flush()  # emit this chunk immediately — the streaming fix
                message["body"] = self.gzip_buffer.getvalue()
                self.gzip_buffer.seek(0)
                self.gzip_buffer.truncate()

                await self.send(self.initial_message)
                await self.send(message)

        elif message_type == "http.response.body":
            # Subsequent chunks of a streamed response.
            body = message.get("body", b"")
            more_body = message.get("more_body", False)

            self.gzip_file.write(body)
            if not more_body:
                self.gzip_file.close()  # final flush + gzip trailer
            else:
                self.gzip_file.flush()  # emit this chunk immediately — the streaming fix

            message["body"] = self.gzip_buffer.getvalue()
            self.gzip_buffer.seek(0)
            self.gzip_buffer.truncate()

            await self.send(message)


async def _unattached_send(message: Message) -> None:
    raise RuntimeError("send awaitable not set")
