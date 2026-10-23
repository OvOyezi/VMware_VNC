"""Image encoding independent of the RFB reader and MCP transport."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import struct
import time

from PIL import Image


@dataclass(frozen=True)
class Screenshot:
    data: bytes
    image_format: str
    desktop_width: int
    desktop_height: int
    x: int
    y: int
    width: int
    height: int
    capture_seconds: float | None = None
    transport_bytes: int | None = None
    wire_encoding: str | None = None
    reference_id: str | None = None
    frame_id: str | None = None
    received_at: float | None = None
    updated_regions: tuple = ()
    capture_source: str | None = None
    source_rgb_sha256: str | None = None
    display_scale: int = 1
    color_semantics: str = "original"

    def metadata(self) -> dict:
        metadata = {
            "desktop_width": self.desktop_width, "desktop_height": self.desktop_height,
            "origin_x": self.x, "origin_y": self.y, "width": self.width, "height": self.height,
            "format": self.image_format, "bytes": len(self.data),
            "coordinates": "Native desktop coordinates: add origin_x/origin_y to image-local coordinates. No scaling.",
            "capture_seconds": self.capture_seconds,
            "transport_bytes": self.transport_bytes,
            "wire_encoding": self.wire_encoding,
            "frame_id": self.frame_id,
            "frame_received_monotonic": self.received_at,
            "updated_regions": self.updated_regions,
            "capture_source": self.capture_source,
            "lossless": self.image_format == "png",
            "display_scale": self.display_scale,
            "color_semantics": self.color_semantics,
            "source_rgb_sha256": self.source_rgb_sha256,
            "source_pixel_format": "RGB8",
            "frame_age_seconds": round(max(0, time.monotonic() - self.received_at), 6) if self.received_at else None,
        }
        if self.reference_id is not None:
            metadata["reference_id"] = self.reference_id
        if self.display_scale != 1:
            metadata["coordinates"] = "Display enlargement: native coordinate = origin + image-local coordinate/display_scale."
        return metadata


def validate_encoding(image_format: str, quality: int) -> str:
    image_format = image_format.lower()
    if image_format not in {"png", "jpeg"}:
        raise ValueError("image_format must be jpeg or png")
    if not 1 <= quality <= 95:
        raise ValueError("quality must be between 1 and 95")
    return image_format


def validate_region(region: tuple[int, int, int, int] | None, desktop_width: int, desktop_height: int) -> tuple[int, int, int, int]:
    x, y, width, height = region if region is not None else (0, 0, desktop_width, desktop_height)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in (x, y, width, height)):
        raise ValueError("region values must be integers")
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > desktop_width or y + height > desktop_height:
        raise ValueError(f"region must be inside the {desktop_width}x{desktop_height} desktop")
    return x, y, width, height


def encode_frame(frame: Image.Image, image_format: str, quality: int, region: tuple[int, int, int, int] | None = None) -> Screenshot:
    image_format = validate_encoding(image_format, quality)
    desktop_width, desktop_height = frame.size
    x, y, width, height = validate_region(region, desktop_width, desktop_height)
    image = frame if region is None else frame.crop((x, y, x + width, y + height))
    if image.mode != "RGB":
        image = image.convert("RGB")
    digest = sha256(struct.pack("!II", width, height) + image.tobytes()).hexdigest()
    output = BytesIO()
    options = {"quality": quality, "subsampling": 0, "optimize": True} if image_format == "jpeg" else {}
    image.save(output, format=image_format.upper(), **options)
    return Screenshot(output.getvalue(), image_format, desktop_width, desktop_height, x, y, width, height,
                      source_rgb_sha256=digest)
