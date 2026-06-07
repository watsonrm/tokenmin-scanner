"""Shareable Tokenmin scorecard — SVG (canonical), HTML wrapper, PNG export.

SPDX-License-Identifier: Apache-2.0

One fixed 1200×630 card (the standard social/OG image size) rendered from a
Tokenmin Score dict (see scoring.py). Styled to the RMW Commerce Consulting
brand (Brand Guidelines v1.0): indigo #263F73, supporting blues, Raleway
(display) + Open Sans (body), and the RMW logomark. Brand assets are bundled
under engine/assets/ so every card is self-contained and on-brand.

The SVG is the canonical artifact — it opens in any browser and embeds straight
into the HTML page. PNG export is best-effort via Pillow (clean pip install, no
system libs), which also gives us pixel-accurate brand typography. The card
carries ONLY aggregate numbers (grade, composite, four pillar scores) — no
paths, names, or content — so it is safe to share by construction.
"""
from __future__ import annotations

import base64
from html import escape
from pathlib import Path

WIDTH, HEIGHT = 1200, 630
_ASSETS = Path(__file__).resolve().parent / "assets"

# --- RMW Commerce brand palette (Brand Guidelines v1.0) ---------------------
PAGE_TOP = "#EDF0F5"
PAGE_BOT = "#E1E6EF"
CARD = "#FFFFFF"
INDIGO = "#263F73"      # primary brand
INK = "#1A2540"         # primary text
INK2 = "#4B5673"        # secondary text
MUTED = "#9B9190"       # metadata / footer
RULE = "#D7DDE8"        # hairline dividers
TRACK = "#E6EAF1"       # bar / ring track
ACCENT = "#4978A8"      # blue-yonder accent

# Grade colors: brand-aligned but kept on a green→red semantic scale so the
# grade reads instantly (A teal · B blue · C amber · D brown · F danger-red).
_GRADE_COLORS = {
    "A": "#2A869C",   # brand teal-blue (positive)
    "B": "#4978A8",   # brand blue-yonder
    "C": "#C8922E",   # amber (derived)
    "D": "#8C5B33",   # brand brown (warning)
    "F": "#B94A48",   # brand danger
}


def grade_color(grade: str) -> str:
    return _GRADE_COLORS.get((grade or "F")[0].upper(), "#B94A48")


def caption_for(score: dict) -> str:
    """Suggested social caption — the share copy."""
    g = score.get("grade", "?")
    comp = score.get("composite", 0)
    tier = score.get("tier", "")
    return (
        f"My Claude Code workflow scored {g} ({comp}/100) on Tokenmin — "
        f"“{tier}”. What's yours? https://tokenmin.ai"
    )


def _bars(score: dict) -> list[tuple[str, int]]:
    pls = score.get("pillars", {}) or {}
    labels = score.get("pillar_labels", {}) or {}
    return [(labels.get(p, f"Pillar {p}"), int(pls.get(p, 0)))
            for p in ("1", "2", "3", "4") if p in pls]


def _next_line(top_finding: dict | None) -> str:
    if not top_finding:
        return "You're dialed in. Nothing urgent to fix."
    title = str(top_finding.get("title", "")).strip()
    return f"Do next: {title}" if title else ""


# --- SVG (canonical) --------------------------------------------------------

def _logo_data_uri() -> str | None:
    try:
        raw = (_ASSETS / "rmw_logomark.png").read_bytes()
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except OSError:
        return None


