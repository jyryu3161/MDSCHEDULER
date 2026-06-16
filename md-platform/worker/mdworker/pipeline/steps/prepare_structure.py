"""Step 4 — prepare_structure (CONTRACT §9.4).

Receptor preparation: strip to ATOM (peptide path) or CIF->PDB convert; apply HETATM
handling per the job's hetatm_decisions; then build receptor topology via the engine
(gmx pdb2gmx amber14sb/tip3p for the real engine; synthesized for the mock engine).

The engine encapsulates the actual gmx/pdb2gmx invocation; this step resolves the receptor
file, applies the HETATM filtering, and delegates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from mdworker.io.receptor import WATER_NAMES


def _apply_hetatm_decisions(src_pdb: str, dest_pdb: Path, decisions: Dict[str, str]) -> Dict[str, int]:
    """Filter receptor HETATM records per decisions; keep ATOM records always.

    Decision values (CONTRACT §6 hetatm_decisions): ligand|cofactor|ion|water|additive|drop.
    'drop' and 'water' (unless explicitly kept) remove the residue's HETATM lines; everything
    else is retained so the downstream topology can include it. Returns a tally of actions.
    """
    kept_records = []
    tally: Dict[str, int] = {}
    for ln in Path(src_pdb).read_text(errors="replace").replace("\r", "").splitlines():
        if ln.startswith("ATOM") or ln.startswith("TER") or ln.startswith("END"):
            kept_records.append(ln)
        elif ln.startswith("HETATM"):
            resname = ln[17:20].strip().upper()
            decision = decisions.get(resname) or decisions.get(ln[17:20].strip()) or (
                "water" if resname in WATER_NAMES else "keep"
            )
            tally[decision] = tally.get(decision, 0) + 1
            if decision in ("drop", "water"):
                continue
            kept_records.append(ln)
    dest_pdb.write_text("\n".join(kept_records) + "\n")
    return tally


def run(ctx, settings) -> Dict[str, Any]:
    from mdworker.pipeline.engine import get_engine

    step = "prepare_structure"
    inputs = ctx.job_meta.get("inputs", {})
    receptor_file = inputs.get("receptor_file")
    if not receptor_file:
        raise ValueError("No receptor file provided; cannot prepare the receptor structure.")

    hetatm_decisions = ctx.job_meta.get("hetatm_decisions", {}) or {}

    # Apply HETATM decisions into a filtered receptor PDB the engine will consume.
    filtered = ctx.prep_dir / "receptor_filtered.pdb"
    suffix = Path(receptor_file).suffix.lower()
    if suffix in (".cif", ".mmcif"):
        # Use the single validated CIF->PDB converter in mdworker.io.receptor (no duplicate
        # CIF logic in the pipeline step).
        from mdworker.io.receptor import cif_to_pdb_lines

        pdb_lines, cif_warnings = cif_to_pdb_lines(receptor_file, hetatm_decisions=hetatm_decisions)
        for w in cif_warnings:
            ctx.warning(step, w)
        if not pdb_lines:
            raise ValueError(
                f"Could not convert CIF receptor {Path(receptor_file).name} to PDB "
                "(no atom records parsed). Provide a PDB receptor."
            )
        filtered.write_text("\n".join(pdb_lines) + "\n")
        receptor_for_engine = str(filtered)
        ctx.info(step, f"Converted CIF receptor to PDB ({filtered.name}, {len(pdb_lines) - 2} atoms).")
    else:
        tally = _apply_hetatm_decisions(receptor_file, filtered, hetatm_decisions)
        receptor_for_engine = str(filtered)
        if tally:
            ctx.info(step, f"Applied HETATM decisions: {tally}.")

    engine = get_engine(settings)
    prepared = engine.prepare_structure(
        ctx, receptor_file=receptor_for_engine, hetatm_decisions=hetatm_decisions
    )
    ctx.info(step, f"Receptor topology prepared via {engine.name} engine.")
    return {
        "topology_path": prepared.topology_path,
        "structure_path": prepared.structure_path,
        "posre_path": prepared.posre_path,
        "engine": engine.name,
        "_prepared": prepared,
    }
