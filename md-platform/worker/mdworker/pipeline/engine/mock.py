"""Mock MD engine (CONTRACT §9 "Mock engine").

Deliberately-designed synthetic engine that lets the FULL pipeline reach 'completed' with
realistic artifacts on a machine WITHOUT GROMACS/acpype (RDKit IS available). It:
  - synthesizes a receptor processed.gro + topology and ligand .itp stubs,
  - assembles the receptor+ligand complex,
  - generates a multi-frame trajectory PDB by random-walk perturbing the complex
    coordinates with numpy (seed derived from pose_index, so deterministic per pose),
  - emits log lines mimicking `gmx mdrun` progress and a realistic ns/day,
  - advances completed_ns toward md_length_ns and updates progress.

Trajectory realism: the protein backbone is restrained (small fluctuations), side chains
move more, and the ligand undergoes correlated drift + breathing so RMSD/RMSF/contact maps
computed downstream have plausible shapes. MD_MOCK_SPEEDUP controls how fast simulated time
advances per real second; the engine sleeps in small slices so progress updates stream.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from .base import LigandParameters, MDEngine, MDResult, PreparedStructure
from .. import structures as struct


def _seed_for_pose(pose_index: int) -> int:
    # Stable, pose-specific seed so the same pose always yields the same trajectory.
    return 1_000 + int(pose_index) * 7919


class MockEngine(MDEngine):
    name = "mock"

    @property
    def is_real(self) -> bool:
        return False

    # ------------------------------------------------------------------ prepare
    def prepare_structure(self, ctx, *, receptor_file: str, hetatm_decisions: Dict[str, str]) -> PreparedStructure:
        step = "prepare_structure"
        ctx.set_status("preparing", current_step=step, progress=12.0)
        ctx.info(step, f"[mock] Synthesizing receptor topology from {Path(receptor_file).name} "
                        f"(ff={self.settings.protein_force_field}, water={self.settings.water_model}).")

        atoms = struct.parse_pdb_atoms(receptor_file)
        if not atoms:
            raise ValueError("Receptor file contained no ATOM/HETATM records.")
        coords = struct.coords_array(atoms)

        # processed.gro (GROMACS .gro: nm units; here we just persist a faithful record).
        gro_path = ctx.prep_dir / "processed.gro"
        self._write_gro(gro_path, atoms, coords / 10.0, title="receptor (mock pdb2gmx)")

        # processed PDB the rest of the pipeline reads back.
        pdb_path = ctx.prep_dir / "receptor_processed.pdb"
        struct.write_pdb(atoms, coords, pdb_path, title="receptor processed (mock)")

        topol = ctx.prep_dir / "topol.top"
        self._write_topology(topol, n_protein_atoms=len(atoms))

        posre = ctx.prep_dir / "posre.itp"
        posre.write_text("; position restraints (mock)\n[ position_restraints ]\n")

        ctx.info(step, f"[mock] Receptor prepared: {len(atoms)} atoms, "
                       f"{len({(a.chain, a.resseq) for a in atoms})} residues.")
        return PreparedStructure(
            topology_path=str(topol),
            structure_path=str(pdb_path),
            posre_path=str(posre),
            extra={"gro_path": str(gro_path), "n_atoms": len(atoms)},
        )

    # ------------------------------------------------------------------ parameterize
    def parameterize_ligand(self, ctx, *, lig_ref_sdf: str, ligand_pdb: str, ligand_type: str) -> LigandParameters:
        step = "parameterize_ligand"
        ctx.set_status("preparing", current_step=step, progress=18.0)
        ff = self.settings.ligand_force_field
        cm = self.settings.ligand_charge_method
        ctx.info(step, f"[mock] Parameterizing ligand (type={ligand_type}, ff={ff}, charges={cm}).")

        lig_atoms = struct.parse_pdb_atoms(ligand_pdb, is_ligand=True)
        n = len(lig_atoms)

        atomtypes = ctx.prep_dir / "LIG_atomtypes.itp"
        atomtypes.write_text(
            "; ligand atom types (mock acpype GAFF2)\n[ atomtypes ]\n"
            ";name  bond_type   mass    charge  ptype  sigma  epsilon\n"
        )
        itp = ctx.prep_dir / "LIG.itp"
        itp.write_text(self._ligand_itp_text(lig_atoms))
        posre = ctx.prep_dir / "posre_LIG.itp"
        posre.write_text("; ligand position restraints (mock)\n#ifdef POSRES_LIG\n[ position_restraints ]\n#endif\n")

        ctx.info(step, f"[mock] Ligand parameterized: {n} atoms, topology written.")
        return LigandParameters(
            itp_path=str(itp),
            atomtypes_itp_path=str(atomtypes),
            posre_path=str(posre),
            gro_path=None,
            charge_method=cm,
            force_field=ff,
            extra={"n_atoms": n},
        )

    # ------------------------------------------------------------------ run MD
    def run_md(
        self,
        ctx,
        *,
        prepared: PreparedStructure,
        ligand: LigandParameters,
        ligand_pdb: str,
        md_length_ns: float,
        assigned_gpu: Optional[int],
    ) -> MDResult:
        receptor_atoms = struct.parse_pdb_atoms(prepared.structure_path)
        ligand_atoms = struct.parse_pdb_atoms(ligand_pdb, is_ligand=True)
        combined, coords0 = struct.assemble_complex(receptor_atoms, ligand_atoms)
        ctx.info(
            "run_md",
            f"[mock] Assembled complex: receptor {len(receptor_atoms)} + ligand "
            f"{len(ligand_atoms)} = {len(combined)} atoms.",
        )

        gpu_label = f"GPU {assigned_gpu}" if assigned_gpu is not None else "CPU"

        # --- box / solvate / genion (synthetic, fast) ---
        self._equil_phase(ctx, "running_em", "Energy minimization", progress_lo=20, progress_hi=30,
                           steps=500, gpu_label=gpu_label, kind="em")
        self._equil_phase(ctx, "running_nvt", "NVT equilibration (100 ps)", progress_lo=30, progress_hi=40,
                           steps=50000, gpu_label=gpu_label, kind="nvt")
        self._equil_phase(ctx, "running_npt", "NPT equilibration (100 ps)", progress_lo=40, progress_hi=50,
                           steps=50000, gpu_label=gpu_label, kind="npt")

        # --- production MD ---
        return self._production(
            ctx,
            combined=combined,
            coords0=coords0,
            md_length_ns=md_length_ns,
            assigned_gpu=assigned_gpu,
            gpu_label=gpu_label,
        )

    # ------------------------------------------------------------------ helpers
    def _equil_phase(self, ctx, status, label, *, progress_lo, progress_hi, steps, gpu_label, kind) -> None:
        ctx.set_status(status, current_step=status, progress=float(progress_lo))
        ctx.info(status, f"[mock] {label} on {gpu_label} ({steps} steps).")
        if kind == "em":
            for i in range(1, 6):
                pot = -1.0e5 - i * 8.3e3 + np.random.default_rng(i).normal(0, 500)
                ctx.info(status, f"[mock] Step {i*100:>5}  Epot = {pot:.3e} kJ/mol  Fmax = {2000/i:.2f}")
        else:
            for frac in (0.25, 0.5, 0.75, 1.0):
                t_ps = steps * 0.002 * frac
                temp = 300.0 + np.random.default_rng(int(frac * 100)).normal(0, 2.0)
                ctx.info(status, f"[mock] t = {t_ps:7.2f} ps  T = {temp:6.2f} K  step {int(steps*frac):>7}")
        ctx.progress(float(progress_hi), current_step=status)

    def _production(self, ctx, *, combined, coords0, md_length_ns, assigned_gpu, gpu_label) -> MDResult:
        status = "running_md"
        ctx.set_status(status, current_step=status, progress=50.0, assigned_gpu=assigned_gpu)

        dt_ps = 0.002  # 2 fs
        total_ps = float(md_length_ns) * 1000.0
        frame_interval_ps = max(1.0, float(self.settings.trajectory_output_ps))
        # Cap trajectory frame count so the viewer file stays manageable for long runs.
        n_frames = int(total_ps / frame_interval_ps) + 1
        n_frames = max(11, min(n_frames, 201))
        effective_interval_ps = total_ps / max(1, (n_frames - 1))

        ctx.info(
            status,
            f"[mock] Production MD: {md_length_ns:g} ns ({total_ps:g} ps, dt={dt_ps} ps, "
            f"{n_frames} frames @ {effective_interval_ps:.1f} ps) on {gpu_label}.",
        )

        frames = self._generate_trajectory(combined, coords0, n_frames, ctx.pose_index)

        traj_path = ctx.viz_dir / "trajectory.pdb"
        struct.write_multimodel_pdb(
            combined, frames, traj_path, title=f"production trajectory pose {ctx.pose_index}"
        )
        final_pdb = ctx.md_dir / "md_final.pdb"
        struct.write_pdb(combined, frames[-1], final_pdb, title="final frame (mock)")

        # MD_MOCK_SPEEDUP governs only how fast WALL time advances (so the demo finishes
        # quickly); the REPORTED ns/day is a realistic figure for a single-GPU run of a small
        # solvated complex (~80-160 ns/day on an L40-class card), with mild per-pose variation
        # so the dashboard ETA is meaningful rather than an absurd number.
        speedup = max(1, int(self.settings.md_mock_speedup))
        ns_per_day = round(95.0 + (int(ctx.pose_index) % 6) * 11.0, 2)
        # Total wall time we will spend streaming progress (bounded for responsiveness).
        wall_total_s = max(0.5, min(float(md_length_ns) / speedup, 20.0))

        update_points = 12
        start = time.monotonic()
        for k in range(1, update_points + 1):
            ctx.check_cancelled()  # abort promptly if cancelled mid-run
            frac = k / update_points
            target_elapsed = wall_total_s * frac
            now = time.monotonic()
            sleep_s = target_elapsed - (now - start)
            if sleep_s > 0:
                time.sleep(min(sleep_s, 2.5))
            completed_ns = round(float(md_length_ns) * frac, 4)
            progress = 50.0 + 35.0 * frac  # production spans 50..85%
            sim_step = int(total_ps * frac / dt_ps)
            ctx.info(
                status,
                f"[mock] step {sim_step:>10}  t = {completed_ns*1000.0:9.1f} ps  "
                f"({completed_ns:.3f}/{md_length_ns:g} ns)  {ns_per_day:.1f} ns/day",
            )
            ctx.progress(
                round(progress, 2),
                current_step=status,
                completed_ns=completed_ns,
                ns_per_day=round(ns_per_day, 2),
            )

        ctx.info(status, f"[mock] Production MD complete: {md_length_ns:g} ns, {ns_per_day:.1f} ns/day.")
        return MDResult(
            trajectory_pdb_path=str(traj_path),
            final_gro_path=str(final_pdb),
            xtc_path=None,
            tpr_path=None,
            completed_ns=float(md_length_ns),
            ns_per_day=round(ns_per_day, 2),
            n_frames=len(frames),
            frame_interval_ps=round(effective_interval_ps, 3),
            extra={"engine": "mock"},
        )

    @staticmethod
    def _generate_trajectory(combined, coords0: np.ndarray, n_frames: int, pose_index: int) -> List[np.ndarray]:
        """Random-walk perturbation producing a physically-plausible trajectory.

        - Backbone atoms: tight harmonic fluctuation about the start (restrained).
        - Side-chain protein atoms: larger fluctuation.
        - Ligand atoms: correlated rigid-body drift + breathing (so ligand RMSD rises then
          plateaus, contacts evolve).
        """
        rng = np.random.default_rng(_seed_for_pose(pose_index))
        n_atoms = coords0.shape[0]

        is_backbone = np.array([a.is_backbone for a in combined], dtype=bool)
        is_ligand = np.array([a.is_ligand for a in combined], dtype=bool)
        is_sidechain = (~is_backbone) & (~is_ligand)

        # Per-atom fluctuation amplitudes (Angstrom).
        amp = np.full(n_atoms, 0.6)
        amp[is_backbone] = 0.25
        amp[is_sidechain] = 0.75
        amp[is_ligand] = 1.1

        # Ligand center for rigid drift.
        lig_idx = np.where(is_ligand)[0]
        lig_center0 = coords0[lig_idx].mean(axis=0) if lig_idx.size else np.zeros(3)

        frames: List[np.ndarray] = [coords0.copy()]
        cur = coords0.copy()
        # Smooth drift target for the ligand (equilibrates within ~half the run).
        drift_dir = rng.normal(0, 1, size=3)
        drift_dir /= (np.linalg.norm(drift_dir) + 1e-9)
        max_drift = 1.8  # Angstrom net displacement of ligand center over the run

        for fi in range(1, n_frames):
            t = fi / (n_frames - 1)
            # Ornstein-Uhlenbeck-like fluctuation about the reference (mean-reverting).
            noise = rng.normal(0, 1, size=(n_atoms, 3)) * amp[:, None] * 0.35
            target = coords0.copy()
            if lig_idx.size:
                # Ligand center follows a saturating drift.
                drift_mag = max_drift * (1.0 - math.exp(-3.0 * t))
                target[lig_idx] += drift_dir * drift_mag
                # Breathing: expand/contract ligand about its (drifted) center slightly.
                breathe = 1.0 + 0.05 * math.sin(2 * math.pi * t * 2.0)
                lig_center_t = target[lig_idx].mean(axis=0)
                target[lig_idx] = lig_center_t + (target[lig_idx] - lig_center_t) * breathe
            # Mean-reverting update toward target + thermal noise.
            cur = cur + 0.5 * (target - cur) + noise
            frames.append(cur.copy())
        return frames

    @staticmethod
    def _write_gro(path: Path, atoms, coords_nm: np.ndarray, *, title: str) -> None:
        lines = [title, str(len(atoms))]
        for i, a in enumerate(atoms):
            x, y, z = coords_nm[i]
            lines.append(
                "%5d%-5s%5s%5d%8.3f%8.3f%8.3f"
                % (a.resseq % 100000, (a.resname or "UNK")[:5], a.name[:5], (i + 1) % 100000, x, y, z)
            )
        # box vector line (nm) — generous cube to contain the synthetic system.
        if len(coords_nm):
            span = coords_nm.max(axis=0) - coords_nm.min(axis=0) + 2.0
        else:
            span = np.array([5.0, 5.0, 5.0])
        lines.append("%10.5f%10.5f%10.5f" % (span[0], span[1], span[2]))
        path.write_text("\n".join(lines) + "\n")

    @staticmethod
    def _write_topology(path: Path, *, n_protein_atoms: int) -> None:
        path.write_text(
            "; mock topology (GROMACS amber14sb/tip3p analog)\n"
            '#include "amber14sb.ff/forcefield.itp"\n'
            "; Include ligand atomtypes\n"
            '#include "LIG_atomtypes.itp"\n'
            '#include "amber14sb.ff/tip3p.itp"\n'
            "; Include ligand topology\n"
            '#include "LIG.itp"\n\n'
            "[ system ]\nMD Platform complex (mock)\n\n"
            "[ molecules ]\n"
            f"Protein             1\n"
            "LIG                 1\n"
        )

    @staticmethod
    def _ligand_itp_text(lig_atoms) -> str:
        lines = [
            "; ligand topology (mock acpype)",
            "[ moleculetype ]",
            "; name  nrexcl",
            "LIG     3",
            "",
            "[ atoms ]",
            ";  nr  type  resnr  residue  atom  cgnr   charge    mass",
        ]
        for i, a in enumerate(lig_atoms, start=1):
            mass = {"C": 12.011, "O": 15.999, "N": 14.007, "H": 1.008, "S": 32.06}.get(a.element, 12.0)
            lines.append(
                f"{i:>5}   {('c' + a.element.lower()):<4} 1   MOL   {a.name:<4} {i:>5}  0.0000  {mass:8.4f}"
            )
        lines += [
            "",
            "; ligand position restraints (equilibration, -DPOSRES_LIG)",
            "#ifdef POSRES_LIG",
            '#include "posre_LIG.itp"',
            "#endif",
            "",
        ]
        return "\n".join(lines) + "\n"
