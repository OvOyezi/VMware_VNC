"""Image encoding independent of the VNC reactor and MCP transport."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

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

    def metadata(self) -> dict:
        return {
            "desktop_width": self.desktop_width, "desktop_height": self.desktop_height,
            "origin_x": self.x, "origin_y": self.y, "width": self.width, "height": self.height,
            "format": self.image_format, "bytes": len(self.data),
            "coordinates": "Native desktop coordinates: add origin_x/origin_y to image-local coordinates. No scaling.",
        }


def validate_encoding(image_format: str, quality: int) -> str:
    image_format = image_format.lower()
    if image_format not in {"png", "jpeg"}:
        raise ValueError("image_format must be jpeg or png")
    if not 1 <= quality <= 95:
        raise ValueError("quality must be between 1 and 95")
    return image_format


def encode_frame(frame: Image.Image, image_format: str, quality: int, region: tuple[int, int, int, int] | None = None) -> Screenshot:
    image_format = validate_encoding(image_format, quality)
    desktop_width, desktop_height = frame.size
    x, y, width, height = region if region is not None else (0, 0, desktop_width, desktop_height)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in (x, y, width, height)):
        raise ValueError("region values must be integers")
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > desktop_width or y + height > desktop_height:
        raise ValueError(f"region must be inside the {desktop_width}x{desktop_height} desktop")
    image = frame if region is None else frame.crop((x, y, x + width, y + height))
    output = BytesIO()
    options = {"quality": quality, "subsampling": 0, "optimize": True} if image_format == "jpeg" else {}
    image.save(output, format=image_format.upper(), **options)
    return Screenshot(output.getvalue(), image_format, desktop_width, desktop_height, x, y, width, height)
