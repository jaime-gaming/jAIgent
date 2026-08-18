"""Draw the Windows executable icon.

This is *not* the jaigent logo — the logo is ASCII and lives in
``jaigent.branding``. Windows executables need a real ``.ico`` resource, and
PyInstaller aborts the build if the file it is pointed at is missing, so one is
committed at ``packaging/icon.ico``.

It is drawn from shapes rather than a font or an exported bitmap, so it can be
regenerated anywhere with no assets and no design tool:

    pip install pillow
    python packaging/make_icon.py

Colours come from the same palette as the terminal logo: xterm 173 terracotta
on a near-black terminal background.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent

#: xterm 173 — the ACCENT in jaigent.branding.
TERRACOTTA = (215, 135, 95, 255)
#: xterm 137 — ACCENT_DIM, used for the rim.
TERRACOTTA_DIM = (175, 135, 95, 255)
#: A dark terminal background rather than pure black, so the icon reads on
#: both light and dark Windows taskbars.
BACKGROUND = (28, 27, 26, 255)

#: Drawn large and downsampled; the small sizes are far cleaner that way.
CANVAS = 1024

#: Every size Windows may ask for, largest first.
ICO_SIZES = [256, 128, 64, 48, 32, 24, 16]


def draw_icon(size: int = CANVAS) -> Image.Image:
    """Render the icon: a lowercase ``j`` in terracotta on a rounded square."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = size / 1024

    # Rounded square background with a dim rim.
    draw.rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=int(180 * unit),
        fill=BACKGROUND,
        outline=TERRACOTTA_DIM,
        width=int(12 * unit),
    )

    stem_width = int(112 * unit)
    stem_left = int(556 * unit)
    stem_right = stem_left + stem_width
    stem_top = int(380 * unit)

    # The dot of the j.
    dot_radius = int(62 * unit)
    dot_centre = (stem_left + stem_width / 2, 250 * unit)
    draw.ellipse(
        (
            dot_centre[0] - dot_radius,
            dot_centre[1] - dot_radius,
            dot_centre[0] + dot_radius,
            dot_centre[1] + dot_radius,
        ),
        fill=TERRACOTTA,
    )

    # The stem, stopping where the hook takes over.
    hook_outer_left = int(300 * unit)
    hook_bottom = int(800 * unit)
    hook_centre_y = hook_bottom - (stem_right - hook_outer_left) / 2
    draw.rectangle((stem_left, stem_top, stem_right - 1, hook_centre_y), fill=TERRACOTTA)

    # The hook: the bottom-left quarter of a ring.
    draw.arc(
        (
            hook_outer_left,
            hook_centre_y - (stem_right - hook_outer_left) / 2,
            stem_right - 1,
            hook_bottom,
        ),
        start=0,
        end=180,
        fill=TERRACOTTA,
        width=stem_width,
    )

    # Round off the exposed ends so the stroke looks drawn, not clipped.
    draw.ellipse(
        (stem_left, stem_top - stem_width / 2, stem_right - 1, stem_top + stem_width / 2),
        fill=TERRACOTTA,
    )

    return image


def main() -> None:
    master = draw_icon()

    png = HERE / "icon.png"
    master.resize((256, 256), Image.LANCZOS).save(png)

    ico = HERE / "icon.ico"
    master.save(ico, sizes=[(n, n) for n in ICO_SIZES])

    print(f"wrote {png}")
    print(f"wrote {ico} ({', '.join(f'{n}x{n}' for n in ICO_SIZES)})")


if __name__ == "__main__":
    main()
