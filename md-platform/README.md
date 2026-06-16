# MD Platform

A web platform that takes AutoDock Vina docking output (multi-pose PDBQT) plus
ligand chemistry and a receptor structure, and runs a standardized
molecular-dynamics (MD) pipeline per pose on GPU workers: structure preparation,
ligand parameterization, solvation/ionization, energy minimization, NVT/NPT
equilibration, production MD, trajectory analysis, and result packaging.

The canonical test case is a small-molecule ligand (3-HDC) docked against a
peptide receptor (KCCIVYP); see "Sample data" below.

## Overview

- **Frontend**: React + TypeScript + Vite, served by nginx (proxies `/api` to the backend).
- **Backend**: FastAPI. Sole writer of the database. Exposes the public `/api`
  (auth, uploads, jobs, queue, GPUs, results, realtime) and an internal
  `/api/internal/*` surface the workers report to.
- **Workers**: one process per GPU. Run the MD pipeline (the `mdworker` Python
  package) and report status/logs/progress back to the backend.
- **Queue**: Redis + RQ (`QUEUE_BACKEND=rq`). A no-Docker local mode runs jobs
  in-process instead (`QUEUE_BACKEND=local`).
- **Database**: PostgreSQL in Docker; SQLite for local dev.
- **MD engine**: GROMACS (GPU) for real runs; a built-in mock engine lets the
  full pipeline complete on a machine without GROMACS/GPU.

## Architecture

```
                          Browser (http://server-ip:8888)
                                      |
                                      v
                    +---------------------------------+
                    |  frontend (nginx :80)           |
                    |  serves the built Vite app;     |
                    |  proxies /api -> backend:8000   |
                    +----------------+----------------+
                                     | /api
                                     v
        +--------------------------------------------------------+
        |  backend (FastAPI, :8000)  -- sole DB writer           |
        |   /api/auth /api/uploads /api/jobs /api/queue          |
        |   /api/gpus /api/.../results /api/events (SSE)          |
        |   /api/internal/*  (worker -> backend; X-Internal-Token)|
        +----+-------------------+--------------------+----------+
             |                   |                    |
        enqueue (RQ)        read/write             report status/logs,
             |              (SQLAlchemy)           request/release GPU lock
             v                   |                    ^
        +---------+         +----+----+               |
        |  redis  |         |   db    |               |
        | (RQ)    |         | postgres|               |
        +----+----+         +---------+               |
             |                                        |
             |  dequeue run_subjob(subjob_id)         |
             v                                        |
   +-------------------+   +-------------------+       |
   |  worker-gpu-0     |   |  worker-gpu-1     |  ...  |
   |  CUDA_VISIBLE=0   |   |  CUDA_VISIBLE=1   |-------+
   |  mdworker pipeline|   |  mdworker pipeline|
   |  GROMACS | mock   |   |  GROMACS | mock   |
   +---------+---------+   +---------+---------+
             |                       |
             +-----------+-----------+
                         v
              ./storage  (bind-mounted into backend + workers)
              uploads/  jobs/{job_id}/pose_NN/...  results/
```

Shared mounts:
- `./storage` -> `/app/storage` in backend and workers (job artifacts).
- `./md-env`  -> `/app/md-env` (read-only) for the GROMACS `.mdp` templates and
  any extra force fields.

## Quickstart (Docker)

Prerequisites: Docker, Docker Compose v2, NVIDIA driver + NVIDIA Container
Toolkit (for GPU workers). Verify GPU passthrough:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

Install and start:

```bash
git clone <repository>
cd md-platform
cp .env.example .env          # the installer does this for you if absent
docker compose up -d
```

Then open `http://<server-ip>:8888` and log in with **`csbl` / `csbl`**. You will
be required to change the password on first login.

Guided alternative (prerequisite checks, secret rotation, image build, up):

```bash
./scripts/install.sh
```

The installer rotates the placeholder secrets in a freshly created `.env`
(`JWT_SECRET`, `INTERNAL_API_TOKEN`, `POSTGRES_PASSWORD`) to strong random
values. The admin login stays `csbl/csbl` by design (forced change on first
login).

Building the `md-env` base image (the worker image is built `FROM` it) is heavy
(GROMACS GPU compile + conda chemistry stack). It is built automatically by the
installer and by `make build`; build it directly with:

```bash
docker build -t md-platform-mdenv:latest ./md-env
# lighter mock-only image (no GROMACS compile):
docker build --build-arg GROMACS_BUILD=OFF -t md-platform-mdenv:latest ./md-env
```

### Adjusting the worker count to your GPUs

`docker-compose.yml` defines `worker-gpu-0` and `worker-gpu-1` (one worker per
GPU). To match your host:

