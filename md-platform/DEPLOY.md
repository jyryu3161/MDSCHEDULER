# Deploying the MD Platform on another server

Two paths. Pick by whether the target server has a GPU and needs **real GROMACS MD**.

---

## A. Turnkey (any server, no GPU) — recommended for "just bring it up"

Runs the **entire platform** in one container: web UI + backend + the `mdworker`
pipeline, with **real AutoDock Vina docking + the peptide-design GA**, and the
**mock MD engine** (synthetic trajectories + analysis). No GPU, GROMACS, Redis, or
Postgres required — only Docker.

```bash
git clone git@github.com:jyryu3161/MDSCHEDULER.git
cd MDSCHEDULER/md-platform
docker compose -f docker-compose.easy.yml up --build     # or: docker-compose -f ...
```

Open **http://SERVER:8888** → log in as `csbl` / `csbl` (you are forced to change the
password on first login).

State (SQLite DB + jobs) persists in the `md_storage` Docker volume. Stop with
`docker compose -f docker-compose.easy.yml down` (add `-v` to also wipe state).

**Before exposing it beyond localhost**, set real secrets (create a `.env` next to the
compose file, or export them):

```bash
APP_PORT=8888
JWT_SECRET=<a long random string>
DEFAULT_ADMIN_ID=<admin login>
DEFAULT_ADMIN_PASSWORD=<strong password>
NUM_GPUS=2            # placeholder GPU rows for the scheduler/UI (mock ignores real devices)
DESIGN_GPU_IDS=1      # which placeholder GPU is the peptide-design pool
DOCK_ENGINE=vina      # vina | smina | gnina | auto  (smina/gnina need their binaries)
```

What works here: upload docking results → MD (mock) → analysis/plots/3D viewer/results,
and the **Peptide Design** tab (real Vina docking → mock-refined ΔG GA). What it does
**not** do: real GROMACS MD or MM/GBSA ΔG (use path B for that).

Single image without compose:
```bash
docker build -f Dockerfile.allinone -t md-platform .
docker run -p 8888:8000 -v md_storage:/app/storage md-platform
```

---

## B. Full real MD (GPU server)

Real GROMACS EM/NVT/NPT/production + MM/GBSA, one worker per GPU. Requires the **NVIDIA
Container Toolkit** and building the heavy `md-env` GROMACS+AmberTools image.

Prerequisites:
```bash
# GPU passthrough must work:
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

Bring up (frontend + backend + Redis + Postgres + GPU workers):
```bash
cd MDSCHEDULER/md-platform
cp .env.example .env          # then edit secrets + POSTGRES_* + NUM_GPUS
docker build -t md-platform-mdenv:latest ./md-env   # GROMACS/AmberTools base (slow, multi-GB)
docker compose up --build
```

Open **http://SERVER:${APP_PORT:-8888}**. Match `docker-compose.yml`'s `worker-gpu-*`
services to the host's GPU count (see the header notes in that file), and set `NUM_GPUS`,
`MD_GPU_IDS`, `DESIGN_GPU_IDS`, `MD_GPU_CONCURRENCY` in `.env`.

The lab host that already has GROMACS + an AmberTools/acpype conda env can instead run the
backend directly with `MD_ENGINE=gromacs` after `source scripts/lab-gromacs-env.sh` (see that
script), avoiding the container GROMACS build.

---

## Notes

- `docker compose` (v2 plugin) and `docker-compose` (v1) are interchangeable above.
- Real **MM/GBSA ΔG** additionally needs `gmx_MMPBSA` + an MPI runtime (`ENABLE_MMPBSA=1`,
  `MMPBSA_MPI_LIB=...`); without it the design GA falls back to a docking-anchored ΔG estimate.
- `smina`/`gnina` docking engines require their binaries on PATH in the worker; otherwise use
  the default `vina` (bundled in path A).
