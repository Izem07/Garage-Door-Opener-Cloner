"""Halo app icon generator: glowing garage line-art with Wi-Fi arcs on deep navy.

Usage: python tools/make_icons.py web   (needs Pillow; writes every icon size into web/)
"""
import sys
from PIL import Image, ImageDraw, ImageFilter, ImageChops

OUT = sys.argv[1]
K = 4                      # supersample factor
S = 1024 * K
P = lambda *v: [int(x * K) for x in v]

LINE = (236, 240, 248, 255)
DIM = (236, 240, 248, 150)




def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def background(rounded: bool):
    bg = Image.new("RGB", (S, S))
    d = ImageDraw.Draw(bg)
    top, bot = (28, 36, 56), (8, 11, 20)
    for y in range(0, S, K):
        d.rectangle([0, y, S, y + K], fill=lerp(top, bot, y / S))
    # soft navy bloom behind the garage
    bloom = Image.new("L", (S, S), 0)
    ImageDraw.Draw(bloom).ellipse(P(212, 330, 812, 930), fill=150)
    bloom = bloom.filter(ImageFilter.GaussianBlur(120 * K))
    bg = Image.composite(Image.new("RGB", (S, S), (40, 62, 104)), bg, bloom)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mask = Image.new("L", (S, S), 0)
    if rounded:
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=228 * K, fill=255)
    else:
        mask.paste(255, [0, 0, S, S])
    out.paste(bg, (0, 0), mask)
    # 1px inner highlight on the top edge, like a glass tile
    if rounded:
        rim = Image.new("L", (S, S), 0)
        ImageDraw.Draw(rim).rounded_rectangle([0, 0, S - 1, S - 1], radius=228 * K, outline=60, width=3 * K)
        fade = Image.linear_gradient("L").resize((S, S)).point(lambda v: 255 - v)
        rim = ImageChops.multiply(rim, fade)
        out = Image.alpha_composite(out, Image.merge("RGBA", (*[Image.new("L", (S, S), 255)] * 3, rim)))
    return out, mask


def stroke(d, pts, w, fill):
    d.line([tuple(P(*p)) for p in pts], fill=fill, width=w * K, joint="curve")
    for x, y in (pts[0], pts[-1]):
        r = w * K / 2
        d.ellipse([x * K - r, y * K - r, x * K + r, y * K + r], fill=fill)


def garage_layer():
    g = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(g)
    stroke(d, [(196, 528), (512, 340), (828, 528)], 30, LINE)          # roof
    stroke(d, [(262, 492), (262, 812), (762, 812), (762, 492)], 28, LINE)  # walls + floor
    d.rounded_rectangle(P(346, 576, 678, 812), radius=10 * K, outline=LINE, width=24 * K)  # door frame
    for y in (636, 692, 748):                                            # door panels
        stroke(d, [(384, y), (640, y)], 14, DIM)
    return g


def halo_layer():
    """White Wi-Fi arcs radiating from the roof peak."""
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy, w = 512, 320, 28
    for r in (70, 130, 190):
        d.arc(P(cx - r, cy - r, cx + r, cy + r), 220, 320, fill=LINE, width=w * K)
        for ang in (220, 320):                       # round caps
            import math
            x = cx + (r - w / 2) * math.cos(math.radians(ang)); y = cy + (r - w / 2) * math.sin(math.radians(ang))
            d.ellipse(P(x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=LINE)
    return layer


def glow(layer, radius, strength, tint=None):
    a = layer.split()[3].filter(ImageFilter.GaussianBlur(radius * K))
    a = a.point(lambda v: min(255, int(v * strength)))
    base = Image.new("RGB", (S, S), tint) if tint else layer.convert("RGB").filter(ImageFilter.GaussianBlur(radius * K))
    return Image.merge("RGBA", (*base.split(), a))


def make(rounded=True):
    img, mask = background(rounded)
    garage, halo = garage_layer(), halo_layer()
    img = Image.alpha_composite(img, glow(garage, 16, .55, (150, 190, 255)))
    img = Image.alpha_composite(img, glow(halo, 26, .9, (190, 215, 255)))
    img = Image.alpha_composite(img, garage)
    img = Image.alpha_composite(img, halo)
    img.putalpha(ImageChops.multiply(img.split()[3], mask))
    return img.resize((1024, 1024), Image.LANCZOS)


icon = make(True)

for name, size in [("icon-512.png", 512), ("icon-192.png", 192), ("favicon-32x32.png", 32)]:
    icon.resize((size, size), Image.LANCZOS).save(f"{OUT}/{name}", optimize=True)
icon.resize((48, 48), Image.LANCZOS).save(f"{OUT}/favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
# iOS applies its own corner mask: apple-touch-icon and maskable icon must be full-bleed & opaque
full = make(False).convert("RGB")
full.resize((180, 180), Image.LANCZOS).save(f"{OUT}/apple-touch-icon.png", optimize=True)
full.resize((512, 512), Image.LANCZOS).save(f"{OUT}/icon-maskable-512.png", optimize=True)
