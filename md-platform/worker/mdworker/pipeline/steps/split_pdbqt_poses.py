"""Step 2 — split_pdbqt_poses (CONTRACT §9.2).

Split a multi-MODEL PDBQT into per-pose PDBQT files, sort poses by docking score (most
negative / best first), select top-n, and write pose_N.pdbqt into the pose prep dir. Since
the runner drives one subjob (one pose) per invocation, this step extracts THIS subjob's
pose (by pose_index) and writes it, while also recording the global ranking for context.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from mdworker.io.pdbqt import parse_pdbqt_models


def run(ctx, settings) -> Dict[str, Any]:
    step = "split_pdbqt_poses"
    ctx.set_status("preparing", current_step=step, progress=6.0)

    inputs = ctx.job_meta.get("inputs", {})
    pose_file = inputs.get("pose_file")
    poses = parse_pdbqt_models(pose_file)
    if not poses:
        raise ValueError("No poses found while splitting PDBQT.")

    # Rank by docking score ascending (most negative = strongest binding first); poses with
    # missing scores sort last but keep their file order among themselves.
    def _key(p):
        s = p["docking_score"]
        return (0, s) if s is not None else (1, p["index"])

    ranked = sorted(poses, key=_key)
    ranking = [
        {"rank": r + 1, "pose_index": p["index"], "docking_score": p["docking_score"]}
        for r, p in enumerate(ranked)
    ]

    # Locate this subjob's pose by its 1-based pose_index.
    target = next((p for p in poses if p["index"] == ctx.pose_index), None)
    if target is None:
        # Fall back to position if explicit MODEL indices were absent.
        if 1 <= ctx.pose_index <= len(poses):
            target = poses[ctx.pose_index - 1]
        else:
            raise ValueError(f"Pose index {ctx.pose_index} not present in {pose_file}.")

    pose_pdbqt = ctx.prep_dir / f"pose_{ctx.pose_index}.pdbqt"
    with pose_pdbqt.open("w") as fh:
        fh.write(f"MODEL {ctx.pose_index}\n")
        if target["docking_score"] is not None:
            fh.write(f"REMARK VINA RESULT: {target['docking_score']:>10.4f}      0.000      0.000\n")
        for ln in target["raw_lines"]:
            fh.write(ln + "\n")
        fh.write("ENDMDL\n")

    (ctx.prep_dir / "pose_ranking.json").write_text(json.dumps(ranking, indent=2))

    ctx.info(
        step,
        f"Extracted pose {ctx.pose_index} (score={target['docking_score']}, "
        f"{len(target['heavy_atoms'])} heavy atoms) from {len(poses)} poses.",
    )
    return {
        "pose_pdbqt": str(pose_pdbqt),
        "pose_index": ctx.pose_index,
        "docking_score": target["docking_score"],
        "ranking": ranking,
    }
