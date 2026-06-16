# MD Platform — Implementation Contract (authoritative)

This file is the **single source of truth** for cross-component contracts. Backend,
worker, frontend, and infra MUST conform to the names, paths, shapes, and enums below.
Derived from `final_pdr_md_platform.md` (the PDR). **This contract OVERRIDES the PDR on
any conflict** — where the PDR is general and this file is concrete, the concrete shapes,
names, paths, and enums here win. Sections §1–§8 are the stable cross-component interfaces
(env, DB, API, validation, storage); §9–§12 are implementation/deployment/acceptance
guidance that components may refine internally so long as the §1–§8 interfaces hold.

System under test: a **small-molecule ligand (3-HDC)** docked against a **peptide
receptor (KCCIVYP)**. PDBQT carries 9 poses + Vina scores; SDF carries chemistry;
peptide PDB is the receptor. This is the canonical happy-path test case.

---

## 0. Global conventions

- Project root: `md-platform/`.
- Backend is the only writer of the DB. Worker writes job artifacts to storage and
  reports status/logs/progress back via the backend's internal API (`/api/internal/*`)
  authenticated with `INTERNAL_API_TOKEN`. (If `QUEUE_BACKEND=local`, the worker runs
  in-process and writes the DB directly through shared SQLAlchemy session helpers.)
- All timestamps are UTC ISO-8601 strings in JSON.
- All IDs that are "string" type are opaque; Job IDs follow `job_YYYYMMDD_NNN`,
  SubJob IDs follow `{job_id}_pose_{NN}` (NN zero-padded, 1-based pose index).
- Money/marketing language is banned in user-facing strings; messages are factual.

## 1. Environment variables (.env)

```
APP_PORT=8888
DEFAULT_ADMIN_ID=csbl
DEFAULT_ADMIN_PASSWORD=csbl
DEFAULT_MD_LENGTH_NS=50
DEFAULT_TOP_N_POSES=3
STORAGE_ROOT=/app/storage              # local dev: ./storage
DATABASE_URL=sqlite:////app/storage/md_platform.db   # compose: postgresql+psycopg://mduser:mdpass@db:5432/mdplatform
REDIS_URL=redis://redis:6379/0
MAX_UPLOAD_SIZE_GB=10
GPU_ASSIGNMENT_MODE=one_job_per_gpu
MD_ENGINE=auto                         # gromacs | mock | auto (auto = gromacs if `gmx` on PATH else mock)
PROTEIN_FORCE_FIELD=amber14sb
LIGAND_FORCE_FIELD=gaff2
LIGAND_CHARGE_METHOD=am1bcc
WATER_MODEL=tip3p
REQUIRE_LIGAND_CHEMISTRY=true
ALLOW_SMILES_INPUT=true
ALLOW_MEEKO_MAPPING_INPUT=true
JWT_SECRET=change-me-in-production
JWT_EXPIRE_MINUTES=480
QUEUE_BACKEND=auto                     # rq | local | auto (auto = rq if REDIS reachable else local)
INTERNAL_API_TOKEN=internal-worker-token-change-me
NUM_GPUS=auto                          # auto = detect via nvidia-smi; integer to force
MD_GPU_IDS=                            # csv GPU ids for the MD pool (empty = all non-design GPUs)
DESIGN_GPU_IDS=                        # csv GPU ids reserved for peptide design (empty = none)
MD_GPU_CONCURRENCY=1                   # parallel MD per MD-pool GPU; runtime-adjustable via dashboard
DOCK_ENGINE=vina                       # peptide-design docking: vina (default, rigid AutoDock Vina 1.2.7) | smina (flexible side chains) | auto
MD_MOCK_SPEEDUP=2000                   # mock engine: ns of "simulation" per real second
TRAJECTORY_OUTPUT_PS=100               # default xtc interval
RETENTION_DAYS=30
```

Backend config (`app/config.py`) reads all of these via pydantic-settings with the
defaults shown. For **local dev** without Docker: `STORAGE_ROOT=./storage`,
`DATABASE_URL=sqlite:///./storage/md_platform.db`, `QUEUE_BACKEND=local`, `MD_ENGINE=mock`.

## 2. Database schema (SQLAlchemy models in `backend/app/models.py`)

