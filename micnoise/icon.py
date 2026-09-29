"""Tray icon artwork, drawn at runtime so the app ships no binary assets."""

from PIL import Image, ImageDraw

# State -> (body colour, accent colour)
_PALETTE = {
    "active": ((0x2E, 0xC4, 0x7B, 255), (0xFF, 0xFF, 0xFF, 255)),
    "bypassed": ((0x8A, 0x92, 0x9E, 255), (0xFF, 0xFF, 0xFF, 255)),
    "stopped": ((0x6B, 0x72, 0x80, 255), (0xE5, 0xE7, 0xEB, 255)),
    "error": ((0xE0, 0x4F, 0x5F, 255), (0xFF, 0xFF, 0xFF, 255)),
}


def make_icon(state="active", size=64):
    """A rounded-square badge with a microphone glyph."""
    body, accent = _PALETTE.get(state, _PALETTE["active"])
    scale = 8  # supersample, then downscale for smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.24), fill=body)

    # Capsule
    cap_w, cap_h = s * 0.26, s * 0.40
    cx, top = s / 2.0, s * 0.17
    d.rounded_rectangle(
        [cx - cap_w / 2, top, cx + cap_w / 2, top + cap_h],
        radius=cap_w / 2,
        fill=accent,
    )

    # Cradle arc
    arc_w = s * 0.44
    arc_top = top + cap_h * 0.42
    arc_bottom = top + cap_h * 1.16
    d.arc(
        [cx - arc_w / 2, arc_top, cx + arc_w / 2, arc_bottom],
        start=0,
        end=180,
        fill=accent,
        width=int(s * 0.055),
    )

    # Stem and base
    stem_w = s * 0.055
    d.rectangle([cx - stem_w / 2, arc_bottom - s * 0.03, cx + stem_w / 2, s * 0.83], fill=accent)
    base_w = s * 0.28
    d.rounded_rectangle(
        [cx - base_w / 2, s * 0.80, cx + base_w / 2, s * 0.855],
        radius=stem_w / 2,
        fill=accent,
    )

    if state == "bypassed":  # a slash across the glyph
        d.line([s * 0.20, s * 0.80, s * 0.80, s * 0.20], fill=(0x22, 0x26, 0x2C, 255),
               width=int(s * 0.09))
        d.line([s * 0.20, s * 0.80, s * 0.80, s * 0.20], fill=accent, width=int(s * 0.045))

    return img.resize((size, size), Image.LANCZOS)


def save_ico(path, sizes=(16, 24, 32, 48, 64, 128, 256)):
    """Write a multi-resolution .ico, used for the built executable."""
    base = make_icon("active", 256)
    base.save(path, format="ICO", sizes=[(s, s) for s in sizes])
    return path


if __name__ == "__main__":  # python -m micnoise.icon build/app.ico
    import sys

    save_ico(sys.argv[1] if len(sys.argv) > 1 else "app.ico")
