"""Shareable Tokenmin scorecard — SVG (canonical), HTML wrapper, PNG export.

SPDX-License-Identifier: Apache-2.0

One fixed 1200×630 card (the standard social/OG image size) rendered from a
Tokenmin Score dict (see scoring.py). The SVG is the canonical artifact — it
opens in any browser and embeds straight into the HTML page. PNG export is
best-effort via Pillow (clean pip install, no system libs) with a cairosvg
fallback; if neither is present the caller still has the SVG + HTML to screenshot.

The card carries ONLY aggregate numbers (grade, composite, four pillar scores) —
no paths, names, or message content — so it is safe to share by construction.
"""
from __future__ import annotations

from html import escape

WIDTH, HEIGHT = 1200, 630

# Palette (placeholder RMW-leaning; Rick can swap the hexes + drop in a logo).
BG = "#0B1020"
BG2 = "#11182E"
CARD = "#0F1730"
TEXT = "#F8FAFC"
MUTED = "#94A3B8"
TRACK = "#1E293B"
ACCENT = "#38BDF8"

_GRADE_COLORS = {
    "A": "#22C55E", "B": "#14B8A6", "C": "#EAB308", "D": "#F97316", "F": "#EF4444",
}


def grade_color(grade: str) -> str:
    return _GRADE_COLORS.get((grade or "F")[0].upper(), "#EF4444")


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
    return [(labels.get(p, f"Pillar {p}"), int(pls.get(p, 0))) for p in ("1", "2", "3", "4") if p in pls]


def _next_line(top_finding: dict | None) -> str:
    if not top_finding:
        return "You're dialed in. Nothing urgent to fix."
    title = str(top_finding.get("title", "")).strip()
    return f"Do next: {title}" if title else ""


# --- SVG (canonical) --------------------------------------------------------

def render_svg(score: dict, top_finding: dict | None = None, meta: dict | None = None) -> str:
    meta = meta or {}
    grade = score.get("grade", "?")
    comp = score.get("composite", 0)
    tier = score.get("tier", "")
    gcolor = grade_color(grade)
    provisional = score.get("provisional")
    percentile = score.get("percentile")
    font = ("-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
            "Helvetica, Arial, sans-serif")

    # Left hero ring.
    cx, cy, r = 230, 300, 150
    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" font-family="{font}">'
    )
    parts.append(
        f'<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{BG}"/><stop offset="1" stop-color="{BG2}"/>'
        f'</linearGradient></defs>'
    )
    parts.append(f'<rect width="{WIDTH}" height="{HEIGHT}" fill="url(#bg)"/>')
    parts.append(f'<rect x="32" y="32" width="{WIDTH-64}" height="{HEIGHT-64}" rx="28" fill="{CARD}"/>')

    # Eyebrow.
    parts.append(
        f'<text x="80" y="104" fill="{MUTED}" font-size="26" font-weight="700" '
        f'letter-spacing="3">TOKENMIN SCORE</text>'
    )

    # Hero grade ring.
    parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{TRACK}" stroke-width="20"/>')
    frac = max(0.0, min(1.0, comp / 100.0))
    import math
    circ = 2 * math.pi * r
    dash = circ * frac
    parts.append(
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{gcolor}" stroke-width="20" '
        f'stroke-linecap="round" stroke-dasharray="{dash:.1f} {circ:.1f}" '
        f'transform="rotate(-90 {cx} {cy})"/>'
    )
    parts.append(
        f'<text x="{cx}" y="{cy+30}" fill="{gcolor}" font-size="150" font-weight="800" '
        f'text-anchor="middle">{escape(grade)}</text>'
    )
    parts.append(
        f'<text x="{cx}" y="{cy+92}" fill="{MUTED}" font-size="30" font-weight="600" '
        f'text-anchor="middle">{comp}/100</text>'
    )

    # Right column: tier + subtitle + pillar bars.
    rx = 470
    parts.append(
        f'<text x="{rx}" y="172" fill="{TEXT}" font-size="62" font-weight="800">{escape(tier)}</text>'
    )
    # Subtitle: provisional note takes priority, else the single next-step hook.
    nxt = _next_line(top_finding)
    if provisional:
        parts.append(
            f'<text x="{rx}" y="212" fill="{MUTED}" font-size="23" font-style="italic">'
            f'provisional — re-run after a week of use</text>'
        )
    elif nxt:
        sub = nxt if len(nxt) <= 52 else nxt[:51] + "…"
        parts.append(
            f'<text x="{rx}" y="212" fill="{ACCENT}" font-size="23" font-weight="600">{escape(sub)}</text>'
        )

    bars = _bars(score)
    by = 268
    bar_w, bar_h = 560, 16
    for label, val in bars:
        parts.append(f'<text x="{rx}" y="{by-8}" fill="{MUTED}" font-size="22" font-weight="600">{escape(label)}</text>')
        parts.append(f'<text x="{rx+bar_w}" y="{by-8}" fill="{TEXT}" font-size="22" font-weight="700" text-anchor="end">{val}</text>')
        parts.append(f'<rect x="{rx}" y="{by}" width="{bar_w}" height="{bar_h}" rx="8" fill="{TRACK}"/>')
        fill_w = max(8, int(bar_w * max(0, min(100, val)) / 100))
        parts.append(f'<rect x="{rx}" y="{by}" width="{fill_w}" height="{bar_h}" rx="8" fill="{grade_color(grade)}"/>')
        by += 64

    # Footer.
    fy = HEIGHT - 64
    parts.append(f'<line x1="80" y1="{fy-26}" x2="{WIDTH-80}" y2="{fy-26}" stroke="{TRACK}" stroke-width="2"/>')
    parts.append(
        f'<text x="80" y="{fy+6}" fill="{MUTED}" font-size="24" font-weight="600">'
        f'tokenmin.ai · built by RMW Commerce</text>'
    )
    if percentile is not None:
        parts.append(
            f'<text x="{WIDTH-80}" y="{fy+6}" fill="{TEXT}" font-size="24" font-weight="700" '
            f'text-anchor="end">Top {100-int(percentile)}% of developers</text>'
        )
    parts.append('</svg>')
    return "".join(parts)


