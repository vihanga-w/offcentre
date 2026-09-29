"""Build offcentre's brand assets from code, so they stay reproducible and font-independent.

    python3 assets/brand/build.py

Writes:
  assets/banner-light.svg, assets/banner-dark.svg   README banner (GitHub light / dark)
  assets/social-preview.svg                         1280x640 source for the repo's social image
  assets/mark.svg                                   the logo mark (avatar)
  web/favicon.svg                                   control-page icon, follows the colour scheme
Render social-preview.svg to PNG with render.sh (headless Chrome).
"""
import math
from pathlib import Path

from logotype import logotype, mark

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "assets"
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Helvetica Neue', Arial, sans-serif"
TAGLINE = "Fix the stereo image when you can’t sit in the middle."
THEMES = {
    "light": dict(bg="#ffffff", ink="#16181d", muted="#5b6270", faint="#dfe3e8", grid="#eef1f4",
                  left="#2f6fdb", right="#d9730d", seat="#16181d", card="#f6f7f9"),
    "dark": dict(bg="#0d1117", ink="#f0f3f6", muted="#9aa4b2", faint="#2a313c", grid="#171d26",
                 left="#6ea0ff", right="#f0a04b", seat="#f0f3f6", card="#161b22"),
}


def pod(x, y, t, color):
    return (f'<rect x="{x-14}" y="{y-17}" width="28" height="34" rx="12" fill="{t["card"]}" stroke="{color}" stroke-width="3"/>'
            f'<circle cx="{x}" cy="{y-9}" r="3.4" fill="{color}"/>')


def seat(x, y, t):
    return (f'<circle cx="{x}" cy="{y}" r="11" fill="{t["seat"]}"/>'
            f'<circle cx="{x}" cy="{y}" r="20" fill="none" stroke="{t["seat"]}" stroke-opacity=".22" stroke-width="2"/>')


def seat_for_ratio(left, right, ratio, xs, ys):
    """A seat position whose distances to the speakers are in `ratio` (far / near)."""
    best = None
    for sx in xs:
        for sy in ys:
            e = abs(math.dist((sx, sy), right) / math.dist((sx, sy), left) - ratio)
            if best is None or e < best[0]:
                best = (e, (sx, sy))
    return best[1]


def plan_scene(t, x0, w, h, left, right, chip_at, uid, seat_xs=None):
    """The room plan: fading grid, speakers, seat, measured distances, resulting correction."""
    xs = seat_xs or range(left[0] - 120, left[0] + 40)
    s = seat_for_ratio(left, right, 4.0, xs, range(left[1] + 60, h - 30))
    grid = []
    for x in range(x0, w, 38):
        grid.append(f'<line x1="{x}" y1="0" x2="{x}" y2="{h}" stroke="{t["grid"]}" stroke-width="1.2"/>')
    for y in range(-4, h, 38):
        grid.append(f'<line x1="{x0}" y1="{y}" x2="{w}" y2="{y}" stroke="{t["grid"]}" stroke-width="1.2"/>')
    defs = (f'<linearGradient id="fade-{uid}" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
            f'<stop offset=".22" stop-color="#fff" stop-opacity="1"/></linearGradient>'
            f'<mask id="m-{uid}"><rect x="{x0}" width="{w - x0}" height="{h}" fill="url(#fade-{uid})"/></mask>'
            f'<clipPath id="r-{uid}"><rect width="{w}" height="{h}" rx="20"/></clipPath>')

    def dim(a, b, col, txt, off, away_from):
        """Line a-b with its length label offset to the side facing away from `away_from`."""
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        nx, ny = -(b[1] - a[1]), b[0] - a[0]
        n = math.hypot(nx, ny)
        nx, ny = nx / n * off, ny / n * off
        if nx * (away_from[0] - mx) + ny * (away_from[1] - my) > 0:
            nx, ny = -nx, -ny
        return (f'<line x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" stroke="{col}" stroke-width="3" stroke-linecap="round"/>'
                f'<text x="{mx + nx:.0f}" y="{my + ny + 6:.0f}" text-anchor="middle" font-family="{FONT}" font-size="18" '
                f'font-weight="650" fill="{col}">{txt}</text>')

    span = (f'<line x1="{left[0]}" y1="{left[1]}" x2="{right[0]}" y2="{right[1]}" stroke="{t["muted"]}" '
            f'stroke-width="1.6" stroke-dasharray="7 6"/>')
    cx, cy = chip_at
    chip = (f'<g transform="translate({cx} {cy})"><rect width="236" height="62" rx="14" fill="{t["card"]}" stroke="{t["faint"]}"/>'
            f'<text x="18" y="25" font-family="{FONT}" font-size="12.5" letter-spacing="1" fill="{t["muted"]}">LEFT SPEAKER</text>'
            f'<text x="18" y="48" font-family="{FONT}" font-size="19" font-weight="650" fill="{t["ink"]}">+8.75 ms · −12 dB</text></g>')
    body = (f'<g clip-path="url(#r-{uid})"><g mask="url(#m-{uid})">{"".join(grid)}</g></g>' + span
            + dim(s, left, t["left"], "1.00 m", 30, right) + dim(s, right, t["right"], "4.00 m", 24, (s[0], h))
            + pod(*left, t, t["left"]) + pod(*right, t, t["right"]) + seat(*s, t) + chip)
    return defs, body


