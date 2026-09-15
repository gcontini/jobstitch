"""jobstitch mark: two pages stitched together across a centre seam.

The detailed icon carries the text lines (with two amber keyword highlights
on the CV side); the favicon is the same composition with the lines dropped
and fewer, larger stitches, so the two pages survive down to 16px.

One geometry table -> SVG (hand-written) and PNG (PIL, 4x supersampled), so
the vector and raster versions cannot drift apart.
"""
from PIL import Image, ImageDraw

S = 512                      # design canvas
SS = 4                       # supersample factor for the raster pass

INK_TOP  = "#21498C"
INK_BOT  = "#0F2756"         # resume3.tex.jinja \definecolor{darkblue}
PAPER    = "#F5F2EA"
THREAD   = "#FF9A3C"
HILITE   = "#FFC978"
TEXTBAR  = "#A4B4CE"
R_SQ     = 114               # rounded-square corner radius


def hx(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def chevrons(x0, x1, y0, height, pitch, count):
    """Stacked '>' stitches, as 3-point polylines."""
    return [[(x0, y0 + i * pitch),
             (x1, y0 + i * pitch + height / 2),
             (x0, y0 + i * pitch + height)] for i in range(count)]


# ---------------------------------------------------------------- geometry ---
ICON = dict(
    panels=[(60, 96, 240, 416), (272, 96, 452, 416)],
    panel_r=18,
    bar_h=14,
    rows=[146, 190, 234, 278, 322, 366],
    left_w=[92, 76, 86, 60, 82, 50],
    right_w=[92, 80, 66, 88, 56, 76],
    left_x=86, right_edge=426,
    hilite_rows=[1, 3],
    threads=chevrons(198, 314, 116, 70, 105, 3),
    thread_w=20,
)

FAVICON = dict(
    panels=[(60, 96, 228, 416), (284, 96, 452, 416)],
    panel_r=18,
    bar_h=0, rows=[], left_w=[], right_w=[],
    left_x=0, right_edge=0, hilite_rows=[],
    threads=chevrons(176, 336, 121, 100, 170, 2),
    thread_w=38,
)


# -------------------------------------------------------------------- SVG ----
def svg(g, title):
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {S} {S}" '
         f'width="{S}" height="{S}" role="img" aria-label="{title}">',
         f'  <title>{title}</title>',
         '  <defs>',
         '    <linearGradient id="ink" x1="0" y1="0" x2="0" y2="1">',
         f'      <stop offset="0" stop-color="{INK_TOP}"/>',
         f'      <stop offset="1" stop-color="{INK_BOT}"/>',
         '    </linearGradient>',
         '    <clipPath id="squircle">',
         f'      <rect width="{S}" height="{S}" rx="{R_SQ}"/>',
         '    </clipPath>',
         '  </defs>',
         '  <g clip-path="url(#squircle)">',
         f'    <rect width="{S}" height="{S}" fill="url(#ink)"/>']

    for (x0, y0, x1, y1) in g["panels"]:
        p.append(f'    <rect x="{x0}" y="{y0}" width="{x1-x0}" '
                 f'height="{y1-y0}" rx="{g["panel_r"]}" fill="{PAPER}"/>')

    if g["rows"]:
        p.append(f'    <g fill="{TEXTBAR}">')
        h, r = g["bar_h"], g["bar_h"] / 2
        for i, y in enumerate(g["rows"]):
            p.append(f'      <rect x="{g["left_x"]}" y="{y-h/2:g}" '
                     f'width="{g["left_w"][i]}" height="{h}" rx="{r:g}"/>')
            w = g["right_w"][i]
            fill = f' fill="{HILITE}"' if i in g["hilite_rows"] else ''
            p.append(f'      <rect x="{g["right_edge"]-w}" y="{y-h/2:g}" '
                     f'width="{w}" height="{h}" rx="{r:g}"{fill}/>')
        p.append('    </g>')

    p.append(f'    <g fill="none" stroke="{THREAD}" '
             f'stroke-width="{g["thread_w"]}" stroke-linecap="round" '
             f'stroke-linejoin="round">')
    for poly in g["threads"]:
        pts = " ".join(f"{x:g},{y:g}" for x, y in poly)
        p.append(f'      <polyline points="{pts}"/>')
    p += ['    </g>', '  </g>', '</svg>']
    return "\n".join(p) + "\n"


# -------------------------------------------------------------------- PNG ----
def png(g, size):
    w = S * SS
    img = Image.new("RGBA", (w, w), (0, 0, 0, 0))

    top, bot = hx(INK_TOP), hx(INK_BOT)
    col = Image.new("RGB", (1, w))
    for y in range(w):
        t = y / (w - 1)
        col.putpixel((0, y), tuple(round(a + (b - a) * t)
                                   for a, b in zip(top, bot)))
    img.paste(col.resize((w, w)), (0, 0))

    d = ImageDraw.Draw(img)
    for (x0, y0, x1, y1) in g["panels"]:
        d.rounded_rectangle([x0*SS, y0*SS, x1*SS, y1*SS],
                            radius=g["panel_r"]*SS, fill=hx(PAPER))

    h = g["bar_h"]
    for i, y in enumerate(g["rows"]):
        for x0, bw, c in (
            (g["left_x"], g["left_w"][i], TEXTBAR),
            (g["right_edge"] - g["right_w"][i], g["right_w"][i],
             HILITE if i in g["hilite_rows"] else TEXTBAR),
        ):
            d.rounded_rectangle(
                [x0*SS, (y - h/2)*SS, (x0 + bw)*SS, (y + h/2)*SS],
                radius=h/2*SS, fill=hx(c))

    tw, col = g["thread_w"] * SS, hx(THREAD) + (255,)
    for poly in g["threads"]:                   # round joins + round caps
        pts = [(x*SS, y*SS) for x, y in poly]
        d.line(pts, fill=col, width=tw, joint="curve")
        for x, y in (pts[0], pts[-1]):
            d.ellipse([x - tw//2, y - tw//2, x + tw//2, y + tw//2], fill=col)

    mask = Image.new("L", (w, w), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w-1, w-1],
                                           radius=R_SQ*SS, fill=255)
    img.putalpha(mask)
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    import sys
    out = sys.argv[1].rstrip("/")
    open(f"{out}/jobstitch-icon.svg", "w").write(svg(ICON, "jobstitch"))
    open(f"{out}/jobstitch-favicon.svg", "w").write(svg(FAVICON, "jobstitch"))
    png(ICON, 512).save(f"{out}/jobstitch-icon.png")
    for n in (512, 256, 128):
        png(ICON, n).save(f"{out}/jobstitch-icon-{n}.png")
    png(FAVICON, 32).save(f"{out}/jobstitch-favicon.png")
    for n in (16, 32, 48, 180):
        png(FAVICON, n).save(f"{out}/jobstitch-favicon-{n}.png")
    print("ok")
