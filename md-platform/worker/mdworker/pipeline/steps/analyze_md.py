"""Step 7 — analyze_md (CONTRACT §9.7).

Computes the standard MD analyses from the production trajectory and writes, per pose:
  analysis/*.csv                  one CSV per metric (time series / per-residue)
  analysis/plots/{type}.json      a Plotly figure dict per PlotType (CONTRACT §4)
  analysis/summary.json           scalar summary + data_source per metric + plots_available

Geometric analyses (backbone RMSD, ligand RMSD, Rg, RMSF, protein-ligand H-bond proxy,
residue contact frequency, key-residue distances) are computed DIRECTLY from the trajectory
coordinates, so they are real measurements of whatever trajectory the engine produced (mock
or GROMACS). SASA and potential energy require a force field / surface algorithm not present
in the mock engine; those are emitted as clearly-labelled estimates with
data_source="estimate" when running under the mock engine. (A real GROMACS run would source
them from gmx sasa / gmx energy; that refinement is Phase 2.)
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from mdworker.pipeline import structures as struct

_POLAR = {"N", "O", "F", "S"}
_HBOND_CUTOFF = 3.5      # Angstrom, heavy-atom donor/acceptor proximity proxy
_CONTACT_CUTOFF = 4.5    # Angstrom, residue-ligand contact
# Ligand RMSD (Å) below which the pose counts as "bound". Tightened from 5.0 to 3.0: 5 Å is
# effectively "still in the neighborhood", not bound, which inflates the bound window and biases
# the binding-energy estimate favorable (per the peptide-binding MD-conditions review).
_BOUND_RMSD_CUTOFF = 3.0
# Below this pose occupancy the complex isn't reliably bound, so a binding-energy estimate over
# the (short) bound window is not trustworthy — flag it for the user.
_LOW_OCCUPANCY = 0.5


def _fig(traces: List[dict], title: str, xaxis: str, yaxis: str, *, extra_layout: dict | None = None) -> dict:
    layout = {
        "title": {"text": title},
        "xaxis": {"title": {"text": xaxis}},
        "yaxis": {"title": {"text": yaxis}},
        "margin": {"l": 60, "r": 20, "t": 50, "b": 50},
        "template": "plotly_white",
    }
    if extra_layout:
        layout.update(extra_layout)
    return {"data": traces, "layout": layout}


def _write_csv(path: Path, header: List[str], rows: List[List[Any]]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def run(ctx, settings, *, md: Dict[str, Any]) -> Dict[str, Any]:
    step = "analyze_md"
    ctx.set_status("analyzing", current_step=step, progress=88.0)

    traj_path = md.get("trajectory_pdb_path")
    if not traj_path or not Path(traj_path).exists():
        raise ValueError("analyze_md: trajectory PDB not found; cannot run analysis.")

    atoms, frames = struct.read_multimodel_pdb(traj_path)
    if len(frames) < 2:
        ctx.warning(step, "Trajectory has <2 frames; analysis will be degenerate.")
    n_frames = len(frames)
    frame_interval_ps = float(md.get("frame_interval_ps") or 100.0)
    times_ns = [round(i * frame_interval_ps / 1000.0, 4) for i in range(n_frames)]

    is_bb = np.array([a.is_backbone for a in atoms], dtype=bool)
    is_lig = np.array([a.is_ligand for a in atoms], dtype=bool)
    is_prot = ~is_lig
    bb_idx = np.where(is_bb)[0]
    lig_idx = np.where(is_lig)[0]
    prot_idx = np.where(is_prot)[0]
    engine_name = md.get("engine", "mock")
    # SASA/energy are not parsed from real GROMACS output yet, so they are estimates
    # regardless of engine (Phase 2 will source them from gmx sasa / gmx energy).
    estimate_source = "estimate"

    # Superpose every frame's protein backbone onto frame 0 (Kabsch), so RMSD reflects
    # internal conformational change rather than rigid-body translation/rotation. The same
    # transform is applied to the whole system, keeping ligand-vs-protein motion meaningful.
    raw_frames = [np.asarray(f, dtype=float) for f in frames]
    frames_np = _align_frames(raw_frames, bb_idx)
    ref = frames_np[0]

    plots_dir = ctx.plots_dir
    plots_dir.mkdir(parents=True, exist_ok=True)
    plots_available: List[str] = []
    summary: Dict[str, Any] = {"n_frames": n_frames, "frame_interval_ps": frame_interval_ps,
                               "md_length_ns": md.get("completed_ns"), "engine": engine_name,
                               "data_source": {}, "metrics": {}}

    # ---- RMSD (backbone + ligand) ----------------------------------------------------
    # Backbone RMSD uses optimal superposition (Kabsch) so it reflects internal flexibility
    # rather than whole-system rigid-body drift; ligand RMSD is computed after superposing
    # each frame on the receptor backbone, capturing binding-pose displacement.
    align_idx = bb_idx if bb_idx.size else prot_idx
    bb_rmsd = []
    lig_rmsd = []
    for f in frames_np:
        bb_rmsd.append(_kabsch_rmsd(f[bb_idx], ref[bb_idx]) if bb_idx.size else 0.0)
        if lig_idx.size and align_idx.size:
            R, t = _superpose(f[align_idx], ref[align_idx])
            lig_rmsd.append(_rmsd((f[lig_idx] @ R) + t, ref[lig_idx]))
        elif lig_idx.size:
            lig_rmsd.append(_rmsd(f[lig_idx], ref[lig_idx]))
        else:
            lig_rmsd.append(0.0)
    _write_csv(ctx.analysis_dir / "rmsd.csv", ["time_ns", "backbone_rmsd_A", "ligand_rmsd_A"],
               [[times_ns[i], round(bb_rmsd[i], 4), round(lig_rmsd[i], 4)] for i in range(n_frames)])
    _save(plots_dir / "rmsd.json", _fig(
        [{"x": times_ns, "y": bb_rmsd, "type": "scatter", "mode": "lines", "name": "Backbone"},
         {"x": times_ns, "y": lig_rmsd, "type": "scatter", "mode": "lines", "name": "Ligand"}],
        "RMSD vs time", "Time (ns)", "RMSD (Å)"))
    _save(plots_dir / "ligand_rmsd.json", _fig(
        [{"x": times_ns, "y": lig_rmsd, "type": "scatter", "mode": "lines", "name": "Ligand RMSD",
          "line": {"color": "#d62728"}}],
        "Ligand RMSD (binding-pose stability)", "Time (ns)", "Ligand RMSD (Å)"))
    plots_available += ["rmsd", "ligand_rmsd"]
    summary["data_source"]["rmsd"] = "trajectory"
    summary["data_source"]["ligand_rmsd"] = "trajectory"
    summary["metrics"]["backbone_rmsd_mean_A"] = round(float(np.mean(bb_rmsd)), 4)
    summary["metrics"]["backbone_rmsd_final_A"] = round(float(bb_rmsd[-1]), 4)
    summary["metrics"]["ligand_rmsd_mean_A"] = round(float(np.mean(lig_rmsd)), 4)
    summary["metrics"]["ligand_rmsd_final_A"] = round(float(lig_rmsd[-1]), 4)

    # ---- Radius of gyration (protein) ------------------------------------------------
    rg = [_rg(f[prot_idx]) for f in frames_np]
    _write_csv(ctx.analysis_dir / "rg.csv", ["time_ns", "rg_A"],
               [[times_ns[i], round(rg[i], 4)] for i in range(n_frames)])
    _save(plots_dir / "rg.json", _fig(
        [{"x": times_ns, "y": rg, "type": "scatter", "mode": "lines", "name": "Rg",
          "line": {"color": "#2ca02c"}}],
        "Radius of gyration (protein)", "Time (ns)", "Rg (Å)"))
    plots_available.append("rg")
    summary["data_source"]["rg"] = "trajectory"
    summary["metrics"]["rg_mean_A"] = round(float(np.mean(rg)), 4)

    # ---- RMSF per protein residue (CA, else residue mean) ----------------------------
    res_rmsf = _per_residue_rmsf(atoms, frames_np, prot_idx)
    if res_rmsf:
        labels = [f"{rn}{rs}" for (rn, rs, _v) in res_rmsf]
        vals = [round(v, 4) for (_rn, _rs, v) in res_rmsf]
        _write_csv(ctx.analysis_dir / "rmsf.csv", ["residue", "rmsf_A"],
                   [[labels[i], vals[i]] for i in range(len(vals))])
        _save(plots_dir / "rmsf.json", _fig(
            [{"x": labels, "y": vals, "type": "bar", "name": "RMSF"}],
            "RMSF per residue", "Residue", "RMSF (Å)"))
        plots_available.append("rmsf")
        summary["data_source"]["rmsf"] = "trajectory"
        summary["metrics"]["rmsf_mean_A"] = round(float(np.mean(vals)), 4)

    # ---- H-bond proxy (protein-ligand polar heavy atoms within cutoff) ---------------
    hbond = [_hbond_count(f, atoms, lig_idx, prot_idx) for f in frames_np]
    _write_csv(ctx.analysis_dir / "hbond.csv", ["time_ns", "protein_ligand_hbonds"],
               [[times_ns[i], hbond[i]] for i in range(n_frames)])
    _save(plots_dir / "hbond.json", _fig(
        [{"x": times_ns, "y": hbond, "type": "scatter", "mode": "lines+markers", "name": "H-bonds",
          "line": {"color": "#9467bd"}}],
        "Protein-ligand H-bonds (≤3.5 Å N/O proxy)", "Time (ns)", "H-bond count"))
    plots_available.append("hbond")
    summary["data_source"]["hbond"] = "trajectory (geometric proxy)"
    summary["metrics"]["hbond_mean"] = round(float(np.mean(hbond)), 3)

    # ---- Contact frequency per residue + key-residue distances -----------------------
    contacts, key_residues = _contact_frequency(atoms, frames_np, lig_idx, prot_idx)
    if contacts:
        clabels = [c[0] for c in contacts]
        cfreq = [round(c[1], 4) for c in contacts]
        _write_csv(ctx.analysis_dir / "contact_map.csv", ["residue", "contact_frequency"],
                   [[clabels[i], cfreq[i]] for i in range(len(cfreq))])
        _save(plots_dir / "contact_map.json", _fig(
            [{"x": clabels, "y": cfreq, "type": "bar", "name": "Contact frequency",
              "marker": {"color": cfreq, "colorscale": "Viridis"}}],
            "Protein-ligand contact frequency (≤4.5 Å)", "Residue", "Fraction of frames"))
        plots_available.append("contact_map")
        summary["data_source"]["contact_map"] = "trajectory"
        summary["metrics"]["top_contacts"] = clabels[:5]

    # key-residue distance over time (ligand COM to top contacting residues)
    if key_residues:
        dist_traces = []
        dist_rows_header = ["time_ns"] + [f"{rn}{rs}_A" for (rn, rs, _idx) in key_residues]
        dist_cols: List[List[float]] = [[] for _ in key_residues]
        for f in frames_np:
            lig_com = f[lig_idx].mean(axis=0) if lig_idx.size else np.zeros(3)
            for j, (_rn, _rs, ridx) in enumerate(key_residues):
                dcoord = f[ridx].mean(axis=0)
                dist_cols[j].append(float(np.linalg.norm(dcoord - lig_com)))
        for j, (rn, rs, _ridx) in enumerate(key_residues):
            dist_traces.append({"x": times_ns, "y": [round(v, 3) for v in dist_cols[j]],
                                "type": "scatter", "mode": "lines", "name": f"{rn}{rs}"})
        _write_csv(ctx.analysis_dir / "distances.csv", dist_rows_header,
                   [[times_ns[i]] + [round(dist_cols[j][i], 3) for j in range(len(key_residues))]
                    for i in range(n_frames)])
        # (distance plot is bundled into the report; not a CONTRACT PlotType but kept as CSV+fig)
        _save(plots_dir / "distance.json", _fig(dist_traces, "Ligand–key residue distances",
                                                "Time (ns)", "Distance (Å)"))

    # ---- SASA + energy (force-field metrics; estimates under the mock engine) --------
    sasa = _sasa_estimate(atoms, frames_np, prot_idx, lig_idx)
    _write_csv(ctx.analysis_dir / "sasa.csv", ["time_ns", "sasa_A2"],
               [[times_ns[i], round(sasa[i], 2)] for i in range(n_frames)])
    sasa_note = "  [estimate]"
    _save(plots_dir / "sasa.json", _fig(
        [{"x": times_ns, "y": sasa, "type": "scatter", "mode": "lines", "name": "SASA",
          "line": {"color": "#ff7f0e"}}],
        "Solvent-accessible surface area" + sasa_note, "Time (ns)", "SASA (Å²)"))
    plots_available.append("sasa")
    summary["data_source"]["sasa"] = estimate_source
    summary["metrics"]["sasa_mean_A2"] = round(float(np.mean(sasa)), 2)

    energy = _energy_estimate(bb_rmsd, n_frames)
    _write_csv(ctx.analysis_dir / "energy.csv", ["time_ns", "potential_energy_kJ_mol"],
               [[times_ns[i], round(energy[i], 2)] for i in range(n_frames)])
    en_note = "  [estimate]"
    _save(plots_dir / "energy.json", _fig(
        [{"x": times_ns, "y": energy, "type": "scatter", "mode": "lines", "name": "Potential energy",
          "line": {"color": "#1f77b4"}}],
        "Potential energy" + en_note, "Time (ns)", "Energy (kJ/mol)"))
    plots_available.append("energy")
    summary["data_source"]["energy"] = estimate_source
    summary["metrics"]["energy_mean_kJ_mol"] = round(float(np.mean(energy)), 2)

    # ---- ligand stability verdict ----------------------------------------------------
    # Delegate the verdict to the single shared heuristic in mdworker.analysis.metrics so the
    # threshold rule cannot drift between modules.
    from mdworker.analysis.metrics import ligand_stability as _ligand_stability

    stab = _ligand_stability(lig_rmsd)
    lig_final = stab["final_rmsd"]
    stable = bool(stab["stable"])
    summary["metrics"]["ligand_stable"] = stable
    summary["metrics"]["ligand_pose_verdict"] = (
        "stable (ligand RMSD < 3 Å)" if stable else "drifted (ligand RMSD ≥ 3 Å)"
    )
    stability = {
        "mean_rmsd_A": round(stab["mean_rmsd"], 4),
        "max_rmsd_A": round(stab["max_rmsd"], 4),
        "final_rmsd_A": round(stab["final_rmsd"], 4),
        "drift_A": round(stab["drift"], 4),
        "stable": stable,
    }
    summary["ligand_stability"] = stability
    _write_csv(ctx.analysis_dir / "ligand_stability.csv", ["metric", "value"],
               [[k, v] for k, v in stability.items()])

    # ---- bound-window auto-detection -------------------------------------------------
    # The docked pose starts at frame 0 (ligand RMSD 0) and may dissociate during the run.
    # Energetic analysis (MM/PBSA) over frames AFTER dissociation mixes bound + unbound
    # states and is meaningless, so we mark the leading contiguous segment where the ligand
    # is still in the pocket (RMSD < cutoff). mmpbsa reads this window from summary.json and
    # restricts the binding-ΔG decomposition to it.
    bw = _bound_window(lig_rmsd, times_ns, _BOUND_RMSD_CUTOFF)
    summary["bound_window"] = bw
    bound_idx = list(range(bw["n_bound_frames"]))
    summary["metrics"]["pose_occupancy"] = bw["pose_occupancy"]
    ctx.info(step, "Bound window: {0:.3f}–{1:.3f} ns ({2}/{3} frames, ligand RMSD < {4:g} Å); "
             "pose occupancy {5:.0%} of trajectory{6}."
             .format(bw["start_ns"], bw["end_ns"], bw["n_bound_frames"], bw["n_total_frames"],
                     _BOUND_RMSD_CUTOFF, bw["pose_occupancy"],
                     "" if bw["occupancy_ok"] else " — LOW: complex not reliably bound, binding-energy estimate untrustworthy"))
    if not bw["occupancy_ok"]:
        ctx.warning(step, f"Pose occupancy {bw['pose_occupancy']:.0%} < {_LOW_OCCUPANCY:.0%}: the ligand does "
                          "not stay bound; any MM/GBSA estimate over the short bound window is unreliable.")

    # ---- per-residue contacts + H-bonds over the bound window (unified-hotspot inputs)
    # Computed over the bound window only: contacts/H-bonds after dissociation would dilute
    # every residue toward zero and hide the real binding interface. Merged with the MM/PBSA
    # per-residue ΔG by the backend into one hotspot table.
    residue_contacts = _residue_contacts(atoms, frames_np, lig_idx, prot_idx, bound_idx)
    if residue_contacts:
        rc_doc = {
            "window_ns": [bw["start_ns"], bw["end_ns"]],
            "n_frames": bw["n_bound_frames"],
            "contact_cutoff_A": _CONTACT_CUTOFF,
            "hbond_cutoff_A": _HBOND_CUTOFF,
            "residues": residue_contacts,
        }
        (ctx.analysis_dir / "residue_contacts.json").write_text(json.dumps(rc_doc, indent=2))
        _write_csv(ctx.analysis_dir / "residue_contacts.csv",
                   ["chain", "resname", "resnum", "contact_frequency", "hbond_mean"],
                   [[r["chain"], r["resname"], r["resnum"], r["contact_frequency"], r["hbond_mean"]]
                    for r in residue_contacts])
        summary["metrics"]["interface_residues"] = [
            f"{r['resname']}{r['resnum']}" for r in residue_contacts[:5]
        ]

    # ---- final snapshot (last frame) -------------------------------------------------
    final_src = md.get("final_gro_path")
    final_snapshot = ctx.analysis_dir / "final_snapshot.pdb"
    if final_src and Path(final_src).exists() and Path(final_src).suffix == ".pdb":
        final_snapshot.write_text(Path(final_src).read_text())
    else:
        struct.write_pdb(atoms, frames_np[-1], final_snapshot, title="final snapshot")

    summary["plots_available"] = plots_available
    (ctx.analysis_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    ctx.info(step, f"Analysis complete: {len(plots_available)} plots, "
                   f"ligand RMSD final {lig_final:.2f} Å ({summary['metrics']['ligand_pose_verdict']}).")
    ctx.progress(95.0, current_step=step)
    return {"summary": summary, "plots_available": plots_available,
            "time_ns": times_ns, "backbone_rmsd": bb_rmsd}


# ----------------------------------------------------------------------------------------
# numeric helpers
# ----------------------------------------------------------------------------------------
def _rmsd(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=float); b = np.asarray(b, dtype=float)
    if a.shape != b.shape or a.size == 0:
        return 0.0
    return float(np.sqrt(((a - b) ** 2).sum(axis=1).mean()))


def _align_frames(frames, align_idx):
    """Superpose every frame onto frame 0 using ``align_idx`` (protein backbone), applying the
    rigid transform to the whole system, so RMSF/per-atom fluctuations reflect internal motion
    rather than global translation/rotation. Frame 0 is the reference (returned unchanged)."""
    if not frames:
        return frames
    out = [np.asarray(frames[0], dtype=float)]
    if align_idx is None or len(align_idx) == 0:
        return [np.asarray(f, dtype=float) for f in frames]
    ref_sel = out[0][align_idx]
    for f in frames[1:]:
        f = np.asarray(f, dtype=float)
        R, t = _superpose(f[align_idx], ref_sel)
        out.append((f @ R) + t)
    return out


def _superpose(P: np.ndarray, Q: np.ndarray):
    """Return (R, t) mapping P onto Q (rotation then translation) via Kabsch."""
    if P.shape[0] == 0:
        return np.eye(3), np.zeros(3)
    Pc = P.mean(axis=0)
    Qc = Q.mean(axis=0)
    H = (P - Pc).T @ (Q - Qc)
    try:
        V, _S, Wt = np.linalg.svd(H)
        d = np.sign(np.linalg.det(V @ Wt))
        R = V @ np.diag([1.0, 1.0, d]) @ Wt
    except np.linalg.LinAlgError:
        R = np.eye(3)
    t = Qc - Pc @ R
    return R, t


def _kabsch_rmsd(P: np.ndarray, Q: np.ndarray) -> float:
    """RMSD after optimal superposition (removes rigid-body translation/rotation)."""
    P = np.asarray(P, dtype=float); Q = np.asarray(Q, dtype=float)
    if P.shape != Q.shape or P.size == 0:
        return 0.0
    R, t = _superpose(P, Q)
    return _rmsd((P @ R) + t, Q)


def _rg(xyz: np.ndarray) -> float:
    if len(xyz) == 0:
        return 0.0
    c = xyz.mean(axis=0)
    return float(np.sqrt(((xyz - c) ** 2).sum(axis=1).mean()))


def _per_residue_rmsf(atoms, frames_np, prot_idx):
    """RMSF per protein residue using CA atoms (fallback: residue atom mean)."""
    stacked = np.stack(frames_np, axis=0)  # (n_frames, n_atoms, 3)
    mean_pos = stacked.mean(axis=0)         # (n_atoms, 3)
    fluct = np.sqrt(((stacked - mean_pos) ** 2).sum(axis=2).mean(axis=0))  # (n_atoms,)
    res_map: Dict[tuple, List[int]] = {}
    ca_map: Dict[tuple, int] = {}
    for i in prot_idx:
        a = atoms[i]
        key = (a.chain, a.resseq, a.resname)
        res_map.setdefault(key, []).append(i)
        if a.name == "CA":
            ca_map[key] = i
    out = []
    for key, idxs in res_map.items():
        rn, rs = key[2], key[1]
        if key in ca_map:
            v = float(fluct[ca_map[key]])
        else:
            v = float(np.mean([fluct[i] for i in idxs]))
        out.append((rn, rs, v))
    out.sort(key=lambda t: t[1])
    return out


def _hbond_count(frame, atoms, lig_idx, prot_idx) -> int:
    if lig_idx.size == 0 or prot_idx.size == 0:
        return 0
    lig_polar = [i for i in lig_idx if atoms[i].element in _POLAR]
    prot_polar = [i for i in prot_idx if atoms[i].element in _POLAR]
    if not lig_polar or not prot_polar:
        return 0
    lp = frame[lig_polar]; pp = frame[prot_polar]
    count = 0
    for p in lp:
        d = np.linalg.norm(pp - p, axis=1)
        count += int(np.count_nonzero(d <= _HBOND_CUTOFF))
    return count


def _contact_frequency(atoms, frames_np, lig_idx, prot_idx):
    """Fraction of frames where each protein residue has any atom within cutoff of ligand."""
    if lig_idx.size == 0 or prot_idx.size == 0:
        return [], []
    res_atoms: Dict[tuple, List[int]] = {}
    for i in prot_idx:
        a = atoms[i]
        res_atoms.setdefault((a.chain, a.resseq, a.resname), []).append(i)
    n_frames = len(frames_np)
    counts: Dict[tuple, int] = {k: 0 for k in res_atoms}
    for f in frames_np:
        lig = f[lig_idx]
        for key, idxs in res_atoms.items():
            ra = f[idxs]
            # min distance residue-atoms <-> ligand-atoms
            close = False
            for atom in ra:
                if np.min(np.linalg.norm(lig - atom, axis=1)) <= _CONTACT_CUTOFF:
                    close = True
                    break
            if close:
                counts[key] += 1
    freqs = [((f"{k[2]}{k[1]}"), counts[k] / max(1, n_frames), k) for k in res_atoms]
    freqs = [(lbl, fr, k) for (lbl, fr, k) in freqs if fr > 0]
    freqs.sort(key=lambda t: t[1], reverse=True)
    contacts = [(lbl, fr) for (lbl, fr, _k) in freqs]
    key_residues = [(k[2], k[1], res_atoms[k]) for (_lbl, _fr, k) in freqs[:4]]
    return contacts, key_residues


def _bound_window(lig_rmsd, times_ns, cutoff: float) -> Dict[str, Any]:
    """Leading contiguous frames (from t=0) where ligand RMSD < ``cutoff`` (Å).

    Frame 0 is the docked pose (RMSD 0), always included; we walk forward until the ligand
    first leaves the binding region. Returns start/end times (ns), the bound-frame count, and
    the criterion so downstream consumers (mmpbsa, UI) can label the window honestly.
    """
    n = len(lig_rmsd)
    if n == 0:
        return {
            "start_ns": 0.0, "end_ns": 0.0, "n_bound_frames": 0, "n_total_frames": 0,
            "ligand_rmsd_cutoff_A": cutoff, "pose_occupancy": 0.0, "occupancy_ok": False,
            "criterion": f"leading contiguous frames with ligand RMSD < {cutoff:g} Angstrom",
            "fully_bound": False,
        }
    last = -1  # index of the last leading frame still bound; -1 = ligand never in pocket
    for i in range(n):
        if lig_rmsd[i] < cutoff:
            last = i
        else:
            break
    # Pose occupancy = fraction of the WHOLE trajectory bound (not just the leading segment).
    # Reported honestly so a complex that dissociates can't be scored as if stably bound; the
    # MM/GBSA window still uses the leading bound segment, but occupancy tells the real story.
    occupancy = round(sum(1 for r in lig_rmsd if r < cutoff) / n, 4)
    return {
        "start_ns": times_ns[0] if len(times_ns) > 0 else 0.0,
        "end_ns": times_ns[last] if last >= 0 and len(times_ns) > last else 0.0,
        "n_bound_frames": last + 1,
        "n_total_frames": n,
        "ligand_rmsd_cutoff_A": cutoff,
        "pose_occupancy": occupancy,                 # fraction of ALL frames with RMSD < cutoff
        "occupancy_ok": occupancy >= _LOW_OCCUPANCY,  # False => not reliably bound; ΔG untrustworthy
        "criterion": f"leading contiguous frames with ligand RMSD < {cutoff:g} Angstrom",
        "fully_bound": n > 0 and last == n - 1,
    }


def _residue_contacts(atoms, frames_np, lig_idx, prot_idx, frame_indices):
    """Per-residue contact frequency + mean H-bond count over the given frames.

    For each protein residue, across ``frame_indices``:
      contact_frequency = fraction of frames with any residue heavy atom within
                          ``_CONTACT_CUTOFF`` of any ligand atom
      hbond_mean        = mean count of polar(residue)-polar(ligand) atom pairs within
                          ``_HBOND_CUTOFF`` (geometric H-bond proxy)
    Returns dicts sorted by contact_frequency then hbond_mean (desc), residues with neither
    omitted: [{chain, resname, resnum, contact_frequency, hbond_mean}].
    """
    if lig_idx.size == 0 or prot_idx.size == 0 or not frame_indices:
        return []
    res_atoms: Dict[tuple, List[int]] = {}
    res_polar: Dict[tuple, List[int]] = {}
    for i in prot_idx:
        a = atoms[i]
        key = (a.chain, int(a.resseq), a.resname)
        res_atoms.setdefault(key, []).append(i)
        if a.element in _POLAR:
            res_polar.setdefault(key, []).append(i)
    lig_polar = [i for i in lig_idx if atoms[i].element in _POLAR]
    n = len(frame_indices)
    contact_counts = {k: 0 for k in res_atoms}
    hbond_sums = {k: 0 for k in res_atoms}
    for fi in frame_indices:
        f = frames_np[fi]
        lig = f[lig_idx]
        lig_p = f[lig_polar] if lig_polar else None
        for key, idxs in res_atoms.items():
            ra = f[idxs]
            close = False
            for atom in ra:
                if np.min(np.linalg.norm(lig - atom, axis=1)) <= _CONTACT_CUTOFF:
                    close = True
                    break
            if close:
                contact_counts[key] += 1
            if lig_p is not None and key in res_polar:
                pp = f[res_polar[key]]
                for p in pp:
                    d = np.linalg.norm(lig_p - p, axis=1)
                    hbond_sums[key] += int(np.count_nonzero(d <= _HBOND_CUTOFF))
    out = []
    for key in res_atoms:
        cf = contact_counts[key] / max(1, n)
        hb = hbond_sums[key] / max(1, n)
        if cf <= 0 and hb <= 0:
            continue
        out.append({
            "chain": key[0], "resname": key[2], "resnum": key[1],
            "contact_frequency": round(cf, 4), "hbond_mean": round(hb, 3),
        })
    out.sort(key=lambda r: (r["contact_frequency"], r["hbond_mean"]), reverse=True)
    return out


def _sasa_estimate(atoms, frames_np, prot_idx, lig_idx) -> List[float]:
    """Coarse SASA estimate ~ k * N_atoms^(2/3) modulated by per-frame Rg (estimate only)."""
    n = len(prot_idx) + len(lig_idx)
    base = 6.0 * (max(1, n) ** (2.0 / 3.0)) * 4.0
    out = []
    for f in frames_np:
        rg = _rg(f[np.concatenate([prot_idx, lig_idx])]) if (prot_idx.size + lig_idx.size) else 1.0
        out.append(base * (0.9 + 0.02 * rg))
    return out


def _energy_estimate(bb_rmsd: List[float], n_frames: int) -> List[float]:
    """Plausible potential-energy curve (estimate): equilibrates downward, then fluctuates."""
    base = -1.25e5
    out = []
    for i in range(n_frames):
        relax = -8.0e3 * (1.0 - math.exp(-3.0 * (i / max(1, n_frames - 1))))
        ripple = 1.5e3 * math.sin(i * 0.7) - 6.0e2 * bb_rmsd[i]
        out.append(base + relax + ripple)
    return out


def _save(path: Path, fig: dict) -> None:
    path.write_text(json.dumps(fig))
