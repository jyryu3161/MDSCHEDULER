"""Trajectory metric computations (numpy/scipy). Engine-agnostic.

All functions operate on:
  - atoms:  list of mdworker.pipeline.structures.Atom (frame 0 topology)
  - frames: list[np.ndarray] of shape (n_atoms, 3), Angstrom

They are robust to small systems (the canonical 7-residue peptide + 25-heavy-atom ligand).
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

import numpy as np


# Approximate van der Waals radii (Angstrom) for a simple SASA estimate.
_VDW = {"H": 1.20, "C": 1.70, "N": 1.55, "O": 1.52, "S": 1.80, "P": 1.80, "F": 1.47,
        "Cl": 1.75, "Br": 1.85, "I": 1.98, "Na": 2.27, "Mg": 1.73, "Zn": 1.39}


def _kabsch_rmsd(P: np.ndarray, Q: np.ndarray) -> float:
    """RMSD between two coordinate sets after optimal superposition (Kabsch)."""
    if P.shape[0] == 0:
        return 0.0
    Pc = P - P.mean(axis=0)
    Qc = Q - Q.mean(axis=0)
    H = Pc.T @ Qc
    try:
        V, S, Wt = np.linalg.svd(H)
    except np.linalg.LinAlgError:
        diff = Pc - Qc
        return float(np.sqrt((diff * diff).sum() / P.shape[0]))
    d = np.sign(np.linalg.det(V @ Wt))
    D = np.diag([1.0, 1.0, d])
    R = V @ D @ Wt
    Pr = Pc @ R
    diff = Pr - Qc
    return float(np.sqrt((diff * diff).sum() / P.shape[0]))


def _rmsd_no_align(P: np.ndarray, Q: np.ndarray) -> float:
    if P.shape[0] == 0:
        return 0.0
    diff = P - Q
    return float(np.sqrt((diff * diff).sum() / P.shape[0]))


def frame_times_ps(n_frames: int, total_ns: float) -> np.ndarray:
    if n_frames <= 1:
        return np.array([0.0])
    return np.linspace(0.0, total_ns * 1000.0, n_frames)


def backbone_mask(atoms: Sequence) -> np.ndarray:
    return np.array([getattr(a, "is_backbone", False) for a in atoms], dtype=bool)


def ligand_mask(atoms: Sequence) -> np.ndarray:
    return np.array([getattr(a, "is_ligand", False) for a in atoms], dtype=bool)


def compute_rmsd(atoms, frames, *, mask: np.ndarray) -> List[float]:
    """RMSD of the masked atoms vs frame 0, with optimal alignment per frame."""
    if not frames:
        return []
    ref = frames[0][mask]
    out = []
    for f in frames:
        out.append(_kabsch_rmsd(f[mask], ref))
    return out


def compute_ligand_rmsd(atoms, frames, *, lig_mask: np.ndarray, bb_mask: np.ndarray) -> List[float]:
    """Ligand RMSD after superposing each frame on the receptor backbone.

    This captures ligand displacement relative to the binding site, which is the quantity of
    interest for binding stability.
    """
    if not frames or not lig_mask.any():
        return []
    ref_bb = frames[0][bb_mask]
    ref_lig = frames[0][lig_mask]
    out = []
    for f in frames:
        if bb_mask.any():
            # Superpose on backbone, apply transform to ligand.
            R, t = _superpose_transform(f[bb_mask], ref_bb)
            lig = (f[lig_mask] @ R) + t
        else:
            lig = f[lig_mask]
        out.append(_rmsd_no_align(lig, ref_lig))
    return out


def _superpose_transform(P: np.ndarray, Q: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Return (R, t) mapping P onto Q (rotation then translation)."""
    Pc = P.mean(axis=0)
    Qc = Q.mean(axis=0)
    H = (P - Pc).T @ (Q - Qc)
    try:
        V, S, Wt = np.linalg.svd(H)
        d = np.sign(np.linalg.det(V @ Wt))
        R = V @ np.diag([1.0, 1.0, d]) @ Wt
    except np.linalg.LinAlgError:
        R = np.eye(3)
    t = Qc - Pc @ R
    return R, t


def compute_rmsf(atoms, frames, *, mask: np.ndarray) -> Tuple[List[int], List[float], List[str]]:
    """Per-residue RMSF of masked atoms about the mean structure.

    Returns (residue_indices, rmsf_values, residue_labels).
    """
    if not frames:
        return [], [], []
    sel_idx = np.where(mask)[0]
    if sel_idx.size == 0:
        return [], [], []
    coords = np.stack([f[sel_idx] for f in frames], axis=0)  # (T, N, 3)
    mean = coords.mean(axis=0)
    fluct = np.sqrt(((coords - mean) ** 2).sum(axis=2).mean(axis=0))  # per-atom (N,)

    # Aggregate per residue.
    res_keys: Dict[Tuple, List[float]] = {}
    order: List[Tuple] = []
    for local_i, atom_i in enumerate(sel_idx):
        a = atoms[atom_i]
        key = (a.chain, a.resseq, a.resname)
        if key not in res_keys:
            res_keys[key] = []
            order.append(key)
        res_keys[key].append(fluct[local_i])
    indices = list(range(1, len(order) + 1))
    values = [float(np.mean(res_keys[k])) for k in order]
    labels = [f"{k[2]}{k[1]}" for k in order]
    return indices, values, labels


