"""Plotly figure-dict builders (CONTRACT §9.7, §4 PlotType).

Each builder returns a plain dict {"data": [...], "layout": {...}} matching the Plotly JSON
schema, which the frontend renders directly. No plotly dependency is required to *produce*
these dicts; they are hand-built JSON-serializable structures.
"""

from __future__ import annotations

from typing import Dict, List, Sequence


def _line(x: Sequence, y: Sequence, name: str, color: str | None = None) -> Dict:
    trace = {"x": list(x), "y": list(y), "type": "scatter", "mode": "lines", "name": name}
    if color:
        trace["line"] = {"color": color}
    return trace


def _layout(title: str, xaxis: str, yaxis: str) -> Dict:
    return {
        "title": {"text": title},
        "xaxis": {"title": {"text": xaxis}},
        "yaxis": {"title": {"text": yaxis}},
        "margin": {"l": 60, "r": 20, "t": 50, "b": 50},
        "template": "plotly_white",
    }


def rmsd_figure(time_ps: Sequence[float], backbone: Sequence[float], ligand: Sequence[float]) -> Dict:
    data = [_line(time_ps, backbone, "Backbone", "#1f77b4")]
    if ligand:
        data.append(_line(time_ps, ligand, "Ligand", "#d62728"))
    return {"data": data, "layout": _layout("RMSD", "Time (ps)", "RMSD (Å)")}


def rmsf_figure(labels: Sequence[str], values: Sequence[float]) -> Dict:
    data = [{
        "x": list(labels),
        "y": list(values),
        "type": "bar",
        "name": "RMSF",
        "marker": {"color": "#2ca02c"},
    }]
    return {"data": data, "layout": _layout("Per-residue RMSF", "Residue", "RMSF (Å)")}


def rg_figure(time_ps: Sequence[float], rg: Sequence[float]) -> Dict:
    return {"data": [_line(time_ps, rg, "Rg", "#9467bd")],
            "layout": _layout("Radius of Gyration", "Time (ps)", "Rg (Å)")}


def sasa_figure(time_ps: Sequence[float], sasa: Sequence[float]) -> Dict:
    return {"data": [_line(time_ps, sasa, "SASA", "#8c564b")],
            "layout": _layout("Solvent Accessible Surface Area", "Time (ps)", "SASA (Å²)")}


def hbond_figure(time_ps: Sequence[float], counts: Sequence[int]) -> Dict:
    return {"data": [_line(time_ps, counts, "H-bonds", "#17becf")],
            "layout": _layout("Receptor–Ligand Hydrogen Bonds", "Time (ps)", "Count")}


def energy_figure(time_ps: Sequence[float], energy: Dict[str, Sequence[float]]) -> Dict:
    data = [
        _line(time_ps, energy.get("potential", []), "Potential", "#1f77b4"),
        _line(time_ps, energy.get("kinetic", []), "Kinetic", "#ff7f0e"),
        _line(time_ps, energy.get("total", []), "Total", "#2ca02c"),
    ]
    return {"data": data, "layout": _layout("Energy", "Time (ps)", "Energy (kJ/mol)")}


def ligand_rmsd_figure(time_ps: Sequence[float], ligand: Sequence[float]) -> Dict:
    return {"data": [_line(time_ps, ligand, "Ligand RMSD", "#d62728")],
            "layout": _layout("Ligand RMSD (site-aligned)", "Time (ps)", "RMSD (Å)")}


def contact_map_figure(contact: Dict) -> Dict:
    labels = contact.get("residue_labels", [])
    fractions = contact.get("contact_fraction", [])
    # Single-ligand contact profile rendered as a heatmap row (residue x ligand-contact).
    data = [{
        "z": [list(fractions)],
        "x": list(labels),
        "y": ["Ligand"],
        "type": "heatmap",
        "colorscale": "YlOrRd",
        "zmin": 0.0,
        "zmax": 1.0,
        "colorbar": {"title": {"text": "Contact fraction"}},
    }]
    layout = _layout("Residue–Ligand Contact Map", "Residue", "")
    return {"data": data, "layout": layout}


def overlay_rmsd_figure(per_pose: List[Dict]) -> Dict:
    """Pose-comparison overlay: one RMSD trace per pose (CONTRACT §5 plots subjob_id omitted)."""
    palette = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e",
               "#8c564b", "#17becf", "#e377c2", "#7f7f7f"]
    data = []
    for i, p in enumerate(per_pose):
        color = palette[i % len(palette)]
        data.append(_line(p["time_ps"], p["backbone_rmsd"], f"pose {p['pose_index']}", color))
    return {"data": data, "layout": _layout("Backbone RMSD — pose comparison", "Time (ps)", "RMSD (Å)")}