def render_svg(score: dict, top_finding: dict | None = None, meta: dict | None = None) -> str:
    meta = meta or {}
    grade = score.get("grade", "?")
    comp = score.get("composite", 0)
    tier = score.get("tier", "")
    gcolor = grade_color(grade)
    provisional = score.get("provisional")
    percentile = score.get("percentile")
    disp = "'Raleway','Helvetica Neue',Arial,sans-serif"
    body = "'Open Sans','Helvetica Neue',Arial,sans-serif"

    cx, cy, r = 232, 322, 150
    p: list[str] = []
    p.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
             f'viewBox="0 0 {WIDTH} {HEIGHT}">')
    p.append('<defs><linearGradient id="pg" x1="0" y1="0" x2="0" y2="1">'
             f'<stop offset="0" stop-color="{PAGE_TOP}"/>'
             f'<stop offset="1" stop-color="{PAGE_BOT}"/></linearGradient></defs>')
    p.append(f'<rect width="{WIDTH}" height="{HEIGHT}" fill="url(#pg)"/>')
    p.append(f'<rect x="32" y="32" width="{WIDTH-64}" height="{HEIGHT-64}" rx="28" '
             f'fill="{CARD}" stroke="{RULE}" stroke-width="2"/>')

    # Eyebrow + logomark.
    p.append(f'<text x="80" y="100" fill="{INDIGO}" font-family="{disp}" font-size="26" '
             f'font-weight="700" letter-spacing="4">TOKENMIN SCORE</text>')
    logo = _logo_data_uri()
    if logo:
        p.append(f'<image href="{logo}" x="1000" y="56" width="88" height="88"/>')

    # Hero ring + grade.
    import math
    p.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{TRACK}" stroke-width="22"/>')
    circ = 2 * math.pi * r
    dash = circ * max(0.0, min(1.0, comp / 100.0))
    p.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{gcolor}" stroke-width="22" '
             f'stroke-linecap="round" stroke-dasharray="{dash:.1f} {circ:.1f}" '
             f'transform="rotate(-90 {cx} {cy})"/>')
    p.append(f'<text x="{cx}" y="{cy+34}" fill="{gcolor}" font-family="{disp}" font-size="150" '
             f'font-weight="800" text-anchor="middle">{escape(grade)}</text>')
    p.append(f'<text x="{cx}" y="{cy+96}" fill="{INK2}" font-family="{body}" font-size="30" '
             f'font-weight="700" text-anchor="middle">{comp}/100</text>')

    # Right column: tier + subtitle + pillar bars.
    rx = 472
    p.append(f'<text x="{rx}" y="176" fill="{INDIGO}" font-family="{disp}" font-size="60" '
             f'font-weight="800">{escape(tier)}</text>')
    nxt = _next_line(top_finding)
    if provisional:
        p.append(f'<text x="{rx}" y="216" fill="{MUTED}" font-family="{body}" font-size="23" '
                 f'font-style="italic">provisional — re-run after a week of use</text>')
    elif nxt:
        sub = nxt if len(nxt) <= 52 else nxt[:51] + "…"
        p.append(f'<text x="{rx}" y="216" fill="{ACCENT}" font-family="{body}" font-size="23" '
                 f'font-weight="600">{escape(sub)}</text>')

    by, bar_w, bar_h = 282, 556, 16
    for label, val in _bars(score):
        p.append(f'<text x="{rx}" y="{by-8}" fill="{INK2}" font-family="{body}" font-size="22" '
                 f'font-weight="600">{escape(label)}</text>')
        p.append(f'<text x="{rx+bar_w}" y="{by-8}" fill="{INK}" font-family="{body}" font-size="22" '
                 f'font-weight="700" text-anchor="end">{val}</text>')
        p.append(f'<rect x="{rx}" y="{by}" width="{bar_w}" height="{bar_h}" rx="8" fill="{TRACK}"/>')
        fw = max(8, int(bar_w * max(0, min(100, val)) / 100))
        p.append(f'<rect x="{rx}" y="{by}" width="{fw}" height="{bar_h}" rx="8" fill="{gcolor}"/>')
        by += 62

    # Footer.
    fy = HEIGHT - 64
    p.append(f'<line x1="80" y1="{fy-26}" x2="{WIDTH-80}" y2="{fy-26}" stroke="{RULE}" stroke-width="2"/>')
    p.append(f'<text x="80" y="{fy+6}" fill="{INK2}" font-family="{body}" font-size="24" '
             f'font-weight="600">tokenmin.ai · built by RMW Commerce</text>')
    if percentile is not None:
        p.append(f'<text x="{WIDTH-80}" y="{fy+6}" fill="{INDIGO}" font-family="{body}" '
                 f'font-size="24" font-weight="700" text-anchor="end">'
                 f'Top {100-int(percentile)}% of developers</text>')
    p.append('</svg>')
    return "".join(p)


# --- HTML wrapper -----------------------------------------------------------

def wrap_html(svg: str, caption: str) -> str:
    cap = escape(caption)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My Tokenmin Score</title>
<style>
  body {{ margin:0; background:{PAGE_BOT}; color:{INK};
         font-family:'Open Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
         display:flex; flex-direction:column; align-items:center; padding:40px 16px; }}
  .card {{ width:100%; max-width:900px; }}
  .card svg {{ width:100%; height:auto; border-radius:18px;
               box-shadow:0 18px 50px rgba(38,63,115,.22); display:block; }}
  .share {{ max-width:900px; width:100%; margin-top:24px;
            background:{CARD}; border:1px solid {RULE}; border-radius:14px; padding:18px 20px; }}
  .share p {{ margin:0 0 12px; color:{INK2}; font-size:15px; }}
  .row {{ display:flex; gap:10px; align-items:flex-start; }}
  textarea {{ flex:1; background:#F7F9FC; color:{INK}; border:1px solid {RULE};
              border-radius:10px; padding:12px; font-size:15px; resize:vertical; min-height:60px; }}
  button {{ background:{INDIGO}; color:#fff; border:0; border-radius:10px;
            padding:12px 16px; font-weight:700; font-size:15px; cursor:pointer; white-space:nowrap; }}
  button:active {{ transform:translateY(1px); }}
</style></head>
<body>
  <div class="card">{svg}</div>
  <div class="share">
    <p>Copy your caption and post the PNG (saved next to this file):</p>
    <div class="row">
      <textarea id="cap" readonly>{cap}</textarea>
      <button onclick="navigator.clipboard.writeText(document.getElementById('cap').value).then(()=>{{this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',1500);}})">Copy</button>
    </div>
  </div>