def compute_rg(atoms, frames, *, mask: np.ndarray) -> List[float]:
    """Radius of gyration of masked atoms per frame (mass-weighted by element)."""
    if not frames:
        return []
    sel = np.where(mask)[0]
    masses = np.array([_element_mass(atoms[i].element) for i in sel])
    total_m = masses.sum() if masses.sum() > 0 else 1.0
    out = []
    for f in frames:
        c = f[sel]
        com = (c * masses[:, None]).sum(axis=0) / total_m
        d2 = ((c - com) ** 2).sum(axis=1)
        rg = math.sqrt((masses * d2).sum() / total_m)
        out.append(float(rg))
    return out


def compute_sasa(atoms, frames, *, probe_radius: float = 1.4, n_sphere: int = 96) -> List[float]:
    """Shrake-Rupley solvent-accessible surface area estimate per frame (Angstrom^2).

    Uses a coarse fixed sphere of test points for speed; absolute values are approximate but
    the time-series trend is meaningful. Computed on the whole system.
    """
    if not frames:
        return []
    sphere = _fibonacci_sphere(n_sphere)
    radii = np.array([_VDW.get(a.element, 1.70) + probe_radius for a in atoms])
    out = []
    # Subsample frames for SASA if the trajectory is long (cost ~ n_atoms^2 * n_sphere).
    for f in frames:
        out.append(_sasa_frame(f, radii, sphere))
    return out


def _sasa_frame(coords: np.ndarray, radii: np.ndarray, sphere: np.ndarray) -> float:
    n = coords.shape[0]
    total = 0.0
    # Neighbor cutoff = max possible contact distance.
    max_r = float(radii.max())
    for i in range(n):
        ri = radii[i]
        center = coords[i]
        # Candidate neighbors within ri + max_r.
        d = np.linalg.norm(coords - center, axis=1)
        neigh = np.where((d > 1e-6) & (d < ri + max_r))[0]
        pts = center + sphere * ri
        if neigh.size == 0:
            accessible = sphere.shape[0]
        else:
            nb_coords = coords[neigh]
            nb_radii = radii[neigh]
            # A point is buried if within any neighbor's radius.
            buried = np.zeros(pts.shape[0], dtype=bool)
            for k in range(neigh.size):
                dd = np.linalg.norm(pts - nb_coords[k], axis=1)
                buried |= dd < nb_radii[k]
            accessible = int((~buried).sum())
        frac = accessible / sphere.shape[0]
        total += frac * 4.0 * math.pi * ri * ri
    return float(total)


def compute_hbonds(atoms, frames, *, lig_mask: np.ndarray, distance: float = 3.5) -> List[int]:
    """Count putative receptor-ligand hydrogen bonds per frame.

    Heuristic geometric criterion: a donor/acceptor heavy-atom pair (N/O) with one partner on
    the ligand and one on the receptor within `distance` Angstrom. Counts unique pairs.
    """
    if not frames:
        return []
    don_acc = np.array([a.element in ("N", "O") for a in atoms])
    lig_da = np.where(don_acc & lig_mask)[0]
    rec_da = np.where(don_acc & (~lig_mask))[0]
    if lig_da.size == 0 or rec_da.size == 0:
        return [0 for _ in frames]
    out = []
    for f in frames:
        lig_c = f[lig_da]
        rec_c = f[rec_da]
        # Pairwise distances.
        diff = lig_c[:, None, :] - rec_c[None, :, :]
        dmat = np.sqrt((diff * diff).sum(axis=2))
        out.append(int((dmat < distance).sum()))
    return out


def compute_min_distance(atoms, frames, *, lig_mask: np.ndarray) -> List[float]:
    """Minimum receptor-ligand heavy-atom distance per frame (binding-contact proxy)."""
    if not frames:
        return []
    heavy = np.array([a.element != "H" for a in atoms])
    lig = np.where(lig_mask & heavy)[0]
    rec = np.where((~lig_mask) & heavy)[0]
    if lig.size == 0 or rec.size == 0:
        return [0.0 for _ in frames]
    out = []
    for f in frames:
        diff = f[lig][:, None, :] - f[rec][None, :, :]
        dmat = np.sqrt((diff * diff).sum(axis=2))
        out.append(float(dmat.min()))
    return out


