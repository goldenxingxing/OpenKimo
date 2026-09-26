"""Derive the favicon and the menu-bar icon from the brand logo.

    python3 packaging/derive_brand_icons.py

The logo (kimi-cli/web/public/logo.png) is a full square: a blue disc with a white K
on a navy background. Shrunk as it was, it made a coloured square in the macOS menu
bar, where icons are monochrome shapes, and a hard square in the browser tab. From
the same pixels this writes:

* kimi-cli/web/public/favicon.png — the disc and the K on a transparent background.
* packaging/MenuBarIconTemplate.png — a black disc with the K cut out, for macOS to
  use as a template image: it colours the shape to suit a light or dark menu bar.

Run it again after changing the logo. It needs Pillow.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
LOGO = ROOT / "kimi-cli" / "web" / "public" / "logo.png"
FAVICON = ROOT / "kimi-cli" / "web" / "public" / "favicon.png"
MENUBAR = ROOT / "packaging" / "MenuBarIconTemplate.png"

FAVICON_SIZE = 256
MENUBAR_CANVAS = 44  # 22 pt at 2x; rumps draws it in a 20 pt box
MENUBAR_DISC = 34  # about 16 pt, the height of system menu-bar glyphs
SUPERSAMPLE = 4
MENUBAR_STROKE_GROW = 17  # px at logo scale: thickens the K so it reads at 16 pt


def _disc_box(logo: Image.Image) -> tuple[int, int, int, int]:
    """Bounding box of the disc: the pixels that differ from the corner colour."""
    background = Image.new("RGB", logo.size, logo.getpixel((0, 0))[:3])
    difference = ImageChops.difference(logo.convert("RGB"), background).convert("L")
    box = difference.point(lambda v: 255 if v > 40 else 0).getbbox()
    assert box is not None, "the logo has no shape on its background"
    return box


def _disc_mask(size: tuple[int, int], box: tuple[int, int, int, int]) -> Image.Image:
    """An anti-aliased disc, drawn large and scaled down."""
    big = Image.new("L", (size[0] * SUPERSAMPLE, size[1] * SUPERSAMPLE), 0)
    ImageDraw.Draw(big).ellipse([c * SUPERSAMPLE for c in box], fill=255)
    return big.resize(size, Image.Resampling.LANCZOS)


def _glyph_mask(logo: Image.Image) -> Image.Image:
    """How white each pixel is: the K, with its anti-aliased edge."""
    return logo.convert("RGB").convert("L").point(lambda v: max(0, min(255, (v - 150) * 255 // 105)))


def main() -> None:
    logo = Image.open(LOGO).convert("RGBA")
    box = _disc_box(logo)
    disc = _disc_mask(logo.size, box)

    favicon = logo.copy()
    favicon.putalpha(disc)
    favicon.crop(box).resize((FAVICON_SIZE, FAVICON_SIZE), Image.Resampling.LANCZOS).save(
        FAVICON, optimize=True
    )

    bolder_glyph = _glyph_mask(logo).filter(ImageFilter.MaxFilter(MENUBAR_STROKE_GROW))
    shape = ImageChops.subtract(disc, bolder_glyph)
    glyph = Image.new("RGBA", logo.size, (0, 0, 0, 0))
    glyph.putalpha(shape)
    glyph = glyph.crop(box).resize((MENUBAR_DISC, MENUBAR_DISC), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (MENUBAR_CANVAS, MENUBAR_CANVAS), (0, 0, 0, 0))
    offset = (MENUBAR_CANVAS - MENUBAR_DISC) // 2
    canvas.paste(glyph, (offset, offset))
    canvas.save(MENUBAR, optimize=True)

    print(f"wrote {FAVICON.relative_to(ROOT)} and {MENUBAR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