Exactly per PDR §18. Table names lowercase plural.

### users
| col | type | notes |
|---|---|---|
| id | int PK autoincrement | |
| username | str unique, indexed | |
| password_hash | str | bcrypt |
| role | str | `admin` \| `user` |
| is_active | bool default True | |
| must_change_password | bool default True | seeded admin starts True |
| created_at | datetime default utcnow | |

### jobs
| col | type | notes |
|---|---|---|
| id | str PK | `job_YYYYMMDD_NNN` |
| user_id | int FK users.id | |
| name | str | |
| input_type | str | `pdbqt`\|`cif`\|`pdb`\|`mixed` |
| ligand_type | str | `small_molecule`\|`peptide`\|`protein_partner`\|`cofactor`\|`unknown` |
| status | str | JobStatus enum (see §4) |
| md_length_ns | int | default 50 |
| top_n_poses | int | default 3 |
| force_field | str | protein ff, default `amber14sb` |
| ligand_force_field | str | default `gaff2` |
| ligand_chem_source | str | `sdf`\|`mol2`\|`smiles`\|`meeko`\|`manual` |
| water_model | str | default `tip3p` |
| salt_concentration | float | default 0.15 |
| temperature | float | default 300 |
| pressure | float | default 1.0 |
| box_type | str | `dodecahedron`\|`cubic`, default dodecahedron |
| priority | str | `low`\|`normal`\|`high`, default normal |
| created_at | datetime | |
| started_at | datetime nullable | |
| completed_at | datetime nullable | |
| result_path | str nullable | |
| error_message | str nullable | top-level failure summary |

### subjobs
| col | type | notes |
|---|---|---|
| id | str PK | `{job_id}_pose_{NN}` |
| job_id | str FK jobs.id | |
| pose_index | int | 1-based |
| docking_score | float | from Vina REMARK |
| status | str | JobStatus enum |
| assigned_gpu | int nullable | |
| progress | float | 0..100 |
| completed_ns | float | |
| ns_per_day | float | measured speed, 0 until known |
| current_step | str | pipeline step name |
| started_at | datetime nullable | |
| completed_at | datetime nullable | |
| result_path | str nullable | |
| error_message | str nullable | |

### gpustatus
| col | type | notes |
|---|---|---|
| gpu_id | int PK | |
| name | str | |
| status | str | GpuStatus enum (see §4) |
| utilization | float | % |
| memory_used | float | MiB |
| memory_total | float | MiB |
| temperature | float | C |
| assigned_subjob_id | str nullable | most-recent claimer (display); occupancy is `running_count` |
| pool | str | `md` \| `design` \| `excluded` (workload partition; see §1 env) |
| capacity | int | max concurrent subjobs on this GPU (parallel MD); default 1 |
| running_count | int | slots currently in use (0..capacity); authoritative occupancy |
| updated_at | datetime | |

GPU claim/release is slot-counted: a GPU is claimable while `running_count < capacity` and not
admin-blocked. `request_gpu(subjob_id, pool)` atomically takes a slot on the least-loaded GPU
in `pool` and binds it to the subjob (`SubJob.assigned_gpu`); release decrements only when it
clears that binding, so duplicate/concurrent calls cannot double-count. `excluded` GPUs are
never scheduled.

### joblogs
| col | type | notes |
|---|---|---|
| id | int PK autoincrement | |
| job_id | str indexed | |
| subjob_id | str nullable indexed | |
| level | str | `info`\|`warning`\|`error` |
| step | str | pipeline step |
| message | text | |
| created_at | datetime | |

### resourceusage
| col | type | notes |
|---|---|---|
| id | int PK autoincrement | |
| job_id | str nullable | |
| subjob_id | str nullable | |
| cpu_percent | float | |
| memory_used | float | MiB |
| disk_used | float | MiB |
| sampled_at | datetime | |

## 3. Default seed (`backend/app/seed.py`, run on startup)

- Create admin `DEFAULT_ADMIN_ID`/`DEFAULT_ADMIN_PASSWORD` (csbl/csbl), role=admin,
  `must_change_password=True` if no users exist.
- Populate `gpustatus` rows from detected GPUs (nvidia-smi) or NUM_GPUS placeholders.

