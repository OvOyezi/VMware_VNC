"""Behavioral checks for incremental framebuffer assembly and publication."""
from __future__ import annotations

import argparse
from dataclasses import FrozenInstanceError
import importlib
import json
from pathlib import Path
import sys
import traceback
import unittest


_frames = None


def _paint(image, box, color):
    x, y, width, height = box
    image.paste(color, (x, y, x + width, y + height))


class FrameBufferChecks(unittest.TestCase):
    def test_split_initial_frame_accumulates_before_publish(self):
        buffer = _frames.FrameBuffer(3, 2)
        first_canvas = buffer.begin_update()
        _paint(first_canvas, (0, 0, 3, 1), (240, 10, 20))
        buffer.mark_updated((0, 0, 3, 1))
        self.assertFalse(buffer.ready)
        self.assertIsNone(buffer.publish())
        self.assertIsNone(buffer.snapshot)
        self.assertFalse(buffer.ready)

        # Mutating a stale transaction reference must not change the pending copy.
        first_canvas.paste((0, 255, 0), (0, 0, 3, 1))
        second_canvas = buffer.begin_update()
        self.assertEqual(second_canvas.getpixel((1, 0)), (240, 10, 20))
        self.assertEqual(second_canvas.mode, "RGB")
        _paint(second_canvas, (0, 1, 3, 1), (5, 30, 230))
        buffer.mark_updated((0, 1, 3, 1))
        self.assertTrue(buffer.ready)
        self.assertIsNone(buffer.snapshot)

        snapshot = buffer.publish()
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.sequence, 1)
        self.assertTrue(snapshot.frame_id.endswith(":1"))
        self.assertEqual(snapshot.image.size, (3, 2))
        self.assertEqual(snapshot.image.getpixel((1, 0)), (240, 10, 20))
        self.assertEqual(snapshot.image.getpixel((1, 1)), (5, 30, 230))
        self.assertEqual(snapshot.updated_regions, ((0, 0, 3, 1), (0, 1, 3, 1)))
        self.assertIsNone(snapshot.valid_region)
        self.assertGreater(snapshot.received_at, 0)

    def test_split_resize_keeps_old_snapshot_and_resumes_pending_pixels(self):
        buffer = _frames.FrameBuffer(3, 2)
        canvas = buffer.begin_update()
        _paint(canvas, (0, 0, 3, 2), (20, 40, 60))
        buffer.mark_updated((0, 0, 3, 2))
        old_snapshot = buffer.publish()
        assert old_snapshot is not None
        old_pixels = list(old_snapshot.image.get_flattened_data())

        resized = buffer.resize(2, 3)
        self.assertEqual((buffer.width, buffer.height), (2, 3))
        self.assertEqual(resized.mode, "RGB")
        self.assertEqual(resized.getpixel((1, 1)), (20, 40, 60))
        self.assertFalse(buffer.ready)
        with self.assertRaisesRegex(ValueError, "uninitialized"):
            buffer.validate_copy_source(0, 0, 1, 1)
        _paint(resized, (0, 0, 2, 1), (180, 20, 10))
        buffer.mark_updated((0, 0, 2, 1))
        self.assertIsNone(buffer.publish())
        self.assertIs(buffer.snapshot, old_snapshot)
        self.assertEqual(old_snapshot.image.size, (3, 2))
        self.assertEqual(list(old_snapshot.image.get_flattened_data()), old_pixels)

        # The pending image is detached when publish returns None as well.
        resized.paste((0, 255, 0), (0, 0, 2, 1))
        next_canvas = buffer.begin_update()
        self.assertEqual(next_canvas.getpixel((0, 0)), (180, 20, 10))
        _paint(next_canvas, (0, 1, 2, 2), (10, 80, 210))
        buffer.mark_updated((0, 1, 2, 2))
        new_snapshot = buffer.publish()
        self.assertIsNotNone(new_snapshot)
        assert new_snapshot is not None
        self.assertEqual(new_snapshot.sequence, 2)
        self.assertEqual(new_snapshot.image.size, (2, 3))
        self.assertEqual(new_snapshot.image.getpixel((1, 0)), (180, 20, 10))
        self.assertEqual(new_snapshot.image.getpixel((1, 2)), (10, 80, 210))
        self.assertEqual(new_snapshot.updated_regions,
                         ((0, 0, 2, 1), (0, 1, 2, 2)))
        self.assertEqual(list(old_snapshot.image.get_flattened_data()), old_pixels)

    def test_roi_provenance_controls_ready_and_copyrect_sources(self):
        roi = (1, 1, 3, 1)
        buffer = _frames.FrameBuffer(5, 3, initial_region=roi)
        canvas = buffer.begin_update()
        _paint(canvas, (1, 1, 2, 1), (11, 22, 33))
        buffer.mark_updated((1, 1, 2, 1))
        self.assertFalse(buffer.ready)
        buffer.validate_copy_source(1, 1, 2, 1)
        with self.assertRaisesRegex(ValueError, "uninitialized"):
            buffer.validate_copy_source(0, 0, 1, 1)
        _paint(canvas, (3, 1, 1, 1), (44, 55, 66))
        buffer.mark_updated((3, 1, 1, 1))
        self.assertTrue(buffer.ready)
        snapshot = buffer.publish()
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.valid_region, roi)
        self.assertEqual(snapshot.image.getpixel((1, 1)), (11, 22, 33))
        self.assertEqual(snapshot.image.getpixel((3, 1)), (44, 55, 66))

        update = buffer.begin_update()
        buffer.validate_copy_source(1, 1, 3, 1)
        with self.assertRaisesRegex(ValueError, "uninitialized"):
            buffer.validate_copy_source(4, 1, 1, 1)
        _paint(update, (4, 1, 1, 1), (77, 88, 99))
        buffer.mark_updated((4, 1, 1, 1))
        self.assertEqual(buffer.publish().image.getpixel((4, 1)), (77, 88, 99))

    def test_dimensions_regions_and_integer_types_are_strict(self):
        invalid_sizes = [
            (True, 1), (1, False), (0, 1), (1, 0), (-1, 2),
            (1.5, 2), (2, 1.5), (_frames.MAX_FRAME_PIXELS + 1, 1),
        ]
        for width, height in invalid_sizes:
            with self.subTest(width=width, height=height):
                with self.assertRaises(ValueError):
                    _frames.FrameBuffer(width, height)

        for roi in ((0, 0, 0, 1), (0, 0, 5, 1), (True, 0, 1, 1),
                    (0, 0, 1.0, 1), [0, 0, 1, 1], (0, 0, 1)):
            with self.subTest(roi=roi):
                with self.assertRaises(ValueError):
                    _frames.FrameBuffer(4, 4, initial_region=roi)

        buffer = _frames.FrameBuffer(4, 4, initial_region=(2, 2, 2, 2))
        for width, height in ((True, 4), (0, 4), (4, _frames.MAX_FRAME_PIXELS + 1)):
            with self.subTest(resize=(width, height)):
                with self.assertRaises(ValueError):
                    buffer.resize(width, height)
                self.assertEqual((buffer.width, buffer.height), (4, 4))
        with self.assertRaisesRegex(ValueError, "initial region"):
            buffer.resize(3, 4)
        self.assertEqual((buffer.width, buffer.height), (4, 4))

        canvas = buffer.begin_update()
        invalid_boxes = [
            (0, 0, 0, 1), (0, 0, 5, 1), (-1, 0, 1, 1),
            (True, 0, 1, 1), (0, 0, 1.25, 1), [0, 0, 1, 1],
        ]
        for box in invalid_boxes:
            with self.subTest(box=box):
                with self.assertRaises(ValueError):
                    buffer.mark_updated(box)
        _paint(canvas, (2, 2, 2, 2), (1, 2, 3))
        buffer.mark_updated((2, 2, 2, 2))
        snapshot = buffer.publish()
        self.assertEqual(snapshot.valid_region, (2, 2, 2, 2))

        for args in ((True, 0, 1, 1), (0, 0, 0, 1),
                     (0, 0, 1.0, 1), (3, 3, 2, 1)):
            with self.subTest(copy_source=args):
                with self.assertRaises(ValueError):
                    buffer.validate_copy_source(*args)

    def test_resize_during_transaction_and_transaction_boundaries(self):
        buffer = _frames.FrameBuffer(2, 2)
        with self.assertRaises(RuntimeError):
            buffer.publish()
        with self.assertRaises(RuntimeError):
            buffer.mark_updated((0, 0, 1, 1))
        first = buffer.begin_update()
        with self.assertRaises(RuntimeError):
            buffer.begin_update()
        _paint(first, (0, 0, 2, 2), (90, 80, 70))
        buffer.mark_updated((0, 0, 2, 2))
        resized = buffer.resize(3, 2)
        self.assertEqual(resized.getpixel((1, 1)), (90, 80, 70))
        self.assertFalse(buffer.ready)
        with self.assertRaisesRegex(ValueError, "uninitialized"):
            buffer.validate_copy_source(1, 1, 1, 1)
        _paint(resized, (0, 0, 3, 2), (8, 9, 10))
        buffer.mark_updated((0, 0, 3, 2))
        snapshot = buffer.publish()
        self.assertEqual(snapshot.image.getpixel((2, 1)), (8, 9, 10))
        with self.assertRaises(FrozenInstanceError):
            snapshot.sequence = 99

    def test_sequence_ids_and_region_metadata_are_bounded(self):
        buffer = _frames.FrameBuffer(1030, 1)
        canvas = buffer.begin_update()
        for x in range(buffer.width):
            canvas.putpixel((x, 0), (x % 256, 0, 0))
            buffer.mark_updated((x, 0, 1, 1))
        self.assertIsNone(buffer.snapshot)
        first = buffer.publish()
        self.assertEqual(first.sequence, 1)
        self.assertEqual(len(first.updated_regions), 6)
        self.assertLessEqual(len(first.updated_regions), _frames.MAX_UPDATE_REGIONS)
        for x, y, width, height in first.updated_regions:
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertGreater(width, 0)
            self.assertGreater(height, 0)
            self.assertLessEqual(x + width, buffer.width)
            self.assertLessEqual(y + height, buffer.height)

        later_canvas = buffer.begin_update()
        later_canvas.putpixel((0, 0), (1, 2, 3))
        buffer.mark_updated((0, 0, 1, 1))
        second = buffer.publish()
        self.assertEqual(second.sequence, 2)
        self.assertTrue(second.frame_id.endswith(":2"))
        self.assertNotEqual(first.frame_id, second.frame_id)
        self.assertGreaterEqual(second.received_at, first.received_at)
        other = _frames.FrameBuffer(1, 1)
        other_canvas = other.begin_update()
        _paint(other_canvas, (0, 0, 1, 1), (1, 1, 1))
        other.mark_updated((0, 0, 1, 1))
        self.assertNotEqual(first.frame_id, other.publish().frame_id)


class EvidenceTextResult(unittest.TextTestResult):
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
    return importlib.import_module("vmware_vnc.frames")


def _run_checks() -> dict[str, object]:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FrameBufferChecks)
    tests = list(suite)
    result = unittest.TextTestRunner(
        verbosity=2, resultclass=EvidenceTextResult,
    ).run(suite)
    failed = {detail["test"] for detail in result.failure_details}
    checks = {
        test.id().rsplit(".", 1)[-1].removeprefix("test_"): test.id() not in failed
        for test in tests
    }
    report: dict[str, object] = {"success": result.wasSuccessful(), "checks": checks}
    if result.failure_details:
        report["failure"] = result.failure_details
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="repository root")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    args = parser.parse_args(argv)
    try:
        global _frames
        _frames = _load_module(args.source.resolve())
        report = _run_checks()
    except Exception as exc:
        report = {
            "success": False,
            "checks": {"modules_load": False},
            "failure": {
                "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
