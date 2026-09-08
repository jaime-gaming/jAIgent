"""Build the Windows executable icon from the jAI mark.

The mark lives at ``packaging/logo-source.png``. This script downsamples it
into ``icon.png`` (256×256) and a multi-size ``icon.ico`` so PyInstaller can
embed it. The terminal wordmark is still ASCII in ``jaigent.branding``.

    pip install pillow
    python packaging/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "logo-source.png"

#: Every size Windows may ask for, largest first.
ICO_SIZES = [256, 128, 64, 48, 32, 24, 16]


def load_master() -> Image.Image:
    if not SOURCE.is_file():
        raise SystemExit(f"missing mark: {SOURCE}")
    image = Image.open(SOURCE).convert("RGBA")
    # Square canvas; letterbox onto black if the export is not already square.
    width, height = image.size
    side = max(width, height)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 255))
    canvas.paste(image, ((side - width) // 2, (side - height) // 2), image)
    return canvas


def main() -> None:
    master = load_master()

    png = HERE / "icon.png"
    master.resize((256, 256), Image.LANCZOS).save(png)

    ico = HERE / "icon.ico"
    master.save(ico, sizes=[(n, n) for n in ICO_SIZES])

    print(f"wrote {png}")
    print(f"wrote {ico} ({', '.join(f'{n}x{n}' for n in ICO_SIZES)})")


if __name__ == "__main__":
    main()