## 4. Enums (define in `backend/app/models.py` as plain string constants; mirror in `frontend/src/types.ts`)

JobStatus (subjob + job): `uploaded`, `validating`, `queued`, `preparing`,
`running_em`, `running_nvt`, `running_npt`, `running_md`, `analyzing`, `rendering`,
`packaging`, `completed`, `failed`, `cancelled`.

GpuStatus: `available`, `busy`, `disabled`, `maintenance`, `error`.

LigandType: `small_molecule`, `peptide`, `protein_partner`, `cofactor`, `unknown`.

ChemSource: `sdf`, `mol2`, `smiles`, `meeko`, `manual`.

PlotType (for `/plots/{plot_type}`): `rmsd`, `rmsf`, `rg`, `sasa`, `hbond`, `energy`,
`ligand_rmsd`, `contact_map`.

## 5. HTTP API (FastAPI, prefix `/api`)

Auth: JWT bearer in `Authorization: Bearer <token>`. Login returns token. Endpoints
other than `/auth/login` require auth. Admin-only endpoints checked by role.

### Auth (§19.1)
- `POST /api/auth/login` body `{username, password}` → `{access_token, token_type:"bearer", must_change_password, role, username}`. On bad creds: 401. Rate-limit: max 10 failed/min per username (429 thereafter).
- `POST /api/auth/logout` → `{ok:true}` (stateless; client drops token).
- `POST /api/auth/change-password` body `{old_password, new_password}` → `{ok:true}`. Clears must_change_password.
- `GET /api/auth/me` → `{id, username, role, must_change_password, is_active, created_at}`.

### Uploads (§19.3)
- `POST /api/uploads/input` multipart form: fields `pose_file` (PDBQT, required),
  `chemistry_file` (SDF/MOL2, optional), `receptor_file` (PDB/CIF, optional),
  `smiles` (str, optional), plus optional metadata. Returns
  `{upload_id, pose_file, chemistry_file, receptor_file, detected_pose_count, detected_input_type, ligand_type_candidates:[...], hetatm_candidates:[...]}`.
  Stores files under `storage/uploads/{upload_id}/`.
- `GET /api/uploads/{upload_id}/validate` → ValidationReport (see §7).

### Jobs (§19.2)
- `POST /api/jobs` body JobCreate (see §6) → Job. Creates job + subjobs (top_n poses),
  enqueues. Rejects (422) if validation fails (raw-PDBQT-only, mapping mismatch, etc.).
- `GET /api/jobs?mine=true|false` → list of Job (users see own; admin can see all with mine=false).
- `GET /api/jobs/{job_id}` → JobDetail (job + subjobs[] + recent logs).
- `POST /api/jobs/{job_id}/cancel` → Job (status cancelled). Releases GPU locks.
- `POST /api/jobs/{job_id}/retry` → Job (re-queues failed subjobs).
- `DELETE /api/jobs/{job_id}` → `{ok:true}` (admin or owner). Removes storage.

### Queue (§19.4)
- `GET /api/queue` → `{items:[QueueItem], running:[QueueItem]}`. QueueItem: `{job_id, subjob_id, job_name, user, pose_index, status, queue_position, assigned_gpu, progress, completed_ns, md_length_ns, ns_per_day, rough_eta_seconds}`.
- `POST /api/queue/{job_id}/priority` body `{priority}` admin-only → Job.

### GPU (§19.5)
- `GET /api/gpus` → `[GpuStatus]`.
- `POST /api/gpus/{gpu_id}/enable` admin → GpuStatus.
- `POST /api/gpus/{gpu_id}/disable` admin → GpuStatus.
- `POST /api/gpus/{gpu_id}/maintenance` admin → GpuStatus.
- `PATCH /api/gpus/concurrency` admin, body `{pool: "md"|"design", concurrency: 1..16}` → `[GpuStatus]` (sets parallel-MD slots per GPU in the pool; running subjobs are never evicted; persists across restart).

### Peptide design (GA) (§19.6)
- `POST /api/design` (multipart) → `DesignJob`. Form fields: `name`, `initial_sequences`
  (comma/space/newline-separated, all one length, standard AAs), `population_size`,
  `num_generations`, `top_k_md`, `md_length_ns`, `exhaustiveness`, `compound_name`; plus a
  `compound` file (.sdf/.mol/.mol2/.pdb/.smi) OR a `smiles` string. Runs on the GPU design pool.
