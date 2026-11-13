"""Draws the app icon and writes icon.png, icon.ico (Windows) and icon.icns (macOS, needs iconutil).

Usage: python make_icons.py <output dir>
"""
import os, shutil, subprocess, sys
from PIL import Image, ImageDraw, ImageFilter

S = 1024
out = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))


def rounded(size, box, radius, fill):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, radius, fill=fill)
    return layer


def draw():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # Background tile (macOS grid: 824px tile, 100px margin) with a top-to-bottom gradient.
    tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / S
        gd.line([(0, y), (S, y)], fill=(int(48 - 26 * t), int(48 - 26 * t), int(54 - 28 * t), 255))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((100, 100, 924, 924), 185, fill=255)
    tile.paste(grad, (0, 0), mask)
    img = Image.alpha_composite(img, tile)

    # Soft shadow under the floating button.
    shadow = rounded((S, S), (215, 405, 809, 715), 70, (0, 0, 0, 170)).filter(ImageFilter.GaussianBlur(28))
    img = Image.alpha_composite(img, shadow)

    # The button: black outline, dark face, orange accent bar (matches the app).
    img = Image.alpha_composite(img, rounded((S, S), (200, 330, 824, 670), 64, (0, 0, 0, 255)))
    img = Image.alpha_composite(img, rounded((S, S), (212, 342, 812, 658), 54, (58, 58, 62, 255)))
    img = Image.alpha_composite(img, rounded((S, S), (212, 342, 812, 470), 54, (255, 255, 255, 18)))
    img = Image.alpha_composite(img, rounded((S, S), (262, 590, 762, 618), 14, (232, 163, 58, 255)))

    # Label hint: three white bars standing in for button text.
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((322, 432, 702, 470), 19, fill=(255, 255, 255, 235))
    d.rounded_rectangle((382, 498, 642, 528), 15, fill=(255, 255, 255, 140))
    return img


def main():
    os.makedirs(out, exist_ok=True)
    img = draw()
    img.save(os.path.join(out, "icon.png"))
    img.save(os.path.join(out, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

    if shutil.which("iconutil"):
        iconset = os.path.join(out, "icon.iconset")
        os.makedirs(iconset, exist_ok=True)
        for base in (16, 32, 128, 256, 512):
            img.resize((base, base), Image.LANCZOS).save(os.path.join(iconset, f"icon_{base}x{base}.png"))
            img.resize((base * 2, base * 2), Image.LANCZOS).save(os.path.join(iconset, f"icon_{base}x{base}@2x.png"))
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(out, "icon.icns")], check=True)
        shutil.rmtree(iconset)


if __name__ == "__main__":
    main()