def compute_energy(frames, *, base: float = -1.0e5, seed: int = 0) -> Dict[str, List[float]]:
    """Plausible potential/kinetic/total energy time-series (kJ/mol).

    Derived deterministically from the trajectory's RMSD-like motion magnitude so the series
    is consistent with the actual frames (more motion -> slightly higher potential), plus a
    small thermal fluctuation. For the real engine this is replaced by gmx energy output.
    """
    n = len(frames)
    if n == 0:
        return {"potential": [], "kinetic": [], "total": []}
    rng = np.random.default_rng(seed)
    # Motion magnitude relative to frame 0.
    ref = frames[0]
    motion = np.array([float(np.sqrt(((f - ref) ** 2).sum() / max(1, f.shape[0]))) for f in frames])
    potential = base + motion * 800.0 + rng.normal(0, 250.0, size=n)
    # Equilibration: kinetic settles around ~0.3*|base| with thermal noise.
    kinetic = 0.30 * abs(base) + rng.normal(0, 200.0, size=n)
    total = potential + kinetic
    return {
        "potential": [float(x) for x in potential],
        "kinetic": [float(x) for x in kinetic],
        "total": [float(x) for x in total],
    }


def compute_contact_map(atoms, frames, *, lig_mask: np.ndarray, cutoff: float = 4.5) -> Dict:
    """Fraction-of-frames residue-ligand contact map.

    Returns {residue_labels:[...], contact_fraction:[...]} where contact_fraction[i] is the
    fraction of frames in which residue i has any heavy atom within `cutoff` of any ligand
    heavy atom.
    """
    if not frames:
        return {"residue_labels": [], "contact_fraction": []}
    heavy = np.array([a.element != "H" for a in atoms])
    lig = np.where(lig_mask & heavy)[0]
    if lig.size == 0:
        return {"residue_labels": [], "contact_fraction": []}

    # Group receptor heavy atoms by residue.
    res_atoms: Dict[Tuple, List[int]] = {}
    order: List[Tuple] = []
    for i, a in enumerate(atoms):
        if lig_mask[i] or not heavy[i]:
            continue
        key = (a.chain, a.resseq, a.resname)
        if key not in res_atoms:
            res_atoms[key] = []
            order.append(key)
        res_atoms[key].append(i)

    n_frames = len(frames)
    contact_counts = {k: 0 for k in order}
    for f in frames:
        lig_c = f[lig]
        for key in order:
            idx = res_atoms[key]
            rc = f[idx]
            diff = rc[:, None, :] - lig_c[None, :, :]
            dmin = np.sqrt((diff * diff).sum(axis=2)).min()
            if dmin < cutoff:
                contact_counts[key] += 1
    labels = [f"{k[2]}{k[1]}" for k in order]
    fractions = [contact_counts[k] / n_frames for k in order]
    return {"residue_labels": labels, "contact_fraction": fractions}


def ligand_stability(ligand_rmsd: Sequence[float]) -> Dict[str, float]:
    """Summarize ligand stability from its RMSD series."""
    if not ligand_rmsd:
        return {"mean_rmsd": 0.0, "max_rmsd": 0.0, "final_rmsd": 0.0, "drift": 0.0, "stable": True}
    arr = np.array(ligand_rmsd)
    drift = float(arr[-1] - arr[0])
    mean_rmsd = float(arr.mean())
    max_rmsd = float(arr.max())
    final_rmsd = float(arr[-1])
    return {
        "mean_rmsd": mean_rmsd,
        "max_rmsd": max_rmsd,
        "final_rmsd": final_rmsd,
        "drift": drift,
        # Canonical stability heuristic (single source of truth, used by analyze_md): the
        # ligand stays near its docked pose on average AND ends close to it, with no large
        # excursion. final < 3.0 Å AND mean < 3.0 Å AND max < 5.0 Å.
        "stable": bool(final_rmsd < 3.0 and mean_rmsd < 3.0 and max_rmsd < 5.0),
    }


def _fibonacci_sphere(n: int) -> np.ndarray:
    pts = np.zeros((n, 3))
    phi = math.pi * (3.0 - math.sqrt(5.0))
    for i in range(n):
        y = 1 - (i / float(n - 1)) * 2 if n > 1 else 0.0
        radius = math.sqrt(max(0.0, 1 - y * y))
        theta = phi * i
        pts[i] = [math.cos(theta) * radius, y, math.sin(theta) * radius]
    return pts


def _element_mass(element: str) -> float:
    return {"H": 1.008, "C": 12.011, "N": 14.007, "O": 15.999, "S": 32.06,
            "P": 30.974, "F": 18.998, "Cl": 35.45, "Na": 22.99, "Mg": 24.305,
            "Zn": 65.38, "Br": 79.904, "I": 126.90}.get(element, 12.0)