- `GET /api/design` → `[DesignJob]` (own jobs; admin sees all).
- `GET /api/design/{id}` → `DesignJobDetail` = `{job, candidates[] (leaderboard, fitness desc),
  generations[] (best-so-far convergence curve)}`.
- `POST /api/design/{id}/cancel` → `DesignJob` (status cancelled; runner aborts between generations).

GA: fixed-length peptide (= initial length), genes are AA indices 0..19; hybrid evaluation
per generation — dock ALL candidates, MD-refine the top-k by docking score (GROMACS+MM/GBSA
on the design GPU), fitness = −ΔG for refined elites else −docking_score.
Tables `designjobs` + `designcandidates`.

Docking engine (DOCK_ENGINE: vina | smina | auto): **Vina** is the DEFAULT (AutoDock Vina
1.2.7, rigid receptor, deterministic); **Smina** is opt-in (`DOCK_ENGINE=smina`) and adds
flexible receptor side chains (Vina-1.1.2-core scoring + `--flexdist`); `auto` = smina if
installed else vina. The peptide is the RECEPTOR, the SMILES compound the LIGAND. Docking is a
COARSE pre-screen; MD + MM/GBSA is the real ranking arbiter. (Implementation params —
box/exhaustiveness/prep — live in `worker/mdworker/design/docking.py`.)

### Results (§19.6)
- `GET /api/jobs/{job_id}/results` → `{job, subjobs:[{...,analysis_summary, plots_available:[PlotType], has_trajectory, has_movie}]}`.
- `GET /api/jobs/{job_id}/subjobs/{subjob_id}/results` → subjob result detail incl. analysis summary + pose comparison entry.
- `GET /api/jobs/{job_id}/download` → streams `all_results.zip` (Content-Disposition).
- `GET /api/jobs/{job_id}/subjobs/{subjob_id}/download` → streams pose `results.zip`.
- `GET /api/jobs/{job_id}/plots/{plot_type}?subjob_id=` → Plotly figure JSON `{data:[...], layout:{...}}`. Supports pose-comparison when subjob_id omitted (overlay all poses).
- `GET /api/jobs/{job_id}/trajectory?subjob_id=` → streams trajectory file the 3D viewer loads (multi-model PDB `trajectory.pdb` for MVP/mock; real run also offers `.xtc`+`.gro`). Header `X-Trajectory-Format: pdb|xtc`.
- `GET /api/jobs/{job_id}/movie?subjob_id=` → streams mp4/webm if present, else 404.

### Dashboard summary
- `GET /api/dashboard/summary` → `{total_jobs, running_jobs, queued_jobs, completed_jobs, failed_jobs, gpus_available, gpus_busy, storage_used_gb, storage_total_gb}`.

### Realtime (§19.7)
- SSE `GET /api/events/dashboard` → `event: dashboard\ndata: {summary+gpus+queue snapshot}` every ~3s.
- SSE `GET /api/events/jobs/{job_id}` → per-job status/progress/log stream.
- WebSocket `GET /api/ws/dashboard` and `/api/ws/jobs/{job_id}` send the same payloads as JSON frames. (SSE is the MVP primary; WS optional but stub the routes.)

