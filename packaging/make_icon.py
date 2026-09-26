"""Build the VaultNotes app icon from the design's own logo, with no image library.

``design/VaultNotes-Design.html`` draws the brand mark as three SVG primitives
in a 32x32 view box: a hexagon outline (the vault), a filled circle and a short
round-capped stem (the keyhole).  This module rasterizes exactly those shapes,
paints them with the three-colour gradient of the design (``--plain`` ->
``--encrypted`` -> ``--personal``, nebula theme), and writes:

* ``packaging/vaultnotes.png``  - 256 px, used as the window icon in ``--dev``
* ``packaging/vaultnotes.ico``  - 16/24/32/48/64/128/256 px, for PyInstaller

Only the standard library is used (``struct`` + ``zlib``), so the icon can be
regenerated on a machine with no Pillow: ``python packaging/make_icon.py``.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

#: The 32x32 view box of the design's brand SVG.
VIEWBOX = 32.0

#: Gradient stops, taken from the nebula theme in tokens.css (section 6).
STOPS = (
    (0.0, (0x22, 0xD3, 0xEE)),  # --plain
    (0.5, (0xA7, 0x8B, 0xFA)),  # --encrypted
    (1.0, (0xF4, 0x72, 0xB6)),  # --personal
)

#: The hexagon outline of the brand mark, as the design lists it.
HEXAGON = (
    (16.0, 2.5),
    (27.7, 9.25),
    (27.7, 22.75),
    (16.0, 29.5),
    (4.3, 22.75),
    (4.3, 9.25),
)

HEXAGON_WIDTH = 2.0
KEYHOLE_CENTER = (16.0, 14.0)
KEYHOLE_RADIUS = 3.2
STEM = ((16.0, 16.5), (16.0, 22.0))
STEM_WIDTH = 2.6

#: Sizes written into the .ico, smallest first.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

#: Samples per pixel, for a smooth edge without an image library.
SUBSAMPLES = 3


def gradient_color(t: float) -> tuple[int, int, int]:
    """Colour of the design's gradient at position ``t`` (0..1)."""
    t = min(1.0, max(0.0, t))
    for index in range(len(STOPS) - 1):
        start, first = STOPS[index]
        end, second = STOPS[index + 1]
        if t <= end:
            span = end - start or 1.0
            ratio = (t - start) / span
            return tuple(  # type: ignore[return-value]
                int(round(first[channel] + (second[channel] - first[channel]) * ratio))
                for channel in range(3)
            )
    return STOPS[-1][1]


def _distance_to_segment(px: float, py: float, a: tuple, b: tuple) -> float:
    """Distance from a point to a line segment (round caps come free)."""
    ax, ay = a
    bx, by = b
    vx, vy = bx - ax, by - ay
    length_sq = vx * vx + vy * vy
    if length_sq <= 1e-12:
        return math.hypot(px - ax, py - ay)
    ratio = ((px - ax) * vx + (py - ay) * vy) / length_sq
    ratio = min(1.0, max(0.0, ratio))
    return math.hypot(px - (ax + vx * ratio), py - (ay + vy * ratio))


def _distance_to_polygon(px: float, py: float, points: tuple) -> float:
    """Distance from a point to a closed polygon outline."""
    best = math.inf
    count = len(points)
    for index in range(count):
        a = points[index]
        b = points[(index + 1) % count]
        best = min(best, _distance_to_segment(px, py, a, b))
    return best


def _bounds(points: tuple) -> tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _stroke_alpha(distance: float, width: float, feather: float) -> float:
    """Coverage of a stroke/shape edge: 1 inside, fading over ``feather``."""
    half = width / 2.0
    if distance <= half - feather:
        return 1.0
    if distance >= half + feather:
        return 0.0
    return (half + feather - distance) / (2.0 * feather)


def render_icon(size: int) -> bytearray:
    """Rasterize the brand mark into ``size`` x ``size`` RGBA bytes."""
    if size < 8 or size > 512:
        raise ValueError("icon size must be between 8 and 512 pixels")

    scale = size / VIEWBOX
    # One device pixel of softness, in view-box units.
    feather = 1.0 / scale
    # A 1-pixel line at 16 px is mostly antialiasing, which reads as a smudge,
    # so the small sizes of the .ico get a slightly heavier mark.
    weight = 1.4 if size <= 24 else 1.0
    hex_width = HEXAGON_WIDTH * weight
    stem_width = STEM_WIDTH * weight

    hex_bounds = _bounds(HEXAGON)
    circle_bounds = (
        KEYHOLE_CENTER[0] - KEYHOLE_RADIUS,
        KEYHOLE_CENTER[1] - KEYHOLE_RADIUS,
        KEYHOLE_CENTER[0] + KEYHOLE_RADIUS,
        KEYHOLE_CENTER[1] + KEYHOLE_RADIUS,
    )
    stem_bounds = (
        STEM[0][0] - STEM_WIDTH / 2,
        STEM[0][1],
        STEM[0][0] + STEM_WIDTH / 2,
        STEM[1][1],
    )

    pixels = bytearray(size * size * 4)
    offsets = [(x + 0.5) / SUBSAMPLES - 0.5 for x in range(SUBSAMPLES)]

    for py in range(size):
        for px in range(size):
            alpha = 0.0
            t_sum = 0.0
            color_sum = [0, 0, 0]
            for oy in offsets:
                for ox in offsets:
                    vx = (px + ox) / scale      # view-box coordinates
                    vy = (py + oy) / scale

                    hit = _stroke_alpha(_distance_to_polygon(vx, vy, HEXAGON), hex_width, feather)
                    distance = math.hypot(vx - KEYHOLE_CENTER[0], vy - KEYHOLE_CENTER[1])
                    if distance <= KEYHOLE_RADIUS + feather:
                        # The head of the keyhole is a filled disc.
                        hit = max(hit, _stroke_alpha(distance, KEYHOLE_RADIUS * 2, feather))
                    hit = max(hit, _stroke_alpha(_distance_to_segment(vx, vy, STEM[0], STEM[1]), stem_width, feather))

                    if hit <= 0.0:
                        continue
                    alpha += hit
                    t = _gradient_t(vx, vy, hex_bounds, circle_bounds, stem_bounds)
                    r, g, b = gradient_color(t)
                    color_sum[0] += r * hit
                    color_sum[1] += g * hit
                    color_sum[2] += b * hit
                    t_sum += hit

            coverage = alpha / (SUBSAMPLES * SUBSAMPLES)
            if coverage <= 0.0:
                continue
            if t_sum > 0:
                color = (
                    int(color_sum[0] / t_sum),
                    int(color_sum[1] / t_sum),
                    int(color_sum[2] / t_sum),
                )
            else:
                color = gradient_color(0.5)
            index = (py * size + px) * 4
            pixels[index] = color[0]
            pixels[index + 1] = color[1]
            pixels[index + 2] = color[2]
            pixels[index + 3] = min(255, int(round(coverage * 255)))

    return pixels


