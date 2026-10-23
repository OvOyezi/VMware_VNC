# SPDX-License-Identifier: Apache-2.0
# Modified and maintained for VMware VNC; historical attribution is in NOTICE.
"""Pixel comparisons and finite visual conditions, using existing Pillow primitives.

Images are sampled evidence, not proof of application readiness or a continuous
recording. No reference image is written to disk, and no input is sent here.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path
import time
import warnings

from PIL import Image, ImageChops

from .imaging import Screenshot, encode_frame, validate_region


MAX_REFERENCE_PIXELS = 16_777_216
MAX_REFERENCE_FILE_BYTES = 20 * 1024 * 1024
Region = tuple[int, int, int, int]


@dataclass(frozen=True)
class VisualFrame:
    image: Image.Image
    desktop_width: int
    desktop_height: int
    x: int = 0
    y: int = 0
    capture_seconds: float = 0.0
    transport_bytes: int = 0
    wire_encoding: str | None = None
    frame_id: str | None = None
    received_at: float | None = None
    updated_regions: tuple = ()
    capture_source: str | None = None

    @property
    def region(self) -> Region:
        return self.x, self.y, self.image.width, self.image.height

    def crop(self, region: Region | None = None) -> VisualFrame:
        if region is None:
            return self
        x, y, width, height = validate_region(region, self.desktop_width, self.desktop_height)
        if x < self.x or y < self.y or x + width > self.x + self.image.width or y + height > self.y + self.image.height:
            raise ValueError("comparison region must be inside the remembered screenshot")
        return replace(self, image=self.image.crop((x-self.x, y-self.y, x-self.x+width, y-self.y+height)), x=x, y=y)

    def require_bounded(self) -> VisualFrame:
        if self.image.width * self.image.height > MAX_REFERENCE_PIXELS:
            raise ValueError("visual references are limited to 16,777,216 pixels; select a smaller region")
        return self

    def encode(self, image_format: str, quality: int) -> Screenshot:
        started = time.monotonic()
        image = encode_frame(self.image, image_format, quality)
        return replace(image, desktop_width=self.desktop_width, desktop_height=self.desktop_height,
                       x=self.x, y=self.y, capture_seconds=round(self.capture_seconds + time.monotonic()-started, 6),
                       transport_bytes=self.transport_bytes, wire_encoding=self.wire_encoding,
                       frame_id=self.frame_id, received_at=self.received_at,
                       updated_regions=self.updated_regions, capture_source=self.capture_source)


@dataclass(frozen=True)
class VisualReference:
    reference_id: str
    frame: VisualFrame


def reference_from_file(filename: str, actual: VisualFrame) -> VisualFrame:
    """Read an explicitly chosen CLI PNG/JPEG, without opening arbitrary formats."""
    path = Path(filename).expanduser()
    if path.stat().st_size > MAX_REFERENCE_FILE_BYTES:
        raise ValueError("reference image file exceeds 20 MiB")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as image:
            if image.format not in {"PNG", "JPEG"}:
                raise ValueError("reference image must be PNG or JPEG")
            if image.width * image.height > MAX_REFERENCE_PIXELS:
                raise ValueError("reference image exceeds the pixel limit")
            if image.size != actual.image.size:
                raise ValueError("reference image dimensions must match the selected desktop region")
            pixels = image.convert("RGB")
    return replace(actual, image=pixels, capture_seconds=0.0, transport_bytes=0, wire_encoding=None)


def compare_frames(actual: VisualFrame, reference: VisualFrame, pixel_threshold: int) -> dict:
    if (actual.desktop_width, actual.desktop_height, actual.region) != (
        reference.desktop_width, reference.desktop_height, reference.region
    ):
        raise ValueError("desktop geometry changed; inspect a new screenshot before comparing")
    # Compare positions, not colour histograms: swapped pixels must not match.
    left = actual.image if actual.image.mode == "RGB" else actual.image.convert("RGB")
    right = reference.image if reference.image.mode == "RGB" else reference.image.convert("RGB")
    difference = ImageChops.difference(left, right)
    pixels = actual.image.width * actual.image.height
    if difference.getbbox() is None:
        return {"changed_pixels": 0, "total_pixels": pixels, "changed_ratio": 0.0,
                "max_channel_difference": 0, "changed_bbox": None}
    red, green, blue = difference.split()
    largest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    mask = largest.point([0 if value <= pixel_threshold else 255 for value in range(256)])
    changed = mask.histogram()[255]
    bbox = mask.getbbox()
    if bbox is not None:
        bbox = [bbox[0]+actual.x, bbox[1]+actual.y, bbox[2]+actual.x, bbox[3]+actual.y]
    return {"changed_pixels": changed, "total_pixels": pixels,
            "changed_ratio": changed / pixels,
            "max_channel_difference": max(high for _low, high in difference.getextrema()),
            "changed_bbox": bbox}


@dataclass(frozen=True)
class VisualCondition:
    condition: str = "stable"
    timeout: float = 10.0
    interval: float = 0.25
    stable_for: float = 0.5
    pixel_threshold: int = 8
    max_changed_ratio: float = 0.001

    def __post_init__(self) -> None:
        if not isinstance(self.condition, str) or self.condition not in {"changed", "matches", "stable"}:
            raise ValueError("visual condition must be changed, matches or stable")
        for name, low, high in (("timeout", 0, 60), ("interval", 0.1, 5),
                                ("stable_for", 0.1, 10), ("max_changed_ratio", 0, 1)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} must be finite and between {low} and {high}")
        if isinstance(self.pixel_threshold, bool) or not isinstance(self.pixel_threshold, int) or not 0 <= self.pixel_threshold <= 255:
            raise ValueError("pixel_threshold must be an integer between 0 and 255")
        if self.condition == "stable" and self.stable_for > self.timeout:
            raise ValueError("stable_for must not exceed timeout")


class VisualWatch:
    def __init__(self, settings: VisualCondition, reference: VisualFrame | None = None) -> None:
        if settings.condition != "stable" and reference is None:
            raise ValueError("changed and matches require a remembered screenshot reference_id")
        if settings.condition == "stable" and reference is not None:
            raise ValueError("stable samples its own initial frame; omit reference_id")
        self.settings = settings
        self.anchor = reference
        self.stable_since: float | None = None
        self.samples = 0
        self.comparisons = 0
        self.difference: dict | None = None

    def observe(self, frame: VisualFrame, now: float) -> bool:
        self.samples += 1
        if self.anchor is None:
            self.anchor, self.stable_since = frame, now
            return False
        self.difference = compare_frames(frame, self.anchor, self.settings.pixel_threshold)
        self.comparisons += 1
        changed = self.difference["changed_ratio"] > self.settings.max_changed_ratio
        if self.settings.condition == "changed":
            return changed
        if self.settings.condition == "matches":
            return not changed
        if changed:
            # Keep one anchor for the window so small consecutive changes cannot
            # hide cumulative drift. Reset only when its tolerance is exceeded.
            self.anchor, self.stable_since = frame, now
        return self.comparisons > 0 and now - self.stable_since >= self.settings.stable_for

    def metadata(self, matched: bool, elapsed: float, now: float) -> dict:
        return {"condition": self.settings.condition, "matched": matched, "timed_out": not matched,
                "samples": self.samples, "comparisons": self.comparisons,
                "elapsed_seconds": round(elapsed, 6), "timeout_seconds": self.settings.timeout,
                "pixel_threshold": self.settings.pixel_threshold,
                "max_changed_ratio": self.settings.max_changed_ratio,
                "stable_for_seconds": self.settings.stable_for if self.settings.condition == "stable" else None,
                "observed_stable_seconds": round(now-self.stable_since, 6) if self.stable_since is not None else None,
                "difference": self.difference, "application_verified": False}


@dataclass(frozen=True)
class VisualWaitResult:
    screenshot: Screenshot
    details: dict
