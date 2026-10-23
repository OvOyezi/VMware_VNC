"""Bounded UTF-8 text support for the RFB Extended Clipboard extension.

Wire format: https://github.com/rfbproto/rfbproto/blob/master/rfbproto.rst
This implements text only; it does not copy browser or GPL server code.
"""
from __future__ import annotations

import struct
import zlib


TEXT = 1
CAPS = 1 << 24
REQUEST = 1 << 25
PEEK = 1 << 26
NOTIFY = 1 << 27
PROVIDE = 1 << 28
MAX_CLIPBOARD_BYTES = 1024 * 1024
MAX_TEXT_CHARACTERS = 100_000


def validate_text(text: str) -> None:
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARACTERS:
        raise ValueError("text must be a string of at most 100,000 characters")
    if "\0" in text:
        raise ValueError("clipboard text must not contain NUL")
    try:
        text.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ValueError("text contains an invalid Unicode surrogate") from exc


def wire_text(text: str) -> bytes:
    validate_text(text)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n").encode("utf-8") + b"\0"


def message(flags: int, data: bytes = b"") -> bytes:
    payload = struct.pack("!I", flags) + data
    if len(payload) > MAX_CLIPBOARD_BYTES:
        raise ValueError("extended clipboard message exceeds the byte limit")
    return struct.pack("!B3xi", 6, -len(payload)) + payload


class ExtendedClipboard:
    """Protocol state belongs to one connection and runs on its I/O loop."""

    def __init__(self) -> None:
        self.server_flags = 0
        self.server_text_limit = 0
        self.local_text: str | None = None

    @property
    def supported(self) -> bool:
        return bool(self.server_flags & TEXT and self.server_flags & PROVIDE)

    def provide(self) -> bytes:
        if self.local_text is None:
            raise ValueError("no local clipboard text is available")
        data = wire_text(self.local_text)
        return message(PROVIDE | TEXT, zlib.compress(struct.pack("!I", len(data)) + data))

    def set_text(self, text: str) -> tuple[bytes, bool]:
        validate_text(text)
        if not self.supported:
            raise ValueError("server has not negotiated UTF-8 clipboard support")
        data = wire_text(text)
        if len(data) <= self.server_text_limit:
            self.local_text = text
            return self.provide(), True
        if self.server_flags & NOTIFY:
            self.local_text = text
            return message(NOTIFY | TEXT), False
        raise ValueError("server clipboard limit is too small and notify is unavailable")

    def receive(self, payload: bytes) -> tuple[list[bytes], str | None, bool]:
        if not 4 <= len(payload) <= MAX_CLIPBOARD_BYTES:
            raise ValueError("invalid extended clipboard payload size")
        flags, = struct.unpack_from("!I", payload)
        formats, actions = flags & 0xFFFF, flags & 0xFF000000
        if actions & CAPS:
            if len(payload) != 4 + formats.bit_count() * 4:
                raise ValueError("invalid clipboard capabilities")
            sizes = iter(struct.unpack("!" + "I" * formats.bit_count(), payload[4:]))
            self.server_text_limit = 0
            for bit in range(16):
                if formats & (1 << bit):
                    size = next(sizes)
                    if bit == 0:
                        self.server_text_limit = size
            self.server_flags = flags
            return [message(CAPS | TEXT | REQUEST | PEEK | NOTIFY | PROVIDE, struct.pack("!I", 0))], None, False
        if actions not in (REQUEST, PEEK, NOTIFY, PROVIDE):
            raise ValueError("invalid extended clipboard action")
        if actions != PROVIDE and len(payload) != 4:
            raise ValueError("unexpected clipboard action data")
        if actions == REQUEST:
            if formats & TEXT and self.local_text is not None and self.server_flags & PROVIDE:
                return [self.provide()], None, True
        elif actions == PEEK:
            if self.server_flags & NOTIFY:
                return [message(NOTIFY | (TEXT if self.local_text is not None else 0))], None, False
        elif actions == NOTIFY:
            if formats & TEXT and self.server_flags & REQUEST:
                return [message(REQUEST | TEXT)], None, False
        elif actions == PROVIDE:
            decoder = zlib.decompressobj()
            try:
                data = decoder.decompress(payload[4:], MAX_CLIPBOARD_BYTES + 1)
            except zlib.error as exc:
                raise ValueError("invalid clipboard compression") from exc
            # TigerVNC ends each independent message with Z_SYNC_FLUSH, rather
            # than Z_FINISH. Both frames are valid; exact decoded format sizes
            # below still reject truncated or trailing content.
            framed = decoder.eof or payload.endswith(b"\x00\x00\xff\xff")
            if len(data) > MAX_CLIPBOARD_BYTES or decoder.unconsumed_tail or not framed or decoder.unused_data:
                raise ValueError("clipboard decompression limit or framing error")
            offset, text = 0, None
            for bit in range(16):
                if not formats & (1 << bit):
                    continue
                if offset + 4 > len(data):
                    raise ValueError("truncated clipboard format size")
                size, = struct.unpack_from("!I", data, offset)
                offset += 4
                if size > MAX_CLIPBOARD_BYTES or offset + size > len(data):
                    raise ValueError("invalid clipboard format size")
                if bit == 0:
                    value = data[offset:offset + size]
                    if not value.endswith(b"\0"):
                        raise ValueError("UTF-8 clipboard text is not NUL terminated")
                    try:
                        text = value[:-1].decode("utf-8", "strict").replace("\r\n", "\n")
                    except UnicodeDecodeError as exc:
                        raise ValueError("invalid UTF-8 clipboard text") from exc
                    validate_text(text)
                offset += size
            if offset != len(data):
                raise ValueError("unexpected trailing clipboard data")
            # Ownership changed; do not accidentally serve the previous local text.
            self.local_text = None
            return [], text, False
        return [], None, False
