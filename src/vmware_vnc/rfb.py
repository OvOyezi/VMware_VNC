# SPDX-License-Identifier: Apache-2.0
# Modified for VMware VNC.
"""Project-owned asyncio RFB transport and a small synchronous boundary.

The protocol reader publishes only completed framebuffer updates. Input and
network state live on one asyncio thread; image decoding is offloaded. The
synchronous boundary serves the existing CLI and serialized session workers.
"""
from __future__ import annotations

import asyncio
import math
import struct
import threading
import time
from typing import Any
from uuid import uuid4

from PIL import Image

from .clipboard import ExtendedClipboard, MAX_CLIPBOARD_BYTES
from .rfb_codec import FrameDecoder, PixelFormat, MAX_FRAME_PIXELS, MAX_RECT_BYTES
from .frames import FrameBuffer, FrameSnapshot
from .keys import keysyms
from .runtime import RfbRuntime
from .transport import ByteStream, Endpoint, open_transport


EXT_CLIPBOARD = -1063131698
FENCE = -312
CONTINUOUS = -313
REQUEST = 0x80000000
MAX_NAME_BYTES = 65_536
MAX_UPDATE_RECTS = 65_535

class RFBProtocolError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FramebufferUnavailable(ConnectionError):
    """The endpoint is reachable but currently has no complete display."""


