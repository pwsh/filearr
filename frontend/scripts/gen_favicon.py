#!/usr/bin/env python3
"""Regenerate Filearr favicon/app-icon assets.

Mirrors the hand-authored frontend/public/favicon.svg (viewBox 0 0 64 64)
geometry using PIL ImageDraw, since Pillow has no SVG rasterizer. Any
change to favicon.svg's shapes should be mirrored here (and vice versa)
so the .ico/.png outputs stay pixel-consistent with the SVG.

Usage:
    cd frontend && python3 scripts/gen_favicon.py
    (or: uv run --with pillow python scripts/gen_favicon.py)

Requires: Pillow (not an npm/pyproject dependency of the app itself --
this is a one-off asset-generation script, run manually when the mark
changes).
"""
from pathlib import Path

from PIL import Image, ImageDraw

# Colors mirror favicon.svg exactly.
ACCENT = (99, 102, 241, 255)  # #6366f1 - indigo-500, app default --accent
ACCENT_DARK = (67, 56, 202, 255)  # #4338ca - indigo-700, folder back-tab shade
GLASS = (255, 255, 255, 255)  # white magnifying-glass ring/handle
BG_DARK = (15, 23, 42, 255)  # #0f172a - slate-900, PWA manifest theme_color

OUT_DIR = Path(__file__).resolve().parent.parent / "public"

SUPERSAMPLE = 4  # draw at 4x then downsample for anti-aliased edges


def draw_mark(size: int, background: tuple[int, int, int, int] | None = None) -> Image.Image:
    """Draw the folder + magnifying-glass mark at `size` px square.

    Coordinates below are the favicon.svg viewBox-64 numbers scaled by
    `k`. Supersamples at 4x and downsamples with LANCZOS for crisp
    edges even at 16px.
    """
    s = size * SUPERSAMPLE
    k = s / 64  # viewBox units -> supersampled px

    img = Image.new("RGBA", (s, s), background if background else (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Back tab (darker accent) -- svg: rect x=10 y=10 w=20 h=10 rx=3
    draw.rounded_rectangle(
        [10 * k, 10 * k, 30 * k, 20 * k], radius=3 * k, fill=ACCENT_DARK
    )
    # Folder body (accent) -- svg: rect x=6 y=18 w=52 h=34 rx=6
    draw.rounded_rectangle(
        [6 * k, 18 * k, 58 * k, 52 * k], radius=6 * k, fill=ACCENT
    )
    # Magnifying-glass ring -- svg: circle cx=40 cy=38 r=9 stroke-width=5
    r, cx, cy, sw = 9 * k, 40 * k, 38 * k, 5 * k
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=GLASS, width=round(sw))
    # Handle -- svg: line (46.5,44.5) -> (54,52) stroke-width=6 round cap
    hw = 6 * k
    p1, p2 = (46.5 * k, 44.5 * k), (54 * k, 52 * k)
    draw.line([p1, p2], fill=GLASS, width=round(hw))
    cap_r = hw / 2
    for x, y in (p1, p2):  # PIL line() has square ends; add round caps
        draw.ellipse([x - cap_r, y - cap_r, x + cap_r, y + cap_r], fill=GLASS)

    return img.resize((size, size), Image.LANCZOS)


def save_ico() -> None:
    # Pillow's ICO writer treats the base image as the size ceiling (it
    # only emits requested sizes that fit within the base image's
    # dimensions), so the base must be the *largest* frame with the
    # smaller ones passed via append_images -- not the other way round.
    sizes = (16, 32, 48)
    images = {sz: draw_mark(sz) for sz in sizes}
    base = images[max(sizes)]
    others = [images[sz] for sz in sizes if sz != max(sizes)]
    base.save(
        OUT_DIR / "favicon.ico",
        format="ICO",
        sizes=[(sz, sz) for sz in sizes],
        append_images=others,
    )


def save_png(name: str, size: int, *, with_bg: bool) -> None:
    bg = BG_DARK if with_bg else None
    draw_mark(size, background=bg).save(OUT_DIR / name, format="PNG")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    save_ico()
    # App/home-screen icons get an opaque background (matches PWA
    # manifest theme_color) -- transparent PNGs render on a
    # platform-chosen (often white) backdrop on iOS/Android home screens.
    save_png("apple-touch-icon.png", 180, with_bg=True)
    save_png("icon-192.png", 192, with_bg=True)
    save_png("icon-512.png", 512, with_bg=True)
    print(f"Wrote favicon.ico, apple-touch-icon.png, icon-192.png, icon-512.png to {OUT_DIR}")


if __name__ == "__main__":
    main()
