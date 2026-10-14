"""``POST /media``'s ``multipart/form-data`` body, read as it arrives (FR-MEDIA-1, FR-API-4;
#105).

The upload route takes no body parameter, so FastAPI reads nothing before the route's
dependencies have run: the credential and the principal are checked before a byte of the body
is read. Then this module parses the stream with ``python_multipart``'s streaming parser:

- the first part must be ``metadata``, a JSON document of at most 256 KiB
  (``core.media.assets.parse_metadata`` validates it: 422 at ``body.metadata.<field>``);
- the second part must be ``file``: its bytes are handed to the Engine chunk by chunk
  (``UploadForm.chunks``), with the part's own ``Content-Type``, and are never held in memory
  whole;
- a video's poster may follow as a third part, ``poster_file`` (#107): streamed the same way
  (``UploadForm.poster``, read once the ``file`` part has ended);
- nothing else may follow (another part is 422 at ``body``).

A body that is not ``multipart/form-data`` with a boundary, that is malformed, whose parts come
in another order, or that ends before its closing boundary is 422 ``validation_error`` at
``body`` (or ``body.metadata``, ``body.file``). Part headers are bounded (8 headers of at most
4 KiB each).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import Request
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header

from epistrel.core.errors import InvalidRequest
from epistrel.core.media.assets import (
    METADATA_MAX_BYTES,
    POSTER_FILE_FIELD,
    MediaUpload,
    UploadFile,
    parse_metadata,
)

if TYPE_CHECKING:
    from python_multipart.multipart import MultipartCallbacks

METADATA = "metadata"
FILE = "file"
MAX_HEADER_COUNT = 8
MAX_HEADER_SIZE = 4 * 1024


def _refused(message: str, field_name: str = "body") -> InvalidRequest:
    return InvalidRequest(message, {"field": field_name})


@dataclass
class _Sink:
    """Collects the parser's callbacks as events: ``("part", name, content_type)``,
    ``("data", bytes)`` and ``("end",)``."""

    events: list[tuple[Any, ...]] = field(default_factory=list)
    headers: dict[bytes, bytes] = field(default_factory=dict)
    name: bytearray = field(default_factory=bytearray)
    value: bytearray = field(default_factory=bytearray)
    finished: bool = False

    def callbacks(self) -> MultipartCallbacks:
        def on_part_begin() -> None:
            self.headers.clear()

        def on_header_field(data: bytes, start: int, end: int) -> None:
            self.name += data[start:end]

        def on_header_value(data: bytes, start: int, end: int) -> None:
            self.value += data[start:end]

        def on_header_end() -> None:
            self.headers[bytes(self.name).lower()] = bytes(self.value)
            self.name.clear()
            self.value.clear()

        def on_headers_finished() -> None:
            _, options = parse_options_header(self.headers.get(b"content-disposition"))
            name = options.get(b"name")
            content_type = self.headers.get(b"content-type")
            self.events.append(
                (
                    "part",
                    name.decode("latin-1") if name is not None else None,
                    content_type.decode("latin-1") if content_type is not None else None,
                )
            )

        def on_part_data(data: bytes, start: int, end: int) -> None:
            if end > start:
                self.events.append(("data", bytes(data[start:end])))

        def on_part_end() -> None:
            self.events.append(("end",))

        def on_end() -> None:
            self.finished = True

        return {
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
            "on_end": on_end,
        }


def _boundary(request: Request) -> bytes:
    kind, options = parse_options_header(request.headers.get("content-type"))
    boundary = options.get(b"boundary")
    if kind != b"multipart/form-data" or not boundary:
        raise _refused("an upload is a multipart/form-data body (parts `metadata`, then `file`)")
    return boundary


async def _events(request: Request) -> AsyncIterator[tuple[Any, ...]]:
    sink = _Sink()
    try:
        parser = MultipartParser(
            _boundary(request),
            sink.callbacks(),
            max_header_count=MAX_HEADER_COUNT,
            max_header_size=MAX_HEADER_SIZE,
        )
        async for chunk in request.stream():
            if chunk:
                parser.write(chunk)
            while sink.events:
                yield sink.events.pop(0)
        parser.finalize()
    except FormParserError:
        raise _refused("the multipart body is malformed") from None
    while sink.events:
        yield sink.events.pop(0)
    if not sink.finished:
        raise _refused("the multipart body ends before its closing boundary")


@dataclass
class UploadForm:
    """An upload in progress: its validated ``metadata``, the ``file`` part's own
    ``Content-Type``, and the stream positioned at the file's first byte."""

    metadata: MediaUpload
    content_type: str | None
    _events: AsyncIterator[tuple[Any, ...]]

    async def _part(self) -> AsyncIterator[bytes]:
        async for event in self._events:
            if event[0] == "data":
                yield event[1]
            elif event[0] == "end":
                return

    async def _end(self, after: str) -> None:
        async for event in self._events:
            if event[0] == "part":
                raise _refused(f"nothing may follow the `{after}` part")

    async def chunks(self) -> AsyncIterator[bytes]:
        """The file's bytes as they arrive (then ``poster`` reads what follows)."""
        async for chunk in self._part():
            yield chunk

    async def poster(self) -> UploadFile | None:
        """Once the file's bytes are read: the ``poster_file`` part that follows, or ``None``
        when the body ends there (422 for any other part or a truncated body)."""
        async for event in self._events:
            if event[0] != "part":
                continue
            if event[1] != POSTER_FILE_FIELD:
                raise _refused(f"only a `{POSTER_FILE_FIELD}` part may follow the `file` part")
            return UploadFile(content=self._poster_chunks(), content_type=event[2])
        return None

    async def _poster_chunks(self) -> AsyncIterator[bytes]:
        async for chunk in self._part():
            yield chunk
        await self._end(POSTER_FILE_FIELD)


async def read_upload(request: Request) -> UploadForm:
    """Parse the ``metadata`` part and stop at the start of the ``file`` part (see the module
    docstring)."""
    events = _events(request)
    first = await anext(events, None)
    if first is None or first[0] != "part" or first[1] != METADATA:
        raise _refused("the first part must be `metadata`", "body.metadata")
    raw = bytearray()
    async for event in events:
        if event[0] == "end":
            break
        if event[0] == "data":
            raw += event[1]
            if len(raw) > METADATA_MAX_BYTES:
                raise _refused(
                    f"the metadata part is larger than {METADATA_MAX_BYTES} bytes", "body.metadata"
                )
    metadata = parse_metadata(bytes(raw))
    second = await anext(events, None)
    if second is None or second[0] != "part" or second[1] != FILE:
        raise _refused("the second part must be `file`", "body.file")
    return UploadForm(metadata=metadata, content_type=second[2], _events=events)


def declared_length(request: Request) -> int | None:
    """The body's ``Content-Length``, when the request declares one."""
    value = request.headers.get("content-length")
    if value is None or not value.strip().isdigit():
        return None
    return int(value)


__all__ = ["FILE", "METADATA", "UploadForm", "declared_length", "read_upload"]
