# mdworker — MD Platform worker

Docking-to-MD pipeline for the MD Platform (CONTRACT.md §9). Takes an AutoDock Vina
multi-pose PDBQT plus the ligand chemistry (SDF / MOL2 / SMILES / Meeko) and a receptor
(PDB / CIF), and for each selected pose runs the full preprocessing + MD + analysis +
packaging pipeline, reporting status/logs/progress back to the backend.

This package is the generalization of the proven recipe in
`../preprocess_pipeline.sh` — the bond graph / chemistry is taken from the user-supplied
template, never hardcoded to any specific molecule.

## Install

The worker is an installable package named **`mdworker`** (see `pyproject.toml`).

```bash
# editable install into the backend's environment for local dev (QUEUE_BACKEND=local)
pip install -e ./worker

# with the rq extra for the containerized queue worker
pip install "./worker[rq]"
```

Required runtime dependencies: `rdkit`, `numpy`, `scipy`, `networkx`, `httpx`,
`python-dateutil`. Optional, import-guarded: `MDAnalysis` (analysis acceleration),
`weasyprint` (PDF report), `rq` + `redis` (queue worker bootstrap).

## Engines

`MD_ENGINE` selects the engine (CONTRACT §1):

- `gromacs` — real `gmx` / `pdb2gmx` / `acpype` toolchain (wraps the exact commands from
  `preprocess_pipeline.sh`; renders `md.mdp` `nsteps = md_length_ns * 500000` at dt=0.002 ps).
  Requires `gmx` (and `acpype` for small-molecule ligands) on PATH.
- `mock` — synthetic but realistic engine: synthesizes topology/itp stubs, assembles the
  complex, generates a deterministic multi-frame `trajectory.pdb` (numpy random-walk seeded
  by pose index), emits gmx-like log lines and a realistic ns/day, and advances `completed_ns`
  toward `md_length_ns`. Lets the **full** pipeline reach `completed` with real artifacts on a
  machine without GROMACS/acpype (RDKit is still required).
- `auto` (default) — `gromacs` if `gmx` is on PATH, else `mock`.

## Pipeline (one subjob = one pose)

`mdworker.pipeline.runner.run_subjob(subjob_id, *, reporter, settings)` drives:

1. `validate_input` — parse PDBQT poses + Vina scores; classify input; atom-mapping
   feasibility; produce the ValidationReport (CONTRACT §7). Import-light; reused by the
   backend's `/uploads/{id}/validate` route.
2. `split_pdbqt_poses` — split MODEL/ENDMDL, rank by docking score, extract this pose.
3. `assign_bond_orders` — GENERAL bond-order transfer from the chemistry template to the
   pose heavy atoms, then `AddHs(addCoords=True)`; write `pose_N_lig.pdb` + `lig_ref.sdf`.
4. `prepare_structure` — receptor → topology (pdb2gmx amber14sb/tip3p; HETATM decisions).
5. `parameterize_ligand` — small_molecule → acpype GAFF2/AM1-BCC; peptide/protein → pdb2gmx.
6. `run_md` — assemble → box → solvate → genion → EM → NVT → NPT → production
   (status `running_em`/`running_nvt`/`running_npt`/`running_md`; ns/day + completed_ns + progress).
7. `analyze_md` — RMSD/RMSF/Rg/SASA/H-bond/distance/energy/ligand stability/contact map →
   `analysis/*.csv` + `analysis/plots/*.json` (Plotly figure dicts) + `analysis/summary.json`.
8. `render_movie` — ensure multi-MODEL `visualization/trajectory.pdb`; mp4/webm only if a
   renderer is configured (`MOVIE_RENDER_CMD`), else skip gracefully.
9. `package_results` — per-pose `results.zip` + job `summary/{all_results.zip,
   pose_comparison.csv, metadata.json, summary_report.html(+pdf)}`.

GPU: the runner requests a GPU lock from the backend before MD and releases it after
(one subjob = one GPU). The real engine sets `CUDA_VISIBLE_DEVICES` for `gmx mdrun`.

## Reporter seam (CONTRACT §5)

The worker never imports backend internals. `mdworker.pipeline.context` defines the
`Reporter` Protocol and the default `HttpReporter` (POSTs to `/api/internal/*` with the
`X-Internal-Token` header). The backend provides a `DbReporter` implementing the same
signatures for the in-process LocalExecutor when `QUEUE_BACKEND=local`.

## Entry points

- `mdworker.tasks.run_subjob_task(subjob_id)` — rq job function (builds an HttpReporter).
- `worker.py` — rq Worker bootstrap (connect `REDIS_URL`, work the default queue).
- `mdworker-run <subjob_id>` — console script for manual re-runs.

## Storage layout (CONTRACT §8)

```
jobs/{job_id}/
  metadata.json                         # written by the backend before enqueue
  input/{original,processed}/
  pose_NN/{prep,md,analysis,visualization,logs}/  results.zip  pose_summary.json
  summary/{pose_comparison.csv, metadata.json, summary_report.html[, .pdf], all_results.zip}
```

## Environment variables

See CONTRACT.md §1. The worker reads: `MD_ENGINE`, `BACKEND_URL`, `INTERNAL_API_TOKEN`,
`PROTEIN_FORCE_FIELD`, `LIGAND_FORCE_FIELD`, `LIGAND_CHARGE_METHOD`, `WATER_MODEL`,
`REQUIRE_LIGAND_CHEMISTRY`, `ALLOW_SMILES_INPUT`, `ALLOW_MEEKO_MAPPING_INPUT`,
`MDP_TEMPLATE_DIR` (default `/app/md-env/templates/gromacs`), `STORAGE_ROOT`,
`TRAJECTORY_OUTPUT_PS`, `MD_MOCK_SPEEDUP`, `REDIS_URL`, `WORKER_GPU_ID`,
and optionally `MOVIE_RENDER_CMD` (a `{traj}`/`{out}` template enabling movie rendering).