# --- HTML wrapper -----------------------------------------------------------

def wrap_html(svg: str, caption: str) -> str:
    cap = escape(caption)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My Tokenmin Score</title>
<style>
  body {{ margin:0; background:{BG}; color:{TEXT};
         font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
         display:flex; flex-direction:column; align-items:center; padding:40px 16px; }}
  .card {{ width:100%; max-width:900px; }}
  .card svg {{ width:100%; height:auto; border-radius:18px;
               box-shadow:0 20px 60px rgba(0,0,0,.45); display:block; }}
  .share {{ max-width:900px; width:100%; margin-top:24px;
            background:{CARD}; border:1px solid {TRACK}; border-radius:14px; padding:18px 20px; }}
  .share p {{ margin:0 0 12px; color:{MUTED}; font-size:15px; }}
  .row {{ display:flex; gap:10px; align-items:center; }}
  textarea {{ flex:1; background:{BG2}; color:{TEXT}; border:1px solid {TRACK};
              border-radius:10px; padding:12px; font-size:15px; resize:vertical; min-height:60px; }}
  button {{ background:{ACCENT}; color:#04121f; border:0; border-radius:10px;
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
    """Render the card to a 1200×630 PNG. Returns True on success.

    Primary path is Pillow (self-contained wheels, no system libs). Falls back
    to cairosvg if Pillow is absent. Returns False (caller shows a screenshot
    hint) if neither is available.
    """
    if _render_png_pillow(score, top_finding, meta, out_path):
        return True
    return _render_png_cairosvg(score, top_finding, meta, out_path)


def _load_font(size: int, bold: bool = False):
    from PIL import ImageFont
    candidates = (
        ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
         "/System/Library/Fonts/Helvetica.ttc",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]
        if bold else
        ["/System/Library/Fonts/Supplemental/Arial.ttf",
         "/System/Library/Fonts/Helvetica.ttc",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


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

        img = Image.new("RGB", (WIDTH, HEIGHT), BG)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([32, 32, WIDTH - 32, HEIGHT - 32], radius=28, fill=CARD)

        def text(xy, s, size, color, bold=False, anchor="la"):
            d.text(xy, s, font=_load_font(size, bold), fill=color, anchor=anchor)

        text((80, 78), "TOKENMIN SCORE", 26, MUTED, bold=True)

        # Hero ring.
        cx, cy, r = 230, 300, 150
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=TRACK, width=20)
        frac = max(0.0, min(1.0, comp / 100.0))
        if frac > 0:
            d.arc([cx - r, cy - r, cx + r, cy + r], start=-90, end=-90 + 360 * frac,
                  fill=gcolor, width=20)
        text((cx, cy), grade, 150, gcolor, bold=True, anchor="mm")
        text((cx, cy + 90), f"{comp}/100", 30, MUTED, bold=True, anchor="mm")

        # Right column.
        rx = 470
        text((rx, 140), tier, 58, TEXT, bold=True, anchor="lm")
        nxt = _next_line(top_finding)
        if score.get("provisional"):
            text((rx, 190), "provisional — re-run after a week of use", 23, MUTED)
        elif nxt:
            sub = nxt if len(nxt) <= 52 else nxt[:51] + "…"
            text((rx, 190), sub, 23, ACCENT, bold=True)

        by = 268
        bar_w, bar_h = 560, 16
        for p in ("1", "2", "3", "4"):
            pls = score.get("pillars", {})
            if p not in pls:
                continue
            label = score.get("pillar_labels", {}).get(p, f"Pillar {p}")
            val = int(pls[p])
            text((rx, by - 26), label, 22, MUTED, bold=True)
            text((rx + bar_w, by - 26), str(val), 22, TEXT, bold=True, anchor="ra")
            d.rounded_rectangle([rx, by, rx + bar_w, by + bar_h], radius=8, fill=TRACK)
            fill_w = max(8, int(bar_w * max(0, min(100, val)) / 100))
            d.rounded_rectangle([rx, by, rx + fill_w, by + bar_h], radius=8, fill=gcolor)
            by += 64

        fy = HEIGHT - 64
        d.line([80, fy - 26, WIDTH - 80, fy - 26], fill=TRACK, width=2)
        text((80, fy), "tokenmin.ai · built by RMW Commerce", 24, MUTED, bold=True)
        pct = score.get("percentile")
        if pct is not None:
            text((WIDTH - 80, fy), f"Top {100 - int(pct)}% of developers", 24, TEXT, bold=True, anchor="ra")

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