class AsyncRFB:
    """One RFB connection; all methods and mutable protocol state share a loop."""

    def __init__(self, host: str, port: int, password: str | None, timeout: float,
                 encoding: str = "auto", initial_region: tuple[int, int, int, int] | None = None,
                 *, endpoint: Endpoint | None = None, tls_ca_file: str | None = None) -> None:
        self.endpoint = endpoint or Endpoint.from_target(host, port)
        self.tls_ca_file = tls_ca_file
        self.host, self.port, self.password = self.endpoint.host, self.endpoint.port, password
        self.timeout, self.encoding = timeout, encoding
        self.initial_region = initial_region
        self.width = self.height = 0
        self.name = ""
        self.received_bytes = 0
        self.wire_encoding: str | None = None
        self.ext_clipboard = ExtendedClipboard()
        self.clipboard_sent = threading.Event()
        self.closed = threading.Event()
        self.latest_clipboard = ""
        self.clipboard_encoding = "iso-8859-1"
        self.protocol_error: str | None = None
        self.snapshot: FrameSnapshot | None = None
        self._sequence = 0
        self._stream: ByteStream | None = None
        self._frames: FrameBuffer | None = None
        self._reader_task: asyncio.Task | None = None
        self._frame_event = asyncio.Event()
        self._request_lock = asyncio.Lock()
        self._fences: dict[bytes, asyncio.Future] = {}
        self.fence_supported = False
        self.continuous_supported = False
        self.continuous_enabled = False
        self._pointer_x = self._pointer_y = self._buttons = 0
        self._decoder = FrameDecoder(PixelFormat())

    async def _read(self, count: int) -> bytes:
        if not 0 <= count <= MAX_RECT_BYTES:
            raise ValueError("RFB payload exceeds the byte limit")
        if self._stream is None:
            raise ConnectionError("RFB reader is unavailable")
        data = await self._stream.readexactly(count)
        self.received_bytes += len(data)
        return data

    async def _write(self, data: bytes) -> None:
        if self.closed.is_set() or self._stream is None:
            raise ConnectionError(self.protocol_error or "RFB connection is closed")
        await self._stream.write(data)

    async def connect(self) -> None:
        deadline = time.monotonic() + self.timeout
        try:
            async with asyncio.timeout(self.timeout):
                self._stream = await open_transport(self.endpoint, self.timeout, self.tls_ca_file)
                await self._handshake()
            self._reader_task = asyncio.create_task(self._receive(), name="rfb-reader")
            if self.width and self.height:
                try:
                    await self.capture(self.initial_region, timeout=max(0.001, deadline-time.monotonic()))
                except TimeoutError:
                    # A VMware display can retain ServerInit geometry while
                    # asleep and withhold all pixels. Keep only the authenticated
                    # connection for an explicit wake; expose no partial image.
                    if self.closed.is_set():
                        raise ConnectionError(self.protocol_error or "RFB closed before its initial framebuffer")
        except BaseException:
            await self.close()
            raise

    async def _reason(self) -> str:
        length, = struct.unpack("!I", await self._read(4))
        if length > MAX_NAME_BYTES:
            raise ValueError("RFB reason string exceeds the byte limit")
        return (await self._read(length)).decode("utf-8", "replace")[:300]

    async def _handshake(self) -> None:
        banner = await self._read(12)
        if not (banner.startswith(b"RFB ") and banner[7:8] == b"." and banner[-1:] == b"\n"):
            raise ValueError("invalid RFB protocol banner")
        try:
            major, minor = int(banner[4:7]), int(banner[8:11])
        except ValueError as exc:
            raise ValueError("invalid RFB protocol version") from exc
        if major != 3 or minor < 3:
            raise ValueError("unsupported RFB protocol version")
        version = 8 if minor >= 8 else 7 if minor >= 7 else 3
        await self._write(f"RFB 003.{version:03}\n".encode("ascii"))
        if version == 3:
            security, = struct.unpack("!I", await self._read(4))
            if security == 0:
                raise ConnectionError("RFB refused connection: " + await self._reason())
        else:
            count = (await self._read(1))[0]
            if not count:
                raise ConnectionError("RFB refused connection: " + await self._reason())
            offered = await self._read(count)
            security = 2 if self.password is not None and 2 in offered else 1 if 1 in offered else 2 if 2 in offered else 0
            if security == 0:
                raise ValueError("no supported RFB security type (None or VNC Authentication)")
            await self._write(bytes([security]))
        if security == 2:
            if self.password is None:
                raise ValueError("VNC Authentication requires an explicit password")
            from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
            from cryptography.hazmat.primitives.ciphers import Cipher, modes
            challenge = await self._read(16)
            raw = self.password.encode("latin-1", "strict")[:8].ljust(8, b"\0")
            key = bytes(int(f"{value:08b}"[::-1], 2) for value in raw)
            encryptor = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
            await self._write(encryptor.update(challenge) + encryptor.finalize())
        elif security != 1:
            raise ValueError("unsupported RFB security type")
        if security == 2 or version == 8:
            result, = struct.unpack("!I", await self._read(4))
            if result:
                reason = await self._reason() if version == 8 else "authentication failed"
                raise ConnectionError("RFB authentication failed: " + reason)
        await self._write(b"\x01")  # shared display: do not evict other clients
        init = await self._read(24)
        width, height = struct.unpack_from("!HH", init)
        self._validate_size(width, height)
        server_format = PixelFormat.from_bytes(init[4:20])
        name_length, = struct.unpack_from("!I", init, 20)
        if name_length > MAX_NAME_BYTES:
            raise ValueError("RFB desktop name exceeds the byte limit")
        self.name = (await self._read(name_length)).decode("utf-8", "replace")
        self.width, self.height = width, height
        self._frames = FrameBuffer(width, height, self.initial_region) if width and height else None
        # Keep the server's supported true-color layout. VMware consoles may
        # ignore a change to the channel shifts; requesting the advertised
        # format avoids interpreting BGRX bytes as RGBX. Decoders handle the
        # exact advertised masks and byte order rather than guessing by color.
        self._decoder = FrameDecoder(server_format)
        await self._write(b"\x00\0\0\0" + server_format.to_bytes())
        choices = {"auto": [16, 7, 5, 1, 0], "zrle": [16, 0],
                   "tight": [7, 0], "hextile": [5, 0], "raw": [0]}
        if self.encoding not in choices:
            raise ValueError("unknown RFB encoding preference")
        encodings = choices[self.encoding] + [-223, -224, -239, -232, EXT_CLIPBOARD, FENCE, CONTINUOUS]
        await self._write(struct.pack("!BBH", 2, 0, len(encodings)) + struct.pack("!" + "i" * len(encodings), *encodings))

    @staticmethod
    def _validate_size(width: int, height: int) -> None:
        if (width == 0) != (height == 0) or width * height > MAX_FRAME_PIXELS:
            raise ValueError("RFB desktop exceeds the pixel limit or has inconsistent dimensions")

    def _notify_frame(self) -> None:
        event, self._frame_event = self._frame_event, asyncio.Event()
        event.set()

    async def _receive(self) -> None:
        try:
            while not self.closed.is_set():
                kind = (await self._read(1))[0]
                if kind == 0:
                    await self._update()
                elif kind == 1:
                    header = await self._read(5)
                    count, = struct.unpack_from("!H", header, 3)
                    await self._read(count * 6)
                elif kind == 2:
                    continue
                elif kind == 3:
                    await self._clipboard()
                elif kind == 248:
                    await self._fence_message()
                elif kind == 150:
                    self.continuous_supported = True
                else:
                    raise ValueError(f"unsupported RFB server message {kind}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.protocol_error = exc.code if isinstance(exc, RFBProtocolError) else f"{type(exc).__name__}: {str(exc)[:240]}"
        finally:
            await self.close()

    async def _update(self) -> None:
        _, count = struct.unpack("!BH", await self._read(3))
        if count > MAX_UPDATE_RECTS:
            raise ValueError("too many RFB rectangles")
        canvas = await asyncio.to_thread(self._frames.begin_update) if self._frames is not None else None
        for _ in range(count):
            x, y, width, height, encoding = struct.unpack("!HHHHi", await self._read(12))
            if encoding == -224:  # LastRect
                break
            if encoding == -223:
                self._validate_size(width, height)
                self.width, self.height = width, height
                if not width:
                    self._frames = canvas = None
                    self.snapshot = None
                    self._notify_frame()
                elif self._frames is None:
                    self._frames = FrameBuffer(width, height, self.initial_region)
                    canvas = await asyncio.to_thread(self._frames.begin_update)
                else:
                    canvas = await asyncio.to_thread(self._frames.resize, width, height)
                continue
            if encoding == -239:  # separate cursor shape; never paint over pixels
                if width * height > MAX_FRAME_PIXELS:
                    raise ValueError("RFB cursor exceeds the pixel limit")
                await self._read(width * height * self._decoder.pixel_format.bytes_per_pixel
                                 + ((width + 7) // 8) * height)
                continue
            if encoding == -232:
                self._pointer_x, self._pointer_y = x, y
                continue
            if encoding < 0:
                raise ValueError(f"unsupported RFB pseudo-encoding {encoding}")
            if canvas is None or not width or not height or x + width > self.width or y + height > self.height:
                raise ValueError("RFB rectangle is outside the desktop")
            box = (x, y, width, height)
            await self._decoder.decode(self._read, canvas, box, encoding,
                                       validate_copy_source=self._frames.validate_copy_source)
            self.wire_encoding = {0: "raw", 1: "copyrect", 5: "hextile", 7: "tight", 16: "zrle"}.get(encoding, str(encoding))
            self._frames.mark_updated(box)
        if self._frames is None:
            return
        snapshot = await asyncio.to_thread(self._frames.publish)
        if snapshot is None:
            # Keep partial pixels across messages. Request the remaining epoch
            # without exposing a blank or partly initialized desktop to the AI.
            x, y, width, height = self.initial_region or (0, 0, self.width, self.height)
            await self._write(struct.pack("!BBHHHH", 3, 0, x, y, width, height))
            return
        # An empty-display epoch replaces FrameBuffer, so its local counter may
        # restart. Capture sequencing must remain monotonic for this connection.
        self._sequence += 1
        self.snapshot = snapshot
        self._notify_frame()

    async def _clipboard(self) -> None:
        length, = struct.unpack("!3xi", await self._read(7))
        if abs(length) > MAX_CLIPBOARD_BYTES or -4 < length < 0:
            raise RFBProtocolError("clipboard_size_limit")
        payload = await self._read(abs(length))
        if length < 0:
            messages, text, sent = self.ext_clipboard.receive(payload)
            for message in messages:
                await self._write(message)
            if text is not None:
                self.latest_clipboard, self.clipboard_encoding = text, "utf-8"
            if sent:
                self.clipboard_sent.set()
        else:
            self.latest_clipboard, self.clipboard_encoding = payload.decode("latin-1"), "iso-8859-1"

    async def _fence_message(self) -> None:
        flags, length = struct.unpack("!3xIB", await self._read(8))
        if length > 64:
            raise ValueError("RFB Fence payload exceeds 64 bytes")
        payload = await self._read(length)
        self.fence_supported = True
        if flags & REQUEST:
            await self._write(struct.pack("!B3xIB", 248, flags & 3, length) + payload)
        else:
            future = self._fences.pop(payload, None)
            if future is not None and not future.done():
                future.set_result(None)

    async def fence(self) -> None:
        if not self.fence_supported:
            raise ValueError("server has not negotiated Fence")
        payload = uuid4().bytes
        future = asyncio.get_running_loop().create_future()
        self._fences[payload] = future
        try:
            await self._write(struct.pack("!B3xIB", 248, REQUEST | 1, len(payload)) + payload)
            await asyncio.wait_for(future, self.timeout)
        finally:
            self._fences.pop(payload, None)

    async def continuous(self, enabled: bool) -> None:
        if not self.continuous_supported:
            raise ValueError("server has not negotiated ContinuousUpdates")
        await self._write(struct.pack("!BBHHHH", 150, int(enabled), 0, 0, self.width, self.height))
        self.continuous_enabled = enabled

    async def capture(self, region: tuple[int, int, int, int] | None = None,
                      incremental: bool = False, timeout: float | None = None) -> FrameSnapshot:
        if not isinstance(incremental, bool):
            raise ValueError("incremental must be a boolean")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not math.isfinite(timeout) or not 0 < timeout <= 60):
            raise ValueError("capture timeout must be finite and between 0 and 60 seconds")
        async with self._request_lock:
            if not self.width or not self.height:
                raise FramebufferUnavailable("VNC display is unavailable (0x0); explicitly wake the display, then capture a new screenshot")
            if self.snapshot is not None and self.snapshot.valid_region is not None and region != self.snapshot.valid_region:
                raise ValueError("region-only initial connection cannot expose uninitialized pixels outside its region")
            before = self._sequence
            event = self._frame_event
            x, y, width, height = region or (0, 0, self.width, self.height)
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > self.width or y + height > self.height:
                raise ValueError("capture region is outside the desktop")
            await self._write(struct.pack("!BBHHHH", 3, int(incremental), x, y, width, height))
            async with asyncio.timeout(timeout or self.timeout):
                while self._sequence <= before or self.snapshot is None:
                    await event.wait()
                    if self.closed.is_set():
                        raise ConnectionError(self.protocol_error or "RFB connection closed during capture")
                    if not self.width or not self.height:
                        raise FramebufferUnavailable("VNC display became unavailable; explicitly wake it, then capture a new screenshot")
                    event = self._frame_event
            assert self.snapshot is not None
            return self.snapshot

    async def key_down(self, key: str) -> None:
        for value in keysyms(key):
            await self._write(struct.pack("!BB2xI", 4, 1, value))

    async def key_up(self, key: str) -> None:
        for value in reversed(keysyms(key)):
            await self._write(struct.pack("!BB2xI", 4, 0, value))

    async def key_press(self, key: str) -> None:
        await self.key_down(key)
        await self.key_up(key)

    async def pointer(self, x: int | None = None, y: int | None = None,
                      button: int | None = None, down: bool = True) -> None:
        if (x is None) != (y is None):
            raise ValueError("provide both pointer coordinates")
        if x is not None and (type(x) is not int or type(y) is not int
                              or not 0 <= x <= 65535 or not 0 <= y <= 65535):
            # RFB pointer coordinates are unsigned 16-bit values. The session
            # validates them against its latest complete observation, which can
            # be newer than ServerInit on VMware's control connection.
            raise ValueError("pointer coordinates must be unsigned 16-bit integers")
        if not isinstance(down, bool):
            raise ValueError("pointer down must be a boolean")
        if button is not None and (type(button) is not int or not 1 <= button <= 8):
            raise ValueError("RFB mouse button must be an integer from 1 to 8")
        if x is not None:
            self._pointer_x, self._pointer_y = x, y
        if button is not None:
            if not 1 <= button <= 8:
                raise ValueError("RFB mouse button must be 1-8")
            mask = 1 << (button - 1)
            self._buttons = self._buttons | mask if down else self._buttons & ~mask
        await self._write(struct.pack("!BBHH", 5, self._buttons, self._pointer_x, self._pointer_y))

    async def set_clipboard(self, text: str, extended: bool) -> None:
        if extended:
            data, sent = self.ext_clipboard.set_text(text)
            self.clipboard_sent.clear()
            await self._write(data)
            if sent:
                self.clipboard_sent.set()
        else:
            data = text.encode("latin-1", "strict")
            if len(data) > MAX_CLIPBOARD_BYTES:
                raise ValueError("clipboard_size_limit")
            await self._write(struct.pack("!B3xI", 6, len(data)) + data)

    async def close(self, keys: tuple = (), buttons: tuple = ()) -> None:
        if not self.closed.is_set():
            try:
                for key in keys:
                    await self.key_up(key)
                for button in buttons:
                    number = button if type(button) is int else {"left": 1, "middle": 2, "right": 3}[button]
                    await self.pointer(button=number, down=False)
            except (OSError, ConnectionError, ValueError):
                pass
        self.closed.set()
        self._frame_event.set()
        for future in self._fences.values():
            if not future.done():
                future.cancel()
        self._fences.clear()
        task, self._reader_task = self._reader_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        stream, self._stream = self._stream, None
        if stream is not None:
            await stream.close()
        self._frames = None
        self.snapshot = None
        self.latest_clipboard = ""
        self.ext_clipboard.local_text = None
        self.password = None


def runtime_info() -> dict:
    return RfbRuntime.info()


class RFBConnection:
    """Typed synchronous connection boundary for serialized session workers."""

    def __init__(self, host: str, port: int, password: str | None, timeout: float,
                 encoding: str = "auto", initial_region: tuple[int, int, int, int] | None = None,
                 *, url: str | None = None, tls_ca_file: str | None = None) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("RFB timeout must be finite and between 0 and 60 seconds")
        self.endpoint = Endpoint.from_target(host, port, url)
        self.timeout = timeout
        self._state = AsyncRFB(host, port, password, timeout, encoding, initial_region,
                               endpoint=self.endpoint, tls_ca_file=tls_ca_file)
        self.last_snapshot: FrameSnapshot | None = None
        self._close_lock = threading.Lock()
        self._released = False
        self._runtime = RfbRuntime.acquire()
        try:
            self._runtime.call(self._state.connect(), timeout + 1)
            self.last_snapshot = self._state.snapshot
        except BaseException:
            self.close()
            raise

    @property
    def closed(self) -> threading.Event:
        return self._state.closed

    @property
    def ext_clipboard(self) -> ExtendedClipboard:
        return self._state.ext_clipboard

    @property
    def clipboard_sent(self) -> threading.Event:
        return self._state.clipboard_sent

    @property
    def latest_clipboard(self) -> str:
        return self._state.latest_clipboard

    @property
    def clipboard_encoding(self) -> str:
        return self._state.clipboard_encoding

    @property
    def protocol_error(self) -> str | None:
        return self._state.protocol_error

    @property
    def password(self) -> str | None:
        return self._state.password

    @property
    def received_bytes(self) -> int:
        return self._state.received_bytes

    @property
    def wire_encoding(self) -> str | None:
        return self._state.wire_encoding

    @property
    def width(self) -> int:
        return self._state.width

    @property
    def height(self) -> int:
        return self._state.height

    @property
    def name(self) -> str:
        return self._state.name

    @property
    def fence_supported(self) -> bool:
        return self._state.fence_supported

    @property
    def continuous_supported(self) -> bool:
        return self._state.continuous_supported

    @property
    def continuous_enabled(self) -> bool:
        return self._state.continuous_enabled

    @property
    def screen(self) -> Image.Image | None:
        snapshot = self._state.snapshot
        return snapshot.image.copy() if snapshot else None

    @property
    def frame_sequence(self) -> int:
        return self._state._sequence if self._state.snapshot else 0

    @property
    def framebuffer_available(self) -> bool:
        snapshot = self._state.snapshot
        return (snapshot is not None and snapshot.image.size == (self.width, self.height)
                and not self.closed.is_set())

    @property
    def frame_id(self) -> str | None:
        return self._state.snapshot.frame_id if self._state.snapshot else None

    @property
    def frame_time(self) -> float | None:
        return self._state.snapshot.received_at if self._state.snapshot else None

    @property
    def updated_regions(self) -> tuple:
        return self._state.snapshot.updated_regions if self._state.snapshot else ()

    def initial_snapshot(self) -> FrameSnapshot:
        if not self.framebuffer_available or self.last_snapshot is None:
            raise FramebufferUnavailable("RFB did not provide a complete fresh initial framebuffer; no cached image was returned. Explicitly wake the display, then capture a new screenshot")
        return self.last_snapshot

    def capture(self, region: tuple[int, int, int, int] | None = None,
                timeout: float | None = None, incremental: bool = False) -> FrameSnapshot:
        snapshot = self._runtime.call(self._state.capture(region, incremental, timeout), (timeout or self.timeout) + 0.2)
        self.last_snapshot = snapshot
        return snapshot

    def key_down(self, key: str) -> None:
        self._runtime.call(self._state.key_down(key), self.timeout)

    def key_up(self, key: str) -> None:
        self._runtime.call(self._state.key_up(key), self.timeout)

    def key_press(self, key: str) -> None:
        self._runtime.call(self._state.key_press(key), self.timeout)

    def mouse_move(self, x: int, y: int) -> None:
        self._runtime.call(self._state.pointer(x, y), self.timeout)

    def mouse_down(self, button: int) -> None:
        self._runtime.call(self._state.pointer(button=button), self.timeout)

    def mouse_up(self, button: int) -> None:
        self._runtime.call(self._state.pointer(button=button, down=False), self.timeout)

    def type_batch(self, text: str, control: Any) -> None:
        for char in text:
            control.check()
            if char != "\r":
                self.key_press("enter" if char == "\n" else "tab" if char == "\t" else char)

    def set_clipboard(self, text: str) -> None:
        self._runtime.call(self._state.set_clipboard(text, False), self.timeout)

    def set_clipboard_utf8(self, text: str) -> None:
        self._runtime.call(self._state.set_clipboard(text, True), self.timeout)

    def fence(self) -> None:
        self._runtime.call(self._state.fence(), self.timeout + 0.2)

    def continuous(self, enabled: bool) -> None:
        self._runtime.call(self._state.continuous(enabled), self.timeout)

    def close(self, keys: tuple = (), buttons: tuple = ()) -> None:
        with self._close_lock:
            if self._released:
                return
            self._released = True
            try:
                self._runtime.call(self._state.close(keys, buttons), min(self.timeout, 2) + 1)
            finally:
                self.last_snapshot = None
                self._runtime.release()
