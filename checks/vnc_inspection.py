"""Synthetic RGB and loopback adapter checks for VNC inspection."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import importlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

if __package__:
    from .vnc_fidelity import _assert_original_hint, _decode_png, _image_payloads, _mcp_environment, _payload, _result_text
    from .vnc_native_rfb import NativeRfbFixture, _framebuffer_update, _rect
else:
    from vnc_fidelity import _assert_original_hint, _decode_png, _image_payloads, _mcp_environment, _payload, _result_text
    from vnc_native_rfb import NativeRfbFixture, _framebuffer_update, _rect


_inspection = None
_visual = None
_source_root: Path


def _frame(image: Image.Image, x: int = 0, y: int = 0,
           desktop_width: int | None = None,
           desktop_height: int | None = None):
    return _visual.VisualFrame(
        image=image,
        desktop_width=desktop_width if desktop_width is not None else x + image.width,
        desktop_height=desktop_height if desktop_height is not None else y + image.height,
        x=x,
        y=y,
    )


def _open_screenshot(screenshot):
    with Image.open(io.BytesIO(screenshot.data)) as image:
        return image.convert("RGB").copy()


def _packed_rgb(pixels):
    return bytes(channel for pixel in pixels for channel in pixel)


def _led_rect(source: str, mask: int) -> bytes:
    if source == "vmware":
        return _rect(0, 0, 0, 0, struct.pack("!I", mask), encoding=0x574D5668)
    return _rect(0, 0, 0, 0, bytes([mask]), encoding=-261)


class _InspectionPeer(NativeRfbFixture):
    def _initial_update_wire(self) -> bytes:
        initial = super()._initial_update_wire()
        return _framebuffer_update([_led_rect("vmware", 5), initial[4:]])

    def request_count(self) -> int:
        with self.lock:
            return sum(event["type"] == 3 for event in self.events)

    def answer_refresh(self, after: int, color: tuple[int, int, int]) -> None:
        self.wait_for(lambda events: sum(event["type"] == 3 for event in events) > after, timeout=10)
        self.send_update([(0, 0, 2, 2, [color] * 4)])

    def send_report(self, source: str, mask: int) -> None:
        self.send_wire(_framebuffer_update([_led_rect(source, mask)]))
        self.barrier(b"read-reported-leds")

    def client_input(self) -> list[int]:
        with self.lock:
            return [event["type"] for event in self.events if event["type"] in (4, 5, 6)]


def _environment() -> dict[str, str]:
    env = _mcp_environment()
    env.pop("VNC_URL", None)
    return env


@asynccontextmanager
async def _mcp_session():
    params = StdioServerParameters(command=sys.executable, args=[str(_source_root / "main.py"), "mcp"],
                                   cwd=str(_source_root), env=_environment())
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        async with asyncio.timeout(30):
            async with stdio_client(params, errlog=stderr) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    yield session


def _cli_inspect(peer: _InspectionPeer, cwd: Path, *options: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_source_root / "main.py"), "inspect",
         "--host", "127.0.0.1", "--port", str(peer.port), *options],
        cwd=cwd, env=_environment(), capture_output=True, text=True, encoding="utf-8", timeout=15,
    )


class InspectionChecks(unittest.TestCase):
    def test_png_round_trip_rgb_hash_and_input_unchanged(self):
        image = Image.new("RGB", (3, 2))
        image.putdata([(1, 2, 3), (4, 5, 6), (7, 8, 9),
                       (10, 11, 12), (13, 14, 15), (250, 251, 252)])
        original = image.tobytes()
        frame = _frame(image, x=7, y=9, desktop_width=20, desktop_height=20)

        result = _inspection.inspect_frame(frame)
        restored = _open_screenshot(result.images[0][1])

        self.assertEqual(result.images[0][0], "current")
        self.assertEqual(restored.tobytes(), original)
        self.assertEqual(frame.image.tobytes(), original)
        self.assertEqual(result.details["frame"]["rgb_sha256"], _inspection._frame_digest(frame))
        self.assertNotEqual(
            result.details["frame"]["rgb_sha256"],
            _inspection._frame_digest(_frame(image.copy(), x=8, y=9, desktop_width=20, desktop_height=20)),
        )

    def test_single_channel_delta_one_is_measured_at_threshold_zero(self):
        before = Image.new("RGB", (4, 3), (0, 0, 0))
        after = before.copy()
        after.putpixel((2, 1), (1, 0, 0))
        result = _inspection.inspect_frame(
            _frame(after, 10, 20, 40, 40),
            reference=_frame(before, 10, 20, 40, 40),
            points=[(12, 21)],
        )

        self.assertEqual(result.details["comparison"], {
            "changed_pixels": 1,
            "total_pixels": 12,
            "changed_ratio": 1 / 12,
            "max_channel_difference": 1,
            "changed_bbox": [12, 21, 13, 22],
        })
        self.assertEqual(result.details["points"], [{
            "x": 12, "y": 21, "rgb": [1, 0, 0],
            "before_rgb": [0, 0, 0], "delta_rgb": [1, 0, 0],
        }])
        self.assertEqual([name for name, _ in result.images], [
            "current", "detail_current", "detail_reference", "difference_amplified",
        ])
        amplified = _open_screenshot(result.images[-1][1])
        self.assertEqual(amplified.getpixel((2 * 4, 1 * 4)), (16, 0, 0))
        self.assertEqual(result.details["image_metadata"]["difference_amplified"]["color_semantics"],
                         "altered: per-channel absolute difference multiplied by 16 and clipped to 255")

    def test_one_pixel_movement_is_position_sensitive(self):
        before = Image.new("RGB", (3, 2), (0, 0, 0))
        after = before.copy()
        before.putpixel((0, 0), (255, 255, 255))
        after.putpixel((1, 0), (255, 255, 255))

        comparison = _inspection.inspect_frame(
            _frame(after), reference=_frame(before), pixel_threshold=0
        ).details["comparison"]

        self.assertEqual(comparison["changed_pixels"], 2)
        self.assertEqual(comparison["changed_bbox"], [0, 0, 2, 1])
        self.assertEqual(comparison["max_channel_difference"], 255)

    def test_native_origin_region_and_nearest_neighbor_colors(self):
        image = Image.new("RGB", (2, 2))
        image.putdata([(1, 2, 3), (10, 20, 30), (4, 5, 6), (40, 50, 60)])
        original = image.tobytes()
        frame = _frame(image, x=10, y=20, desktop_width=32, desktop_height=32)

        result = _inspection.inspect_frame(frame, points=[(11, 20)],
                                           detail_region=(11, 20, 1, 2), zoom=3)
        detail = result.images[1][1]
        enlarged = _open_screenshot(detail)

        self.assertEqual(detail.x, 11)
        self.assertEqual(detail.y, 20)
        self.assertEqual((detail.width, detail.height), (3, 6))
        self.assertEqual(result.details["image_metadata"]["detail_current"], {
            "origin_x": 11, "origin_y": 20,
            "source_width": 1, "source_height": 2,
            "output_width": 3, "output_height": 6, "scale": 3,
            "color_semantics": "original",
        })
        self.assertEqual(enlarged.tobytes(), _packed_rgb(
            [(10, 20, 30)] * 3 * 3 + [(40, 50, 60)] * 3 * 3
        ))
        self.assertEqual(frame.image.tobytes(), original)

    def test_changed_bbox_detail_is_padded_capped_and_marked_truncated(self):
        before = Image.new("RGB", (600, 20), (0, 0, 0))
        after = before.copy()
        for x in range(100, 400):
            after.putpixel((x, 5), (2, 0, 0))

        result = _inspection.inspect_frame(_frame(after), reference=_frame(before), zoom=1)

        self.assertEqual(result.details["comparison"]["changed_bbox"], [100, 5, 400, 6])
        self.assertEqual(result.details["detail_region"], [122, 0, 256, 14])
        self.assertTrue(result.details["detail_selection"]["truncated"])
        self.assertIn("maximum_256_pixel_side", result.details["detail_selection"]["truncation_reasons"])
        self.assertEqual((result.images[1][1].width, result.images[1][1].height), (256, 14))

    def test_empty_points_and_unchanged_reference_return_only_full_frame(self):
        image = Image.new("RGB", (5, 4), (4, 5, 6))
        frame = _frame(image, 2, 3, 20, 20)

        no_points = _inspection.inspect_frame(frame, points=[])
        unchanged = _inspection.inspect_frame(frame, reference=_frame(image.copy(), 2, 3, 20, 20))

        self.assertEqual([name for name, _ in no_points.images], ["current"])
        self.assertEqual(no_points.details["points"], [])
        self.assertIsNone(no_points.details["detail_region"])
        self.assertEqual(unchanged.details["comparison"]["changed_pixels"], 0)
        self.assertIsNone(unchanged.details["comparison"]["changed_bbox"])
        self.assertEqual([name for name, _ in unchanged.images], ["current"])

    def test_no_reference_first_point_selects_bounded_crop(self):
        frame = _frame(Image.new("RGB", (200, 150), (9, 8, 7)), 30, 40, 300, 300)

        result = _inspection.inspect_frame(frame, points=[(100, 110), (200, 180)], zoom=2)

        self.assertEqual(result.details["detail_region"], [36, 46, 128, 128])
        self.assertEqual(result.details["detail_selection"]["reason"], "first_point")
        self.assertEqual([name for name, _ in result.images], ["current", "detail_current"])
        self.assertEqual(result.details["image_metadata"]["detail_current"]["scale"], 2)

    def test_validation_bounds_and_explicit_size_limits(self):
        frame = _frame(Image.new("RGB", (513, 513), (0, 0, 0)), desktop_width=600, desktop_height=600)
        invalid_calls = [
            lambda: _inspection.inspect_frame(frame, zoom=True),
            lambda: _inspection.inspect_frame(frame, pixel_threshold=False),
            lambda: _inspection.inspect_frame(frame, points=[(0, 0)] * 33),
            lambda: _inspection.inspect_frame(frame, points=[(513, 0)]),
            lambda: _inspection.inspect_frame(frame, points=[(True, 0)]),
            lambda: _inspection.inspect_frame(frame, detail_region=(0, 0, 0, 1)),
            lambda: _inspection.inspect_frame(frame, detail_region=(0, 0, 514, 1)),
            lambda: _inspection.inspect_frame(frame, detail_region=(0, 0, 257, 257), zoom=8),
            lambda: _inspection.inspect_frame(frame, detail_region=(513, 0, 1, 1)),
        ]
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()

        allowed = _inspection.inspect_frame(frame, detail_region=(0, 0, 256, 256), zoom=8)
        self.assertEqual((allowed.images[1][1].width, allowed.images[1][1].height), (2048, 2048))
        self.assertEqual(allowed.details["image_metadata"]["detail_current"]["output_width"] *
                         allowed.details["image_metadata"]["detail_current"]["output_height"],
                         4_194_304)

    def test_reference_geometry_must_match(self):
        frame = _frame(Image.new("RGB", (2, 2)), 1, 1, 10, 10)
        other = _frame(Image.new("RGB", (2, 2)), 2, 1, 10, 10)
        with self.assertRaisesRegex(ValueError, "geometry changed"):
            _inspection.inspect_frame(frame, reference=other)

    async def _connect(self, session: ClientSession, peer: _InspectionPeer) -> dict:
        result = await session.call_tool("vnc_connect", {
            "host": "127.0.0.1", "port": peer.port, "session_id": "inspection",
        })
        self.assertFalse(result.isError, _result_text(result))
        return {"session_id": "inspection", "connection_id": _payload(result)["connection_id"]}

    def test_mcp_keyboard_reports_use_latest_leds_without_capture(self):
        async def run():
            with _InspectionPeer() as peer:
                async with _mcp_session() as session:
                    ids = await self._connect(session, peer)
                    requested = ["caps_lock", "num_lock", "scroll_lock"]
                    before = peer.request_count()
                    for source, expected in (("vmware", [True, False, True]), ("qemu", [False, True, False])):
                        if source == "qemu":
                            await asyncio.to_thread(peer.send_report, source, 2)
                        result = await session.call_tool("vnc_inspect", {**ids, "keyboard_fields": requested})
                        self.assertFalse(result.isError, _result_text(result))
                        details, images = _image_payloads(result)
                        self.assertEqual(details["keyboard"], {
                            "fields": {name: {"value": value, "source": source} for name, value in zip(requested, expected)},
                            "unresolved_fields": [],
                            "visual_evidence": {"available": False, "reason": "all_fields_reported"},
                        })
                        self.assertEqual(details["image_order"], [])
                        self.assertNotIn("frame_id", details)
                        self.assertEqual(images, {})

                    for extra, message in (
                        ({"frame_id": "expired"}, "frame_id"),
                        ({"connection_id": "expired"}, "target changed"),
                        ({"keyboard_fields": []}, "keyboard_fields"),
                        ({"keyboard_fields": ["bogus"]}, "keyboard_fields"),
                        ({"points": []}, "pixel inspection"),
                        ({"reference_id": "some-reference"}, "pixel inspection"),
                        ({"detail_region": {"x": 0, "y": 0, "width": 1, "height": 1}}, "pixel inspection"),
                        ({"zoom": 2}, "pixel inspection"),
                        ({"pixel_threshold": 1}, "pixel inspection"),
                    ):
                        with self.subTest(extra=extra):
                            result = await session.call_tool("vnc_inspect", {**ids, "keyboard_fields": requested, **extra})
                            self.assertTrue(result.isError, _result_text(result))
                            self.assertIn(message, _result_text(result).lower())
                    await asyncio.to_thread(peer.barrier, b"no-input-or-refresh")
                    self.assertEqual(peer.request_count(), before)
                    self.assertEqual(peer.client_input(), [])
        asyncio.run(run())

    def test_mcp_keyboard_evidence_reuses_original_frame_and_native_crop(self):
        async def run():
            with _InspectionPeer() as peer:
                async with _mcp_session() as session:
                    ids = await self._connect(session, peer)
                    unresolved = ["ime_name", "ime_mode", "character_width", "composition"]
                    fields = [*unresolved, "num_lock"]
                    before = peer.request_count()
                    pixel = (52, 138, 203)
                    result, _ = await asyncio.gather(
                        session.call_tool("vnc_inspect", {**ids, "keyboard_fields": fields}),
                        asyncio.to_thread(peer.answer_refresh, before, pixel),
                    )
                    self.assertFalse(result.isError, _result_text(result))
                    details, images = _image_payloads(result)
                    frame_id = details["frame_id"]
                    self.assertEqual(details["image_order"], ["current"])
                    self.assertEqual(set(images), {"current"})
                    self.assertEqual(details["keyboard"]["unresolved_fields"], unresolved)
                    self.assertEqual(details["keyboard"]["fields"], {
                        **{name: {"value": None, "source": None} for name in unresolved},
                        "num_lock": {"value": False, "source": "vmware"},
                    })
                    evidence = details["keyboard"]["visual_evidence"]
                    self.assertTrue(evidence["available"])
                    self.assertEqual(evidence["frame_id"], frame_id)
                    self.assertIn("unambiguous", evidence["instruction"])
                    self.assertFalse(details["cached_frame"])
                    _assert_original_hint(self, images["current"][1])
                    self.assertEqual(_decode_png(images["current"][1]).tobytes(), bytes(pixel) * 4)

                    peer.send_update([(1, 1, 1, 1, [(7, 9, 11)])])
                    await asyncio.to_thread(peer.barrier, b"newer-wire-pixels")
                    result = await session.call_tool("vnc_inspect", {
                        **ids, "keyboard_fields": fields, "frame_id": frame_id,
                        "region": {"x": 1, "y": 1, "width": 1, "height": 1},
                    })
                    self.assertFalse(result.isError, _result_text(result))
                    details, images = _image_payloads(result)
                    self.assertTrue(details["cached_frame"])
                    self.assertEqual(details["keyboard"]["visual_evidence"]["frame_id"], frame_id)
                    self.assertEqual(set(images), {"current"})
                    mapping, image = images["current"]
                    self.assertEqual(mapping["mapping"]["origin_x"], 1)
                    self.assertEqual(mapping["mapping"]["origin_y"], 1)
                    self.assertEqual(mapping["mapping"]["scale"], 1)
                    self.assertEqual(mapping["mapping"]["color_semantics"], "original")
                    self.assertEqual(_decode_png(image).tobytes(), bytes(pixel))

                    direct = await session.call_tool("vnc_inspect", {
                        **ids, "frame_id": frame_id, "keyboard_fields": ["num_lock"],
                    })
                    self.assertFalse(direct.isError, _result_text(direct))
                    details, images = _image_payloads(direct)
                    self.assertEqual(details["keyboard"]["unresolved_fields"], [])
                    self.assertNotIn("frame_id", details)
                    self.assertEqual(images, {})

                    normal = await session.call_tool("vnc_inspect", {
                        **ids, "frame_id": frame_id, "points": [{"x": 1, "y": 1}],
                    })
                    self.assertFalse(normal.isError, _result_text(normal))
                    details, images = _image_payloads(normal)
                    self.assertNotIn("keyboard", details)
                    self.assertEqual(details["points"], [{"x": 1, "y": 1, "rgb": list(pixel)}])
                    self.assertEqual(set(images), {"current", "detail_current"})
                    await asyncio.to_thread(peer.barrier, b"pinned-did-not-refresh")
                    self.assertEqual(peer.request_count(), before + 1)
                    self.assertEqual(peer.client_input(), [])
        asyncio.run(run())

    def test_mcp_keyboard_keeps_protocol_values_when_framebuffer_unavailable(self):
        async def run():
            with _InspectionPeer(width=0, height=0) as peer:
                async with _mcp_session() as session:
                    ids = await self._connect(session, peer)
                    await asyncio.to_thread(peer.wait_for, lambda events: any(event["type"] == 2 for event in events))
                    unreported = await session.call_tool("vnc_inspect", {
                        **ids, "keyboard_fields": ["scroll_lock"],
                    })
                    self.assertFalse(unreported.isError, _result_text(unreported))
                    unknown = _payload(unreported)["keyboard"]
                    self.assertEqual(unknown["fields"], {"scroll_lock": {"value": None, "source": None}})
                    self.assertEqual(unknown["unresolved_fields"], ["scroll_lock"])
                    await asyncio.to_thread(peer.send_report, "vmware", 2)
                    result = await session.call_tool("vnc_inspect", {
                        **ids, "keyboard_fields": ["num_lock", "ime_mode"],
                    })
                    self.assertFalse(result.isError, _result_text(result))
                    details, images = _image_payloads(result)
                    self.assertEqual(details["keyboard"], {
                        "fields": {
                            "num_lock": {"value": True, "source": "vmware"},
                            "ime_mode": {"value": None, "source": None},
                        },
                        "unresolved_fields": ["ime_mode"],
                        "visual_evidence": {"available": False, "reason": "framebuffer_unavailable"},
                    })
                    self.assertEqual(images, {})
                    self.assertEqual(peer.request_count(), 0)
                    self.assertEqual(peer.client_input(), [])
                    disconnected = await session.call_tool("vnc_disconnect", ids)
                    self.assertFalse(disconnected.isError, _result_text(disconnected))
                    inactive = await session.call_tool("vnc_inspect", {
                        **ids, "keyboard_fields": ["num_lock", "ime_mode"],
                    })
                    self.assertTrue(inactive.isError, _result_text(inactive))
        asyncio.run(run())

    def test_cli_keyboard_selects_png_only_when_needed(self):
        with tempfile.TemporaryDirectory() as workdir:
            cwd = Path(workdir)
            explicit = cwd / "unused"
            default = cwd / "vnc-inspection"
            with _InspectionPeer() as peer:
                direct = _cli_inspect(peer, cwd, "--keyboard-field", "caps_lock",
                                      "--keyboard-field", "num_lock", "--output-dir", str(explicit))
                self.assertEqual(direct.returncode, 0, direct.stderr)
                details = json.loads(direct.stdout)
                self.assertEqual(details["keyboard"], {
                    "fields": {"caps_lock": {"value": True, "source": "vmware"},
                               "num_lock": {"value": False, "source": "vmware"}},
                    "unresolved_fields": [],
                    "visual_evidence": {"available": False, "reason": "all_fields_reported"},
                })
                self.assertEqual(details["saved"], {})
                self.assertFalse(explicit.exists())
                self.assertFalse(default.exists())
                self.assertEqual(peer.request_count(), 1)
                self.assertEqual(peer.client_input(), [])

            with _InspectionPeer(width=0, height=0) as peer:
                unavailable = _cli_inspect(peer, cwd, "--keyboard-field", "ime_mode")
                self.assertEqual(unavailable.returncode, 0, unavailable.stderr)
                self.assertEqual(json.loads(unavailable.stdout)["keyboard"]["visual_evidence"], {
                    "available": False, "reason": "framebuffer_unavailable",
                })
                self.assertFalse(default.exists())
                self.assertEqual(peer.request_count(), 0)

            async def with_image(peer, *options):
                return await asyncio.gather(
                    asyncio.to_thread(_cli_inspect, peer, cwd, *options),
                    asyncio.to_thread(peer.answer_refresh, 1, (12, 34, 56)),
                )

            with _InspectionPeer() as peer:
                visual, _ = asyncio.run(with_image(peer, "--keyboard-field", "ime_mode",
                                                  "--region", "0", "1", "1", "1"))
                self.assertEqual(visual.returncode, 0, visual.stderr)
                details = json.loads(visual.stdout)
                self.assertEqual(details["keyboard"]["fields"], {"ime_mode": {"value": None, "source": None}})
                self.assertEqual(details["keyboard"]["visual_evidence"]["frame_id"], details["frame_id"])
                self.assertEqual(details["saved"], {"current": str((default / "current.png").resolve())})
                self.assertEqual(list(default.iterdir()), [default / "current.png"])
                with Image.open(default / "current.png") as image:
                    self.assertEqual(image.format, "PNG")
                    self.assertEqual(image.size, (1, 1))
                    self.assertEqual(image.getpixel((0, 0)), (12, 34, 56))
                self.assertEqual(peer.request_count(), 2)
                self.assertEqual(peer.client_input(), [])

            with _InspectionPeer() as peer:
                ordinary, _ = asyncio.run(with_image(peer, "--output-dir", str(explicit), "--point", "0", "1"))
                self.assertEqual(ordinary.returncode, 0, ordinary.stderr)
                details = json.loads(ordinary.stdout)
                self.assertNotIn("keyboard", details)
                self.assertEqual(details["points"], [{"x": 0, "y": 1, "rgb": [12, 34, 56]}])
                self.assertEqual(set(details["saved"]), {"current", "detail_current"})
                self.assertEqual(peer.request_count(), 2)

    def test_cli_keyboard_rejects_pixel_arguments_before_connect(self):
        with tempfile.TemporaryDirectory() as workdir, _InspectionPeer() as peer:
            invalid_options = (
                ("--reference-file", "unused.png"), ("--point", "0", "0"),
                ("--detail-region", "0", "0", "1", "1"), ("--zoom", "3"),
                ("--pixel-threshold", "1"),
            )
            for options in invalid_options:
                with self.subTest(options=options):
                    result = _cli_inspect(peer, Path(workdir), "--keyboard-field", "caps_lock", *options)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("pixel inspection", result.stderr)
            ordinary = _cli_inspect(peer, Path(workdir))
            self.assertEqual(ordinary.returncode, 2, ordinary.stderr)
            self.assertIn("--output-dir", ordinary.stderr)
            self.assertEqual(peer.events, [])


def _load_modules(source_root: Path):
    package_root = source_root / "src"
    module_path = package_root / "vmware_vnc" / "inspection.py"
    if not module_path.is_file():
        raise FileNotFoundError(f"Inspection module not found: {module_path}")
    sys.path.insert(0, str(package_root))
    inspection = importlib.import_module("vmware_vnc.inspection")
    visual = importlib.import_module("vmware_vnc.visual")
    if Path(inspection.__file__).resolve() != module_path.resolve():
        raise ImportError(f"Inspection module loaded from a different source: {inspection.__file__}")
    return inspection, visual


def _run_checks() -> dict[str, object]:
    if __package__:
        from ._unittest_report import run_cases
    else:
        from _unittest_report import run_cases

    output = io.StringIO()
    checks, result = run_cases(InspectionChecks, stream=output)
    report: dict[str, object] = {"success": result.wasSuccessful(), "checks": checks, "failures": result.failure_details}
    if not result.wasSuccessful():
        report["details"] = output.getvalue()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="repository root")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    args = parser.parse_args(argv)

    global _inspection, _visual, _source_root
    try:
        _source_root = args.source.resolve()
        _inspection, _visual = _load_modules(_source_root)
        report = _run_checks()
    except Exception as exc:
        report = {"success": False, "checks": {"module_loads": False},
                  "failure": f"{type(exc).__name__}: {exc}"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
