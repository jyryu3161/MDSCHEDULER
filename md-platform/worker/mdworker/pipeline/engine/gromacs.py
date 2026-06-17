"""Real GROMACS MD engine (CONTRACT §9).

Wraps the exact gmx/pdb2gmx/acpype/grompp/mdrun commands proven in
preprocess_pipeline.sh, generalized: ligand topology comes from acpype on the
assign_bond_orders output (lig_ref.sdf), receptor topology from pdb2gmx, and production
runs from an md.mdp template whose nsteps is rendered from md_length_ns at dt=0.002 ps
(nsteps = ns * 500000).

This engine requires `gmx` (and, for small-molecule ligands, `acpype`) on PATH. When they
are absent the runner selects MockEngine instead (see engine/__init__.get_engine). All
subprocess output is streamed to the JobContext log so the live log viewer shows real gmx
progress.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from .base import LigandParameters, MDEngine, MDResult, PreparedStructure
from .. import structures as struct


# Default MDP templates used when MDP_TEMPLATE_DIR does not supply one. {NSTEPS},
# {DT}, {TEMPERATURE}, {PRESSURE}, {XTC_INTERVAL} are substituted at render time.
_DEFAULT_MDP: Dict[str, str] = {
    "ions.mdp": (
        "integrator = steep\nemtol = 1000.0\nemstep = 0.01\nnsteps = 50000\n"
        "nstlist = 10\ncutoff-scheme = Verlet\ncoulombtype = cutoff\n"
        "rcoulomb = 1.0\nrvdw = 1.0\npbc = xyz\n"
    ),
    "em.mdp": (
        "integrator = steep\nemtol = 1000.0\nemstep = 0.01\nnsteps = 50000\n"
        "nstlist = 10\ncutoff-scheme = Verlet\nns_type = grid\ncoulombtype = PME\n"
        "rcoulomb = 1.0\nrvdw = 1.0\npbc = xyz\n"
    ),
    "nvt.mdp": (
        "integrator = md\nnsteps = {NSTEPS}\ndt = 0.002\n"
        "nstxout-compressed = 5000\ncontinuation = no\nconstraint_algorithm = lincs\n"
        "constraints = h-bonds\ncutoff-scheme = Verlet\ncoulombtype = PME\n"
        "rcoulomb = 1.0\nrvdw = 1.0\ntcoupl = V-rescale\ntc-grps = Protein_MOL Water_and_ions\n"
        "tau_t = 0.1 0.1\nref_t = {TEMPERATURE} {TEMPERATURE}\npcoupl = no\npbc = xyz\n"
        "gen_vel = yes\ngen_temp = {TEMPERATURE}\ngen_seed = -1\n"
    ),
    "npt.mdp": (
        "integrator = md\nnsteps = {NSTEPS}\ndt = 0.002\n"
        "nstxout-compressed = 5000\ncontinuation = yes\nconstraint_algorithm = lincs\n"
        "constraints = h-bonds\ncutoff-scheme = Verlet\ncoulombtype = PME\n"
        "rcoulomb = 1.0\nrvdw = 1.0\ntcoupl = V-rescale\ntc-grps = Protein_MOL Water_and_ions\n"
        "tau_t = 0.1 0.1\nref_t = {TEMPERATURE} {TEMPERATURE}\n"
        "pcoupl = C-rescale\npcoupltype = isotropic\ntau_p = 2.0\nref_p = {PRESSURE}\n"
        "compressibility = 4.5e-5\npbc = xyz\ngen_vel = no\n"
    ),
    "md.mdp": (
        "integrator = md\nnsteps = {NSTEPS}\ndt = {DT}\n"
        "nstxout-compressed = {XTC_INTERVAL}\nnstenergy = {XTC_INTERVAL}\n"
        "nstlog = {XTC_INTERVAL}\ncontinuation = yes\nconstraint_algorithm = lincs\n"
        "constraints = h-bonds\ncutoff-scheme = Verlet\ncoulombtype = PME\n"
        "rcoulomb = 1.0\nrvdw = 1.0\ntcoupl = V-rescale\ntc-grps = Protein_MOL Water_and_ions\n"
        "tau_t = 0.1 0.1\nref_t = {TEMPERATURE} {TEMPERATURE}\n"
        "pcoupl = C-rescale\npcoupltype = isotropic\ntau_p = 2.0\nref_p = {PRESSURE}\n"
        "compressibility = 4.5e-5\npbc = xyz\ngen_vel = no\n"
    ),
}

# dt=0.002 ps -> 500000 steps per ns (CONTRACT §9 engine note).
_STEPS_PER_NS = 500000

# Water keywords GROMACS recognizes without a per-ff watermodels.dat entry (3-/built-in models).
# OPC, TIP4P-D, etc. live ONLY in a force field's watermodels.dat, so they must be confirmed there.
_BUILTIN_WATER = {"tip3p", "spc", "spce", "tip4p", "tip4pew", "tip5p"}


def _ff_top_dirs(gmx_path: Optional[str], extra: Optional[Path] = None) -> List[Path]:
    """Directories GROMACS searches for ``<ff>.ff`` force-field folders, best-effort.

    Mirrors gmx's own lookup order well enough for a preflight: the run cwd (a project-local
    ``*.ff`` override), every entry of $GMXLIB, $GMXDATA/top, and ``<prefix>/share/gromacs/top``
    derived from the gmx binary location. Only existing directories are returned. An empty
    result means we could not locate the data dir and the caller should NOT downgrade the FF.
    """
    dirs: List[Path] = []
    if extra is not None:
        dirs.append(Path(extra))
    for d in os.environ.get("GMXLIB", "").split(os.pathsep):
        if d:
            dirs.append(Path(d))
    gmxdata = os.environ.get("GMXDATA")
    if gmxdata:
        dirs.append(Path(gmxdata) / "top")
    if gmx_path:
        try:
            prefix = Path(gmx_path).resolve().parent.parent
            dirs.append(prefix / "share" / "gromacs" / "top")
        except (OSError, RuntimeError):
            pass
    seen: set = set()
    out: List[Path] = []
    for d in dirs:
        key = str(d)
        if key in seen:
            continue
        seen.add(key)
        if d.is_dir():
            out.append(d)
    return out


def _ff_water_available(top_dirs: List[Path], ff: str, water: str) -> bool:
    """True if ``<ff>.ff`` exists in any top dir AND supports the ``water`` model.

    A water model is supported when it is a GROMACS built-in (3-point/tip4p family) or it is
    listed as a keyword in that force field's ``watermodels.dat``. ``none``/``select`` are always
    accepted (caller-managed). Returns False if the ff directory is not found anywhere.
    """
    ff_dir_name = ff if ff.endswith(".ff") else ff + ".ff"
    w = (water or "").strip().lower()
    for d in top_dirs:
        ffd = d / ff_dir_name
        if not ffd.is_dir():
            continue
        # gmx pdb2gmx validates `-water` built-ins (tip3p/spc/tip4p/…) against a hardcoded enum,
        # not watermodels.dat — that file only feeds the interactive `select` list for CUSTOM
        # models (e.g. OPC). So a built-in is available whenever the ff dir exists; OPC and other
        # non-built-ins must be listed in the ff's watermodels.dat (checked below).
        if w in ("none", "select", "") or w in _BUILTIN_WATER:
            return True
        wm = ffd / "watermodels.dat"
        if wm.exists():
            tokens = {
                ln.split()[0].lower()
                for ln in wm.read_text(errors="replace").splitlines()
                if ln.strip() and not ln.strip().startswith(";")
            }
            if w in tokens:
                return True
    return False


class GromacsEngine(MDEngine):
    name = "gromacs"

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._gmx = shutil.which("gmx") or "gmx"
        self._acpype = shutil.which("acpype")
        # Resolved once per run by _ff_water() (preflight + fallback), then reused so the receptor
        # and peptide-ligand pdb2gmx calls — and the MM/GBSA AmberTools rebuild — all agree.
        self._resolved_ff: Optional[str] = None
        self._resolved_water: Optional[str] = None

    @property
    def is_real(self) -> bool:
        return True

    # ------------------------------------------------------------------ force-field preflight
    def _ff_water(self, ctx, *, cwd: Optional[Path] = None) -> Tuple[str, str]:
        """Resolve the (protein_force_field, water_model) to actually use for this run.

        Preflights the requested pair against the GROMACS top dirs. If the force field/water is
        present, use it. If it is missing and FORCEFIELD_AUTOFALLBACK is on, fall back to the
        stock amber14sb/tip3p pair with a logged warning (so a plain GROMACS install still runs).
        If the top dirs can't be located at all, trust the request as-is (don't false-downgrade)
        and let gmx pdb2gmx surface any real error. The choice is memoized and recorded in
        ``cwd/forcefield.json`` for the MM/GBSA step to read.
        """
        if self._resolved_ff is not None and self._resolved_water is not None:
            return self._resolved_ff, self._resolved_water
        req_ff = self.settings.protein_force_field
        req_w = self.settings.water_model
        top_dirs = _ff_top_dirs(self._gmx if self._gmx != "gmx" else shutil.which("gmx"), extra=cwd)
        if not top_dirs:
            ctx.info("prepare_structure", f"GROMACS top dir not located for FF preflight; using "
                     f"requested force field '{req_ff}' + water '{req_w}' as-is.")
            ff, water = req_ff, req_w
        elif _ff_water_available(top_dirs, req_ff, req_w):
            ctx.info("prepare_structure", f"Force field '{req_ff}' + water '{req_w}' available.")
            ff, water = req_ff, req_w
        elif self.settings.forcefield_autofallback:
            ff = self.settings.protein_force_field_fallback
            water = self.settings.water_model_fallback
            ctx.warning("prepare_structure",
                        f"Force field '{req_ff}' + water '{req_w}' not found in GROMACS top dirs "
                        f"{[str(d) for d in top_dirs]}; falling back to '{ff}' + '{water}'. Install "
                        f"the {req_ff} GROMACS port (with OPC in its watermodels.dat) to enable it.")
        else:
            ctx.warning("prepare_structure",
                        f"Force field '{req_ff}' + water '{req_w}' not found and "
                        "FORCEFIELD_AUTOFALLBACK is off; attempting it anyway (gmx may fail).")
            ff, water = req_ff, req_w
        self._resolved_ff, self._resolved_water = ff, water
        if cwd is not None:
            try:
                (Path(cwd) / "forcefield.json").write_text(json.dumps(
                    {"protein_force_field": ff, "water_model": water,
                     "requested_force_field": req_ff, "requested_water_model": req_w}))
            except OSError:
                pass
        return ff, water

    # ------------------------------------------------------------------ subprocess
    def _run(self, ctx, step: str, args: List[str], *, cwd: Path, stdin_text: Optional[str] = None,
             env: Optional[dict] = None, check: bool = True) -> int:
        """Run a subprocess, streaming combined stdout+stderr to the log incrementally.

        gmx mdrun logs progress to stderr over long runs; we use Popen with merged streams
        and forward each line as it arrives so the live log viewer updates and we never hold
        the full output in memory. Only a bounded tail is retained for error reporting.
        """
        import signal
        import threading
        from collections import deque

        from mdworker.pipeline.context import JobCancelled

        ctx.info(step, f"$ {' '.join(args)}")
        # start_new_session=True puts gmx (and any children it spawns) in its own process
        # group, so a cancel can terminate the WHOLE group, not just the parent — this is what
        # actually force-stops a running MD instead of orphaning gmx mdrun.
        proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env or os.environ.copy(),
            start_new_session=True,
        )
        if stdin_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except BrokenPipeError:
                pass

        # Drain output on a background thread so the main thread can poll for cancellation
        # even while gmx is quiet for long stretches (mdrun emits sparse progress lines).
        tail: "deque[str]" = deque(maxlen=20)

        def _drain() -> None:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    tail.append(line)
                    ctx.info(step, line)

        drainer = threading.Thread(target=_drain, name=f"gmx-drain-{step}", daemon=True)
        drainer.start()

        while True:
            try:
                rc = proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if ctx.is_cancelled():
                    self._terminate_group(proc, signal)
                    drainer.join(timeout=2.0)
                    raise JobCancelled(
                        f"{step}: cancelled — terminated gmx process group for '{args[0]} "
                        f"{args[1] if len(args) > 1 else ''}'."
                    )
        drainer.join(timeout=5.0)
        if rc != 0 and check:
            raise RuntimeError(
                f"Command failed ({rc}): {' '.join(args)}\n" + "\n".join(tail)
            )
        return rc

    @staticmethod
    def _terminate_group(proc, signal) -> None:
        """SIGTERM then SIGKILL the subprocess's whole process group (best-effort)."""
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            return
        for sig, wait_s in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, OSError):
                return
            try:
                proc.wait(timeout=wait_s)
                return
            except subprocess.TimeoutExpired:
                continue

    def _mdp(self, name: str) -> str:
        tmpl_dir = Path(self.settings.mdp_template_dir)
        candidate = tmpl_dir / name
        if candidate.exists():
            return candidate.read_text()
        return _DEFAULT_MDP[name]

    def _render_mdp(self, name: str, dest: Path, *, nsteps: Optional[int] = None) -> Path:
        text = self._mdp(name)
        nsteps_s = str(nsteps if nsteps is not None else 50000)
        xtc_s = str(max(1, int(self.settings.trajectory_output_ps) * 500))  # ps / dt(0.002)
        temp_s = str(self.settings.extra.get("temperature", 300))
        pres_s = str(self.settings.extra.get("pressure", 1.0))
        # Support BOTH placeholder styles: the md-env file templates use __NAME__ (CONTRACT
        # §11) while the built-in _DEFAULT_MDP fallbacks use {NAME}. Substituting both keeps
        # either source working — a missing key is simply a no-op replace.
        repl = {
            "{NSTEPS}": nsteps_s, "__NSTEPS__": nsteps_s,
            "{XTC_INTERVAL}": xtc_s, "__NSTXOUT_COMPRESSED__": xtc_s, "__XTC_INTERVAL__": xtc_s,
            "{DT}": "0.002", "__DT__": "0.002",
            "{TEMPERATURE}": temp_s, "__TEMPERATURE__": temp_s,
            "{PRESSURE}": pres_s, "__PRESSURE__": pres_s,
        }
        for k, v in repl.items():
            text = text.replace(k, v)
        dest.write_text(text)
        return dest

    # ------------------------------------------------------------------ prepare
    def prepare_structure(self, ctx, *, receptor_file: str, hetatm_decisions: Dict[str, str]) -> PreparedStructure:
        step = "prepare_structure"
        ctx.set_status("preparing", current_step=step, progress=12.0)
        prep = ctx.prep_dir

        # Strip to ATOM records / handle CRLF as in preprocess_pipeline.sh prep_peptide.
        clean_pdb = prep / "receptor_clean.pdb"
        src_lines = Path(receptor_file).read_text(errors="replace").replace("\r", "").splitlines()
        kept = [ln for ln in src_lines if ln.startswith(("ATOM", "TER", "END"))]
        clean_pdb.write_text("\n".join(kept) + "\n")
        # Resolve the protein force field + water model (ff19SB/OPC by default, with preflight
        # fallback to amber14sb/tip3p). Done here, before the first pdb2gmx, so the whole run
        # (receptor + peptide ligand + MM/GBSA) uses one consistent pair.
        ff, water = self._ff_water(ctx, cwd=prep)
        ctx.info(step, f"Prepared receptor PDB ({len(kept)} records). Running gmx pdb2gmx "
                       f"(-ff {ff} -water {water}).")

        self._run(
            ctx, step,
            [self._gmx, "pdb2gmx", "-f", str(clean_pdb), "-o", "processed.gro",
             "-p", "topol.top", "-i", "posre.itp",
             "-ff", ff, "-water", water, "-ignh"],
            cwd=prep,
        )
        return PreparedStructure(
            topology_path=str(prep / "topol.top"),
            structure_path=str(prep / "processed.gro"),
            posre_path=str(prep / "posre.itp"),
            extra={},
        )

    # ------------------------------------------------------------------ parameterize
    def parameterize_ligand(self, ctx, *, lig_ref_sdf: str, ligand_pdb: str, ligand_type: str) -> LigandParameters:
        step = "parameterize_ligand"
        ctx.set_status("preparing", current_step=step, progress=18.0)
        prep = ctx.prep_dir

        if ligand_type in ("peptide", "protein_partner"):
            # Peptide/protein ligand -> pdb2gmx path, using the SAME resolved protein FF/water as
            # the receptor (preflight fallback applied) so the complex topology is self-consistent.
            ff, water = self._ff_water(ctx, cwd=prep)
            ctx.info(step, f"Peptide/protein ligand: parameterizing via pdb2gmx (-ff {ff}).")
            self._run(
                ctx, step,
                [self._gmx, "pdb2gmx", "-f", ligand_pdb, "-o", "ligand_processed.gro",
                 "-p", "ligand.top", "-i", "posre_LIG.itp",
                 "-ff", ff, "-water", water, "-ignh"],
                cwd=prep,
            )
            # Convert the standalone ligand.top into an includable .itp and capture the real
            # moleculetype name so the complex topology references it correctly.
            lig_itp, mol_name = self._top_to_itp(prep / "ligand.top", prep / "ligand.itp")
            return LigandParameters(
                itp_path=str(lig_itp),
                atomtypes_itp_path=None,
                posre_path=str(prep / "posre_LIG.itp") if (prep / "posre_LIG.itp").exists() else None,
                gro_path=str(prep / "ligand_processed.gro"),
                charge_method="n/a",
                force_field=self.settings.protein_force_field,
                extra={"path": "pdb2gmx", "mol_name": mol_name, "itp_name": "ligand.itp"},
            )

        # small_molecule / cofactor -> acpype GAFF2 + AM1-BCC.
        if not self._acpype:
            raise RuntimeError(
                "acpype not found on PATH; required to parameterize a small-molecule ligand "
                "with the GROMACS engine. Use MD_ENGINE=mock or install AmberTools/acpype."
            )
        charge_flag = "bcc" if self.settings.ligand_charge_method.lower() in ("am1bcc", "bcc") else "gas"
        ff = "gaff2" if self.settings.ligand_force_field.lower() in ("gaff2", "gaff") else "gaff2"
        acp = prep / "LIG.acpype"
        src_itp = acp / "LIG_GMX.itp"
        src_gro = acp / "LIG_GMX.gro"
        fp_marker = acp / ".acpype_input.sha256"
        # Fingerprint the ligand input + parameterization options so a warm prep/ is reused
        # ONLY when it was produced from the identical ligand and settings. AM1-BCC
        # (antechamber/sqm) is the slowest step (minutes); safe reuse speeds retries without
        # ever grafting a stale/different ligand's topology onto these coordinates.
        import hashlib
        cur_fp = "missing"
        if Path(lig_ref_sdf).exists():
            h = hashlib.sha256(Path(lig_ref_sdf).read_bytes())
            h.update(f"|{charge_flag}|{ff}".encode())
            cur_fp = h.hexdigest()
        cached_fp = fp_marker.read_text().strip() if fp_marker.exists() else None
        reuse = (
            src_itp.exists() and src_gro.exists()
            and cached_fp is not None and cached_fp == cur_fp
        )
        if reuse:
            ctx.info(step, "Reusing cached acpype output (matching ligand fingerprint).")
        else:
            ctx.info(step, f"Small-molecule ligand: acpype -c {charge_flag} -a {ff}.")
            # Stale/partial cache for a different input: clear it before recomputing.
            if acp.exists():
                shutil.rmtree(acp, ignore_errors=True)
            # assign_bond_orders already writes lig_ref.sdf into prep/; copy only when the
            # source is elsewhere (avoid a SameFileError when it is already the acpype input).
            lig_ref_dest = prep / "lig_ref.sdf"
            src = Path(lig_ref_sdf)
            if src.resolve() != lig_ref_dest.resolve():
                shutil.copy(src, lig_ref_dest)
            self._run(
                ctx, step,
                [self._acpype, "-i", "lig_ref.sdf", "-c", charge_flag, "-a", ff, "-n", "0", "-b", "LIG"],
                cwd=prep,
            )
            if not src_itp.exists():
                raise RuntimeError(f"acpype did not produce {src_itp}.")
            fp_marker.write_text(cur_fp)
        # Split atomtypes / moleculetype as in preprocess_pipeline.sh prep_ligand_top.
        atomtypes_itp, lig_itp = self._split_acpype_itp(src_itp, prep)
        posre = acp / "posre_LIG.itp"
        posre_dest = prep / "posre_LIG.itp"
        if posre.exists():
            shutil.copy(posre, posre_dest)
        return LigandParameters(
            itp_path=str(lig_itp),
            atomtypes_itp_path=str(atomtypes_itp),
            posre_path=str(posre_dest) if posre.exists() else None,
            gro_path=str(acp / "LIG_GMX.gro") if (acp / "LIG_GMX.gro").exists() else None,
            charge_method=self.settings.ligand_charge_method,
            force_field=ff,
            extra={"acpype_dir": str(acp)},
        )

    @staticmethod
    def _split_acpype_itp(src_itp: Path, prep: Path):
        text = src_itp.read_text().splitlines()
        atomtypes: List[str] = []
        moleculetype: List[str] = []
        in_at = False
        in_mol = False
        for ln in text:
            stripped = ln.strip().lower()
            if re.match(r"^\[\s*atomtypes\s*\]", stripped):
                in_at = True
                in_mol = False
            elif re.match(r"^\[\s*moleculetype\s*\]", stripped):
                in_at = False
                in_mol = True
            if in_at:
                atomtypes.append(ln)
            if in_mol:
                moleculetype.append(ln)
        atomtypes_itp = prep / "LIG_atomtypes.itp"
        lig_itp = prep / "LIG.itp"
        atomtypes_itp.write_text("\n".join(atomtypes) + "\n")
        moleculetype.append("")
        moleculetype.append("; ligand position restraints (equilibration, -DPOSRES_LIG)")
        moleculetype.append("#ifdef POSRES_LIG")
        moleculetype.append('#include "posre_LIG.itp"')
        moleculetype.append("#endif")
        lig_itp.write_text("\n".join(moleculetype) + "\n")
        return atomtypes_itp, lig_itp

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
        run = ctx.md_dir
        env = os.environ.copy()
        if assigned_gpu is not None:
            env["CUDA_VISIBLE_DEVICES"] = str(assigned_gpu)
        gpu_args = ["-nb", "gpu", "-update", "gpu"] if assigned_gpu is not None else []

        # Assemble complex coords + topology (port of assemble_complex.py).
        self._assemble(ctx, prepared, ligand, ligand_pdb, run)

        # editconf box -> solvate -> genion (CONTRACT §9 run_md sequence).
        self._render_mdp("ions.mdp", run / "ions.mdp")
        box_type = self.settings.extra.get("box_type", "dodecahedron")
        box_pad = str(self.settings.box_padding_nm)
        self._run(ctx, "preparing",
                  [self._gmx, "editconf", "-f", "complex.gro", "-o", "boxed.gro",
                   "-bt", box_type, "-d", box_pad, "-c"], cwd=run)
        self._run(ctx, "preparing",
                  [self._gmx, "solvate", "-cp", "boxed.gro", "-cs", "spc216.gro",
                   "-o", "solv.gro", "-p", "topol.top"], cwd=run)
        self._run(ctx, "preparing",
                  [self._gmx, "grompp", "-f", "ions.mdp", "-c", "solv.gro",
                   "-p", "topol.top", "-o", "ions.tpr", "-maxwarn", "2"], cwd=run)
        conc = self.settings.extra.get("salt_concentration", 0.15)
        self._run(ctx, "preparing",
                  [self._gmx, "genion", "-s", "ions.tpr", "-o", "solv_ions.gro",
                   "-p", "topol.top", "-pname", "NA", "-nname", "CL", "-neutral",
                   "-conc", str(conc)], cwd=run, stdin_text="SOL\n")
        self._run(ctx, "preparing",
                  [self._gmx, "make_ndx", "-f", "solv_ions.gro", "-o", "index.ndx"],
                  cwd=run, stdin_text='"Protein" | "MOL"\nq\n')

        # EM
        ctx.set_status("running_em", current_step="running_em", progress=25.0)
        self._render_mdp("em.mdp", run / "em.mdp")
        self._run(ctx, "running_em",
                  [self._gmx, "grompp", "-f", "em.mdp", "-c", "solv_ions.gro",
                   "-p", "topol.top", "-o", "em.tpr", "-maxwarn", "2"], cwd=run)
        self._run(ctx, "running_em",
                  [self._gmx, "mdrun", "-v", "-deffnm", "em", "-ntmpi", "1"], cwd=run, env=env)

        # NVT
        ctx.set_status("running_nvt", current_step="running_nvt", progress=33.0)
        self._render_mdp("nvt.mdp", run / "nvt.mdp", nsteps=int(self.settings.nvt_steps))
        self._run(ctx, "running_nvt",
                  [self._gmx, "grompp", "-f", "nvt.mdp", "-c", "em.gro", "-r", "em.gro",
                   "-p", "topol.top", "-n", "index.ndx", "-o", "nvt.tpr", "-maxwarn", "2"], cwd=run)
        self._run(ctx, "running_nvt",
                  [self._gmx, "mdrun", "-v", "-deffnm", "nvt", "-ntmpi", "1"] + gpu_args, cwd=run, env=env)

        # NPT
        ctx.set_status("running_npt", current_step="running_npt", progress=42.0)
        self._render_mdp("npt.mdp", run / "npt.mdp", nsteps=int(self.settings.npt_steps))
        self._run(ctx, "running_npt",
                  [self._gmx, "grompp", "-f", "npt.mdp", "-c", "nvt.gro", "-r", "nvt.gro",
                   "-t", "nvt.cpt", "-p", "topol.top", "-n", "index.ndx",
                   "-o", "npt.tpr", "-maxwarn", "2"], cwd=run)
        self._run(ctx, "running_npt",
                  [self._gmx, "mdrun", "-v", "-deffnm", "npt", "-ntmpi", "1"] + gpu_args, cwd=run, env=env)

        # Production MD
        ctx.set_status("running_md", current_step="running_md", progress=50.0, assigned_gpu=assigned_gpu)
        nsteps = int(round(float(md_length_ns) * _STEPS_PER_NS))
        self._render_mdp("md.mdp", run / "md.mdp", nsteps=nsteps)
        self._run(ctx, "running_md",
                  [self._gmx, "grompp", "-f", "md.mdp", "-c", "npt.gro", "-t", "npt.cpt",
                   "-p", "topol.top", "-n", "index.ndx", "-o", "md.tpr", "-maxwarn", "2"], cwd=run)
        ctx.info("running_md", f"Production MD: {md_length_ns:g} ns ({nsteps} steps, dt=0.002 ps).")
        self._run(ctx, "running_md",
                  [self._gmx, "mdrun", "-v", "-deffnm", "md"] + gpu_args, cwd=run, env=env)

        # Build a viewer trajectory PDB (every Nth frame) via trjconv.
        traj_pdb = ctx.viz_dir / "trajectory.pdb"
        xtc = run / "md.xtc"
        gro = run / "md.gro"
        ns_per_day, completed = self._parse_perf(run / "md.log", md_length_ns)
        index = run / "index.ndx"
        if xtc.exists():
            # Cap the viewer trajectory at ~150 frames and DROP water/ions: outputting the full
            # solvated System for every frame produces a huge PDB and makes the geometric
            # analysis (which treats non-ligand atoms as protein) count water. We center on the
            # complex and output the solute-only group "Protein_MOL".
            xtc_interval_steps = max(1, int(self.settings.trajectory_output_ps) * 500)
            n_xtc = max(1, (nsteps or xtc_interval_steps) // xtc_interval_steps)
            skip = str(max(1, (n_xtc + 149) // 150))  # ceil(n_xtc/150) -> always <=150 frames
            # MUST pass -n index.ndx: "Protein_MOL" lives in index.ndx, not in md.tpr's default
            # groups (without -n, trjconv errors "No such group"). Fallbacks degrade gracefully
            # for systems lacking that group so the viewer always gets frames.
            attempts = [
                (["-pbc", "mol", "-center", "-skip", skip, "-n", str(index)], "Protein_MOL\nProtein_MOL\n"),
                (["-pbc", "mol", "-center", "-skip", skip, "-n", str(index)], "Protein\nProtein\n"),
                (["-pbc", "whole", "-skip", skip, "-n", str(index)], "System\n"),
                (["-pbc", "whole", "-skip", skip], "System\n"),
            ]
            for extra, stdin in attempts:
                if "-n" in extra and not index.exists():
                    continue
                self._run(ctx, "rendering",
                          [self._gmx, "trjconv", "-s", "md.tpr", "-f", "md.xtc",
                           "-o", str(traj_pdb)] + extra,
                          cwd=run, stdin_text=stdin, check=False)
                if traj_pdb.exists():
                    break
        if not traj_pdb.exists() and gro.exists():
            # Fallback: at least one model from the final structure.
            atoms = struct.parse_pdb_atoms(str(gro)) if gro.suffix == ".pdb" else []
            if atoms:
                struct.write_multimodel_pdb(atoms, [struct.coords_array(atoms)], traj_pdb)

        n_frames = 0
        if traj_pdb.exists():
            _, frames = struct.read_multimodel_pdb(traj_pdb)
            n_frames = len(frames)

        # The viewer trajectory was thinned by trjconv -skip, so its real per-frame spacing is
        # md_length / (n_frames-1), NOT trajectory_output_ps. Report the EFFECTIVE interval so
        # the analysis time axis spans the true 0..md_length_ns (not a compressed range).
        if n_frames > 1:
            frame_interval_ps = float(md_length_ns) * 1000.0 / (n_frames - 1)
        else:
            frame_interval_ps = float(self.settings.trajectory_output_ps)

        return MDResult(
            trajectory_pdb_path=str(traj_pdb),
            final_gro_path=str(gro) if gro.exists() else None,
            xtc_path=str(xtc) if xtc.exists() else None,
            tpr_path=str(run / "md.tpr"),
            completed_ns=float(md_length_ns),
            ns_per_day=ns_per_day,
            n_frames=n_frames,
            frame_interval_ps=round(frame_interval_ps, 4),
            extra={"engine": "gromacs"},
        )

    def _assemble(self, ctx, prepared, ligand, ligand_pdb, run: Path) -> None:
        """Port of assemble_complex.py: build complex.gro + topol.top with ligand injected."""
        receptor_atoms = struct.parse_pdb_atoms(prepared.structure_path) if prepared.structure_path.endswith(".pdb") else None
        # When the receptor structure is a .gro, copy through gmx-friendly assembly: the
        # proven recipe concatenates peptide .gro atoms with ligand pdb coords. We delegate
        # to the shared assembler using parsed atoms, then write .gro.
        lig_atoms = struct.parse_pdb_atoms(ligand_pdb, is_ligand=True)
        # CRITICAL (matches preprocess_pipeline.sh assemble_complex): the ligand COORDINATES
        # come from the pose PDB, but the ligand ATOM NAMES must come from acpype's LIG_GMX.gro
        # so they match the LIG.itp topology exactly — otherwise `gmx grompp` rejects the run
        # with "atom name N in topol.top and .gro does not match". Both files share the same
        # atom order (lig_ref.sdf and pose_N_lig.pdb are AddHs() of the same heavy template),
        # so an index-aligned name copy is correct.
        acp_gro = ligand.gro_path
        if acp_gro and Path(acp_gro).exists():
            acp_atoms = self._read_gro_atoms(Path(acp_gro))
            if len(acp_atoms) != len(lig_atoms):
                raise RuntimeError(
                    f"ligand atom-count mismatch between pose PDB ({len(lig_atoms)}) and "
                    f"acpype LIG_GMX.gro ({len(acp_atoms)}); cannot align topology to coordinates."
                )
            for la, aa in zip(lig_atoms, acp_atoms):
                la.name = aa.name
                la.resname = aa.resname
        if receptor_atoms is None:
            # Read .gro atoms.
            receptor_atoms = self._read_gro_atoms(Path(prepared.structure_path))
        combined, coords = struct.assemble_complex(receptor_atoms, lig_atoms)
        # Write complex.gro (nm).
        from .mock import MockEngine
        MockEngine._write_gro(run / "complex.gro", combined, coords / 10.0, title="complex")

        lig_path = (ligand.extra or {}).get("path", "acpype")
        # Topology injection branches by ligand parameterization path: the acpype
        # (small-molecule) path injects LIG_atomtypes.itp + LIG.itp; the pdb2gmx
        # (peptide/protein) path has no separate ligand .itp to include.
        self._inject_topology(prepared.topology_path, ligand, run, lig_path=lig_path)
        # Copy the ligand topology + restraints referenced by the injected includes.
        copy_set = [ligand.itp_path, ligand.posre_path]
        if lig_path == "acpype":
            copy_set.append(ligand.atomtypes_itp_path)
        for itp in copy_set:
            if itp and Path(itp).exists():
                shutil.copy(itp, run / Path(itp).name)
        if prepared.posre_path and Path(prepared.posre_path).exists():
            shutil.copy(prepared.posre_path, run / Path(prepared.posre_path).name)

    @staticmethod
    def _read_gro_atoms(path: Path) -> List[struct.Atom]:
        lines = path.read_text().splitlines()
        n = int(lines[1])
        atoms: List[struct.Atom] = []
        for i in range(n):
            ln = lines[2 + i]
            resseq = int(ln[0:5])
            resname = ln[5:10].strip()
            name = ln[10:15].strip()
            x = float(ln[20:28]) * 10.0
            y = float(ln[28:36]) * 10.0
            z = float(ln[36:44]) * 10.0
            element = "".join(c for c in name if c.isalpha())[:1] or "C"
            atoms.append(struct.Atom("ATOM", i + 1, name, resname, "A", resseq, x, y, z, element,
                                     is_backbone=name in {"N", "CA", "C", "O"}))
        return atoms

    @staticmethod
    def _top_to_itp(top_path: Path, itp_dest: Path):
        """Convert a standalone pdb2gmx ligand.top into an includable .itp.

        Extracts the section from the first ``[ moleculetype ]`` up to (but not including)
        the ``[ system ]`` section, and returns (itp_dest, molecule_name). The molecule name
        is the first token on the data line under ``[ moleculetype ]``.
        """
        lines = top_path.read_text().splitlines()
        body: List[str] = []
        mol_name = "LIG"
        capture = False
        seen_mol_header = False
        for ln in lines:
            stripped = ln.strip().lower()
            if re.match(r"^\[\s*moleculetype\s*\]", stripped):
                capture = True
                seen_mol_header = True
            elif re.match(r"^\[\s*system\s*\]", stripped) or re.match(r"^\[\s*molecules\s*\]", stripped):
                capture = False
            if capture:
                body.append(ln)
        # Resolve molecule name from the first non-comment data line after the header.
        if seen_mol_header:
            grab = False
            for ln in body:
                s = ln.strip()
                if re.match(r"^\[\s*moleculetype\s*\]", s.lower()):
                    grab = True
                    continue
                if grab and s and not s.startswith(";"):
                    mol_name = s.split()[0]
                    break
        itp_dest.write_text("\n".join(body) + "\n")
        return itp_dest, mol_name

    @staticmethod
    def _inject_topology(topol_path: str, ligand, run: Path, *, lig_path: str = "acpype") -> None:
        """Inject the ligand into the receptor topology and write run/topol.top.

        Branches by ligand parameterization path:
          - "acpype" (small-molecule): inject the split LIG_atomtypes.itp after the force
            field include, LIG.itp before the solvent include, and 'LIG 1' in [ molecules ].
          - "pdb2gmx" (peptide/protein ligand): include the converted ligand .itp (same
            force field as the receptor, so no separate atomtypes block) and emit the actual
            moleculetype name captured from ligand.top in [ molecules ].
        """
        top = Path(topol_path).read_text().splitlines()
        extra = ligand.extra or {}

        if lig_path == "acpype":
            mol_name = "LIG"
            lig_itp_name = "LIG.itp"
            inject_atomtypes = True
        else:
            mol_name = extra.get("mol_name", "LIG")
            lig_itp_name = extra.get("itp_name", "ligand.itp")
            inject_atomtypes = False

        out: List[str] = []
        injected_at = injected_mol = False
        for line in top:
            out.append(line)
            if (
                inject_atomtypes
                and (not injected_at)
                and line.strip().startswith("#include")
                and "forcefield.itp" in line
            ):
                out += ["; Include ligand atomtypes", '#include "LIG_atomtypes.itp"']
                injected_at = True

        final: List[str] = []
        for line in out:
            low = line.lower()
            if (not injected_mol) and line.strip().startswith("#include") and (
                "tip3p" in low or "/ions.itp" in low or "water" in low
            ):
                final += ["; Include ligand topology", f'#include "{lig_itp_name}"', ""]
                injected_mol = True
            final.append(line)
        text = "\n".join(final)
        if not injected_mol:
            text = text.replace(
                "[ system ]",
                f'; Include ligand topology\n#include "{lig_itp_name}"\n\n[ system ]',
                1,
            )

        if not text.endswith("\n"):
            text += "\n"
        text += f"{mol_name:<20}1\n"
        (run / "topol.top").write_text(text)

    @staticmethod
    def _parse_perf(log_path: Path, md_length_ns: float):
        """Extract ns/day from md.log Performance line; fall back to a sane default."""
        ns_per_day = 0.0
        if log_path.exists():
            text = log_path.read_text(errors="replace")
            m = re.search(r"Performance:\s+([\d.]+)", text)
            if m:
                try:
                    ns_per_day = float(m.group(1))
                except ValueError:
                    ns_per_day = 0.0
        return ns_per_day, float(md_length_ns)
