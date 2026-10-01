#!/usr/bin/env python3
"""Regenerate the README header art (hero-dark.svg, hero-light.svg). Stdlib only.

The diagram follows the code: the gateway's observer hooks feed the in-process hub,
processes outside the gateway forward through POST /keryx/publish, subscribers read
GET /keryx/stream, and every non-/keryx path is relayed to the native API server.
"""
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))

THEMES = {
    "dark": dict(bg="#0d1117", panel="#161b22", line="#30363d", text="#e6edf3", dim="#8b949e",
                 accent="#bc8cff"),
    "light": dict(bg="#ffffff", panel="#f6f8fa", line="#d0d7de", text="#1f2328", dim="#656d76",
                  accent="#8250df"),
}
FONT = "ui-sans-serif, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"
ALT = ("keryx-stream: Hermes hook events and forwarded worker events go into one hub on port 8646, "
       "which streams them to the Keryx app over SSE and relays every other path to the API server")


def box(x, y, w, h, label, sub, c, stroke, mono_sub=False):
    cy = y + h / 2
    sub_font = MONO if mono_sub else FONT
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{c["panel"]}" stroke="{stroke}" stroke-width="1.5"/>'
            f'<text x="{x + w / 2}" y="{cy - 4}" text-anchor="middle" font-family="{FONT}" font-size="17" font-weight="600" fill="{c["text"]}">{label}</text>'
            f'<text x="{x + w / 2}" y="{cy + 17}" text-anchor="middle" font-family="{sub_font}" font-size="12.5" fill="{c["dim"]}">{sub}</text>')


def arrow(x1, y1, x2, y2, color, dashed=False):
    """A line from (x1, y1) to (x2, y2) with a head at the second point, in any direction."""
    a = math.atan2(y2 - y1, x2 - x1)
    bx, by = x2 - 7 * math.cos(a), y2 - 7 * math.sin(a)
    px, py = -math.sin(a) * 5, math.cos(a) * 5
    hx, hy = x2 - 8 * math.cos(a), y2 - 8 * math.sin(a)
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    return (f'<line x1="{x1}" y1="{y1}" x2="{bx:.1f}" y2="{by:.1f}" stroke="{color}" stroke-width="1.8"{dash}/>'
            f'<path d="M{hx + px:.1f},{hy + py:.1f} L{x2},{y2} L{hx - px:.1f},{hy - py:.1f} Z" fill="{color}"/>')


def label(x, y, text, c, color=None, anchor="middle", mono=True):
    return (f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-family="{MONO if mono else FONT}" font-size="12.5" '
            f'fill="{color or c["dim"]}">{text}</text>')


def hero(c):
    W, H = 1200, 340
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{ALT}">',
         f'<rect width="{W}" height="{H}" rx="16" fill="{c["bg"]}" stroke="{c["line"]}"/>',
         f'<text x="60" y="78" font-family="{MONO}" font-size="38" font-weight="700" fill="{c["text"]}">keryx-stream</text>',
         f'<text x="60" y="112" font-family="{FONT}" font-size="18" fill="{c["dim"]}">'
         'A Hermes plugin that streams each agent turn live to the Keryx app. No core patches.</text>']
    lx, lw = 60, 250          # sources
    hx, hw = 470, 260         # the hub
    rx, rw = 890, 250         # consumers
    top, bot, h = 150, 248, 62
    s.append(box(lx, top, lw, h, "Hermes gateway", "observer hooks", c, c["line"]))
    s.append(box(lx, bot, lw, h, "hermes chat · cron", "other processes", c, c["line"]))
    s.append(box(hx, top, hw, bot + h - top, "keryx-stream", "hub · :8646 · bearer auth", c, c["accent"]))
    s.append(box(rx, top, rw, h, "Keryx app", "renders the turn live", c, c["line"]))
    s.append(box(rx, bot, rw, h, "Hermes API server", "/health · /v1/* · /api/*", c, c["line"]))
    s.append(arrow(lx + lw, top + h / 2, hx, top + h / 2, c["dim"]))
    s.append(label((lx + lw + hx) / 2, top + h / 2 - 9, "turn events", c))
    s.append(arrow(lx + lw, bot + h / 2, hx, bot + h / 2, c["dim"], dashed=True))
    s.append(label((lx + lw + hx) / 2, bot + h / 2 - 9, "POST /keryx/publish", c))
    s.append(arrow(hx + hw, top + h / 2, rx, top + h / 2, c["accent"]))
    s.append(label((hx + hw + rx) / 2, top + h / 2 - 9, "SSE /keryx/stream", c, color=c["accent"]))
    s.append(arrow(hx + hw, bot + h / 2, rx, bot + h / 2, c["dim"]))
    s.append(label((hx + hw + rx) / 2, bot + h / 2 - 9, "relay the rest", c))
    s.append("</svg>")
    return "".join(s)


for theme, colors in THEMES.items():
    with open(os.path.join(HERE, f"hero-{theme}.svg"), "w", encoding="utf-8") as f:
        f.write(hero(colors))
print("ok")