def _gradient_t(
    vx: float,
    vy: float,
    hex_bounds: tuple,
    circle_bounds: tuple,
    stem_bounds: tuple,
) -> float:
    """Where a point sits on its own primitive's gradient (the SVG default).

    The design uses ``x1=0 y1=0 x2=1 y2=1`` with the default
    ``gradientUnits="objectBoundingBox"``, so every shape is shaded across its
    own bounding box.  Knowing which shape a pixel belongs to is enough to
    reproduce that without a real SVG engine.
    """
    inside_circle = math.hypot(vx - KEYHOLE_CENTER[0], vy - KEYHOLE_CENTER[1]) <= KEYHOLE_RADIUS + 0.2
    if inside_circle:
        bounds = circle_bounds
    elif (
        stem_bounds[0] - 0.2 <= vx <= stem_bounds[2] + 0.2
        and stem_bounds[1] - 0.2 <= vy <= stem_bounds[3] + 0.2
    ):
        bounds = stem_bounds
    else:
        bounds = hex_bounds
    x0, y0, x1, y1 = bounds
    tx = (vx - x0) / ((x1 - x0) or 1.0)
    ty = (vy - y0) / ((y1 - y0) or 1.0)
    return (tx + ty) / 2.0


# ----------------------------------------------------------------------
# PNG (section 11: no extra dependencies, so the file is written by hand)
# ----------------------------------------------------------------------

def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write_png(path: Path | str, size: int, rgba: bytearray) -> None:
    """Write RGBA bytes out as an 8-bit truecolour PNG."""
    stride = size * 4
    raw = b"".join(b"\x00" + bytes(rgba[y * stride : (y + 1) * stride]) for y in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    body = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )
    Path(path).write_bytes(body)


def _ico_entry(size: int, rgba: bytearray) -> bytes:
    """One .ico image: a 32-bit DIB plus an all-zero AND mask.

    The alpha channel carries the transparency, so the AND mask stays empty -
    that is how modern Windows icons are usually stored and it keeps the
    file readable by tools that ignore alpha.
    """
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    rows = []
    for y in range(size - 1, -1, -1):  # DIBs are bottom-up
        row = bytearray()
        for x in range(size):
            index = (y * size + x) * 4
            row += bytes((rgba[index + 2], rgba[index + 1], rgba[index], rgba[index + 3]))
        rows.append(bytes(row))
    mask_row = b"\x00" * (((size + 31) // 32) * 4)
    return header + b"".join(rows) + mask_row * size


def write_ico(path: Path | str, sizes: tuple[int, ...] = ICO_SIZES) -> None:
    """Rasterize every size and write one multi-resolution ``.ico``."""
    images = [(size, _ico_entry(size, render_icon(size))) for size in sizes]
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    data_size = sum(len(image) for _, image in images)
    offset = 6 + 16 * count
    directory = b""
    cursor = offset
    for size, image in images:
        width = 0 if size >= 256 else size   # 0 means 256 in the .ico format
        directory += struct.pack(
            "<BBBBHHII", width, width, 0, 0, 1, 32, len(image), cursor
        )
        cursor += len(image)
    Path(path).write_bytes(header + directory + b"".join(image for _, image in images))
    assert data_size > 0


def main() -> None:
    """Write both icon files next to this script."""
    folder = Path(__file__).resolve().parent
    png_path = folder / "vaultnotes.png"
    ico_path = folder / "vaultnotes.ico"

    write_png(png_path, 256, render_icon(256))
    write_ico(ico_path)

    print(f"wrote {png_path.name} ({png_path.stat().st_size:,} bytes)")
    print(f"wrote {ico_path.name} ({ico_path.stat().st_size:,} bytes, {len(ICO_SIZES)} sizes)")


if __name__ == "__main__":  # pragma: no cover - command line entry
    main()
