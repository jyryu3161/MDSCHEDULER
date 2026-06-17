"""Plotly figure-dict builders (CONTRACT §9.7, §4 PlotType).

Each builder returns a plain dict {"data": [...], "layout": {...}} matching the Plotly JSON
schema, which the frontend renders directly. No plotly dependency is required to *produce*
these dicts; they are hand-built JSON-serializable structures. Styling (palette, axes, fonts,
white background) comes from the shared publication theme so every figure matches.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

from . import theme


def _line(x: Sequence, y: Sequence, name: str, color: str | None = None) -> Dict:
    trace = {"x": list(x), "y": list(y), "type": "scatter", "mode": "lines", "name": name}
    if color:
        trace["line"] = {"color": color, "width": theme.LINE_WIDTH}
    return trace


def _fig(data: List[Dict], title: str, xaxis: str, yaxis: str) -> Dict:
    return {"data": theme.style_traces(data),
            "layout": theme.pub_layout(title, xaxis, yaxis, legend=len(data) > 1)}


def rmsd_figure(time_ps: Sequence[float], backbone: Sequence[float], ligand: Sequence[float]) -> Dict:
    data = [_line(time_ps, backbone, "Backbone", theme.C_BACKBONE)]
    if ligand is not None and len(ligand) > 0:
        data.append(_line(time_ps, ligand, "Ligand", theme.C_LIGAND))
    return _fig(data, "RMSD", "Time (ps)", "RMSD (Å)")


def rmsf_figure(labels: Sequence[str], values: Sequence[float]) -> Dict:
    data = [{"x": list(labels), "y": list(values), "type": "bar", "name": "RMSF",
             "marker": {"color": theme.C_RMSF}}]
    return _fig(data, "Per-residue RMSF", "Residue", "RMSF (Å)")


def rg_figure(time_ps: Sequence[float], rg: Sequence[float]) -> Dict:
    return _fig([_line(time_ps, rg, "Rg", theme.C_RG)], "Radius of Gyration", "Time (ps)", "Rg (Å)")


def sasa_figure(time_ps: Sequence[float], sasa: Sequence[float]) -> Dict:
    return _fig([_line(time_ps, sasa, "SASA", theme.C_SASA)],
                "Solvent Accessible Surface Area", "Time (ps)", "SASA (Å²)")


def hbond_figure(time_ps: Sequence[float], counts: Sequence[int]) -> Dict:
    return _fig([_line(time_ps, counts, "H-bonds", theme.C_HBOND)],
                "Receptor–Ligand Hydrogen Bonds", "Time (ps)", "Count")


def energy_figure(time_ps: Sequence[float], energy: Dict[str, Sequence[float]]) -> Dict:
    data = [
        _line(time_ps, energy.get("potential", []), "Potential", theme.PALETTE[0]),
        _line(time_ps, energy.get("kinetic", []), "Kinetic", theme.PALETTE[1]),
        _line(time_ps, energy.get("total", []), "Total", theme.PALETTE[2]),
    ]
    return _fig(data, "Energy", "Time (ps)", "Energy (kJ/mol)")


def ligand_rmsd_figure(time_ps: Sequence[float], ligand: Sequence[float]) -> Dict:
    return _fig([_line(time_ps, ligand, "Ligand RMSD", theme.C_LIGAND)],
                "Ligand RMSD (site-aligned)", "Time (ps)", "RMSD (Å)")


def contact_map_figure(contact: Dict) -> Dict:
    labels = contact.get("residue_labels", [])
    fractions = contact.get("contact_fraction", [])
    # Single-ligand contact profile rendered as a heatmap row (residue x ligand-contact).
    data = [{
        "z": [list(fractions)],
        "x": list(labels),
        "y": ["Ligand"],
        "type": "heatmap",
        "colorscale": theme.SEQUENTIAL,
        "zmin": 0.0,
        "zmax": 1.0,
        "colorbar": {"title": {"text": "Contact fraction", "side": "right"}, "thickness": 14},
    }]
    return {"data": data, "layout": theme.pub_layout("Residue–Ligand Contact Map", "Residue", "",
                                                     legend=False)}


def overlay_rmsd_figure(per_pose: List[Dict]) -> Dict:
    """Pose-comparison overlay: one RMSD trace per pose (CONTRACT §5 plots subjob_id omitted)."""
    data = []
    for i, p in enumerate(per_pose):
        color = theme.PALETTE[i % len(theme.PALETTE)]
        data.append(_line(p["time_ps"], p["backbone_rmsd"], f"pose {p['pose_index']}", color))
    return _fig(data, "Backbone RMSD — pose comparison", "Time (ps)", "RMSD (Å)")