### Internal (worker → backend; header `X-Internal-Token: INTERNAL_API_TOKEN`)
- `POST /api/internal/subjobs/{subjob_id}/status` body `{status, current_step?, progress?, completed_ns?, ns_per_day?, assigned_gpu?, error_message?}`.
- `POST /api/internal/jobs/{job_id}/status` body `{status?, result_path?, error_message?, started_at?, completed_at?}`.
- `POST /api/internal/logs` body `{job_id, subjob_id?, level, step, message}`.
- `POST /api/internal/gpus/{gpu_id}/assign` body `{subjob_id|null, status}`.
- `GET /api/internal/subjobs/{subjob_id}/cancelled` → `{cancelled: bool}` (cancel-signal poll; the rq-mode worker's HttpReporter.is_cancelled uses it to force-kill the in-flight gmx process group when a job is cancelled).
- `POST /api/internal/gpus/request` body `{subjob_id}` → `{gpu_id: int|null}` (atomically picks an `available` GPU, marks it `busy`+assigned; null if none free → caller waits/queues).
- `POST /api/internal/gpus/release` body `{subjob_id}` → `{ok:true}` (frees the GPU lock held by subjob).

### Worker↔Backend seam (Reporter) — pin EXACTLY in both worker and backend

The worker never imports backend internals. `worker/pipeline/context.py` defines a
`Reporter` Protocol and an `HttpReporter` (default; posts to the Internal API using
`BACKEND_URL` + `X-Internal-Token`). The backend provides a `DbReporter` implementing the
same methods against the DB, used by the in-process **LocalExecutor** when `QUEUE_BACKEND=local`.
Both implement these exact methods:

```python
class Reporter(Protocol):
    def set_subjob_status(self, subjob_id, *, status=None, current_step=None, progress=None,
        completed_ns=None, ns_per_day=None, assigned_gpu=None, error_message=None,
        started_at=None, completed_at=None, result_path=None) -> None: ...
    def set_job_status(self, job_id, *, status=None, result_path=None, error_message=None,
        started_at=None, completed_at=None) -> None: ...
    def log(self, job_id, subjob_id, level, step, message) -> None: ...
    def request_gpu(self, subjob_id) -> "int | None": ...   # None when no GPU free
    def release_gpu(self, subjob_id) -> None: ...
```

`runner.run_subjob(subjob_id, *, reporter, settings)` drives the steps using `JobContext`
(wraps reporter + paths). Enqueue contract: backend `queue_manager.enqueue(subjob_id)` →
in `rq` mode pushes job calling `mdworker.tasks.run_subjob_task(subjob_id)` (which builds an
HttpReporter); in `local` mode submits to a `ThreadPoolExecutor` calling
`mdworker.pipeline.runner.run_subjob(subjob_id, reporter=DbReporter(), settings=...)`.

Packaging (no sys.path hacks): the worker is an installable package named **`mdworker`**
with `worker/pyproject.toml` (`[project] name="mdworker"`, package dir `worker/mdworker/` —
i.e. the pipeline lives at `worker/mdworker/pipeline/...`). Local dev installs it editable
into the backend's environment (`pip install -e ./worker`); the backend imports `mdworker`
as a normal module. The worker Docker image installs the same package. The `Reporter`
Protocol is defined in `mdworker` and re-used by the backend's `DbReporter`
(backend depends on `mdworker` for the Protocol type only).

## 6. JobCreate request shape

```json
{
  "upload_id": "string (required)",
  "name": "string (optional, auto if absent)",
  "ligand_type": "small_molecule|peptide|protein_partner|cofactor|unknown",
  "ligand_chem_source": "sdf|mol2|smiles|meeko|manual",
  "top_n_poses": 3,
  "md_length_ns": 50,
  "md_preset": "quick|standard|extended|custom",
  "force_field": "amber14sb",
  "ligand_force_field": "gaff2",
  "water_model": "tip3p",
  "box_type": "dodecahedron",
  "salt_concentration": 0.15,
  "temperature": 300,
  "pressure": 1.0,
  "use_gpu": true,
  "priority": "normal",
  "hetatm_decisions": { "RESNAME": "ligand|cofactor|ion|water|additive|drop" },
  "cif_options": { "keep_waters": false, "keep_ions": true, "select_chain": "All" }
}
```

Presets (§10.3): quick=10ns, standard=50ns(default), extended=100ns, custom=use md_length_ns
(custom only allowed for admin/advanced — backend enforces: non-admin custom → 403).

## 7. ValidationReport shape (worker `validate_input` + backend `/uploads/{id}/validate`)

```json
{
  "ok": true,
  "input_type": "pdbqt|cif|pdb|mixed",
  "pose_count": 9,
  "poses": [{"index":1,"docking_score":-2.9}, ...],
  "ligand_type_candidates": ["small_molecule"],
  "chem_source": "sdf|mol2|smiles|meeko|none",
  "atom_mapping": {
     "attempted": true,
     "success": true,
     "template_heavy_atoms": 25,
     "pose_heavy_atoms": 25,
     "molformula_template": "C23H40O2",
     "molformula_pose": "C23H40O2",
     "matched_atoms": 25,
     "message": "Bond orders assignable from SDF template to all poses."
  },
  "hetatm_candidates": [{"resname":"HOH","count":12,"suggested":"water"}],
  "receptor": {"format":"pdb|cif","chains":["A"],"n_residues":7,"n_atoms":110,"has_hetatm":false},
  "errors": [],
  "warnings": []
}
```

**Hard rules (PDR §6, §7.3, §28.1) — backend rejects job creation (422) when violated:**
- Raw PDBQT only (no SDF/MOL2 and no valid SMILES and no Meeko mapping) when
  `REQUIRE_LIGAND_CHEMISTRY=true` → reject with code `CHEMISTRY_REQUIRED`.
- `atom_mapping.success == false` → reject with code `ATOM_MAPPING_FAILED`.
- Heavy-atom composition mismatch (formula/count) → reject `CHEMISTRY_MISMATCH`.
- SMILES path requires ALLOW_SMILES_INPUT and successful mapping, else reject.
- Bond order is NEVER perceived from PDBQT alone for parameterization.

Error JSON for rejects: `{"detail": {"code": "...", "message": "...", "report": {...}}}`.

## 8. Storage layout (PDR §20.2) — under `STORAGE_ROOT`

```
uploads/{upload_id}/{pose_file, chemistry_file?, receptor_file?, meta.json}
jobs/{job_id}/
  metadata.json
  input/{original/, processed/}
  pose_01/{prep/, md/, analysis/, visualization/, logs/, results.zip}
  pose_02/...
  summary/{pose_comparison.csv, summary_report.html, summary_report.pdf, all_results.zip}
results/  # (symlink/cache of generated zips; optional)
```

## 9. Worker pipeline (PDR §10.1, §20.1) — `worker/mdworker/pipeline/`

`runner.run_subjob(subjob_id)` executes ordered steps; each step updates status via the
backend client (`pipeline/context.py: JobContext` with `.log()`, `.set_status()`,
`.progress()`), and writes artifacts to the pose dir. Steps (files in `mdworker/pipeline/steps/`):

1. `validate_input.py` — parse PDBQT poses + scores; classify input; produce ValidationReport. (also used by backend upload validate, so keep import-safe & dependency-light.)
2. `split_pdbqt_poses.py` — split MODEL/ENDMDL; sort by score; select top-n; write `pose_N.pdbqt`.
3. `assign_bond_orders.py` — **GENERAL** (not hardcoded): read chemistry template (SDF/MOL2 → RDKit mol, or SMILES), read pose heavy-atom coords, build pose mol, `AssignBondOrdersFromTemplate(template, pose)`, add Hs (`AddHs(addCoords=True)`), verify formula; write `pose_N_lig.pdb` + `lig_ref.sdf`. Reject on mapping failure. (Reference impl in `../preprocess_pipeline.sh build_ligand` is the proven recipe but hardcodes C23H40O2 — generalize it.)
4. `prepare_structure.py` — receptor: strip to ATOM (peptide path) or CIF→PDB convert; HETATM handling per decisions; `gmx pdb2gmx` (amber14sb/tip3p) → topol.top + processed.gro. (mock engine: synthesize processed.gro.)
5. `parameterize_ligand.py` — small_molecule: `acpype -i lig_ref.sdf -c bcc -a gaff2` → split LIG_atomtypes.itp + LIG.itp + posre. peptide/protein_partner ligand: pdb2gmx path (ff14sb). (mock: synthesize itp stubs.)
6. `run_md.py` — assemble complex (peptide.gro + ligand coords) → editconf box → solvate (TIP3P) → genion (0.15 M NaCl) → EM → NVT 100ps → NPT 100ps → production MD (md_length_ns). Status transitions: preparing→running_em→running_nvt→running_npt→running_md. Emits ns/day + completed_ns + progress. Uses `engine/gromacs.py` (real `gmx`) or `engine/mock.py` (synthetic xtc/pdb trajectory + log lines + realistic ns/day). Engine chosen by `MD_ENGINE`/auto.
7. `analyze_md.py` — RMSD (backbone + ligand), RMSF, Rg, SASA, H-bond, distance, energy, ligand stability, contact map, final snapshot. Write `analysis/*.csv` + `analysis/plots/*.json` (Plotly fig JSON) + `analysis/summary.json`. Use MDAnalysis/MDTraj if available else compute from mock frames / gmx tools.
8. `render_movie.py` — optional; mp4/webm of trajectory (ligand-site centered). Skip gracefully if renderer absent; always ensure a multi-model `visualization/trajectory.pdb` exists for the NGL/Mol* viewer.
9. `package_results.py` — per-pose `results.zip` + job `summary/all_results.zip` + `metadata.json` + `summary_report.html` (+pdf if possible) + `pose_comparison.csv`.

**Mock engine** must let the FULL pipeline run to `completed` with realistic artifacts on a
machine without GROMACS/acpype, so §29 criteria are demonstrable locally. Real engine wraps
the exact commands from `preprocess_pipeline.sh` + a production `md.mdp`.

GPU: runner requests a GPU lock from backend (`/internal/gpus/{id}/assign`) before MD;
sets `CUDA_VISIBLE_DEVICES`; releases on completion/failure/cancel. One subjob = one GPU.

## 10. Frontend (PDR §26) — `frontend/` React+TS+Vite+Tailwind

Routes: `/login`, `/` (dashboard), `/upload`, `/jobs/:jobId`, `/jobs/:jobId/results`, `/admin` (admin only).
- `src/api.ts`: typed client for every endpoint in §5; injects JWT; baseURL `/api` (proxied).
- `src/types.ts`: mirror enums + DTOs from §2/§4/§6/§7.
- Login: force password-change modal when `must_change_password`.
- Upload: 3 file inputs + SMILES; calls `/uploads/input` then `/uploads/{id}/validate`;
  shows mapping validation result + HETATM review table; disables submit on hard-rule failure;
  preset + advanced options; storage estimate.
- Dashboard: summary cards, GPU panel (enable/disable toggles for admin), storage card,
  queue table, running table, recent completed; live via SSE `/events/dashboard` (fallback poll 5s).
- JobDetail: metadata, per-pose progress + current step, log viewer, links to results.
- Results: NGL or Mol* 3D viewer loading `/trajectory`, frame slider, Plotly plots
  (rmsd/rmsf/rg/sasa/hbond/energy), pose comparison table + overlay, movie player if present,
  download buttons (job zip, pose zip, per-file).
- Dev: Vite proxy `/api` → `http://localhost:8000`. Prod: nginx serves build + proxies `/api` to backend; container listens 80 (compose maps host 8888→80).

## 11. Docker (PDR §21) — `docker-compose.yml`

Services: `frontend` (build ./frontend, ports "8888:80"), `backend` (build ./backend, exposes 8000),
`redis` (redis:7), `db` (postgres:16), `worker-gpu-0..N` (build ./worker, env CUDA_VISIBLE_DEVICES + WORKER_GPU_ID, deploy.resources.reservations.devices for nvidia), `nginx` optional (frontend already proxies). All mount `./storage:/app/storage`. `.env` via env_file.
- backend Dockerfile: python:3.11-slim + requirements.
- worker Dockerfile: FROM md-env image (GROMACS+AmberTools+ACPYPE+RDKit+MDAnalysis) + worker code.
- md-env Dockerfile: CUDA base + GROMACS GPU build (documented; build may be heavy — provide a `mock`-capable fallback tag too) + conda env (ambertools, acpype, rdkit, mdanalysis).
- scripts/: `install.sh` (env check, nvidia toolkit check, compose up), `backup.sh`, `healthcheck.sh`.

## 12. Acceptance (PDR §29) — must be demonstrable

1 access :8888 · 2 login csbl/csbl · 3 force pw change · 4 upload pdbqt+chem+receptor ·
5 auto pose detect + top-n · 6 SDF/MOL2 chem applied to poses · 7 SMILES/Meeko only on mapping success ·
8 small/peptide/protein paths · 9 mismatch error · 10 per-pose jobs queued · 11 default 50 ns ·
12 presets · 13 GROMACS + fixed ff toolchain · 14 one GPU per job · 15 dashboard job+GPU status ·
16 analysis graphs · 17 trajectory/movie structure change · 18 download all results · 19 docker compose portable.
