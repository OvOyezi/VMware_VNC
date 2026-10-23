# SPDX-License-Identifier: Apache-2.0
# Modified for VMware VNC.
"""Precise pixel inspection for a captured VNC frame and optional reference."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import math
import struct

from PIL import Image, ImageChops

from .imaging import Screenshot, validate_region
from .visual import VisualFrame, compare_frames


MAX_POINTS = 32
MAX_DETAIL_SIDE = 512
MAX_AUTOMATIC_DETAIL_SIDE = 256
MAX_POINT_DETAIL_SIDE = 128
MAX_DETAIL_OUTPUT_PIXELS = 4_194_304


@dataclass(frozen=True)
class InspectionResult:
    images: list[tuple[str, Screenshot]]
    details: dict


def _require_integer(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _validate_points(points: list[tuple[int, int]] | None, frame: VisualFrame) -> list[tuple[int, int]]:
    if points is None:
        return []
    if not isinstance(points, list):
        raise ValueError("points must be a list of native-coordinate pairs")
    if len(points) > MAX_POINTS:
        raise ValueError(f"points is limited to {MAX_POINTS} coordinates")
    checked: list[tuple[int, int]] = []
    left, top = frame.x, frame.y
    right, bottom = left + frame.image.width, top + frame.image.height
    for index, point in enumerate(points):
        if not isinstance(point, tuple) or len(point) != 2:
            raise ValueError(f"points[{index}] must be a pair of integers")
        x = _require_integer(point[0], f"points[{index}].x", -2**31, 2**31 - 1)
        y = _require_integer(point[1], f"points[{index}].y", -2**31, 2**31 - 1)
        if not (left <= x < right and top <= y < bottom):
            raise ValueError(f"points[{index}] must be inside the selected frame")
        checked.append((x, y))
    return checked


def _validate_frame(frame: VisualFrame, name: str) -> None:
    if not isinstance(frame, VisualFrame):
        raise ValueError(f"{name} must be a VisualFrame")
    if not isinstance(frame.image, Image.Image):
        raise ValueError(f"{name}.image must be a Pillow image")
    _require_integer(frame.desktop_width, f"{name}.desktop_width", 1, 2**31 - 1)
    _require_integer(frame.desktop_height, f"{name}.desktop_height", 1, 2**31 - 1)
    _require_integer(frame.x, f"{name}.x", 0, 2**31 - 1)
    _require_integer(frame.y, f"{name}.y", 0, 2**31 - 1)
    # Check that the frame's native-coordinate rectangle is valid within its
    # desktop before using any coordinates from it.
    validate_region(frame.region, frame.desktop_width, frame.desktop_height)


def _rgb_image(image: Image.Image) -> Image.Image:
    return image if image.mode == "RGB" else image.convert("RGB")


def _frame_digest(frame: VisualFrame) -> str:
    rgb = _rgb_image(frame.image)
    digest = sha256()
    digest.update(b"vnc-inspection-rgb-v1\0")
    digest.update(struct.pack(
        ">IIIIII", frame.desktop_width, frame.desktop_height,
        rgb.width, rgb.height, frame.x, frame.y,
    ))
    digest.update(rgb.tobytes())
    return digest.hexdigest()


def _frame_details(frame: VisualFrame) -> dict[str, object]:
    return {
        "desktop_width": frame.desktop_width,
        "desktop_height": frame.desktop_height,
        "origin_x": frame.x,
        "origin_y": frame.y,
        "width": frame.image.width,
        "height": frame.image.height,
        "rgb_sha256": _frame_digest(frame),
        "hash_input": (
            "SHA-256 of vnc-inspection-rgb-v1, big-endian desktop width/height, "
            "frame width/height, native origin x/y, then RGB bytes"
        ),
    }


def _image_metadata(origin_x: int, origin_y: int, source_width: int,
                    source_height: int, output_width: int, output_height: int,
                    scale: int, color_semantics: str = "original") -> dict[str, object]:
    return {
        "origin_x": origin_x,
        "origin_y": origin_y,
        "source_width": source_width,
        "source_height": source_height,
        "output_width": output_width,
        "output_height": output_height,
        "scale": scale,
        "color_semantics": color_semantics,
    }


def _png_screenshot(image: Image.Image, frame: VisualFrame, origin_x: int,
                    origin_y: int) -> Screenshot:
    output = BytesIO()
    image.save(output, format="PNG")
    return Screenshot(
        output.getvalue(), "png", frame.desktop_width, frame.desktop_height,
        origin_x, origin_y, image.width, image.height,
    )


def _crop_inside_frame(frame: VisualFrame, region: tuple[int, int, int, int]) -> Image.Image:
    x, y, width, height = region
    return frame.image.crop((x - frame.x, y - frame.y,
                             x - frame.x + width, y - frame.y + height))


def _clamp_start(start: int, low: int, size: int, available: int) -> int:
    return min(max(start, low), low + available - size)


def _automatic_changed_region(frame: VisualFrame, bbox: list[int]) -> tuple[tuple[int, int, int, int], dict[str, object]]:
    bx0, by0, bx1, by1 = bbox
    frame_x0, frame_y0 = frame.x, frame.y
    frame_x1, frame_y1 = frame.x + frame.image.width, frame.y + frame.image.height
    padded = (bx0 - 8, by0 - 8, bx1 + 8, by1 + 8)
    clipped = (max(frame_x0, padded[0]), max(frame_y0, padded[1]),
               min(frame_x1, padded[2]), min(frame_y1, padded[3]))
    padded_width, padded_height = clipped[2] - clipped[0], clipped[3] - clipped[1]
    width, height = min(MAX_AUTOMATIC_DETAIL_SIDE, padded_width), min(MAX_AUTOMATIC_DETAIL_SIDE, padded_height)
    truncation: list[str] = []
    if clipped != padded:
        truncation.append("frame_bounds")
    if padded_width > MAX_AUTOMATIC_DETAIL_SIDE or padded_height > MAX_AUTOMATIC_DETAIL_SIDE:
        truncation.append("maximum_256_pixel_side")

    if padded_width > MAX_AUTOMATIC_DETAIL_SIDE:
        left = math.floor((bx0 + bx1) / 2 - width / 2)
        left = _clamp_start(left, frame_x0, width, frame.image.width)
    else:
        left = clipped[0]
    if padded_height > MAX_AUTOMATIC_DETAIL_SIDE:
        top = math.floor((by0 + by1) / 2 - height / 2)
        top = _clamp_start(top, frame_y0, height, frame.image.height)
    else:
        top = clipped[1]
    region = (left, top, width, height)
    return region, {
        "automatic": True,
        "reason": "changed_bbox",
        "padded_region": list(padded),
        "truncated": bool(truncation),
        "truncation_reasons": truncation,
    }


def _automatic_point_region(frame: VisualFrame, point: tuple[int, int]) -> tuple[tuple[int, int, int, int], dict[str, object]]:
    x, y = point
    width = min(MAX_POINT_DETAIL_SIDE, frame.image.width)
    height = min(MAX_POINT_DETAIL_SIDE, frame.image.height)
    raw_left = math.floor(x + 0.5 - width / 2)
    raw_top = math.floor(y + 0.5 - height / 2)
    left = _clamp_start(raw_left, frame.x, width, frame.image.width)
    top = _clamp_start(raw_top, frame.y, height, frame.image.height)
    reasons: list[str] = []
    if raw_left < frame.x or raw_top < frame.y or raw_left + width > frame.x + frame.image.width or raw_top + height > frame.y + frame.image.height:
        reasons.append("frame_bounds")
    if frame.image.width < MAX_POINT_DETAIL_SIDE or frame.image.height < MAX_POINT_DETAIL_SIDE:
        reasons.append("frame_size")
    return (left, top, width, height), {
        "automatic": True,
        "reason": "first_point",
        "center_point": [x, y],
        "truncated": bool(reasons),
        "truncation_reasons": reasons,
    }


def inspect_frame(
    frame: VisualFrame,
    reference: VisualFrame | None = None,
    points: list[tuple[int, int]] | None = None,
    detail_region: tuple[int, int, int, int] | None = None,
    zoom: int = 4,
    pixel_threshold: int = 0,
) -> InspectionResult:
    """Return exact RGB measurements and optional enlarged, lossless details.

    Points and detail regions use native desktop coordinates. Detail images are
    nearest-neighbor enlargements; ``difference_amplified`` deliberately uses
    altered colors to make small channel changes visible.
    """
    _validate_frame(frame, "frame")
    if reference is not None:
        _validate_frame(reference, "reference")
    checked_points = _validate_points(points, frame)
    zoom = _require_integer(zoom, "zoom", 1, 8)
    pixel_threshold = _require_integer(pixel_threshold, "pixel_threshold", 0, 255)

    if detail_region is not None:
        if not isinstance(detail_region, tuple) or len(detail_region) != 4:
            raise ValueError("detail_region must be a native-coordinate (x, y, width, height) tuple")
        x, y, width, height = detail_region
        for name, value in zip(("detail_region.x", "detail_region.y", "detail_region.width", "detail_region.height"), detail_region):
            _require_integer(value, name, 0, 2**31 - 1)
        region = validate_region(detail_region, frame.desktop_width, frame.desktop_height)
        if (x < frame.x or y < frame.y or x + width > frame.x + frame.image.width or
                y + height > frame.y + frame.image.height):
            raise ValueError("detail_region must be inside the selected frame")
        if width > MAX_DETAIL_SIDE or height > MAX_DETAIL_SIDE:
            raise ValueError("detail_region is limited to 512 pixels on each side")
        if width * height * zoom * zoom > MAX_DETAIL_OUTPUT_PIXELS:
            raise ValueError("zoomed detail image is limited to 4,194,304 pixels")
        selected_detail = region
        detail_selection: dict[str, object] = {
            "automatic": False,
            "reason": "explicit",
            "truncated": False,
            "truncation_reasons": [],
        }
    else:
        selected_detail = None
        detail_selection = {"automatic": False, "reason": None, "truncated": False, "truncation_reasons": []}

    comparison = compare_frames(frame, reference, pixel_threshold) if reference is not None else None

    if detail_region is None and reference is not None and comparison["changed_bbox"] is not None:
        selected_detail, detail_selection = _automatic_changed_region(frame, comparison["changed_bbox"])
    elif detail_region is None and reference is None and checked_points:
        selected_detail, detail_selection = _automatic_point_region(frame, checked_points[0])

    current_rgb = _rgb_image(frame.image)
    reference_rgb = _rgb_image(reference.image) if reference is not None else None
    point_results: list[dict[str, object]] = []
    for x, y in checked_points:
        local = (x - frame.x, y - frame.y)
        result: dict[str, object] = {"x": x, "y": y, "rgb": list(current_rgb.getpixel(local))}
        if reference_rgb is not None:
            before = reference_rgb.getpixel(local)
            after = current_rgb.getpixel(local)
            result["before_rgb"] = list(before)
            result["delta_rgb"] = [after[i] - before[i] for i in range(3)]
        point_results.append(result)

    images: list[tuple[str, Screenshot]] = [("current", frame.encode("png", 95))]
    image_metadata: dict[str, dict[str, object]] = {
        "current": _image_metadata(frame.x, frame.y, frame.image.width, frame.image.height,
                                    frame.image.width, frame.image.height, 1)
    }
    if selected_detail is not None:
        x, y, width, height = selected_detail
        current_crop = _rgb_image(_crop_inside_frame(frame, selected_detail))
        reference_crop = _rgb_image(_crop_inside_frame(reference, selected_detail)) if reference is not None else None
        output_size = (width * zoom, height * zoom)
        current_detail = current_crop.resize(output_size, Image.Resampling.NEAREST)
        images.append(("detail_current", _png_screenshot(current_detail, frame, x, y)))
        image_metadata["detail_current"] = _image_metadata(x, y, width, height,
                                                           output_size[0], output_size[1], zoom)
        if reference_crop is not None:
            reference_detail = reference_crop.resize(output_size, Image.Resampling.NEAREST)
            images.append(("detail_reference", _png_screenshot(reference_detail, frame, x, y)))
            image_metadata["detail_reference"] = _image_metadata(x, y, width, height,
                                                                output_size[0], output_size[1], zoom)
            absolute = ImageChops.difference(current_crop, reference_crop)
            lookup = [min(value * 16, 255) for value in range(256)]
            amplified = Image.merge("RGB", tuple(channel.point(lookup) for channel in absolute.split()))
            amplified = amplified.resize(output_size, Image.Resampling.NEAREST)
            images.append(("difference_amplified", _png_screenshot(amplified, frame, x, y)))
            image_metadata["difference_amplified"] = _image_metadata(
                x, y, width, height, output_size[0], output_size[1], zoom,
                "altered: per-channel absolute difference multiplied by 16 and clipped to 255",
            )

    details: dict[str, object] = {
        "frame": _frame_details(frame),
        "reference": _frame_details(reference) if reference is not None else None,
        "comparison": comparison,
        "points": point_results,
        "detail_region": list(selected_detail) if selected_detail is not None else None,
        "detail_selection": detail_selection,
        "zoom": zoom,
        "pixel_threshold": pixel_threshold,
        "image_metadata": image_metadata,
    }
    return InspectionResult(images, details)
