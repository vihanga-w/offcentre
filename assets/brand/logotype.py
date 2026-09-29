"""Custom monoline logotype for 'offcentre'. Units: x-height 100, stroke 18, baseline y=0.
Every stroke's round caps are accounted for so bowls, stems and arches share one 0..-100 band."""
import math

H, SW = 100, 18
HALF = SW / 2
R = (H - SW) / 2          # bowl radius on the stroke centreline (41)
CY = -H / 2               # bowl centre y

def pt(cx, cy, r, deg):
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)

def letters():
    """Return (paths, dot) with glyphs laid out left to right; paths are stroke-only."""
    out, x = [], 0.0
    gap = 13

    # o (the mark): full ring. Its off-centre dot is returned separately in the accent colour.
    cx = x + H / 2
    out.append(f"M{cx - R:.1f} {CY} A{R} {R} 0 1 1 {cx + R:.1f} {CY} A{R} {R} 0 1 1 {cx - R:.1f} {CY}")
    dot = (cx - 11, CY + 4, 13)
    x += H + gap

    # ff: two stems with hooked tops and one shared crossbar (a ligature)
    for i in range(2):
        sx = x + 18 + i * 46
        out.append(f"M{sx} {-HALF} V{-104} A26 26 0 0 1 {sx + 26} {-130} H{sx + 34}")
    out.append(f"M{x} {-H + HALF} H{x + 18 + 46 + 36}")
    x += 18 + 46 + 36 + gap

    # c: bowl open on the right
    cx = x + H / 2
    a0, a1 = pt(cx, CY, R, -42), pt(cx, CY, R, 42)
    out.append(f"M{a0[0]:.1f} {a0[1]:.1f} A{R} {R} 0 1 0 {a1[0]:.1f} {a1[1]:.1f}")
    x += a0[0] - x + HALF + gap

    # e: bar through the middle, bowl open at lower right
    cx = x + H / 2
    b = pt(cx, CY, R, 0)
    e1 = pt(cx, CY, R, 48)
    out.append(f"M{cx - R:.1f} {CY} H{b[0]:.1f} A{R} {R} 0 1 0 {e1[0]:.1f} {e1[1]:.1f}")
    x += H + gap

    # n: stem + arch
    sx, ar = x + HALF, 32
    out.append(f"M{sx} {-HALF} V{-H + HALF} M{sx} {-H + HALF + ar} A{ar} {ar} 0 0 1 {sx + 2 * ar} {-H + HALF + ar} V{-HALF}")
    x += HALF + 2 * ar + HALF + gap

    # t: tall stem curling into a foot, crossbar at x-height
    sx = x + 17
    out.append(f"M{sx} {-128} V{-30} A21 21 0 0 0 {sx + 21} {-HALF} H{sx + 28}")
    out.append(f"M{x} {-H + HALF} H{sx + 26}")
    x += 17 + 28 + HALF + gap - 4

    # r: stem + shoulder
    sx = x + HALF
    out.append(f"M{sx} {-HALF} V{-H + HALF} M{sx} {-58} C{sx} {-80} {sx + 20} {-H + HALF} {sx + 46} {-H + HALF}")
    x += HALF + 46 + HALF + gap - 2

    # e again
    cx = x + H / 2
    b = pt(cx, CY, R, 0)
    e1 = pt(cx, CY, R, 48)
    out.append(f"M{cx - R:.1f} {CY} H{b[0]:.1f} A{R} {R} 0 1 0 {e1[0]:.1f} {e1[1]:.1f}")
    x += H

    return out, dot, x

PATHS, DOT, WIDTH = letters()

def logotype(x, y, height, ink, accent):
    """Wordmark with its x-height scaled to `height` px, baseline at y."""
    s = height / H
    d = " ".join(PATHS)
    return (f'<g transform="translate({x:.1f} {y:.1f}) scale({s:.4f})">'
            f'<path d="{d}" fill="none" stroke="{ink}" stroke-width="{SW}" stroke-linecap="round" stroke-linejoin="round"/>'
            f'<circle cx="{DOT[0]:.1f}" cy="{DOT[1]:.1f}" r="{DOT[2]}" fill="{accent}"/></g>')

def mark(x, y, size, ink, accent):
    """Just the 'o' with its dot, centred on (x, y), `size` px across."""
    s = size / H
    cx = H / 2
    return (f'<g transform="translate({x - size/2:.1f} {y + size/2:.1f}) scale({s:.4f})">'
            f'<circle cx="{cx}" cy="{CY}" r="{R}" fill="none" stroke="{ink}" stroke-width="{SW}"/>'
            f'<circle cx="{DOT[0]:.1f}" cy="{DOT[1]:.1f}" r="{DOT[2]}" fill="{accent}"/></g>')

def width_at(height):
    return WIDTH * height / H
