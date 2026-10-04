#!/usr/bin/env python3
"""Regenerate web/og.png -- the Open Graph preview card.

NOT needed to run dropzone. The server has no third-party dependencies and
never imports this; the PNG it produces is committed. Run it only when the
card needs to change:

    python3 tools/make_og.py

Requires Pillow, which is a build-time-only dependency:

    pip install pillow

Design notes: the card mirrors the site exactly -- same near-black ground,
same electric-blue accent, same data-bar motif as the animated background.
A preview card that does not look like the page it links to is worse than no
card at all.
"""
from __future__ import annotations

import os
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1200, 630
BG = (8, 9, 12)          # --bg
PANEL = (17, 20, 25)     # --panel
LINE = (35, 40, 51)      # --line
ACC = (77, 141, 255)     # --acc
ACC_2 = (122, 169, 255)  # --acc-2
FG = (233, 236, 241)     # --fg
DIM = (138, 147, 163)    # --dim

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "web", "og.png")

FONT_DIRS = [
    "/usr/share/fonts/truetype/lato",
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
]


def font(name: str, size: int):
    for d in FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def glow(img: Image.Image, cx: int, cy: int, r: int, colour, strength: float) -> None:
    """Soft radial wash, painted on its own layer and blurred.

    A real gradient would band badly at this size once the PNG is scaled down
    by a chat client; blurring a solid disc avoids that entirely.
    """
    layer = Image.new("RGB", img.size, (0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    layer = layer.filter(ImageFilter.GaussianBlur(r * 0.45))
    img.paste(Image.blend(img, Image.blend(img, layer, 1.0), 0.0), (0, 0))
    # Additive-ish blend so the wash lightens rather than replaces.
    px = img.load()
    lp = layer.load()
    for y in range(img.size[1]):
        for x in range(img.size[0]):
            r0, g0, b0 = px[x, y]
            r1, g1, b1 = lp[x, y]
            px[x, y] = (
                min(255, int(r0 + r1 * strength)),
                min(255, int(g0 + g1 * strength)),
                min(255, int(b0 + b1 * strength)),
            )


def main() -> None:
    random.seed(7)  # deterministic output; a card must not churn per build

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # --- ambient washes, echoing the two drifting radial layers on the page
    glow(img, 210, 90, 460, ACC, 0.30)
    glow(img, 1080, 600, 380, (46, 230, 168), 0.13)

    # --- data-bar motif, the same falling-bars idea as the CSS background
    bars = Image.new("RGB", (W, H), (0, 0, 0))
    bd = ImageDraw.Draw(bars)
    x = 0
    while x < W:
        h = random.choice([10, 14, 18, 22, 28, 34])
        y = random.randint(0, H - h)
        a = random.choice([0.05, 0.07, 0.10, 0.13])
        col = tuple(int(c * a) for c in ACC_2)
        bd.rectangle([x, y, x + 2, y + h], fill=col)
        x += random.choice([14, 18, 22, 26, 34])
    bars = bars.filter(ImageFilter.GaussianBlur(0.6))
    img.paste(Image.blend(img, bars, 1.0), (0, 0))
    px, bp = img.load(), bars.load()
    for yy in range(H):
        for xx in range(W):
            r0, g0, b0 = px[xx, yy]
            r1, g1, b1 = bp[xx, yy]
            px[xx, yy] = (min(255, r0 + r1), min(255, g0 + g1), min(255, b0 + b1))

    d = ImageDraw.Draw(img)

    # --- brand mark: the arrow-into-tray from the header
    ms = 44
    mx, my = 84, 150
    d.rounded_rectangle([mx, my, mx + ms, my + ms], radius=12, fill=ACC)
    cx, cy = mx + ms / 2, my + ms / 2
    d.line([cx, cy + 13, cx, cy - 11], fill=BG, width=4)          # shaft
    d.line([cx - 8, cy - 3, cx, cy - 11], fill=BG, width=4)      # left barb
    d.line([cx + 8, cy - 3, cx, cy - 11], fill=BG, width=4)      # right barb
    d.line([mx + 7, cy + 12, mx + 7, cy + 17], fill=BG, width=4)  # tray
    d.line([mx + ms - 7, cy + 12, mx + ms - 7, cy + 17], fill=BG, width=4)

    # --- wordmark and tagline.
    # Every draw below uses anchor "ls" (left, BASELINE) so the vertical
    # rhythm is expressed in baselines. Pillow's default top-left anchor
    # anchors to the ascender box, which for a 92px face sits far above the
    # baseline and silently overlaps whatever is drawn next.
    A = "ls"
    tx = mx + ms + 30
    f_word = font("Lato-Heavy.ttf", 92)
    f_tag = font("Lato-Regular.ttf", 33)
    d.text((tx, 258), "dropzone", font=f_word, fill=FG, anchor=A)
    d.text((tx + 3, 320), "upload anything", font=f_tag, fill=DIM, anchor=A)

    # --- accent rule
    d.rounded_rectangle([mx, 384, mx + 92, 388], radius=2, fill=ACC)

    # --- three supporting lines, phrased as facts rather than marketing
    f_li = font("Lato-Regular.ttf", 27)
    lines = [
        "Resumable chunked uploads",
        "No Cloudflare 100 MB request ceiling",
        "One file, no dependencies",
    ]
    for i, s in enumerate(lines):
        by = 452 + i * 46
        d.ellipse([mx + 5, by - 9, mx + 13, by - 1], fill=ACC)
        d.text((mx + 36, by), s, font=f_li, fill=DIM, anchor=A)

    # --- mono caption bottom-right, like the page footer
    f_cap = font("DejaVuSansMono.ttf", 22)
    cap = "python3 dropzone.py --key ..."
    wcap = d.textlength(cap, font=f_cap)
    d.text((W - 76 - wcap, 590), cap, font=f_cap, fill=(100, 109, 125), anchor=A)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    print("wrote %s (%d bytes)" % (OUT, os.path.getsize(OUT)))


if __name__ == "__main__":
    main()