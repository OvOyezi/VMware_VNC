"""Independent golden-wire checks for the bounded RFB rectangle decoder."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
from pathlib import Path
import struct
import sys
import traceback
import unittest
import zlib
from PIL import Image


_codec = None


def _rgb_format():
    return _codec.PixelFormat()


def _pixel(color):
    """Native bytes for the default little-endian R,G,B,unused format."""
    return bytes((color[0], color[1], color[2], 0))


def _compact_length(value):
    if value < 128:
        return bytes((value,))
    if value < 16384:
        return bytes(((value & 0x7F) | 0x80, value >> 7))
    return bytes(((value & 0x7F) | 0x80, ((value >> 7) & 0x7F) | 0x80, value >> 14))


def _zrle_wire(tile_data, compressor=None):
    compressor = compressor or zlib.compressobj()
    compressed = compressor.compress(tile_data) + compressor.flush(zlib.Z_SYNC_FLUSH)
    return struct.pack(">I", len(compressed)) + compressed, compressor


def _tight_compressed(control, filtered_data, compressor=None, filter_id=None):
    compressor = compressor or zlib.compressobj()
    compressed = compressor.compress(filtered_data) + compressor.flush(zlib.Z_SYNC_FLUSH)
    prefix = bytes((control,))
    if filter_id is not None:
        prefix += bytes((filter_id,))
    return prefix + _compact_length(len(compressed)) + compressed, compressor


async def _decode(decoder, canvas, box, encoding, wire, fragment=0):
    position = 0

    async def read(count):
        nonlocal position
        if position >= len(wire):
            return b""
        length = min(count, fragment) if fragment else count
        chunk = wire[position:position + length]
        position += len(chunk)
        return chunk

    await decoder.decode(read, canvas, box, encoding)
    if position != len(wire):
        raise AssertionError(f"decoder left {len(wire) - position} wire bytes unread")


def _run(coro):
    return asyncio.run(coro)


class CodecChecks(unittest.TestCase):
    def assert_pixels(self, image, expected):
        self.assertEqual(list(image.get_flattened_data()), expected)

    def test_pixel_format_wire_roundtrip_and_validation(self):
        fmt = _codec.PixelFormat()
        wire = bytes((32, 24, 0, 1, 0, 255, 0, 255, 0, 255, 0, 8, 16, 0, 0, 0))
        self.assertEqual(_codec.PixelFormat.from_bytes(wire), fmt)
        self.assertEqual(fmt.to_bytes(), wire)
        with self.assertRaisesRegex(ValueError, "exactly 16"):
            _codec.PixelFormat.from_bytes(wire[:-1])
        with self.assertRaisesRegex(ValueError, "indexed-color"):
            _codec.PixelFormat(truecolor=False).validate()
        with self.assertRaisesRegex(ValueError, "overlap"):
            _codec.PixelFormat(greenshift=0).validate()
        with self.assertRaisesRegex(ValueError, "depth"):
            _codec.PixelFormat(depth=15, bpp=16, redmax=31, greenmax=63,
                               bluemax=31, redshift=11, greenshift=5, blueshift=0).validate()

    def test_rgb565_little_and_big_endian(self):
        little = _codec.PixelFormat(
            bpp=16, depth=16, bigendian=False, redmax=31, greenmax=63,
            bluemax=31, redshift=11, greenshift=5, blueshift=0,
        )
        big = _codec.PixelFormat(
            bpp=16, depth=16, bigendian=True, redmax=31, greenmax=63,
            bluemax=31, redshift=11, greenshift=5, blueshift=0,
        )
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
        words = (0xF800, 0x07E0, 0x001F, 0xFFFF)
        self.assert_pixels(little.to_rgb(b"".join(word.to_bytes(2, "little") for word in words), 4, 1), colors)
        self.assert_pixels(big.to_rgb(b"".join(word.to_bytes(2, "big") for word in words), 4, 1), colors)

        # ZRLE CPIXEL may carry the most significant three bytes when all
        # channel fields live in bits 8..31.
        high_bytes = _codec.PixelFormat(
            bpp=32, depth=24, bigendian=True, redmax=255, greenmax=255,
            bluemax=255, redshift=24, greenshift=16, blueshift=8,
        )
        self.assert_pixels(high_bytes.to_rgb(bytes((12, 34, 56)), 1, 1, compact=True), [(12, 34, 56)])
        zrle, _ = _zrle_wire(b"\x01" + bytes((12, 34, 56)))
        canvas = Image.new("RGB", (1, 1))
        _run(_decode(_codec.FrameDecoder(high_bytes), canvas, (0, 0, 1, 1), 16, zrle))
        self.assert_pixels(canvas, [(12, 34, 56)])

        low_bytes = _codec.PixelFormat(
            bpp=32, depth=24, bigendian=False, redmax=255, greenmax=255,
            bluemax=255, redshift=16, greenshift=8, blueshift=0,
        )
        zrle, _ = _zrle_wire(b"\x01" + bytes((56, 34, 12)))
        canvas = Image.new("RGB", (1, 1))
        _run(_decode(_codec.FrameDecoder(low_bytes), canvas, (0, 0, 1, 1), 16, zrle))
        self.assert_pixels(canvas, [(12, 34, 56)])

    def test_raw_and_fragmented_reads(self):
        colors = [(220, 30, 60), (5, 120, 240)]
        canvas = Image.new("RGB", (2, 1))
        wire = _pixel(colors[0]) + _pixel(colors[1])
        _run(_decode(_codec.FrameDecoder(_rgb_format()), canvas, (0, 0, 2, 1), 0, wire, fragment=2))
        self.assert_pixels(canvas, colors)

    def test_copyrect_is_overlap_safe(self):
        colors = [(10, 0, 0), (20, 0, 0), (30, 0, 0), (40, 0, 0)]
        canvas = Image.new("RGB", (4, 1))
        decoder = _codec.FrameDecoder(_rgb_format())
        _run(_decode(decoder, canvas, (0, 0, 4, 1), 0, b"".join(_pixel(c) for c in colors)))
        _run(_decode(decoder, canvas, (1, 0, 3, 1), 1, struct.pack(">HH", 0, 0), fragment=1))
        self.assert_pixels(canvas, [colors[0], colors[0], colors[1], colors[2]])

    def test_hextile_colored_subrectangle(self):
        red, green = (240, 0, 0), (0, 220, 20)
        wire = bytes((2 | 8 | 16,)) + _pixel(red) + b"\x01" + _pixel(green) + b"\x00\x00"
        canvas = Image.new("RGB", (2, 2))
        _run(_decode(_codec.FrameDecoder(_rgb_format()), canvas, (0, 0, 2, 2), 5, wire, fragment=3))
        self.assert_pixels(canvas, [green, red, red, red])

    def test_zrle_narrow_palette_rows_keep_row_padding(self):
        red, blue = (255, 0, 0), (0, 0, 255)
        tile = b"\x02" + bytes(red) + bytes(blue) + b"\x40\xA0"
        wire, _ = _zrle_wire(tile)
        canvas = Image.new("RGB", (3, 2))
        _run(_decode(_codec.FrameDecoder(_rgb_format()), canvas, (0, 0, 3, 2), 16, wire, fragment=2))
        self.assert_pixels(canvas, [red, blue, red, blue, red, blue])

    def test_zrle_raw_and_solid_tiles(self):
        red, green, blue = (255, 0, 0), (0, 255, 0), (0, 0, 255)
        cases = (
            (2, 1, b"\x00" + bytes(red) + bytes(green), [red, green]),
            (2, 1, b"\x01" + bytes(blue), [blue, blue]),
        )
        for width, height, tile, expected in cases:
            with self.subTest(subencoding=tile[0]):
                wire, _ = _zrle_wire(tile)
                canvas = Image.new("RGB", (width, height))
                _run(_decode(_codec.FrameDecoder(_rgb_format()), canvas, (0, 0, width, height), 16, wire))
                self.assert_pixels(canvas, expected)

    def test_zrle_plain_and_palette_rle(self):
        red, blue = (255, 0, 0), (0, 0, 255)
        plain = b"\x80" + bytes(red) + b"\x03" + bytes(blue) + b"\x00"
        palette = b"\x82" + bytes(red) + bytes(blue) + b"\x81\x02\x00\x01"
        for tile, expected in (
            (plain, [red, red, red, red, blue]),
            (palette, [blue, blue, blue, red, blue]),
        ):
            wire, _ = _zrle_wire(tile)
            canvas = Image.new("RGB", (5, 1))
            _run(_decode(_codec.FrameDecoder(_rgb_format()), canvas, (0, 0, 5, 1), 16, wire))
            self.assert_pixels(canvas, expected)

    def test_tight_fill_and_tpixel_rgb_order(self):
        color = (7, 90, 230)
        canvas = Image.new("RGB", (3, 2))
        wire = b"\x80" + bytes(color)
        _run(_decode(_codec.FrameDecoder(_rgb_format()), canvas, (0, 0, 3, 2), 7, wire, fragment=1))
        self.assert_pixels(canvas, [color] * 6)

    def test_tight_basic_copy_and_persistent_stream_reset(self):
        colors = [(x * 20, x * 10, 255 - x * 20) for x in range(4)]
        raw = b"".join(bytes(color) for color in colors)
        compressor = zlib.compressobj()
        first, compressor = _tight_compressed(0x00, raw, compressor)
        canvas = Image.new("RGB", (2, 2))
        decoder = _codec.FrameDecoder(_rgb_format())
        _run(_decode(decoder, canvas, (0, 0, 2, 2), 7, first, fragment=1))
        self.assert_pixels(canvas, colors)

        # FillCompression still applies the reset bit to stream zero.
        fill = b"\x81\x01\x02\x03"
        _run(_decode(decoder, canvas, (0, 0, 2, 2), 7, fill))
        reset_stream = zlib.compressobj()
        second, _ = _tight_compressed(0x01, raw, reset_stream)
        _run(_decode(decoder, canvas, (0, 0, 2, 2), 7, second))
        self.assert_pixels(canvas, colors)

    def test_tight_palette_filter_with_narrow_row_padding(self):
        red, blue = (255, 0, 0), (0, 0, 255)
        # 0x40 selects BasicCompression stream 0 and carries filter id 1.
        wire = b"\x40\x01\x01" + bytes(red) + bytes(blue) + b"\x40\xA0"
        canvas = Image.new("RGB", (3, 2))
        _run(_decode(_codec.FrameDecoder(_rgb_format()), canvas, (0, 0, 3, 2), 7, wire))
        self.assert_pixels(canvas, [red, blue, red, blue, red, blue])

    def test_tight_gradient_filter(self):
        colors = [
            (10, 20, 30), (20, 30, 40), (30, 40, 50),
            (15, 25, 35), (25, 35, 45), (35, 45, 55),
        ]
        residual = bytes((
            10, 20, 30, 10, 10, 10, 10, 10, 10,
            5, 5, 5, 0, 0, 0, 0, 0, 0,
        ))
        wire, _ = _tight_compressed(0x40, residual, filter_id=2)
        canvas = Image.new("RGB", (3, 2))
        _run(_decode(_codec.FrameDecoder(_rgb_format()), canvas, (0, 0, 3, 2), 7, wire, fragment=2))
        self.assert_pixels(canvas, colors)

        rgb565 = _codec.PixelFormat(
            bpp=16, depth=16, bigendian=False, redmax=31, greenmax=63,
            bluemax=31, redshift=11, greenshift=5, blueshift=0,
        )
        # The second pixel's predicted RGB565 value equals its left neighbor,
        # so its packed residual is zero.
        small = b"\x40\x02\x00\xF8\x00\x00"
        canvas = Image.new("RGB", (2, 1))
        _run(_decode(_codec.FrameDecoder(rgb565), canvas, (0, 0, 2, 1), 7, small))
        self.assert_pixels(canvas, [(255, 0, 0), (255, 0, 0)])

    def test_tight_uncompressed_basic_and_no_zlib_filter(self):
        colors = [(i, i + 1, i + 2) for i in range(4)]
        raw = b"".join(bytes(color) for color in colors)
        canvas = Image.new("RGB", (2, 2))
        # BasicCompressionWithoutZlib (0xA0) carries raw CopyFilter bytes.
        _run(_decode(_codec.FrameDecoder(_rgb_format()), canvas, (0, 0, 2, 2), 7, b"\xA0" + raw))
        self.assert_pixels(canvas, colors)
        # Its filtered form (0xE0) also carries raw bytes without a length field.
        flat_gradient = bytes((5, 6, 7, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        gradient = b"\xE0\x02" + flat_gradient
        canvas = Image.new("RGB", (2, 2))
        _run(_decode(_codec.FrameDecoder(_rgb_format()), canvas, (0, 0, 2, 2), 7, gradient))
        self.assert_pixels(canvas, [(5, 6, 7)] * 4)

    def test_lossy_and_unknown_encodings_are_rejected(self):
        canvas = Image.new("RGB", (1, 1))
        decoder = _codec.FrameDecoder(_rgb_format())
        with self.assertRaisesRegex(ValueError, "lossy JPEG"):
            _run(_decode(decoder, canvas, (0, 0, 1, 1), 21, b""))
        with self.assertRaisesRegex(ValueError, "lossy Tight JPEG"):
            _run(_decode(decoder, canvas, (0, 0, 1, 1), 7, b"\x90"))

    def test_bad_dimensions_truncation_and_reader_contract(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            _rgb_format().to_rgb(b"", 0, 1)
        with self.assertRaisesRegex(ValueError, "pixel limit"):
            _rgb_format().to_rgb(b"", 4097, 4097)
        canvas = Image.new("RGB", (2, 2))
        with self.assertRaisesRegex(ValueError, "outside"):
            _run(_decode(_codec.FrameDecoder(_rgb_format()), canvas, (1, 1, 2, 2), 0, b""))
        with self.assertRaisesRegex(ValueError, "truncated"):
            _run(_decode(_codec.FrameDecoder(_rgb_format()), canvas, (0, 0, 1, 1), 0, b"\0\0"))

        async def oversized_reader(count):
            return b"x" * (count + 1)

        with self.assertRaisesRegex(ValueError, "more bytes"):
            _run(_codec.FrameDecoder(_rgb_format()).decode(oversized_reader, canvas, (0, 0, 1, 1), 0))

    def test_oversized_compressed_length_and_trailing_zrle_output(self):
        canvas = Image.new("RGB", (1, 1))
        oversized = struct.pack(">I", _codec.MAX_RECT_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "compressed length"):
            _run(_decode(_codec.FrameDecoder(_rgb_format()), canvas, (0, 0, 1, 1), 16, oversized))

        tile = b"\x01" + bytes((1, 2, 3)) + b"\x00"
        wire, _ = _zrle_wire(tile)
        with self.assertRaisesRegex(ValueError, "trailing bytes"):
            _run(_decode(_codec.FrameDecoder(_rgb_format()), canvas, (0, 0, 1, 1), 16, wire))

    def test_zrle_decompression_bomb_is_bounded(self):
        compressor = zlib.compressobj()
        compressed = compressor.compress(b"x" * (_codec.MAX_RECT_BYTES + 2)) + compressor.flush(zlib.Z_SYNC_FLUSH)
        wire = struct.pack(">I", len(compressed)) + compressed
        canvas = Image.new("RGB", (1, 1))
        with self.assertRaisesRegex(ValueError, "byte limit"):
            _run(_decode(_codec.FrameDecoder(_rgb_format()), canvas, (0, 0, 1, 1), 16, wire, fragment=4096))

    def test_hextile_raw_tile_and_invalid_tile_geometry(self):
        colors = [(x, x + 1, x + 2) for x in range(16)]
        raw_tile = b"\x01" + b"".join(_pixel(color) for color in colors)
        canvas = Image.new("RGB", (16, 1))
        _run(_decode(_codec.FrameDecoder(_rgb_format()), canvas, (0, 0, 16, 1), 5, raw_tile))
        self.assert_pixels(canvas, colors)

        # A 2x1 tile cannot contain a two-pixel subrectangle at x=1.
        bad = b"\x0E" + _pixel((1, 2, 3)) + _pixel((4, 5, 6)) + b"\x01\x10\x10"
        with self.assertRaisesRegex(ValueError, "exceeds its tile"):
            _run(_decode(_codec.FrameDecoder(_rgb_format()), Image.new("RGB", (2, 1)),
                         (0, 0, 2, 1), 5, bad))


class EvidenceTextTestResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failure_details = []

    def _record(self, test, err):
        exc_type, exc, tb = err
        self.failure_details.append({
            "test": test.id(),
            "exception_type": f"{exc_type.__module__}.{exc_type.__qualname__}",
            "message": str(exc),
            "traceback": "".join(traceback.format_exception(exc_type, exc, tb)),
        })

    def addFailure(self, test, err):
        self._record(test, err)
        super().addFailure(test, err)

    def addError(self, test, err):
        self._record(test, err)
        super().addError(test, err)


def _load_module(source: Path):
    src = source / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    return importlib.import_module("vmware_vnc.rfb_codec")


def _run_checks() -> dict[str, object]:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CodecChecks)
    tests = list(suite)
    result = unittest.TextTestRunner(
        verbosity=2, resultclass=EvidenceTextTestResult,
    ).run(suite)
    failed = {detail["test"] for detail in result.failure_details}
    checks = {
        test.id().rsplit(".", 1)[-1].removeprefix("test_"): test.id() not in failed
        for test in tests
    }
    return {"success": result.wasSuccessful(), "checks": checks, "failures": result.failure_details}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="repository root")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    args = parser.parse_args(argv)
    try:
        global _codec
        _codec = _load_module(args.source.resolve())
        report = _run_checks()
    except Exception as exc:
        report = {
            "success": False,
            "checks": {"modules_load": False},
            "failures": [{
                "test": "modules_load",
                "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
