"""Publication-quality Plotly styling — shared single source of truth for every MD figure.

Goal: figures that drop straight into a paper. A consistent, colorblind-safe look:
the Okabe–Ito qualitative palette, perceptually-uniform sequential / diverging scales,
clean black axis lines with outside ticks, readable sans-serif type, and a white
background. Builders return plain JSON-serializable dicts (no plotly dependency); the
frontend renders them and its toolbar exports high-resolution SVG / PNG for figures.

Keep these values in sync with frontend/src/plotTheme.ts (the TypeScript mirror used for
figures built in the browser, e.g. the design convergence curve)."""

from __future__ import annotations

from typing import Dict, List, Optional

# Okabe–Ito colorblind-safe qualitative palette, ordered for lines/categories on white.
PALETTE: List[str] = [
    "#0072B2",  # blue
    "#D55E00",  # vermillion
    "#009E73",  # bluish green
    "#CC79A7",  # reddish purple
    "#E69F00",  # orange
    "#56B4E9",  # sky blue
    "#555555",  # gray
    "#F0E442",  # yellow (use sparingly — too light for thin lines)
]

# Named semantic colors so a given observable keeps ONE color across all figures.
C_BACKBONE = "#0072B2"   # blue
C_LIGAND = "#D55E00"     # vermillion
C_RG = "#009E73"         # bluish green
C_HBOND = "#CC79A7"      # reddish purple
C_SASA = "#E69F00"       # orange
C_ENERGY = "#0072B2"     # blue
C_RMSF = "#0072B2"       # blue
C_ACCENT = "#0072B2"

# Signed quantities (e.g. per-residue ΔG): favorable (negative) = blue, unfavorable
# (positive) = vermillion. Blue/red is colorblind-distinguishable, unlike green/red.
C_FAVORABLE = "#0072B2"
C_UNFAVORABLE = "#D55E00"

# Perceptually-uniform, colorblind-safe sequential scale for heatmaps / intensity bars.
SEQUENTIAL = "Viridis"

FONT_FAMILY = "Inter, Helvetica Neue, Helvetica, Arial, sans-serif"
INK = "#1a1a1a"          # near-black for axes/text (softer than pure #000)
GRID = "#e8ecf1"         # very light gridlines
LINE_WIDTH = 2.2


def _axis(title: str) -> Dict:
    return {
        "title": {"text": title, "font": {"size": 15, "color": INK}},
        "showline": True,
        "linecolor": INK,
        "linewidth": 1.2,
        "mirror": False,
        "ticks": "outside",
        "tickcolor": INK,
        "ticklen": 5,
        "tickwidth": 1.1,
        "tickfont": {"size": 13, "color": INK},
        "gridcolor": GRID,
        "zeroline": False,
        "automargin": True,
    }


def pub_layout(title: str, xaxis: str, yaxis: str, *,
               legend: bool = True, extra: Optional[Dict] = None) -> Dict:
    """A complete publication layout dict (font, axes, white bg, legend, palette)."""
    layout: Dict = {
        "title": {"text": title, "x": 0.02, "xanchor": "left",
                  "font": {"size": 17, "color": INK}},
        "xaxis": _axis(xaxis),
        "yaxis": _axis(yaxis),
        "font": {"family": FONT_FAMILY, "size": 13, "color": INK},
        "paper_bgcolor": "white",
        "plot_bgcolor": "white",
        "margin": {"l": 72, "r": 26, "t": 54, "b": 58},
        "colorway": PALETTE,
        "hovermode": "x unified",
        "showlegend": legend,
        "legend": {
            "font": {"size": 12, "color": INK},
            "bgcolor": "rgba(255,255,255,0.65)",
            "bordercolor": "rgba(0,0,0,0)",
            "borderwidth": 0,
        },
    }
    if extra:
        layout.update(extra)
    return layout


def style_traces(traces: List[Dict]) -> List[Dict]:
    """Apply consistent line width / marker styling in place, cycling the palette for any
    trace that does not set its own color. Returns the same list."""
    ci = 0
    for tr in traces:
        ttype = tr.get("type", "scatter")
        if ttype == "scatter":
            line = tr.setdefault("line", {})
            if "color" not in line:
                line["color"] = PALETTE[ci % len(PALETTE)]
                ci += 1
            line.setdefault("width", LINE_WIDTH)
            if "markers" in tr.get("mode", ""):
                mk = tr.setdefault("marker", {})
                mk.setdefault("size", 5)
                mk.setdefault("color", line["color"])
        elif ttype == "bar":
            mk = tr.setdefault("marker", {})
            if "color" not in mk:
                mk["color"] = PALETTE[ci % len(PALETTE)]
                ci += 1
            # Thin dark outline gives bars definition in print.
            mk.setdefault("line", {"color": INK, "width": 0.6})
    return traces


def figure(traces: List[Dict], title: str, xaxis: str, yaxis: str, *,
           extra_layout: Optional[Dict] = None) -> Dict:
    """Convenience: styled traces + publication layout. Legend auto-hidden for a single series."""
    return {
        "data": style_traces(traces),
        "layout": pub_layout(title, xaxis, yaxis, legend=len(traces) > 1, extra=extra_layout),
    }
