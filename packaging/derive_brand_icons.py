"""Derive every OpenKimo icon from the brand source image.

    python3 packaging/derive_brand_icons.py

The source (packaging/LogoSource.png) is a full square: a blue disc with a white K on
a navy background. Used as it was, it was a hard-cornered square on every platform.
From the same pixels this writes:

* kimi-cli/web/public/logo.png — the square with rounded corners. The web UI shows
  it, and the Windows build turns it into the program, installer and tray icons.
* kimi-cli/web/public/favicon.png — the same, small, for the browser tab.
* packaging/AppIcon.png — the rounded square inset on Apple's app-icon grid, for the
  macOS .icns: full-bleed, it sat larger than every other icon in the Dock.
* packaging/MenuBarIconTemplate.png — a black disc with the K cut out, for macOS to
  use as a template image: it colours the shape to suit a light or dark menu bar.

Run it again after changing the source. It needs Pillow.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "packaging" / "LogoSource.png"
LOGO = ROOT / "kimi-cli" / "web" / "public" / "logo.png"
FAVICON = ROOT / "kimi-cli" / "web" / "public" / "favicon.png"
APP_ICON = ROOT / "packaging" / "AppIcon.png"
MENUBAR = ROOT / "packaging" / "MenuBarIconTemplate.png"

FAVICON_SIZE = 256
CORNER_RADIUS = 0.225  # of the side: the rounding of an app icon
# Apple's macOS app-icon grid: an 824 px rounded square, radius 185, centred on
# a 1024 px canvas, over a soft shadow.
APP_ICON_CANVAS = 1024
APP_ICON_BODY = 824
APP_ICON_RADIUS = 185
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


def _rounded(image: Image.Image, size: int, radius: int) -> Image.Image:
    """The image at `size`, its corners rounded off with an anti-aliased edge."""
    big = Image.new("L", (size * SUPERSAMPLE, size * SUPERSAMPLE), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        [0, 0, size * SUPERSAMPLE - 1, size * SUPERSAMPLE - 1],
        radius=radius * SUPERSAMPLE,
        fill=255,
    )
    out = image.resize((size, size), Image.Resampling.LANCZOS)
    out.putalpha(big.resize((size, size), Image.Resampling.LANCZOS))
    return out


def _app_icon(source: Image.Image) -> Image.Image:
    body = _rounded(source, APP_ICON_BODY, APP_ICON_RADIUS)
    offset = (APP_ICON_CANVAS - APP_ICON_BODY) // 2
    shadow = Image.new("RGBA", (APP_ICON_CANVAS, APP_ICON_CANVAS), (0, 0, 0, 0))
    silhouette = Image.new("RGBA", body.size, (0, 0, 0, 77))
    silhouette.putalpha(body.getchannel("A").point(lambda a: a * 77 // 255))
    shadow.paste(silhouette, (offset, offset + 10), silhouette)
    canvas = shadow.filter(ImageFilter.GaussianBlur(14))
    canvas.alpha_composite(body, (offset, offset))
    return canvas


def main() -> None:
    logo = Image.open(SOURCE).convert("RGBA")
    box = _disc_box(logo)
    disc = _disc_mask(logo.size, box)

    side = logo.size[0]
    _rounded(logo, side, round(side * CORNER_RADIUS)).save(LOGO, optimize=True)
    _rounded(logo, FAVICON_SIZE, round(FAVICON_SIZE * CORNER_RADIUS)).save(FAVICON, optimize=True)
    _app_icon(logo).save(APP_ICON, optimize=True)

    bolder_glyph = _glyph_mask(logo).filter(ImageFilter.MaxFilter(MENUBAR_STROKE_GROW))
    shape = ImageChops.subtract(disc, bolder_glyph)
    glyph = Image.new("RGBA", logo.size, (0, 0, 0, 0))
    glyph.putalpha(shape)
    glyph = glyph.crop(box).resize((MENUBAR_DISC, MENUBAR_DISC), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (MENUBAR_CANVAS, MENUBAR_CANVAS), (0, 0, 0, 0))
    offset = (MENUBAR_CANVAS - MENUBAR_DISC) // 2
    canvas.paste(glyph, (offset, offset))
    canvas.save(MENUBAR, optimize=True)

    for path in (LOGO, FAVICON, APP_ICON, MENUBAR):
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
