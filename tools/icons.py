"""Draw Echolot's icon: a dot and three rings like a sonar ping, dark blue in the middle, fading and
lighter outwards. Writes the SVG (header, README) and, from the same geometry, favicon.ico (16, 32, 48 px)
and apple-touch-icon.png (180 px on white). numpy and the standard library only.

  uv run python tools/icons.py
"""

import struct
import zlib
from pathlib import Path

import numpy as np

STATIC = Path(__file__).resolve().parents[1] / "src" / "echolot" / "web" / "static"
INNER, OUTER = "#174ea6", "#6aa9f2"  # the accent blues of the light and the dark theme
DOT = 6.5
RINGS = [(15.0, 4.6, 1.0), (23.0, 3.8, 0.7), (29.6, 2.8, 0.42)]  # radius, width, opacity in a 64 box


def svg() -> str:
    rings = "".join(
        f'<circle cx="32" cy="32" r="{r}" stroke-width="{w}"{f' stroke-opacity="{o}"' if o < 1 else ""}/>'
        for r, w, o in RINGS
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<defs><radialGradient id="echo" cx="32" cy="32" r="32" gradientUnits="userSpaceOnUse">'
        f'<stop offset="0" stop-color="{INNER}"/><stop offset="1" stop-color="{OUTER}"/></radialGradient></defs>'
        f'<g fill="none" stroke="url(#echo)">{rings}</g><circle cx="32" cy="32" r="{DOT}" fill="url(#echo)"/></svg>\n'
    )


def render(size: int, pad: float = 0.0, background: str | None = None) -> np.ndarray:
    """RGBA pixels, 4x4 supersampled; pad: the margin around the 64 box, as a share of the size."""
    n = size * 4
    scale = 64 / (1 - 2 * pad)
    c = ((np.arange(n) + 0.5) / n - 0.5) * scale + 32
    x, y = np.meshgrid(c, c)
    r = np.hypot(x - 32, y - 32)
    alpha = (r <= DOT).astype(float)
    for radius, width, opacity in RINGS:
        alpha = np.maximum(alpha, (np.abs(r - radius) <= width / 2) * opacity)
    t = np.clip(r / 32, 0, 1)[..., None]
    rgb = _rgb(INNER) * (1 - t) + _rgb(OUTER) * t
    a = alpha.reshape(size, 4, size, 4).mean(axis=(1, 3))
    premultiplied = (rgb * alpha[..., None]).reshape(size, 4, size, 4, 3).mean(axis=(1, 3))
    if background:
        colour, a = premultiplied + _rgb(background) * (1 - a[..., None]), np.ones_like(a)
    else:
        colour = premultiplied / np.maximum(a[..., None], 1e-9)
    return np.dstack([colour, a * 255]).round().clip(0, 255).astype(np.uint8)


def png(pixels: np.ndarray) -> bytes:
    height, width, _ = pixels.shape
    raw = b"".join(b"\x00" + row.tobytes() for row in pixels)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8 bit RGBA
    chunks = _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b"")
    return b"\x89PNG\r\n\x1a\n" + chunks


def ico(images: dict[int, bytes]) -> bytes:
    """An ICO file of PNG images (all browsers read those)."""
    offset, entries, data = 6 + 16 * len(images), b"", b""
    for size, image in images.items():
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), offset + len(data))
        data += image
    return struct.pack("<HHH", 0, 1, len(images)) + entries + data


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _rgb(hex_colour: str) -> np.ndarray:
    return np.array([int(hex_colour[i : i + 2], 16) for i in (1, 3, 5)], dtype=float)


if __name__ == "__main__":
    (STATIC / "echolot.svg").write_text(svg())
    (STATIC / "favicon.ico").write_bytes(ico({s: png(render(s)) for s in (16, 32, 48)}))
    (STATIC / "apple-touch-icon.png").write_bytes(png(render(180, pad=0.14, background="#ffffff")))
    print("wrote", ", ".join(p for p in ("echolot.svg", "favicon.ico", "apple-touch-icon.png")))