- **Single GPU**: delete the `worker-gpu-1` service.
- **More GPUs**: copy a `worker-gpu-N` block, set `CUDA_VISIBLE_DEVICES=N`,
  `WORKER_GPU_ID=N`, and the `deploy.resources.reservations.devices[].device_ids`
  entry to `"N"`. Set `NUM_GPUS` in `.env` to match (or leave `NUM_GPUS=auto`).

## Local development (without Docker)

This runs the backend with SQLite, the in-process local queue, and the mock MD
engine - no GROMACS, Redis, or GPU required. The full pipeline still completes
with realistic artifacts (CONTRACT §9 mock engine).

```bash
# One-time setup: venv, backend requirements, editable mdworker, frontend deps.
make install-local

# Terminal 1 - backend (SQLite + local queue + mock engine on :8000):
make dev-backend

# Terminal 2 - frontend (Vite dev server; proxies /api -> http://localhost:8000):
make dev-frontend
```

What the local profile sets (CONTRACT §1 local-dev overrides):

```
STORAGE_ROOT=./storage
DATABASE_URL=sqlite:///./storage/md_platform.db
QUEUE_BACKEND=local
MD_ENGINE=mock
```

In `QUEUE_BACKEND=local` the backend runs subjobs in-process via a
`ThreadPoolExecutor` and a `DbReporter` (the same `Reporter` Protocol the Docker
workers use over HTTP), so no separate worker process is needed.

To exercise the RQ path instead, run a local Redis and a worker:

```bash
# needs the [rq] extra: pip install -e ./worker[rq]
make dev-worker            # RQ worker against redis://localhost:6379/0
```

The worker package is installable and named `mdworker` (`pip install -e ./worker`);
the backend imports it for the `Reporter` Protocol type and the local executor.

## Fixed MD toolchain

The scientific toolchain is fixed (CONTRACT §1 / PDR §5.4) and provided by the
`md-env` image:

| Component            | Choice                              |
|----------------------|-------------------------------------|
| Protein force field  | AMBER14SB (`PROTEIN_FORCE_FIELD`)   |
| Ligand force field   | GAFF2 (`LIGAND_FORCE_FIELD`)        |
| Ligand charges       | AM1-BCC (`LIGAND_CHARGE_METHOD`)    |
| Water model          | TIP3P (`WATER_MODEL`)               |
| MD engine            | GROMACS (GPU); mock fallback        |
| Ligand parameterize  | ACPYPE (`acpype -i lig_ref.sdf -c bcc -a gaff2`) |
| Bond-order assignment| RDKit `AssignBondOrdersFromTemplate` from the chemistry file (never from PDBQT alone) |

Standard GROMACS `.mdp` templates live in
`md-env/templates/gromacs/`:

- `ions.mdp` - minimal, for the genion `grompp`.
- `em.mdp` - steepest-descent minimization (50000 steps, `emtol` 1000).
- `nvt.mdp` - 100 ps NVT, V-rescale at 300 K, position restraints.
- `npt.mdp` - 100 ps NPT, Parrinello-Rahman at 1 bar, position restraints.
- `md.mdp` - production; `dt` 0.002 ps, `__NSTEPS__` and
  `__NSTXOUT_COMPRESSED__` placeholders substituted by the engine from
  `md_length_ns` and `TRAJECTORY_OUTPUT_PS`; two-group thermostat
  (`Protein_MOL` / `Water_and_ions`).

The proven reference recipe these encode is `preprocess_pipeline.sh` in the
parent repository; the worker generalizes it (no hardcoded molecule).

## Configuration

All settings come from `.env` (see `.env.example`). Key variables (CONTRACT §1):

- `APP_PORT` (default 8888) - host port mapped to the frontend.
- `DEFAULT_ADMIN_ID` / `DEFAULT_ADMIN_PASSWORD` - seeded admin (csbl/csbl).
- `DATABASE_URL` / `REDIS_URL` - in Docker, Postgres + Redis service URLs.
- `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` - Postgres credentials;
  `docker-compose.yml` interpolates these into both the `db` service and the
  backend `DATABASE_URL`, so they always agree. Defaults are non-secret
  placeholders for the quickstart; set real values in `.env` for any deployment.
- `MD_ENGINE` (`auto` | `gromacs` | `mock`), `QUEUE_BACKEND` (`auto` | `rq` | `local`).
- `NUM_GPUS` (`auto` or an integer), `GPU_ASSIGNMENT_MODE=one_job_per_gpu`.
- `JWT_SECRET`, `INTERNAL_API_TOKEN` - rotate before exposing the server.
- `DEFAULT_MD_LENGTH_NS` (50), `DEFAULT_TOP_N_POSES` (3), `TRAJECTORY_OUTPUT_PS` (100),
  `MAX_UPLOAD_SIZE_GB` (10), `RETENTION_DAYS` (30).

## Operations

