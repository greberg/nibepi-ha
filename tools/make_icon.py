"""Draws the brand icons in custom_components/nibepi/brand (HA 2026.3+
shows them for the integration): a house with rising heat.

Usage: python3 tools/make_icon.py custom_components/nibepi/brand
"""

import math, sys
from PIL import Image, ImageDraw

S = 2048
def render(house, wave, out_sizes, prefix, outdir):
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # House: roof apex at top, walls to the bottom, full width (trimmed, no padding).
    m = 0
    roof = [(S/2, 40), (S-40, S*0.46), (S-200, S*0.46), (S-200, S-40), (200, S-40), (200, S*0.46), (40, S*0.46)]
    d.polygon(roof, fill=house)
    # Chimney.
    d.rectangle([S*0.70, S*0.12, S*0.80, S*0.34], fill=house)
    # Three rising heat waves inside the house, drawn as dense discs.
    w = 120
    for x0 in (S*0.33, S*0.50, S*0.67):
        for k in range(0, 1201):
            t = k/1200
            y = S*0.52 + (S*0.88 - S*0.52) * t
            x = x0 + 50*math.sin(2*math.pi*t*1.5)
            d.ellipse([x-w/2, y-w/2, x+w/2, y+w/2], fill=wave)
    bbox = im.getbbox()
    im = im.crop(bbox)
    side = max(im.size)
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.paste(im, ((side-im.size[0])//2, (side-im.size[1])//2))
    for size, name in out_sizes:
        sq.resize((size, size), Image.LANCZOS).save("%s/%s%s" % (outdir, prefix, name), optimize=True)

out = sys.argv[1]
sizes = [(256, "icon.png"), (512, "icon@2x.png")]
render((21, 101, 170, 255), (255, 140, 30, 255), sizes, "", out)
render((90, 170, 235, 255), (255, 160, 60, 255), sizes, "dark_", out)
