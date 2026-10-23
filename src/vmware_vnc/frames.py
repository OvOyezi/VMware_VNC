# SPDX-License-Identifier: Apache-2.0
# Modified for VMware VNC.
"""Immutable published snapshots backed by a bounded incremental framebuffer."""
from __future__ import annotations

from dataclasses import dataclass
import time
from uuid import uuid4

from PIL import Image, ImageDraw

from .rfb_codec import MAX_FRAME_PIXELS


MAX_UPDATE_REGIONS = 1024


def _checked_dimensions(width: int, height: int) -> None:
    if (not isinstance(width, int) or isinstance(width, bool)
            or not isinstance(height, int) or isinstance(height, bool)):
        raise ValueError("frame dimensions must be integers")
    if width <= 0 or height <= 0:
        raise ValueError("frame dimensions must be positive")
    if width * height > MAX_FRAME_PIXELS:
        raise ValueError("frame exceeds the pixel limit")


def _checked_box(box: tuple[int, int, int, int], width: int, height: int,
                 *, name: str = "region") -> tuple[int, int, int, int]:
    if not isinstance(box, tuple) or len(box) != 4:
        raise ValueError(f"{name} must be a four-integer tuple")
    x, y, box_width, box_height = box
    if any(not isinstance(value, int) or isinstance(value, bool)
           for value in (x, y, box_width, box_height)):
        raise ValueError(f"{name} coordinates and dimensions must be integers")
    if x < 0 or y < 0 or box_width <= 0 or box_height <= 0:
        raise ValueError(f"{name} must have a positive area inside the frame")
    if x + box_width > width or y + box_height > height:
        raise ValueError(f"{name} is outside the frame")
    if box_width * box_height > MAX_FRAME_PIXELS:
        raise ValueError(f"{name} exceeds the pixel limit")
    return box


def _covers(mask: Image.Image, box: tuple[int, int, int, int]) -> bool:
    x, y, width, height = box
    return mask.crop((x, y, x + width, y + height)).getextrema() == (1, 1)


def _union(boxes: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    left = min(box[0] for box in boxes)
    top = min(box[1] for box in boxes)
    right = max(box[0] + box[2] for box in boxes)
    bottom = max(box[1] + box[3] for box in boxes)
    return left, top, right - left, bottom - top


@dataclass(frozen=True)
class FrameSnapshot:
    image: Image.Image
    frame_id: str
    sequence: int
    received_at: float
    updated_regions: tuple[tuple[int, int, int, int], ...]
    valid_region: tuple[int, int, int, int] | None = None


class FrameBuffer:
    """Accumulate decoded pixels and publish only initialized framebuffer areas.

    Boxes use the RFB convention ``(x, y, width, height)``. A resize starts a
    fresh initialization epoch: old pixels remain available as canvas content,
    while their validity is cleared until the server sends them again.
    """

    def __init__(self, width: int, height: int,
                 initial_region: tuple[int, int, int, int] | None = None) -> None:
        _checked_dimensions(width, height)
        if initial_region is not None:
            initial_region = _checked_box(initial_region, width, height,
                                          name="initial region")

        self._width = width
        self._height = height
        self._initial_region = initial_region
        self._identity = uuid4().hex
        self._sequence = 0
        self._snapshot: FrameSnapshot | None = None
        self._canvas = Image.new("RGB", (width, height))
        self._known = Image.new("1", (width, height), 0)
        self._regions: list[tuple[int, int, int, int]] = []
        self._working: Image.Image | None = None
        self._working_known: Image.Image | None = None
        self._working_regions: list[tuple[int, int, int, int]] | None = None

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def snapshot(self) -> FrameSnapshot | None:
        return self._snapshot

    @property
    def initial_region(self) -> tuple[int, int, int, int] | None:
        return self._initial_region

    @property
    def ready(self) -> bool:
        known = self._working_known if self._active else self._known
        return _covers(known, self._required_region())

    @property
    def _active(self) -> bool:
        return self._working is not None

    def _required_region(self) -> tuple[int, int, int, int]:
        return self._initial_region or (0, 0, self._width, self._height)

    def _clear_working(self) -> None:
        self._working = None
        self._working_known = None
        self._working_regions = None

    def begin_update(self) -> Image.Image:
        """Start an isolated update transaction and return its RGB canvas."""
        if self._active:
            raise RuntimeError("a framebuffer update is already active")
        self._working = self._canvas.copy()
        self._working_known = self._known.copy()
        self._working_regions = list(self._regions)
        return self._working

    def resize(self, width: int, height: int) -> Image.Image:
        """Resize the working canvas, retain intersecting pixels, and clear validity."""
        _checked_dimensions(width, height)
        if self._initial_region is not None:
            _checked_box(self._initial_region, width, height, name="initial region")

        source = self._working if self._active else self._canvas
        assert source is not None
        canvas = Image.new("RGB", (width, height))
        copy_width = min(source.width, width)
        copy_height = min(source.height, height)
        if copy_width and copy_height:
            canvas.paste(source.crop((0, 0, copy_width, copy_height)), (0, 0))

        self._width, self._height = width, height
        self._working = canvas
        self._working_known = Image.new("1", (width, height), 0)
        self._working_regions = []
        return canvas

    def mark_updated(self, box: tuple[int, int, int, int]) -> None:
        """Mark a successfully decoded rectangle as known and record its bounds."""
        if not self._active:
            raise RuntimeError("begin_update() or resize() must start an update first")
        box = _checked_box(box, self._width, self._height, name="updated region")
        assert self._working_known is not None
        assert self._working_regions is not None
        x, y, width, height = box
        ImageDraw.Draw(self._working_known).rectangle(
            (x, y, x + width - 1, y + height - 1), fill=1
        )
        if len(self._working_regions) < MAX_UPDATE_REGIONS:
            self._working_regions.append(box)
        else:
            self._working_regions[:] = [_union([*self._working_regions, box])]

    def validate_copy_source(self, sx: int, sy: int, width: int, height: int) -> None:
        """Reject a CopyRect source unless every source pixel is initialized."""
        box = _checked_box((sx, sy, width, height), self._width, self._height,
                           name="CopyRect source")
        known = self._working_known if self._active else self._known
        assert known is not None
        if not _covers(known, box):
            raise ValueError("CopyRect source contains uninitialized pixels")

    def publish(self) -> FrameSnapshot | None:
        """Commit a snapshot when its required area is known; otherwise keep pixels pending."""
        if not self._active:
            raise RuntimeError("no framebuffer update is active")
        assert self._working is not None
        assert self._working_known is not None
        assert self._working_regions is not None

        if not self.ready:
            # Detach from the caller's working-canvas reference so later writes
            # cannot change the saved partial image.
            self._canvas = self._working.copy()
            self._known = self._working_known
            self._regions = self._working_regions
            self._clear_working()
            return None

        image = self._working.copy()
        self._sequence += 1
        valid_region = (self._initial_region if self._snapshot is None
                        else self._snapshot.valid_region)
        snapshot = FrameSnapshot(
            image=image,
            frame_id=f"{self._identity}:{self._sequence}",
            sequence=self._sequence,
            received_at=time.monotonic(),
            updated_regions=tuple(self._working_regions),
            valid_region=valid_region,
        )
        self._snapshot = snapshot
        self._canvas = image
        self._known = self._working_known
        self._regions = []
        self._clear_working()
        return snapshot