```bash
make up        # build + start the stack
make down      # stop the stack
make logs      # follow all service logs
make health    # ./scripts/healthcheck.sh --gpus
make backup    # ./scripts/backup.sh (storage tar + pg_dump)
```

- `scripts/install.sh` - prerequisite checks, `.env` prep + secret rotation,
  `md-env` build, `docker compose up -d`, prints the access URL.
- `scripts/backup.sh` - timestamped tarball of `storage/`, `.env`, and a
  PostgreSQL dump under `backups/`.
- `scripts/healthcheck.sh` - probes `/api/health` and `/api/gpus` through the
  frontend proxy; `--gpus` adds host and in-container GPU diagnostics
  (informational; they do not change the pass/fail result).

## Security notes

For lab/internal operation (PDR §22):

- Passwords are stored as bcrypt hashes; never in plain text. First login forces
  a password change for the seeded admin.
- Run `scripts/install.sh` so placeholder secrets in `.env` are rotated; or set
  `JWT_SECRET`, `INTERNAL_API_TOKEN`, and `POSTGRES_PASSWORD` manually.
- `.env` is gitignored; keep it (and backups, which include `.env`) private.
- Upload files are validated by extension/type and size (`MAX_UPLOAD_SIZE_GB`).
  Download and job paths are constrained to the storage tree (no path traversal).
- Admin-only API routes are role-checked; login is rate-limited.
- Prefer internal-network operation. If exposing externally, terminate HTTPS and
  add authentication/IP restrictions at a reverse proxy in front of the frontend.
- Schedule regular backups (`scripts/backup.sh`) and set per-user storage quotas.

## Sample data -> a first job

`samples/` contains the canonical 3-HDC / KCCIVYP test case:

| File                              | Role in a job                                  |
|-----------------------------------|------------------------------------------------|
| `3-HDC_KCCIVYP.pdbqt`             | `pose_file` - 9 Vina poses with REMARK scores  |
| `Structure2D_3-HDC.sdf`           | `chemistry_file` - ligand chemistry (bond orders) |
| `fold_3_hdc_kccivyp_model_0.pdb`  | `receptor_file` - the KCCIVYP peptide receptor |

Run a first job through the UI:

1. Open `http://<server-ip>:8888`, log in `csbl/csbl`, change the password.
2. Go to Upload. Set the **pose file** to `3-HDC_KCCIVYP.pdbqt`, the
   **chemistry file** to `Structure2D_3-HDC.sdf`, and the **receptor file** to
   `fold_3_hdc_kccivyp_model_0.pdb`.
3. The platform auto-detects 9 poses and validates that the SDF chemistry maps
   onto the pose heavy atoms (the hard rule: chemistry must come from the SDF,
   never from the raw PDBQT).
4. Choose `ligand_type = small_molecule`, the default 50 ns length (or a preset),
   keep the top 3 poses, and submit. One subjob per pose is queued (one GPU per
   subjob).
5. Watch progress on the dashboard / job detail; when complete, view the
   analysis plots and 3D trajectory and download the per-pose and combined
   result archives.

Without GPUs/GROMACS, set `MD_ENGINE=mock` (the local-dev default) to run the
same flow end-to-end with synthetic-but-realistic artifacts.

## Acceptance checklist (PDR §29 / CONTRACT §12)

| # | Criterion | Where to verify |
|---|-----------|-----------------|
| 1 | Access on port 8888 | Open `http://<server-ip>:8888` |
| 2 | Login `csbl`/`csbl` | Login screen |
| 3 | Forced password change | First-login modal |
| 4 | Upload PDBQT + chemistry + receptor | Upload page (sample data) |
| 5 | Auto pose detection + top-N selection | Upload validation (9 poses; top 3) |
| 6 | SDF/MOL2 chemistry applied to poses | Validation atom-mapping result |
| 7 | SMILES/Meeko accepted only on mapping success | Upload validation |
| 8 | Small-molecule / peptide / protein-partner paths | Ligand-type selection |
| 9 | Heavy-atom composition mismatch rejected | Upload validation error |
| 10 | Per-pose subjobs queued | Dashboard queue table |
| 11 | Default 50 ns MD | Job parameters |
| 12 | Presets (quick/standard/extended/custom) | Upload presets |
| 13 | GROMACS + fixed force-field toolchain | Job logs / "Fixed MD toolchain" above |
| 14 | One GPU per job | GPU panel; `./scripts/healthcheck.sh --gpus` |
| 15 | Dashboard job + GPU status | Dashboard |
| 16 | Analysis graphs (RMSD/RMSF/Rg/SASA/H-bond/energy) | Results page |
| 17 | Trajectory/movie shows structural change | Results 3D viewer |
| 18 | Download all results | Results download buttons |
| 19 | Portable `docker compose` deployment | `docker compose up -d --build` |
