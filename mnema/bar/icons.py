"""Icons drawn in code (Pillow): the app icon and the GNOME tray icons.

The macOS menu bar draws itself (SF Symbol and text); this is for the Mnema.app icon and the Linux
tray, where the icon is a picture. Drawn at 4x and scaled down for smooth edges.
"""

from __future__ import annotations

from typing import Any

ACCENT = (124, 92, 255)  # brand purple, same as the CLI ACCENT
RED = (235, 64, 52)
AMBER = (245, 166, 35)
GREY = (190, 190, 190)

_WAVE = (0.30, 0.55, 0.85, 0.60, 1.0, 0.70, 0.40)  # bar heights of the wave


def _wave(draw: Any, box: tuple[float, float, float, float], color: Any) -> None:
    x0, y0, x1, y1 = box
    n = len(_WAVE)
    step = (x1 - x0) / n
    w = step * 0.55
    mid = (y0 + y1) / 2
    for i, h in enumerate(_WAVE):
        cx = x0 + step * (i + 0.5)
        half = (y1 - y0) * h / 2
        draw.rounded_rectangle(
            (cx - w / 2, mid - half, cx + w / 2, mid + half), radius=w / 2, fill=color
        )


def app_icon(size: int = 1024) -> Any:
    """A rounded square with a gradient and a white wave."""
    from PIL import Image, ImageDraw

    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    top, bottom = (150, 120, 255), (86, 52, 214)
    for y in range(s):
        t = y / (s - 1)
        gd.line(
            [(0, y), (s, y)],
            fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)),
        )
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (s * 0.09, s * 0.09, s * 0.91, s * 0.91), radius=s * 0.2, fill=255
    )
    img.paste(grad, (0, 0), mask)
    _wave(ImageDraw.Draw(img), (s * 0.24, s * 0.28, s * 0.76, s * 0.72), (255, 255, 255, 255))
    return img.resize((size, size), Image.Resampling.LANCZOS)


def tray_icon(kind: str, size: int = 64) -> Any:
    """GNOME tray icon: idle — a light wave, recording — a red dot, saving — a purple wave,
    warning — a yellow dot with "!". Light, bright colors for the dark GNOME panel."""
    from PIL import Image, ImageDraw

    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if kind == "paused":
        for x in (0.28, 0.56):
            d.rounded_rectangle(
                (s * x, s * 0.22, s * (x + 0.16), s * 0.78), radius=s * 0.04, fill=GREY + (255,)
            )
    elif kind == "recording":
        d.ellipse((s * 0.18, s * 0.18, s * 0.82, s * 0.82), fill=RED + (255,))
    elif kind == "warning":
        d.ellipse((s * 0.14, s * 0.14, s * 0.86, s * 0.86), fill=AMBER + (255,))
        d.rounded_rectangle(
            (s * 0.45, s * 0.28, s * 0.55, s * 0.60), radius=s * 0.05, fill=(40, 30, 0, 255)
        )
        d.ellipse((s * 0.44, s * 0.66, s * 0.56, s * 0.78), fill=(40, 30, 0, 255))
    else:
        _wave(
            d,
            (s * 0.10, s * 0.18, s * 0.90, s * 0.82),
            (ACCENT if kind == "saving" else GREY) + (255,),
        )
    return img.resize((size, size), Image.Resampling.LANCZOS)
