"""Draw the integration's brand icons into custom_components/eufy_clean_local/brand/.

Home Assistant (2026.x) serves brand images from a custom integration's own
`brand/` folder: icon.png (256 px), icon@2x.png (512 px) and dark variants.
The artwork is original: a top-down robot vacuum with a small house badge for
"local". Run with Pillow installed:

    python tools/make_icon.py
"""

from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw, ImageFilter

OUT = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "eufy_clean_local" / "brand"
S = 1024  # drawing size; scaled down for smooth edges

NAVY = (0x00, 0x5D, 0x8E, 255)
BLUE = (0x4C, 0xA2, 0xFF, 255)
SKY = (0x8F, 0xC3, 0xFF, 255)
RING = (0xC9, 0xD4, 0xE1, 255)
WHITE = (255, 255, 255, 255)
GREEN = (0x2E, 0xB8, 0x72, 255)


def draw(dark: bool) -> Image.Image:
    rim = SKY if dark else NAVY
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # Soft shadow under the robot.
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse([70, 110, S - 70, S - 30], fill=(0, 0, 0, 90 if not dark else 60))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(28)))

    d = ImageDraw.Draw(img)
    body = [60, 60, S - 60, S - 60]
    d.ellipse(body, fill=WHITE, outline=rim, width=44)
    # Front bumper (facing up, like the robot on a map heading north).
    d.arc([60, 60, S - 60, S - 60], start=-150, end=-30, fill=BLUE, width=96)
    # Top cover ring and the lidar tower.
    c = S / 2
    r = 250
    d.ellipse([c - r, c - r + 40, c + r, c + r + 40], outline=RING, width=26)
    t = 140
    ty = c + 70
    d.ellipse([c - t, ty - t, c + t, ty + t], fill=NAVY)
    d.ellipse([c - t * 0.45, ty - t * 0.45, c + t * 0.45, ty + t * 0.45], fill=SKY)

    # "Local" badge: a house in a green circle, bottom right.
    bx, by, br = S - 230, S - 230, 175
    d.ellipse([bx - br - 18, by - br - 18, bx + br + 18, by + br + 18], fill=(0, 0, 0, 0))
    d.ellipse([bx - br, by - br, bx + br, by + br], fill=GREEN, outline=WHITE, width=26)
    roof = [(bx - 105, by - 10), (bx, by - 100), (bx + 105, by - 10)]
    d.polygon(roof, fill=WHITE)
    d.rectangle([bx - 72, by - 14, bx + 72, by + 92], fill=WHITE)
    d.rectangle([bx - 22, by + 30, bx + 22, by + 92], fill=GREEN)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for dark in (False, True):
        art = draw(dark)
        prefix = "dark_" if dark else ""
        for size, suffix in ((256, ""), (512, "@2x")):
            path = OUT / f"{prefix}icon{suffix}.png"
            art.resize((size, size), Image.LANCZOS).save(path, optimize=True)
            print(path.relative_to(OUT.parent.parent.parent), f"{size}x{size}")


if __name__ == "__main__":
    main()
