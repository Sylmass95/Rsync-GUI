#!/usr/bin/env python3
"""Génère le logo (icône macOS) : assets/AppIcon.icns et assets/logo.png.

Usage : python3 assets/make_icon.py   (nécessite Pillow ; iconutil fourni par macOS)
"""
import os
import shutil
import subprocess
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
S = 1024 * 2  # supersampling


def gradient(size, top, bottom):
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(size):
            px[x, y] = c
    return img


def rounded_mask(size, inset, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([inset, inset, size - inset, size - inset], radius, fill=255)
    return m


def pane(draw, box, fill, accent):
    x0, y0, x1, y1 = box
    r = int((x1 - x0) * 0.09)
    draw.rounded_rectangle(box, r, fill=fill)
    bar = int((y1 - y0) * 0.14)
    draw.rounded_rectangle([x0, y0, x1, y0 + bar], r, fill=accent)
    draw.rectangle([x0, y0 + bar // 2, x1, y0 + bar], fill=accent)
    # lignes de fichiers
    gap = (y1 - y0 - bar) / 5.2
    for i in range(4):
        yy = y0 + bar + gap * (i + 0.7)
        w = (x1 - x0) * (0.72 if i % 2 == 0 else 0.5)
        draw.rounded_rectangle([x0 + (x1 - x0) * 0.12, yy, x0 + (x1 - x0) * 0.12 + w, yy + gap * 0.32],
                               int(gap * 0.16), fill=(200, 210, 232))


def build():
    inset = int(S * 0.04)
    base = gradient(S, (58, 124, 255), (96, 48, 232)).convert("RGBA")
    mask = rounded_mask(S, inset, int(S * 0.225))
    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    icon.paste(base, (0, 0), mask)

    # reflet léger en haut
    gloss = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(gloss)
    gd.ellipse([-S * 0.2, -S * 0.75, S * 1.2, S * 0.42], fill=(255, 255, 255, 34))
    gloss.putalpha(ImageChops.multiply(gloss.getchannel("A"), mask))
    icon = Image.alpha_composite(icon, gloss)

    # ombre portée douce sous les panneaux
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    pw, ph = int(S * 0.31), int(S * 0.40)
    top = int(S * 0.29)
    left_box = [int(S * 0.115), top, int(S * 0.115) + pw, top + ph]
    right_box = [int(S * 0.575), top, int(S * 0.575) + pw, top + ph]
    for b in (left_box, right_box):
        sd.rounded_rectangle([b[0], b[1] + int(S * 0.02), b[2], b[3] + int(S * 0.02)], int(S * 0.03), fill=(20, 10, 80, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(S * 0.018))
    icon = Image.alpha_composite(icon, shadow)

    d = ImageDraw.Draw(icon)
    pane(d, left_box, (255, 255, 255), (118, 150, 255))
    pane(d, right_box, (255, 255, 255), (150, 110, 255))

    # flèche de transfert entre les deux panneaux
    cy = top + ph // 2
    ax0, ax1 = int(S * 0.445), int(S * 0.555)
    th = int(S * 0.026)
    d.rounded_rectangle([ax0, cy - th, ax1 - int(S * 0.03), cy + th], th, fill=(255, 214, 92))
    head = int(S * 0.062)
    d.polygon([(ax1 + int(S * 0.006), cy), (ax1 - head, cy - head), (ax1 - head, cy + head)], fill=(255, 214, 92))

    return icon.resize((1024, 1024), Image.LANCZOS)


def main():
    icon = build()
    icon.save(os.path.join(HERE, "logo.png"))
    iconset = os.path.join(HERE, "AppIcon.iconset")
    shutil.rmtree(iconset, ignore_errors=True)
    os.makedirs(iconset)
    for size in (16, 32, 128, 256, 512):
        icon.resize((size, size), Image.LANCZOS).save(os.path.join(iconset, "icon_%dx%d.png" % (size, size)))
        icon.resize((size * 2, size * 2), Image.LANCZOS).save(os.path.join(iconset, "icon_%dx%d@2x.png" % (size, size)))
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(HERE, "AppIcon.icns")], check=True)
    shutil.rmtree(iconset)
    print("OK : assets/AppIcon.icns, assets/logo.png")


if __name__ == "__main__":
    sys.exit(main())