</body></html>
"""


# --- PNG export (best-effort) -----------------------------------------------

def render_png(score: dict, top_finding: dict | None, meta: dict | None, out_path: str) -> bool:
    """Render the card to a 1200×630 PNG. Returns True on success. Pillow is the
    primary path (self-contained wheels, brand fonts); cairosvg is the fallback.
    Returns False if neither is available (caller shows a screenshot hint)."""
    if _render_png_pillow(score, top_finding, meta, out_path):
        return True
    return _render_png_cairosvg(score, top_finding, meta, out_path)


_FONT_CACHE: dict = {}


def _font(family: str, size: int, weight: int = 600):
    """Load a bundled brand variable font at a given weight. family: 'display'
    (Raleway) | 'body' (Open Sans). Falls back to Pillow's default."""
    key = (family, size, weight)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    from PIL import ImageFont
    fname = "Raleway-Variable.ttf" if family == "display" else "OpenSans-Variable.ttf"
    try:
        f = ImageFont.truetype(str(_ASSETS / "fonts" / fname), size)
        try:
            f.set_variation_by_axes([weight])  # variable fonts default to thin
        except Exception:
            pass
    except OSError:
        f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def _render_png_pillow(score: dict, top_finding, meta, out_path: str) -> bool:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    try:
        grade = score.get("grade", "?")
        comp = int(score.get("composite", 0))
        tier = score.get("tier", "")
        gcolor = grade_color(grade)

        # Vertical page gradient.
        img = Image.new("RGB", (WIDTH, HEIGHT), PAGE_TOP)
        top = tuple(int(PAGE_TOP[i:i+2], 16) for i in (1, 3, 5))
        bot = tuple(int(PAGE_BOT[i:i+2], 16) for i in (1, 3, 5))
        px = img.load()
        for y in range(HEIGHT):
            t = y / HEIGHT
            row = tuple(int(top[c] + (bot[c] - top[c]) * t) for c in range(3))
            for x in range(WIDTH):
                px[x, y] = row
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([32, 32, WIDTH - 32, HEIGHT - 32], radius=28,
                            fill=CARD, outline=RULE, width=2)

        def text(xy, s, fam, size, color, weight=600, anchor="la"):
            d.text(xy, s, font=_font(fam, size, weight), fill=color, anchor=anchor)

        text((80, 76), "TOKENMIN SCORE", "display", 26, INDIGO, 700)

        # Logomark top-right (RGBA paste).
        try:
            logo = Image.open(_ASSETS / "rmw_logomark.png").convert("RGBA")
            logo = logo.resize((88, 88), Image.LANCZOS)
            img.paste(logo, (1000, 56), logo)
        except Exception:
            pass

        # Hero ring + grade.
        cx, cy, r = 232, 322, 150
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=TRACK, width=22)
        frac = max(0.0, min(1.0, comp / 100.0))
        if frac > 0:
            d.arc([cx - r, cy - r, cx + r, cy + r], start=-90, end=-90 + 360 * frac,
                  fill=gcolor, width=22)
        text((cx, cy), grade, "display", 150, gcolor, 800, anchor="mm")
        text((cx, cy + 92), f"{comp}/100", "body", 30, INK2, 700, anchor="mm")

        # Right column.
        rx = 472
        text((rx, 150), tier, "display", 58, INDIGO, 800, anchor="lm")
        nxt = _next_line(top_finding)
        if score.get("provisional"):
            text((rx, 196), "provisional — re-run after a week of use", "body", 23, MUTED)
        elif nxt:
            sub = nxt if len(nxt) <= 52 else nxt[:51] + "…"
            text((rx, 196), sub, "body", 23, ACCENT, 600)

        by, bar_w, bar_h = 282, 556, 16
        for p in ("1", "2", "3", "4"):
            pls = score.get("pillars", {})
            if p not in pls:
                continue
            label = score.get("pillar_labels", {}).get(p, f"Pillar {p}")
            val = int(pls[p])
            text((rx, by - 26), label, "body", 22, INK2, 600)
            text((rx + bar_w, by - 26), str(val), "body", 22, INK, 700, anchor="ra")
            d.rounded_rectangle([rx, by, rx + bar_w, by + bar_h], radius=8, fill=TRACK)
            fw = max(8, int(bar_w * max(0, min(100, val)) / 100))
            d.rounded_rectangle([rx, by, rx + fw, by + bar_h], radius=8, fill=gcolor)
            by += 62

        fy = HEIGHT - 64
        d.line([80, fy - 26, WIDTH - 80, fy - 26], fill=RULE, width=2)
        text((80, fy), "tokenmin.ai · built by RMW Commerce", "body", 24, INK2, 600)
        pct = score.get("percentile")
        if pct is not None:
            text((WIDTH - 80, fy), f"Top {100 - int(pct)}% of developers", "body", 24, INDIGO, 700, anchor="ra")

        img.save(out_path, "PNG")
        return True
    except Exception:
        return False


def _render_png_cairosvg(score, top_finding, meta, out_path: str) -> bool:
    try:
        import cairosvg  # type: ignore
    except ImportError:
        return False
    try:
        svg = render_svg(score, top_finding, meta)
        cairosvg.svg2png(bytestring=svg.encode("utf-8"), write_to=out_path,
                         output_width=WIDTH, output_height=HEIGHT)
        return True
    except Exception:
        return False
