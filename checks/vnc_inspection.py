"""Synthetic RGB checks for precise VNC frame inspection."""
from __future__ import annotations

import argparse
import importlib
import io
import json
from pathlib import Path
import sys
import unittest

from PIL import Image


_inspection = None
_visual = None


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
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(InspectionChecks)
    tests = list(suite)
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    failed_ids = {test.id() for test, _ in (*result.failures, *result.errors)}
    checks = {
        test.id().rsplit(".", 1)[-1].removeprefix("test_"): test.id() not in failed_ids
        for test in tests
    }
    report: dict[str, object] = {"success": result.wasSuccessful(), "checks": checks}
    if not result.wasSuccessful():
        report["details"] = output.getvalue()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="repository root")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    args = parser.parse_args(argv)

    global _inspection, _visual
    try:
        _inspection, _visual = _load_modules(args.source.resolve())
        report = _run_checks()
    except Exception as exc:
        report = {"success": False, "checks": {"module_loads": False},
                  "failure": f"{type(exc).__name__}: {exc}"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
