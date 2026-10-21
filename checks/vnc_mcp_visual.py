"""Standard-library checks for visual comparisons and read-only MCP waits.

The integration check starts the local MCP entry point and uses only the
64x48 synthetic RFB fixture from vnc_mcp_regression. It never connects to a
real desktop or sends keyboard or pointer input.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from io import BytesIO
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
import unittest
from unittest import mock

from mcp import types as mcp_types
from mcp.shared.exceptions import McpError
from PIL import Image


_visual = None
_regression = None
_sessions = None
_operations = None
_source_root: Path | None = None
_integration_checks: dict[str, bool] = {}
_server_stderr = ""

_INTEGRATION_CHECK_NAMES = (
    "stdio_exposes_vnc_wait_for",
    "remembered_reference_and_image_payload",
    "matches_and_timeout_return_final_image",
    "changed_condition_detects_pixel_change",
    "stable_condition_succeeds",
    "reference_replacement_and_plain_capture",
    "reference_is_scoped_to_connection_and_session",
    "reconnect_expires_reference",
    "same_session_operation_and_second_wait",
    "cancelled_wait_cleans_up",
    "visual_wait_sends_no_input",
    "cli_timeout_exit_3_saves_last_image",
    "cli_stable_exit_0_saves_image",
    "roi_refresh_first_frame_uses_native_coordinates",
    "roi_refresh_reference_and_wait_request_roi",
    "roi_fresh_first_frame_uses_native_coordinates",
    "roi_fresh_reference_and_wait_request_roi",
)


class VisualComparisonChecks(unittest.TestCase):
    def test_pixel_positions_matter_even_when_histograms_match(self):
        left = Image.new("RGB", (2, 1))
        left.putdata([(255, 0, 0), (0, 0, 255)])
        swapped = Image.new("RGB", (2, 1))
        swapped.putdata([(0, 0, 255), (255, 0, 0)])
        actual = _visual.VisualFrame(left, 2, 1)
        reference = _visual.VisualFrame(swapped, 2, 1)

        difference = _visual.compare_frames(actual, reference, pixel_threshold=0)

        self.assertEqual(left.histogram(), swapped.histogram())
        self.assertEqual(difference["changed_pixels"], 2)
        self.assertEqual(difference["changed_ratio"], 1.0)

    def test_channel_threshold_and_native_origin_bbox(self):
        reference_image = Image.new("RGB", (4, 3), (10, 20, 30))
        actual_image = reference_image.copy()
        actual_image.putpixel((1, 1), (15, 26, 35))
        actual = _visual.VisualFrame(actual_image, 20, 30, x=10, y=20)
        reference = _visual.VisualFrame(reference_image, 20, 30, x=10, y=20)

        included = _visual.compare_frames(actual, reference, pixel_threshold=5)
        excluded = _visual.compare_frames(actual, reference, pixel_threshold=6)

        self.assertEqual(included["changed_pixels"], 1)
        self.assertEqual(included["changed_bbox"], [11, 21, 12, 22])
        self.assertEqual(included["max_channel_difference"], 6)
        self.assertEqual(excluded["changed_pixels"], 0)
        self.assertIsNone(excluded["changed_bbox"])

    def test_crop_preserves_origin_and_rejects_roi_outside_sample(self):
        frame = _visual.VisualFrame(Image.new("RGB", (5, 4)), 12, 10, x=2, y=3)

        cropped = frame.crop((3, 4, 2, 2))

        self.assertEqual(cropped.region, (3, 4, 2, 2))
        self.assertEqual(cropped.image.size, (2, 2))
        with self.assertRaisesRegex(ValueError, "inside the remembered screenshot"):
            frame.crop((6, 4, 2, 2))
        with self.assertRaisesRegex(ValueError, "inside the 12x10 desktop"):
            frame.crop((11, 9, 2, 2))

    def test_comparison_rejects_geometry_and_region_mismatch(self):
        image = Image.new("RGB", (2, 2))
        actual = _visual.VisualFrame(image, 10, 8, x=2, y=1)
        wrong_geometry = _visual.VisualFrame(image.copy(), 11, 8, x=2, y=1)
        wrong_region = _visual.VisualFrame(image.copy(), 10, 8, x=3, y=1)

        with self.assertRaisesRegex(ValueError, "desktop geometry changed"):
            _visual.compare_frames(actual, wrong_geometry, 0)
        with self.assertRaisesRegex(ValueError, "desktop geometry changed"):
            _visual.compare_frames(actual, wrong_region, 0)

    def test_reference_pixel_limit_is_enforced(self):
        frame = _visual.VisualFrame(Image.new("RGB", (3, 3)), 3, 3)
        with mock.patch.object(_visual, "MAX_REFERENCE_PIXELS", 8):
            with self.assertRaisesRegex(ValueError, "limited to 16,777,216 pixels"):
                frame.require_bounded()

    def test_reference_file_loads_real_png_and_preserves_selected_geometry(self):
        image = Image.new("RGB", (4, 3), (18, 76, 141))
        actual = _visual.VisualFrame(
            Image.new("RGB", (4, 3), (0, 0, 0)), 20, 18, x=7, y=5,
            capture_seconds=0.2, transport_bytes=123, wire_encoding="raw",
        )
        with tempfile.TemporaryDirectory(prefix="vnc-reference-") as temp_dir:
            path = Path(temp_dir) / "reference.png"
            image.save(path, format="PNG")

            reference = _visual.reference_from_file(str(path), actual)

        self.assertEqual(reference.region, (7, 5, 4, 3))
        self.assertEqual((reference.desktop_width, reference.desktop_height), (20, 18))
        self.assertEqual(reference.image.getpixel((1, 1)), (18, 76, 141))
        self.assertEqual(reference.capture_seconds, 0.0)
        self.assertEqual(reference.transport_bytes, 0)
        self.assertIsNone(reference.wire_encoding)

    def test_reference_file_rejects_wrong_dimensions_and_bmp(self):
        actual = _visual.VisualFrame(Image.new("RGB", (4, 3)), 4, 3)
        with tempfile.TemporaryDirectory(prefix="vnc-reference-") as temp_dir:
            wrong_size = Path(temp_dir) / "wrong-size.png"
            bmp = Path(temp_dir) / "reference.bmp"
            Image.new("RGB", (5, 3)).save(wrong_size, format="PNG")
            Image.new("RGB", (4, 3)).save(bmp, format="BMP")

            with self.assertRaisesRegex(ValueError, "dimensions must match"):
                _visual.reference_from_file(str(wrong_size), actual)
            with self.assertRaisesRegex(ValueError, "must be PNG or JPEG"):
                _visual.reference_from_file(str(bmp), actual)

    def test_reference_file_byte_and_pixel_limits_without_large_images(self):
        actual = _visual.VisualFrame(Image.new("RGB", (4, 3)), 4, 3)
        with tempfile.TemporaryDirectory(prefix="vnc-reference-") as temp_dir:
            path = Path(temp_dir) / "reference.png"
            Image.new("RGB", (4, 3), (20, 40, 60)).save(path, format="PNG")
            byte_limit = path.stat().st_size - 1
            with mock.patch.object(_visual, "MAX_REFERENCE_FILE_BYTES", byte_limit):
                with self.assertRaisesRegex(ValueError, "file exceeds 20 MiB"):
                    _visual.reference_from_file(str(path), actual)

            with mock.patch.object(_visual, "MAX_REFERENCE_PIXELS", 11):
                with self.assertRaisesRegex(ValueError, "exceeds the pixel limit"):
                    _visual.reference_from_file(str(path), actual)

    def test_visual_condition_limits_and_invalid_arguments(self):
        valid = _visual.VisualCondition(
            condition="changed", timeout=0, interval=0.1, stable_for=0.1,
            pixel_threshold=0, max_changed_ratio=1,
        )
        self.assertEqual(valid.timeout, 0)

        invalid = (
            {"condition": "unchanged"},
            {"timeout": True},
            {"timeout": float("nan")},
            {"timeout": 60.01},
            {"interval": 0.09},
            {"interval": True},
            {"stable_for": 10.01},
            {"pixel_threshold": True},
            {"pixel_threshold": 256},
            {"max_changed_ratio": -0.01},
            {"max_changed_ratio": 1.01},
            {"condition": "stable", "timeout": 0.2, "stable_for": 0.3},
        )
        for options in invalid:
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    _visual.VisualCondition(**options)

        with self.assertRaisesRegex(ValueError, "require a remembered"):
            _visual.VisualWatch(_visual.VisualCondition(condition="changed"))
        with self.assertRaisesRegex(ValueError, "omit reference_id"):
            _visual.VisualWatch(
                _visual.VisualCondition(),
                _visual.VisualFrame(Image.new("RGB", (1, 1)), 1, 1),
            )

    def test_stable_watch_resets_for_visible_change(self):
        settings = _visual.VisualCondition(
            condition="stable", timeout=2, interval=0.1, stable_for=0.3,
            pixel_threshold=4, max_changed_ratio=0,
        )
        watch = _visual.VisualWatch(settings)
        anchor = _visual.VisualFrame(Image.new("RGB", (1, 1), (0, 0, 0)), 1, 1)
        changed_image = Image.new("RGB", (1, 1), (0, 0, 9))
        changed = _visual.VisualFrame(changed_image, 1, 1)

        self.assertFalse(watch.observe(anchor, 0.0))
        self.assertFalse(watch.observe(anchor, 0.2))
        self.assertFalse(watch.observe(changed, 0.25))
        self.assertEqual(watch.stable_since, 0.25)
        self.assertFalse(watch.observe(changed, 0.5))
        self.assertTrue(watch.observe(changed, 0.56))

    def test_stable_watch_uses_fixed_anchor_to_catch_cumulative_drift(self):
        settings = _visual.VisualCondition(
            condition="stable", timeout=2, interval=0.1, stable_for=0.4,
            pixel_threshold=4, max_changed_ratio=0,
        )
        watch = _visual.VisualWatch(settings)
        anchor = _visual.VisualFrame(Image.new("RGB", (1, 1), (0, 0, 0)), 1, 1)
        small_drift = _visual.VisualFrame(Image.new("RGB", (1, 1), (4, 0, 0)), 1, 1)
        cumulative_drift = _visual.VisualFrame(Image.new("RGB", (1, 1), (8, 0, 0)), 1, 1)

        self.assertFalse(watch.observe(anchor, 0.0))
        self.assertFalse(watch.observe(small_drift, 0.1))
        self.assertEqual(watch.stable_since, 0.0)
        self.assertFalse(watch.observe(cumulative_drift, 0.2))
        self.assertEqual(watch.stable_since, 0.2)
        self.assertFalse(watch.observe(cumulative_drift, 0.5))
        self.assertTrue(watch.observe(cumulative_drift, 0.61))


class VisualMcpIntegrationChecks(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_visual_wait_lifecycle(self):
        global _server_stderr
        fixture_a = _regression.Fixture("visual A", 41)
        fixture_b = _regression.Fixture("visual B", 193)
        env = dict(os.environ)
        for name in (
            "VNC_HOST", "VNC_PORT", "VNC_PASSWORD", "VNC_PASSWORD_ENV",
            "VNC_PROFILE", "VNC_PROFILES", "VNC_PROFILES_FILE",
        ):
            env.pop(name, None)
        env.update(
            VNC_TIMEOUT="1.5",
            VNC_ACTION_TIMEOUT="4",
            VNC_SCREENSHOT_MODE="refresh",
            VNC_IDLE_TIMEOUT="0",
        )
        params = _regression.StdioServerParameters(
            command=sys.executable,
            args=[str(_source_root / "main.py"), "mcp"],
            cwd=str(_source_root),
            env=env,
        )

        try:
            with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr:
                async with _regression.stdio_client(params, errlog=stderr) as streams:
                    async with _regression.ClientSession(*streams) as session:
                        await session.initialize()

                        async def raw_call(name: str, **arguments):
                            return await session.call_tool(name, arguments)

                        async def call(name: str, **arguments):
                            result = await raw_call(name, **arguments)
                            self.assertFalse(result.isError, _result_text(result))
                            return _payload(result)

                        async def wait_for(condition: str, **arguments):
                            return await raw_call("vnc_wait_for", condition=condition, **arguments)

                        def image_from(result, image_format="png"):
                            blocks = [item for item in result.content if item.type == "image"]
                            self.assertEqual(len(blocks), 1, _result_text(result))
                            self.assertEqual(blocks[0].mimeType, "image/" + image_format)
                            image = Image.open(BytesIO(base64.b64decode(blocks[0].data)))
                            image.load()
                            self.assertEqual(image.size, (64, 48))
                            return image

                        listed = await session.list_tools()
                        self.assertIn("vnc_wait_for", {tool.name for tool in listed.tools})
                        _integration_checks["stdio_exposes_vnc_wait_for"] = True

                        connected = await call(
                            "vnc_connect", host="127.0.0.1", port=fixture_a.port
                        )
                        default_connection = connected["connection_id"]
                        self.assertTrue(connected["connected"])
                        baseline_result = await raw_call(
                            "vnc_screenshot", image_format="png", remember=True,
                            connection_id=default_connection,
                        )
                        self.assertFalse(baseline_result.isError, _result_text(baseline_result))
                        baseline = _payload(baseline_result)
                        reference_one = baseline["reference_id"]
                        self.assertTrue(reference_one)
                        self.assertEqual(image_from(baseline_result).size, (64, 48))
                        _integration_checks["remembered_reference_and_image_payload"] = True

                        plain_capture = await call(
                            "vnc_screenshot", image_format="png",
                            connection_id=default_connection,
                        )
                        self.assertNotIn("reference_id", plain_capture)
                        still_matches = await wait_for(
                            "matches", reference_id=reference_one, timeout=0,
                            image_format="png", connection_id=default_connection,
                        )
                        self.assertFalse(still_matches.isError, _result_text(still_matches))
                        still_matches_data = _payload(still_matches)
                        self.assertTrue(still_matches_data["matched"])
                        image_from(still_matches)

                        unchanged = await wait_for(
                            "changed", reference_id=reference_one, timeout=0,
                            image_format="png", connection_id=default_connection,
                        )
                        self.assertFalse(unchanged.isError, _result_text(unchanged))
                        unchanged_data = _payload(unchanged)
                        self.assertFalse(unchanged_data["matched"])
                        self.assertTrue(unchanged_data["timed_out"])
                        image_from(unchanged)
                        _integration_checks["matches_and_timeout_return_final_image"] = True

                        stable = await wait_for(
                            "stable", timeout=0.8, interval=0.1, stable_for=0.2,
                            image_format="png", connection_id=default_connection,
                        )
                        self.assertFalse(stable.isError, _result_text(stable))
                        stable_data = _payload(stable)
                        self.assertTrue(stable_data["matched"])
                        self.assertGreaterEqual(stable_data["comparisons"], 2)
                        image_from(stable)
                        _integration_checks["stable_condition_succeeds"] = True

                        fixture_a.color = 101
                        changed = await wait_for(
                            "changed", reference_id=reference_one, timeout=1.5,
                            interval=0.1, image_format="png",
                            connection_id=default_connection,
                        )
                        self.assertFalse(changed.isError, _result_text(changed))
                        changed_data = _payload(changed)
                        self.assertTrue(changed_data["matched"])
                        self.assertGreater(changed_data["difference"]["changed_pixels"], 0)
                        image_from(changed)

                        baseline_two_result = await raw_call(
                            "vnc_screenshot", image_format="png", remember=True,
                            connection_id=default_connection,
                        )
                        self.assertFalse(baseline_two_result.isError, _result_text(baseline_two_result))
                        baseline_two = _payload(baseline_two_result)
                        reference_two = baseline_two["reference_id"]
                        self.assertTrue(reference_two)
                        self.assertNotEqual(reference_two, reference_one)
                        replaced_reference = await wait_for(
                            "matches", reference_id=reference_one, timeout=0,
                            image_format="png", connection_id=default_connection,
                        )
                        self.assertTrue(replaced_reference.isError)
                        newest_reference = await wait_for(
                            "matches", reference_id=reference_two, timeout=0,
                            image_format="png", connection_id=default_connection,
                        )
                        self.assertFalse(newest_reference.isError, _result_text(newest_reference))
                        self.assertTrue(_payload(newest_reference)["matched"])
                        image_from(newest_reference)
                        _integration_checks["changed_condition_detects_pixel_change"] = True
                        _integration_checks["reference_replacement_and_plain_capture"] = True

                        named = await call(
                            "vnc_connect", host="127.0.0.1", port=fixture_b.port,
                            session_id="visual_peer",
                        )
                        named_connection = named["connection_id"]
                        named_reference_result = await raw_call(
                            "vnc_screenshot", image_format="png", remember=True,
                            session_id="visual_peer", connection_id=named_connection,
                        )
                        self.assertFalse(named_reference_result.isError, _result_text(named_reference_result))
                        named_reference = _payload(named_reference_result)["reference_id"]
                        cross_session = await wait_for(
                            "matches", reference_id=named_reference, timeout=0,
                            image_format="png", connection_id=default_connection,
                        )
                        self.assertTrue(cross_session.isError)
                        self.assertTrue(
                            (await raw_call(
                                "vnc_screenshot", image_format="png", session_id="visual_peer"
                            )).isError,
                            "named-session calls must supply both session_id and connection_id",
                        )
                        _integration_checks["reference_is_scoped_to_connection_and_session"] = True

                        disconnected = await call(
                            "vnc_disconnect", connection_id=default_connection
                        )
                        self.assertFalse(disconnected["connected"])
                        reconnected = await call(
                            "vnc_connect", host="127.0.0.1", port=fixture_a.port
                        )
                        new_connection = reconnected["connection_id"]
                        self.assertNotEqual(new_connection, default_connection)
                        after_reconnect = await wait_for(
                            "matches", reference_id=reference_two, timeout=0,
                            image_format="png", connection_id=new_connection,
                        )
                        self.assertTrue(after_reconnect.isError)
                        _integration_checks["reconnect_expires_reference"] = True

                        waiting_task = asyncio.create_task(wait_for(
                            "stable", timeout=5, interval=0.1, stable_for=2,
                            image_format="png", connection_id=new_connection,
                        ))
                        await asyncio.sleep(0)
                        cancel_request_id = session._request_id - 1
                        active = False
                        for _ in range(50):
                            inventory = await call("vnc_sessions")
                            row = next(
                                (item for item in inventory["sessions"] if item["session_id"] == "default"),
                                None,
                            )
                            if row and row["visual_wait_active"]:
                                active = True
                                break
                            await asyncio.sleep(0.02)
                        self.assertTrue(active, "visual wait never became active")

                        started = time.monotonic()
                        status = await asyncio.wait_for(call("vnc_status"), timeout=1.0)
                        self.assertTrue(status["connected"])
                        self.assertLess(time.monotonic() - started, 1.0)
                        _integration_checks["same_session_operation_and_second_wait"] = True

                        competing_wait = await wait_for(
                            "stable", timeout=1, interval=0.1, stable_for=0.2,
                            image_format="png", connection_id=new_connection,
                        )
                        self.assertTrue(competing_wait.isError)

                        await session.send_notification(mcp_types.ClientNotification(
                            mcp_types.CancelledNotification(
                                params=mcp_types.CancelledNotificationParams(
                                    requestId=cancel_request_id,
                                    reason="visual check cancellation cleanup",
                                )
                            )
                        ))
                        cancellation_received = False
                        try:
                            cancelled_response = await asyncio.wait_for(waiting_task, timeout=1.5)
                            cancellation_received = cancelled_response.isError
                        except TimeoutError:
                            self.fail("cancelled MCP wait did not return promptly")
                        except McpError as exc:
                            # MCP 1.30.0 RequestResponder.cancel sends code 0.
                            # Registry cleanup below proves completion; do not
                            # infer state from exception message fragments.
                            cancellation_received = exc.error.code == 0
                        self.assertTrue(cancellation_received, "MCP server did not acknowledge request cancellation")

                        active = True
                        for _ in range(50):
                            inventory = await call("vnc_sessions")
                            row = next(
                                (item for item in inventory["sessions"] if item["session_id"] == "default"),
                                None,
                            )
                            if row and not row["visual_wait_active"]:
                                active = False
                                break
                            await asyncio.sleep(0.02)
                        self.assertFalse(active, "cancelled visual wait left its active marker set")
                        self.assertTrue((await call("vnc_status"))["connected"])
                        _integration_checks["cancelled_wait_cleans_up"] = True

                        self.assertFalse(
                            any(event[0] in {"key", "pointer"} for fixture in (fixture_a, fixture_b) for event in fixture.events),
                            "read-only visual waits generated keyboard or pointer input",
                        )
                        _integration_checks["visual_wait_sends_no_input"] = True

                        await call("vnc_disconnect", connection_id=new_connection)
                        await call(
                            "vnc_disconnect", session_id="visual_peer",
                            connection_id=named_connection,
                        )
                stderr.seek(0)
                _server_stderr = stderr.read()

            await self._run_cli_wait_checks(fixture_a, fixture_b)
        finally:
            fixture_a.close()
            fixture_b.close()

        fixture_errors = [*fixture_a.errors, *fixture_b.errors]
        self.assertEqual(fixture_errors, [])

    async def test_roi_capture_preserves_native_coordinates_in_refresh_and_fresh(self):
        for mode in ("refresh", "fresh"):
            with self.subTest(screenshot_mode=mode):
                fixture = _regression.Fixture(
                    f"ROI {mode}", 73 if mode == "refresh" else 149, honor_regions=True
                )
                try:
                    await self._check_roi_mode(mode, fixture)
                finally:
                    fixture.close()
                self.assertEqual(fixture.errors, [])

    async def _check_roi_mode(self, mode, fixture):
        env = dict(os.environ)
        for name in (
            "VNC_HOST", "VNC_PORT", "VNC_PASSWORD", "VNC_PASSWORD_ENV",
            "VNC_PROFILE", "VNC_PROFILES", "VNC_PROFILES_FILE",
        ):
            env.pop(name, None)
        env.update(
            VNC_TIMEOUT="1.5",
            VNC_ACTION_TIMEOUT="4",
            VNC_SCREENSHOT_MODE=mode,
            VNC_IDLE_TIMEOUT="0",
        )
        params = _regression.StdioServerParameters(
            command=sys.executable,
            args=[str(_source_root / "main.py"), "mcp"],
            cwd=str(_source_root),
            env=env,
        )
        region = {"x": 9, "y": 11, "width": 17, "height": 13}
        roi_request = (0, region["x"], region["y"], region["width"], region["height"])

        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr:
            async with _regression.stdio_client(params, errlog=stderr) as streams:
                async with _regression.ClientSession(*streams) as session:
                    await session.initialize()

                    async def call(name, **arguments):
                        result = await session.call_tool(name, arguments)
                        self.assertFalse(result.isError, _result_text(result))
                        return result, _payload(result)

                    connected_result, connected = await call(
                        "vnc_connect", host="127.0.0.1", port=fixture.port
                    )
                    self.assertTrue(connected["connected"])
                    connection_id = connected["connection_id"]

                    start = len(fixture.framebuffer_requests)
                    roi_result, roi = await call(
                        "vnc_screenshot", image_format="png", region=region,
                        remember=True, connection_id=connection_id,
                    )
                    roi_image = self._decode_mcp_png(roi_result)
                    self._assert_roi_metadata(roi, region)
                    self._assert_fixture_pixels(roi_image, fixture, region["x"], region["y"])
                    self.assertIn(roi_request, fixture.framebuffer_requests[start:])
                    self.assertEqual(
                        fixture.framebuffer_requests[start], roi_request,
                        f"{mode} did not make the first framebuffer request for the requested ROI",
                    )
                    self.assertTrue(roi["reference_id"])
                    first_reference = roi["reference_id"]
                    _integration_checks[f"roi_{mode}_first_frame_uses_native_coordinates"] = True

                    full_result, full = await call(
                        "vnc_screenshot", image_format="png", connection_id=connection_id
                    )
                    full_image = self._decode_mcp_png(full_result)
                    self.assertEqual((full["desktop_width"], full["desktop_height"]), (64, 48))
                    self.assertEqual((full["origin_x"], full["origin_y"], full["width"], full["height"]), (0, 0, 64, 48))
                    self._assert_fixture_pixels(full_image, fixture, 0, 0)
                    self.assertEqual(
                        fixture.framebuffer_requests[start + 1], (0, 0, 0, 64, 48),
                        f"{mode} full screenshot did not request the full framebuffer after the ROI",
                    )

                    before_reference = len(fixture.framebuffer_requests)
                    reference_result, reference = await call(
                        "vnc_screenshot", image_format="png", region=region,
                        remember=True, connection_id=connection_id,
                    )
                    self._assert_roi_metadata(reference, region)
                    self._assert_fixture_pixels(self._decode_mcp_png(reference_result), fixture, region["x"], region["y"])
                    self.assertNotEqual(reference["reference_id"], first_reference)
                    reference_requests = fixture.framebuffer_requests[before_reference:]
                    self.assertTrue(reference_requests)
                    self.assertTrue(all(request == roi_request for request in reference_requests), reference_requests)

                    before_wait = len(fixture.framebuffer_requests)
                    wait_result, waited = await call(
                        "vnc_wait_for", condition="matches", reference_id=reference["reference_id"],
                        region=region, timeout=0, image_format="png", connection_id=connection_id,
                    )
                    self.assertTrue(waited["matched"])
                    self._assert_roi_metadata(waited, region)
                    self._assert_fixture_pixels(self._decode_mcp_png(wait_result), fixture, region["x"], region["y"])
                    wait_requests = fixture.framebuffer_requests[before_wait:]
                    self.assertTrue(wait_requests)
                    self.assertTrue(all(request == roi_request for request in wait_requests), wait_requests)
                    _integration_checks[f"roi_{mode}_reference_and_wait_request_roi"] = True

                    self.assertFalse(
                        any(event[0] in {"key", "pointer"} for event in fixture.events),
                        f"{mode} ROI capture or wait sent desktop input",
                    )
                    await call("vnc_disconnect", connection_id=connection_id)

    def _decode_mcp_png(self, result):
        blocks = [item for item in result.content if item.type == "image"]
        self.assertEqual(len(blocks), 1, _result_text(result))
        self.assertEqual(blocks[0].mimeType, "image/png")
        image = Image.open(BytesIO(base64.b64decode(blocks[0].data)))
        image.load()
        return image

    def _assert_roi_metadata(self, metadata, region):
        self.assertEqual((metadata["desktop_width"], metadata["desktop_height"]), (64, 48))
        self.assertEqual(
            (metadata["origin_x"], metadata["origin_y"], metadata["width"], metadata["height"]),
            (region["x"], region["y"], region["width"], region["height"]),
        )

    def _assert_fixture_pixels(self, image, fixture, x, y):
        self.assertEqual(image.size, (17, 13) if (x, y) != (0, 0) else (64, 48))
        for local_y in range(image.height):
            for local_x in range(image.width):
                self.assertEqual(
                    image.getpixel((local_x, local_y)),
                    fixture.rgb(x + local_x, y + local_y),
                    f"pixel mismatch at native ({x + local_x}, {y + local_y})",
                )

    async def _run_cli_wait_checks(self, timeout_fixture, stable_fixture):
        with tempfile.TemporaryDirectory(prefix="vnc-mcp-visual-") as temp_dir:
            temp_path = Path(temp_dir)
            timeout_image = temp_path / "timeout.png"
            timeout_exit, timeout_stdout, timeout_stderr = await self._run_cli_wait(
                timeout_fixture, "changed", 0, timeout_image
            )
            self.assertEqual(
                timeout_exit, 3,
                f"timeout CLI exit was {timeout_exit}; stdout={timeout_stdout!r}; stderr={timeout_stderr!r}",
            )
            timeout_result = json.loads(timeout_stdout)
            self.assertFalse(timeout_result["matched"])
            self.assertTrue(timeout_result["timed_out"])
            self.assertEqual(Path(timeout_result["saved"]).resolve(), timeout_image.resolve())
            self._assert_saved_image(timeout_image)
            _integration_checks["cli_timeout_exit_3_saves_last_image"] = True

            stable_image = temp_path / "stable.png"
            stable_exit, stable_stdout, stable_stderr = await self._run_cli_wait(
                stable_fixture, "stable", 0.8, stable_image, stable_for=0.2
            )
            self.assertEqual(
                stable_exit, 0,
                f"stable CLI exit was {stable_exit}; stdout={stable_stdout!r}; stderr={stable_stderr!r}",
            )
            stable_result = json.loads(stable_stdout)
            self.assertTrue(stable_result["matched"])
            self.assertFalse(stable_result["timed_out"])
            self.assertEqual(Path(stable_result["saved"]).resolve(), stable_image.resolve())
            self._assert_saved_image(stable_image)
            _integration_checks["cli_stable_exit_0_saves_image"] = True

    async def _run_cli_wait(self, fixture, condition, wait_timeout, output, stable_for=0.2):
        env = dict(os.environ)
        for name in (
            "VNC_HOST", "VNC_PORT", "VNC_PASSWORD", "VNC_PASSWORD_ENV",
            "VNC_PROFILES_FILE", "VNC_PROFILE", "VNC_PROFILES",
        ):
            env.pop(name, None)
        env["VNC_SCREENSHOT_MODE"] = "refresh"
        args = [
            sys.executable,
            str(_source_root / "main.py"),
            "wait-for",
            "--host", "127.0.0.1",
            "--port", str(fixture.port),
            "--timeout", "2",
            "--condition", condition,
            "--wait-timeout", str(wait_timeout),
            "--interval", "0.1",
            "--stable-for", str(stable_for),
            "--format", "png",
            "--output", str(output),
        ]
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(_source_root),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=8)
        except TimeoutError:
            process.kill()
            await process.wait()
            self.fail(f"CLI {condition} wait exceeded 8 seconds")
        return process.returncode, stdout.decode("utf-8", "replace").strip(), stderr.decode("utf-8", "replace").strip()

    def _assert_saved_image(self, path: Path):
        self.assertTrue(path.is_file(), f"CLI did not save the last screenshot at {path}")
        with Image.open(path) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (64, 48))


class _ReadOnlyFakeBackend:
    """Minimal registry backend that accepts status and visual samples only."""

    def __init__(self, sample_delay: float = 0.0):
        self.connection_id = "fake-visual-connection"
        self.action_timeout = 5.0
        self.connected = True
        self.width = self.height = 4
        self.frame = _visual.VisualFrame(Image.new("RGB", (4, 4), (40, 80, 120)), 4, 4)
        self.sample_delay = sample_delay
        self.samples = 0
        self.methods: list[str] = []

    def execute(self, method, args, control: _operations.ActionControl, expected, pin):
        control.check()
        if pin and expected != self.connection_id:
            raise ConnectionError("fake backend connection changed")
        self.methods.append(method)
        if method == "status":
            result = {
                "connected": self.connected,
                "connection_id": self.connection_id,
                "width": self.width,
                "height": self.height,
            }
        elif method == "visual_sample":
            if self.sample_delay:
                time.sleep(self.sample_delay)
            control.check()
            self.samples += 1
            region = args[0]
            result = self.frame.crop(region) if region is not None else self.frame
        else:
            raise AssertionError(f"read-only fake backend rejected {method!r}")
        control.check()
        return result

    def disconnect(self):
        self.connected = False
        self.connection_id = None
        return {"connected": False, "connection_id": None}


class VisualRegistryChecks(unittest.IsolatedAsyncioTestCase):
    async def test_stable_registry_wait_samples_three_times_and_encodes_final_image_once(self):
        backend = _ReadOnlyFakeBackend()
        registry = _sessions.SessionRegistry(lambda: backend, idle_timeout=0)
        encoder = mock.patch.object(_visual, "encode_frame", wraps=_visual.encode_frame)
        encoder_mock = encoder.start()
        try:
            result = await registry.wait_for_visual(
                condition="stable", timeout=1.5, interval=0.1, stable_for=0.2,
                image_format="png", quality=80,
            )
            self.assertTrue(result.value.details["matched"])
            self.assertGreaterEqual(result.value.details["samples"], 3)
            self.assertEqual(backend.samples, result.value.details["samples"])
            self.assertEqual(encoder_mock.call_count, 1)
            self.assertEqual(result.value.screenshot.width, 4)
            self.assertTrue(set(backend.methods) <= {"status", "visual_sample"})
            sampled = backend.samples
            await asyncio.sleep(0.15)
            self.assertEqual(backend.samples, sampled, "a completed stable wait kept sampling in the background")
        finally:
            encoder.stop()
            await registry.close()
        self.assertFalse(backend.connected)

    async def test_visual_wait_budget_end_does_not_schedule_late_samples(self):
        backend = _ReadOnlyFakeBackend(sample_delay=0.01)
        registry = _sessions.SessionRegistry(lambda: backend, idle_timeout=0)
        try:
            result = await registry.wait_for_visual(
                condition="stable", timeout=0.28, interval=0.1, stable_for=0.28,
                image_format="png", quality=80,
            )
            self.assertFalse(result.value.details["matched"])
            self.assertTrue(result.value.details["timed_out"])
            self.assertGreaterEqual(backend.samples, 2)
            sampled = backend.samples
            await asyncio.sleep(0.15)
            self.assertEqual(backend.samples, sampled, "a timed-out wait scheduled another sample after returning")
            self.assertTrue(set(backend.methods) <= {"status", "visual_sample"})
        finally:
            await registry.close()
        self.assertFalse(backend.connected)


def _payload(result) -> dict:
    if result.structuredContent:
        return result.structuredContent
    for item in result.content:
        if item.type == "text":
            return json.loads(item.text)
    raise AssertionError("MCP tool result contained no JSON text payload")


def _result_text(result) -> str:
    return "\n".join(
        getattr(item, "text", "")
        for item in result.content
        if getattr(item, "type", None) == "text"
    )


def _load_modules(source_root: Path) -> None:
    global _visual, _regression, _sessions, _operations, _source_root
    _source_root = source_root.resolve()
    source_path = str(_source_root / "src")
    checks_path = str(_source_root / "checks")
    for value in (source_path, checks_path):
        if value not in sys.path:
            sys.path.insert(0, value)

    _visual = importlib.import_module("vnc_mcp.visual")
    expected_visual = (_source_root / "src" / "vnc_mcp" / "visual.py").resolve()
    if Path(_visual.__file__).resolve() != expected_visual:
        raise ImportError(f"visual module loaded from a different source: {_visual.__file__}")

    _regression = importlib.import_module("vnc_mcp_regression")
    expected_regression = (_source_root / "checks" / "vnc_mcp_regression.py").resolve()
    if Path(_regression.__file__).resolve() != expected_regression:
        raise ImportError(f"RFB fixture loaded from a different source: {_regression.__file__}")

    _sessions = importlib.import_module("vnc_mcp.sessions")
    _operations = importlib.import_module("vnc_mcp.operations")


class EvidenceTextTestResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failure_details = []

    def _record_failure(self, test, err, subtest=None):
        exception_type, exception, _ = err
        detail = {
            "test": test.id(),
            "exception_type": f"{exception_type.__module__}.{exception_type.__qualname__}",
            "message": str(exception),
            "traceback": self._exc_info_to_string(err, test),
        }
        if subtest is not None:
            detail["subtest"] = str(subtest)
        self.failure_details.append(detail)

    def addFailure(self, test, err):
        self._record_failure(test, err)
        super().addFailure(test, err)

    def addError(self, test, err):
        self._record_failure(test, err)
        super().addError(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._record_failure(test, err, subtest)
        super().addSubTest(test, subtest, err)


def _run_checks() -> dict[str, object]:
    global _integration_checks, _server_stderr
    _integration_checks = {name: False for name in _INTEGRATION_CHECK_NAMES}
    _server_stderr = ""
    unit_suite = unittest.defaultTestLoader.loadTestsFromTestCase(VisualComparisonChecks)
    registry_suite = unittest.defaultTestLoader.loadTestsFromTestCase(VisualRegistryChecks)
    integration_suite = unittest.defaultTestLoader.loadTestsFromTestCase(VisualMcpIntegrationChecks)
    tests = list(unit_suite) + list(registry_suite) + list(integration_suite)
    suite = unittest.TestSuite([unit_suite, registry_suite, integration_suite])
    output = io.StringIO()
    result = unittest.TextTestRunner(
        stream=output, verbosity=2, resultclass=EvidenceTextTestResult
    ).run(suite)
    failed_ids = {detail["test"] for detail in result.failure_details}
    checks = {
        test.id().rsplit(".", 1)[-1].removeprefix("test_"): test.id() not in failed_ids
        for test in tests
    }
    checks.update(_integration_checks)
    report: dict[str, object] = {
        "success": result.wasSuccessful() and all(checks.values()),
        "checks": checks,
        "failures": result.failure_details,
        "scope": "local MCP stdio plus synthetic 127.0.0.1 RFB; no real VM or input",
    }
    if output.getvalue():
        report["test_output"] = output.getvalue()
    if _server_stderr.strip():
        report["mcp_server_stderr"] = _server_stderr[-12000:]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="repository root")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    args = parser.parse_args(argv)

    try:
        _load_modules(args.source)
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
            "scope": "checks did not start",
        }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    passed = sum(bool(value) for value in report["checks"].values())
    total = len(report["checks"])
    print(f"Visual checks: {passed}/{total} passed; report: {args.output}")
    if not report["success"]:
        for failure in report.get("failures", []):
            print(f"{failure['test']}: {failure['exception_type']}: {failure['message']}")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