def svg(w, h, t, defs, body, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" '
            f'aria-label="{title}"><title>{title}</title><defs>{defs}</defs>'
            f'<rect width="{w}" height="{h}" rx="20" fill="{t["bg"]}"/>{body}</svg>\n')


def banner(t, theme):
    w, h = 1280, 360
    defs, scene = plan_scene(t, 640, w, h, (812, 110), (1192, 110), (940, 236), theme)
    text = (logotype(72, 178, 58, t["ink"], t["left"])
            + f'<text x="74" y="236" font-family="{FONT}" font-size="25" fill="{t["muted"]}">{TAGLINE}</text>')
    return svg(w, h, t, defs, scene + text, f"offcentre: {TAGLINE}")


def social(t):
    """GitHub's link preview: 1280x640, with a 40 px safe margin."""
    w, h = 1280, 640
    defs, scene = plan_scene(t, 640, w, h, (900, 220), (1200, 220), (930, 450), "social",
                             seat_xs=range(830, 960))
    text = (logotype(80, 300, 70, t["ink"], t["left"])
            + f'<text x="84" y="376" font-family="{FONT}" font-size="30" fill="{t["muted"]}">Fix the stereo image when</text>'
            + f'<text x="84" y="416" font-family="{FONT}" font-size="30" fill="{t["muted"]}">you can’t sit in the middle.</text>'
            + f'<text x="84" y="520" font-family="{FONT}" font-size="17" font-weight="600" letter-spacing="2" '
              f'fill="{t["muted"]}" fill-opacity=".85">MACOS · AIRPLAY · HOMEPOD · ANY STEREO PAIR</text>')
    return svg(w, h, t, defs, scene + text, f"offcentre: {TAGLINE}")


def mark_svg(t, size=512):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}" role="img" '
            f'aria-label="offcentre"><title>offcentre</title><rect width="{size}" height="{size}" fill="{t["bg"]}"/>'
            + mark(size / 2, size / 2, size * 0.62, t["ink"], t["left"]) + "</svg>\n")


def favicon():
    """Transparent icon whose ring follows the browser's colour scheme."""
    L, D = THEMES["light"], THEMES["dark"]
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            f'<style>.ink{{stroke:{L["ink"]}}}.dot{{fill:{L["left"]}}}'
            f'@media (prefers-color-scheme: dark){{.ink{{stroke:{D["ink"]}}}.dot{{fill:{D["left"]}}}}}</style>'
            '<circle class="ink" cx="32" cy="32" r="24.6" fill="none" stroke-width="10.8"/>'
            '<circle class="dot" cx="25.4" cy="34.4" r="7.8"/></svg>\n')


if __name__ == "__main__":
    for theme, t in THEMES.items():
        (ASSETS / f"banner-{theme}.svg").write_text(banner(t, theme))
    (ASSETS / "social-preview.svg").write_text(social(THEMES["light"]))
    (ASSETS / "mark.svg").write_text(mark_svg(THEMES["light"]))
    (ROOT / "web" / "favicon.svg").write_text(favicon())
    print("wrote banner-light.svg, banner-dark.svg, social-preview.svg, mark.svg, web/favicon.svg")
