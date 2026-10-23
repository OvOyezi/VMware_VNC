"""Bounded, lossless RFB pixel and rectangle decoding helpers.

This module is deliberately independent from the VNC transport and runtime.
It implements only the lossless encodings used by the client: Raw, CopyRect,
Hextile, ZRLE, and the lossless subset of Tight.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
import zlib

from PIL import Image, ImageDraw


MAX_FRAME_PIXELS = 16_777_216
MAX_RECT_BYTES = 64 * 1024 * 1024
_MAX_TIGHT_COMPACT_LENGTH = (1 << 22) - 1


def _checked_dimensions(width: int, height: int) -> int:
    if (not isinstance(width, int) or isinstance(width, bool)
            or not isinstance(height, int) or isinstance(height, bool)):
        raise ValueError("rectangle dimensions must be integers")
    if width <= 0 or height <= 0:
        raise ValueError("rectangle dimensions must be positive")
    pixels = width * height
    if pixels > MAX_FRAME_PIXELS:
        raise ValueError("rectangle exceeds the pixel limit")
    return pixels


@dataclass(frozen=True)
class PixelFormat:
    bpp: int = 32
    depth: int = 24
    bigendian: bool = False
    truecolor: bool = True
    redmax: int = 255
    greenmax: int = 255
    bluemax: int = 255
    redshift: int = 0
    greenshift: int = 8
    blueshift: int = 16

    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview) -> "PixelFormat":
        if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) != 16:
            raise ValueError("RFB pixel format must be exactly 16 bytes")
        raw = bytes(data)
        result = cls(
            bpp=raw[0], depth=raw[1], bigendian=bool(raw[2]),
            truecolor=bool(raw[3]), redmax=int.from_bytes(raw[4:6], "big"),
            greenmax=int.from_bytes(raw[6:8], "big"),
            bluemax=int.from_bytes(raw[8:10], "big"), redshift=raw[10],
            greenshift=raw[11], blueshift=raw[12],
        )
        result.validate()
        return result

    def to_bytes(self) -> bytes:
        self.validate()
        return bytes((self.bpp, self.depth, int(self.bigendian), int(self.truecolor))) + b"".join((
            self.redmax.to_bytes(2, "big"), self.greenmax.to_bytes(2, "big"),
            self.bluemax.to_bytes(2, "big"), bytes((self.redshift, self.greenshift, self.blueshift)),
            b"\x00\x00\x00",
        ))

    def validate(self) -> None:
        if (not isinstance(self.bpp, int) or isinstance(self.bpp, bool)
                or self.bpp not in (8, 16, 32)):
            raise ValueError("bits-per-pixel must be 8, 16, or 32")
        if (not isinstance(self.depth, int) or isinstance(self.depth, bool)
                or self.depth < 1 or self.depth > self.bpp):
            raise ValueError("pixel depth must be between 1 and bits-per-pixel")
        if not isinstance(self.bigendian, bool) or not isinstance(self.truecolor, bool):
            raise ValueError("pixel format flags must be booleans")
        if not self.truecolor:
            raise ValueError("indexed-color pixel formats are unsupported")

        fields = (
            ("red", self.redmax, self.redshift),
            ("green", self.greenmax, self.greenshift),
            ("blue", self.bluemax, self.blueshift),
        )
        used_mask = 0
        total_component_bits = 0
        for name, maximum, shift in fields:
            if (not isinstance(maximum, int) or isinstance(maximum, bool)
                    or maximum <= 0 or maximum > 0xFFFF
                    or (maximum + 1) & maximum):
                raise ValueError(f"{name} maximum must be a positive all-ones bit mask")
            if not isinstance(shift, int) or isinstance(shift, bool) or shift < 0:
                raise ValueError(f"{name} shift must be a non-negative integer")
            component_bits = maximum.bit_length()
            if shift + component_bits > self.bpp:
                raise ValueError(f"{name} mask exceeds bits-per-pixel")
            mask = maximum << shift
            if used_mask & mask:
                raise ValueError("red, green, and blue masks must not overlap")
            used_mask |= mask
            total_component_bits += component_bits
        if self.depth not in (total_component_bits, self.bpp):
            raise ValueError("pixel depth must match the color masks or bits-per-pixel")

    @property
    def bytes_per_pixel(self) -> int:
        return self.bpp // 8

    @property
    def _byteorder(self) -> str:
        return "big" if self.bigendian else "little"

    def _tbytes(self) -> int:
        if (self.bpp == 32 and self.depth == 24
                and (self.redmax, self.greenmax, self.bluemax) == (255, 255, 255)):
            return 3
        return self.bytes_per_pixel

    def _cpixel_layout(self) -> tuple[int, int]:
        """Return (wire byte count, shift added to the compact integer)."""
        if self.bpp == 32 and self.depth <= 24:
            ranges = (
                (self.redshift, self.redmax.bit_length()),
                (self.greenshift, self.greenmax.bit_length()),
                (self.blueshift, self.bluemax.bit_length()),
            )
            fits_low = all(shift + bits <= 24 for shift, bits in ranges)
            fits_high = all(shift >= 8 for shift, _ in ranges)
            if fits_low or fits_high:
                # The RFB convention selects the least significant three bytes
                # if both layouts are possible.
                return 3, 0 if fits_low else 8
        return self.bytes_per_pixel, 0

    def _component(self, pixel: int, maximum: int, shift: int) -> int:
        value = (pixel >> shift) & maximum
        return (value * 255 + maximum // 2) // maximum

    def _pixel_rgb(self, pixel: int) -> tuple[int, int, int]:
        return (
            self._component(pixel, self.redmax, self.redshift),
            self._component(pixel, self.greenmax, self.greenshift),
            self._component(pixel, self.bluemax, self.blueshift),
        )

    def _zrle_cpixel_rgb(self, data: bytes | bytearray | memoryview) -> tuple[int, int, int]:
        size, extra_shift = self._cpixel_layout()
        if len(data) != size:
            raise ValueError("invalid ZRLE compressed-pixel size")
        pixel = int.from_bytes(data, self._byteorder) << extra_shift
        return self._pixel_rgb(pixel)

    def to_rgb(self, data: bytes | bytearray | memoryview, width: int, height: int,
               compact: bool = False) -> Image.Image:
        self.validate()
        pixels = _checked_dimensions(width, height)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ValueError("pixel data must be bytes-like")
        raw = bytes(data)
        if not isinstance(compact, bool):
            raise ValueError("compact must be a boolean")

        if compact:
            tight_compact = self._tbytes() == 3
            pixel_size = 3 if tight_compact else self._cpixel_layout()[0]
            expected = pixels * pixel_size
            if len(raw) != expected:
                raise ValueError(f"pixel data length must be exactly {expected} bytes")
            if tight_compact:
                # Tight's 32bpp/depth-24 TPIXEL is explicitly RGB byte order.
                rgb = raw
            else:
                pixel_size, extra_shift = self._cpixel_layout()
                if pixel_size != self.bytes_per_pixel:
                    out = bytearray(pixels * 3)
                    for index in range(pixels):
                        start = index * pixel_size
                        pixel = int.from_bytes(raw[start:start + pixel_size], self._byteorder) << extra_shift
                        out[index * 3:index * 3 + 3] = bytes(self._pixel_rgb(pixel))
                    rgb = bytes(out)
                else:
                    rgb = self._native_to_rgb(raw, pixels)
        else:
            expected = pixels * self.bytes_per_pixel
            if len(raw) != expected:
                raise ValueError(f"pixel data length must be exactly {expected} bytes")
            rgb = self._native_to_rgb(raw, pixels)
        return Image.frombytes("RGB", (width, height), rgb)

    def _native_to_rgb(self, raw: bytes, pixels: int) -> bytes:
        size = self.bytes_per_pixel
        out = bytearray(pixels * 3)
        # Byte-aligned RGB888 is the negotiated desktop format. Extended slice
        # copies stay in native code; a Python loop per screen pixel would make
        # a 1080p RAW frame much slower than its network transfer.
        if ((self.redmax, self.greenmax, self.bluemax) == (255, 255, 255)
                and all(shift % 8 == 0 for shift in (self.redshift, self.greenshift, self.blueshift))):
            for channel, shift in enumerate((self.redshift, self.greenshift, self.blueshift)):
                offset = size - 1 - shift // 8 if self.bigendian else shift // 8
                out[channel::3] = raw[offset::size]
            return bytes(out)
        for index in range(pixels):
            start = index * size
            pixel = int.from_bytes(raw[start:start + size], self._byteorder)
            out[index * 3:index * 3 + 3] = bytes(self._pixel_rgb(pixel))
        return bytes(out)


ReadCallable = Callable[[int], Awaitable[bytes]]


async def _read_exact(read: ReadCallable, count: int, *, limit: int = MAX_RECT_BYTES) -> bytes:
    if not isinstance(count, int) or isinstance(count, bool) or count < 0 or count > limit:
        raise ValueError("requested wire data exceeds the byte limit")
    result = bytearray()
    while len(result) < count:
        try:
            chunk = await read(count - len(result))
        except (EOFError, asyncio.IncompleteReadError) as exc:
            raise ValueError("truncated RFB rectangle data") from exc
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise ValueError("RFB reader must return bytes")
        if not chunk:
            raise ValueError("truncated RFB rectangle data")
        if len(chunk) > count - len(result):
            raise ValueError("RFB reader returned more bytes than requested")
        result.extend(chunk)
    return bytes(result)


def _validate_canvas_box(canvas: Image.Image, box: tuple[int, int, int, int], fmt: PixelFormat) -> tuple[int, int, int, int, int]:
    if not isinstance(canvas, Image.Image) or canvas.mode != "RGB":
        raise ValueError("canvas must be a Pillow RGB image")
    if not isinstance(box, (tuple, list)) or len(box) != 4:
        raise ValueError("rectangle box must contain x, y, width, and height")
    x, y, width, height = box
    if any(not isinstance(value, int) or isinstance(value, bool) for value in (x, y, width, height)):
        raise ValueError("rectangle box values must be integers")
    pixels = _checked_dimensions(width, height)
    if x < 0 or y < 0 or x + width > canvas.width or y + height > canvas.height:
        raise ValueError("rectangle is outside the framebuffer canvas")
    if pixels * fmt.bytes_per_pixel > MAX_RECT_BYTES:
        raise ValueError("rectangle pixel data exceeds the byte limit")
    return x, y, width, height, pixels


def _apply_raw(fmt: PixelFormat, canvas: Image.Image, box: tuple[int, int, int, int], data: bytes) -> None:
    x, y, width, height = box
    image = fmt.to_rgb(data, width, height)
    canvas.paste(image, (x, y))


def _apply_hextile(fmt: PixelFormat, canvas: Image.Image, box: tuple[int, int, int, int], data: bytes) -> None:
    x0, y0, width, height = box
    bpp = fmt.bytes_per_pixel
    cursor = 0
    background: bytes | None = None
    foreground: bytes | None = None
    draw = ImageDraw.Draw(canvas)

    def take(count: int, label: str) -> bytes:
        nonlocal cursor
        if count < 0 or cursor + count > len(data):
            raise ValueError(f"truncated Hextile {label}")
        result = data[cursor:cursor + count]
        cursor += count
        return result

    def rgb(pixel: bytes) -> tuple[int, int, int]:
        return fmt.to_rgb(pixel, 1, 1).getpixel((0, 0))

    for tile_y in range(0, height, 16):
        tile_h = min(16, height - tile_y)
        for tile_x in range(0, width, 16):
            tile_w = min(16, width - tile_x)
            flags = take(1, "subencoding")[0]
            if flags & 1:
                raw = take(tile_w * tile_h * bpp, "raw tile")
                tile = fmt.to_rgb(raw, tile_w, tile_h)
                canvas.paste(tile, (x0 + tile_x, y0 + tile_y))
                background = None
                foreground = None
                continue
            if flags & ~0x1E:
                raise ValueError("invalid Hextile subencoding flags")
            if flags & 2:
                background = take(bpp, "background pixel")
            elif background is None:
                raise ValueError("first non-raw Hextile tile must specify its background")
            if flags & 4:
                if flags & 16:
                    raise ValueError("Hextile foreground and colored-subrect flags conflict")
                foreground = take(bpp, "foreground pixel")
            if flags & 8:
                count = take(1, "subrectangle count")[0]
            else:
                count = 0
            tile_left, tile_top = x0 + tile_x, y0 + tile_y
            bg = rgb(background)
            draw.rectangle((tile_left, tile_top, tile_left + tile_w - 1, tile_top + tile_h - 1), fill=bg)
            colored = bool(flags & 16)
            if colored:
                foreground = None
            elif count and foreground is None:
                raise ValueError("Hextile foreground cannot be carried into this tile")
            for _ in range(count):
                color = rgb(take(bpp, "subrectangle pixel")) if colored else rgb(foreground or b"")
                position = take(1, "subrectangle position")[0]
                size = take(1, "subrectangle size")[0]
                sx, sy = position >> 4, position & 0x0F
                sw, sh = (size >> 4) + 1, (size & 0x0F) + 1
                if sx + sw > tile_w or sy + sh > tile_h:
                    raise ValueError("Hextile subrectangle exceeds its tile")
                draw.rectangle((tile_left + sx, tile_top + sy,
                                tile_left + sx + sw - 1, tile_top + sy + sh - 1), fill=color)
            if colored:
                foreground = None
    if cursor != len(data):
        raise ValueError("trailing bytes after Hextile rectangle")


class _Cursor:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, count: int, label: str) -> bytes:
        if count < 0 or self.pos + count > len(self.data):
            raise ValueError(f"truncated ZRLE {label}")
        result = self.data[self.pos:self.pos + count]
        self.pos += count
        return result

    def u8(self, label: str) -> int:
        return self.take(1, label)[0]


def _zrle_decompress(stream: Any, compressed: bytes, limit: int = MAX_RECT_BYTES) -> bytes:
    try:
        raw = stream.decompress(compressed, limit + 1)
    except zlib.error as exc:
        raise ValueError("invalid ZRLE zlib stream") from exc
    if len(raw) > limit or stream.unconsumed_tail:
        raise ValueError("ZRLE decompressed data exceeds the byte limit")
    if stream.unused_data or stream.eof:
        raise ValueError("trailing or terminated data in persistent ZRLE stream")
    return raw


def _zrle_apply(fmt: PixelFormat, canvas: Image.Image, box: tuple[int, int, int, int], raw: bytes) -> None:
    x0, y0, width, height = box
    cursor = _Cursor(raw)
    cpixel_size, extra_shift = fmt._cpixel_layout()
    out = bytearray(width * height * 3)

    def cpixel(label: str) -> tuple[int, int, int]:
        packed = cursor.take(cpixel_size, label)
        pixel = int.from_bytes(packed, fmt._byteorder) << extra_shift
        return fmt._pixel_rgb(pixel)

    def put(px: int, py: int, color: tuple[int, int, int]) -> None:
        index = (py * width + px) * 3
        out[index:index + 3] = bytes(color)

    for tile_y in range(0, height, 64):
        tile_h = min(64, height - tile_y)
        for tile_x in range(0, width, 64):
            tile_w = min(64, width - tile_x)
            subencoding = cursor.u8("tile subencoding")
            rle = bool(subencoding & 0x80)
            palette_size = subencoding & 0x7F
            if not rle and palette_size == 0:
                for row in range(tile_h):
                    colors = [cpixel("raw pixel") for _ in range(tile_w)]
                    for column, color in enumerate(colors):
                        put(tile_x + column, tile_y + row, color)
                continue
            if not rle and palette_size == 1:
                color = cpixel("solid pixel")
                row_data = bytes(color) * tile_w
                for row in range(tile_h):
                    start = ((tile_y + row) * width + tile_x) * 3
                    out[start:start + tile_w * 3] = row_data
                continue
            if not rle and 2 <= palette_size <= 16:
                palette = [cpixel("palette pixel") for _ in range(palette_size)]
                bits = 1 if palette_size == 2 else 2 if palette_size <= 4 else 4
                per_byte = 8 // bits
                row_bytes = (tile_w + per_byte - 1) // per_byte
                for row in range(tile_h):
                    packed = cursor.take(row_bytes, "packed palette row")
                    for column in range(tile_w):
                        shift = 8 - bits * ((column % per_byte) + 1)
                        index = (packed[column // per_byte] >> shift) & ((1 << bits) - 1)
                        if index >= palette_size:
                            raise ValueError("ZRLE palette index is out of range")
                        put(tile_x + column, tile_y + row, palette[index])
                continue
            if not rle and palette_size != 0:
                raise ValueError("invalid ZRLE palette subencoding")
            if rle and palette_size == 1:
                raise ValueError("invalid ZRLE RLE subencoding")
            if rle and palette_size == 0:
                total = tile_w * tile_h
                written = 0
                while written < total:
                    color = cpixel("RLE pixel")
                    run = 1
                    while True:
                        part = cursor.u8("RLE length")
                        run += part
                        if part != 255:
                            break
                    if run > total - written:
                        raise ValueError("ZRLE run exceeds its tile")
                    for offset in range(written, written + run):
                        put(tile_x + offset % tile_w, tile_y + offset // tile_w, color)
                    written += run
                continue

            # Palette RLE encodings use palette sizes 2..127 (encoded as
            # 130..255); each run can span row boundaries.
            if not rle or not 2 <= palette_size <= 127:
                raise ValueError("invalid ZRLE subencoding")
            palette = [cpixel("palette RLE pixel") for _ in range(palette_size)]
            total = tile_w * tile_h
            written = 0
            while written < total:
                code = cursor.u8("palette RLE index")
                index = code & 0x7F
                if index >= palette_size:
                    raise ValueError("ZRLE palette RLE index is out of range")
                run = 1
                if code & 0x80:
                    while True:
                        part = cursor.u8("palette RLE length")
                        run += part
                        if part != 255:
                            break
                if run > total - written:
                    raise ValueError("ZRLE palette RLE run exceeds its tile")
                color = palette[index]
                for offset in range(written, written + run):
                    put(tile_x + offset % tile_w, tile_y + offset // tile_w, color)
                written += run

    if cursor.pos != len(raw):
        raise ValueError("trailing bytes after ZRLE rectangle")
    canvas.paste(Image.frombytes("RGB", (width, height), bytes(out)), (x0, y0))


def _gradient_rgb(fmt: PixelFormat, width: int, height: int, residual: bytes,
                  compact_rgb: bool) -> bytes:
    out = bytearray(width * height * 3)
    if compact_rgb:
        source = residual
        previous = [[0] * width for _ in range(3)]
        for y in range(height):
            current = [[0] * width for _ in range(3)]
            for x in range(width):
                source_pos = (y * width + x) * 3
                target_pos = source_pos
                for channel in range(3):
                    left = current[channel][x - 1] if x else 0
                    above = previous[channel][x]
                    upper_left = previous[channel][x - 1] if x else 0
                    predictor = max(0, min(255, left + above - upper_left))
                    value = (source[source_pos + channel] + predictor) & 0xFF
                    current[channel][x] = value
                    out[target_pos + channel] = value
            previous = current
        return bytes(out)

    size = fmt.bytes_per_pixel
    previous = [[0] * width for _ in range(3)]
    for y in range(height):
        current = [[0] * width for _ in range(3)]
        for x in range(width):
            pos = (y * width + x) * size
            packed = int.from_bytes(residual[pos:pos + size], fmt._byteorder)
            values = []
            for channel, (maximum, shift) in enumerate((
                (fmt.redmax, fmt.redshift),
                (fmt.greenmax, fmt.greenshift),
                (fmt.bluemax, fmt.blueshift),
            )):
                left = current[channel][x - 1] if x else 0
                above = previous[channel][x]
                upper_left = previous[channel][x - 1] if x else 0
                predictor = max(0, min(maximum, left + above - upper_left))
                difference = (packed >> shift) & maximum
                value = (predictor + difference) & maximum
                current[channel][x] = value
                values.append((value * 255 + maximum // 2) // maximum)
            target = (y * width + x) * 3
            out[target:target + 3] = bytes(values)
        previous = current
    return bytes(out)


def _tight_decompress(stream: Any, compressed: bytes, expected: int) -> bytes:
    try:
        raw = stream.decompress(compressed, expected + 1)
    except zlib.error as exc:
        raise ValueError("invalid Tight zlib stream") from exc
    if len(raw) != expected or stream.unconsumed_tail:
        raise ValueError("Tight decompressed data has an invalid length")
    if stream.unused_data or stream.eof:
        raise ValueError("trailing or terminated data in persistent Tight stream")
    return raw


def _tight_apply(fmt: PixelFormat, canvas: Image.Image, box: tuple[int, int, int, int],
                 mode: str, stream_index: int | None, compressed: bytes, use_zlib: bool,
                 expected: int, palette_wire: bytes, palette_count: int,
                 filter_id: int, streams: list[Any]) -> None:
    x, y, width, height = box
    payload = _tight_decompress(streams[stream_index], compressed, expected) if use_zlib else compressed
    if len(payload) != expected:
        raise ValueError("Tight pixel data has an invalid length")
    tbytes = fmt._tbytes()
    compact_rgb = tbytes == 3

    if filter_id == 0:
        image = fmt.to_rgb(payload, width, height, compact=compact_rgb)
    elif filter_id == 1:
        if not 2 <= palette_count <= 256:
            raise ValueError("Tight palette must contain between 2 and 256 colors")
        palette_image = fmt.to_rgb(palette_wire, palette_count, 1, compact=compact_rgb)
        palette = [palette_image.getpixel((index, 0)) for index in range(palette_count)]
        output = bytearray(width * height * 3)
        if palette_count == 2:
            row_bytes = (width + 7) // 8
            for row in range(height):
                packed = payload[row * row_bytes:(row + 1) * row_bytes]
                for column in range(width):
                    index = (packed[column // 8] >> (7 - (column & 7))) & 1
                    target = (row * width + column) * 3
                    output[target:target + 3] = bytes(palette[index])
        else:
            for index, palette_index in enumerate(payload):
                if palette_index >= palette_count:
                    raise ValueError("Tight palette index is out of range")
                target = index * 3
                output[target:target + 3] = bytes(palette[palette_index])
        image = Image.frombytes("RGB", (width, height), bytes(output))
    elif filter_id == 2:
        if fmt.bpp not in (16, 32):
            raise ValueError("Tight gradient filter requires 16- or 32-bit pixels")
        image = Image.frombytes("RGB", (width, height),
                                _gradient_rgb(fmt, width, height, payload, compact_rgb))
    else:
        raise ValueError("unsupported Tight filter")
    canvas.paste(image, (x, y))


class FrameDecoder:
    """Decode bounded RFB rectangle payloads into an RGB framebuffer."""

    def __init__(self, pixel_format: PixelFormat):
        if not isinstance(pixel_format, PixelFormat):
            raise ValueError("pixel_format must be a PixelFormat")
        pixel_format.validate()
        self.pixel_format = pixel_format
        self._zrle: Any = zlib.decompressobj()
        self._tight_streams: list[Any] = [zlib.decompressobj() for _ in range(4)]
        self._decode_lock = asyncio.Lock()

    async def decode(self, read: ReadCallable, canvas: Image.Image,
                     box: tuple[int, int, int, int], encoding: int) -> None:
        fmt = self.pixel_format
        fmt.validate()
        if not callable(read):
            raise ValueError("read must be an async callable")
        x, y, width, height, pixels = _validate_canvas_box(canvas, box, fmt)
        rect = (x, y, width, height)
        if not isinstance(encoding, int) or isinstance(encoding, bool):
            raise ValueError("RFB encoding must be an integer")
        if encoding == 21:
            raise ValueError("lossy JPEG rectangles are unsupported")
        if encoding not in (0, 1, 5, 7, 16):
            raise ValueError(f"unsupported RFB encoding {encoding}")
        if encoding == 7 and width > 2048:
            raise ValueError("Tight rectangle width must not exceed 2048 pixels")
        if encoding == 0 and pixels * fmt.bytes_per_pixel > MAX_RECT_BYTES:
            raise ValueError("raw rectangle exceeds the byte limit")

        async with self._decode_lock:
            if encoding == 0:
                raw = await _read_exact(read, pixels * fmt.bytes_per_pixel)
                await asyncio.to_thread(_apply_raw, fmt, canvas, rect, raw)
            elif encoding == 1:
                source = await _read_exact(read, 4)
                sx = int.from_bytes(source[:2], "big")
                sy = int.from_bytes(source[2:], "big")
                if sx + width > canvas.width or sy + height > canvas.height:
                    raise ValueError("CopyRect source is outside the framebuffer canvas")
                await asyncio.to_thread(_apply_copyrect, canvas, rect, sx, sy)
            elif encoding == 5:
                wire = bytearray()

                async def read_add(count: int) -> bytes:
                    if len(wire) + count > MAX_RECT_BYTES:
                        raise ValueError("Hextile rectangle exceeds the byte limit")
                    part = await _read_exact(read, count)
                    wire.extend(part)
                    return part

                tiles = 0
                bpp = fmt.bytes_per_pixel
                for tile_y in range(0, height, 16):
                    tile_h = min(16, height - tile_y)
                    for tile_x in range(0, width, 16):
                        tile_w = min(16, width - tile_x)
                        flags = (await read_add(1))[0]
                        if flags & 1:
                            await read_add(tile_w * tile_h * bpp)
                        else:
                            if flags & 2:
                                await read_add(bpp)
                            if flags & 4:
                                await read_add(bpp)
                            count = (await read_add(1))[0] if flags & 8 else 0
                            item_size = bpp + 2 if flags & 16 else 2
                            await read_add(count * item_size)
                        tiles += 1
                        if tiles % 512 == 0:
                            await asyncio.sleep(0)
                await asyncio.to_thread(_apply_hextile, fmt, canvas, rect, bytes(wire))
            elif encoding == 16:
                length = int.from_bytes(await _read_exact(read, 4), "big")
                limit = _zrle_limit(fmt, width, height)
                if length <= 0 or length > min(MAX_RECT_BYTES, 2 * limit + 1024):
                    raise ValueError("ZRLE compressed length is outside the byte limit")
                compressed = await _read_exact(read, length)
                await asyncio.to_thread(_decode_zrle_and_apply, self, canvas, rect, compressed)
            elif encoding == 7:
                control = (await _read_exact(read, 1))[0]
                for index in range(4):
                    if control & (1 << index):
                        self._tight_streams[index] = zlib.decompressobj()
                mode = control & 0xF0
                tbytes = fmt._tbytes()
                palette_wire = b""
                palette_count = 0
                filter_id = 0
                stream_index: int | None = None
                use_zlib = False

                if mode == 0x80:
                    fill = await _read_exact(read, tbytes)
                    await asyncio.to_thread(_tight_fill, fmt, canvas, rect, fill)
                    return
                if mode == 0x90:
                    raise ValueError("lossy Tight JPEG rectangles are unsupported")
                if mode in (0xA0, 0xE0):
                    use_zlib = False
                    has_filter = mode == 0xE0
                elif mode <= 0x70:
                    stream_index = (control >> 4) & 0x03
                    use_zlib = True
                    has_filter = bool(control & 0x40)
                else:
                    raise ValueError("invalid Tight compression-control byte")

                if has_filter:
                    filter_id = (await _read_exact(read, 1))[0]
                if filter_id not in (0, 1, 2):
                    raise ValueError("unsupported Tight filter")
                if filter_id == 2 and fmt.bpp not in (16, 32):
                    raise ValueError("Tight gradient filter requires 16- or 32-bit pixels")

                if filter_id == 0 or filter_id == 2:
                    expected = pixels * tbytes
                else:
                    palette_count = (await _read_exact(read, 1))[0] + 1
                    if palette_count < 2:
                        raise ValueError("Tight palette must contain between 2 and 256 colors")
                    palette_bytes = palette_count * tbytes
                    if palette_bytes > MAX_RECT_BYTES:
                        raise ValueError("Tight palette exceeds the byte limit")
                    palette_wire = await _read_exact(read, palette_bytes)
                    expected = ((width + 7) // 8) * height if palette_count == 2 else pixels

                if expected > MAX_RECT_BYTES:
                    raise ValueError("Tight pixel data exceeds the byte limit")
                if not use_zlib or expected < 12:
                    payload = await _read_exact(read, expected)
                else:
                    length = await _read_compact_length(read)
                    if length <= 0 or length > MAX_RECT_BYTES:
                        raise ValueError("Tight compressed length is outside the byte limit")
                    payload = await _read_exact(read, length)
                await asyncio.to_thread(
                    _tight_apply, fmt, canvas, rect, "basic", stream_index, payload,
                    use_zlib and expected >= 12, expected, palette_wire, palette_count,
                    filter_id, self._tight_streams,
                )


def _apply_copyrect(canvas: Image.Image, box: tuple[int, int, int, int], sx: int, sy: int) -> None:
    x, y, width, height = box
    # crop() returns an independent image, so overlapping source/destination
    # regions have the RFB memmove semantics.
    copied = canvas.crop((sx, sy, sx + width, sy + height))
    canvas.paste(copied, (x, y))


def _decode_zrle_and_apply(decoder: FrameDecoder, canvas: Image.Image,
                           box: tuple[int, int, int, int], compressed: bytes) -> None:
    raw = _zrle_decompress(decoder._zrle, compressed, _zrle_limit(decoder.pixel_format, box[2], box[3]))
    _zrle_apply(decoder.pixel_format, canvas, box, raw)


def _zrle_limit(fmt: PixelFormat, width: int, height: int) -> int:
    tiles = ((width + 63) // 64) * ((height + 63) // 64)
    pixel_bytes = fmt._cpixel_layout()[0]
    return min(MAX_RECT_BYTES, width * height * (pixel_bytes + 2) + tiles * (1 + 127 * pixel_bytes))


def _tight_fill(fmt: PixelFormat, canvas: Image.Image, box: tuple[int, int, int, int], pixel: bytes) -> None:
    x, y, width, height = box
    if len(pixel) != fmt._tbytes():
        raise ValueError("invalid Tight fill pixel length")
    color = tuple(pixel) if len(pixel) == 3 else fmt.to_rgb(pixel, 1, 1).getpixel((0, 0))
    canvas.paste(color, (x, y, x + width, y + height))


async def _read_compact_length(read: ReadCallable) -> int:
    first = (await _read_exact(read, 1))[0]
    value = first & 0x7F
    if first & 0x80:
        second = (await _read_exact(read, 1))[0]
        value |= (second & 0x7F) << 7
        if second & 0x80:
            third = (await _read_exact(read, 1))[0]
            value |= third << 14
            if value < (1 << 14):
                raise ValueError("non-canonical Tight compact length")
        elif value < (1 << 7):
            raise ValueError("non-canonical Tight compact length")
    return value
