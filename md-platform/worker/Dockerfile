# Worker image: GROMACS + AmberTools + ACPYPE + RDKit (from md-env) + the mdworker package.
# Build context is the PROJECT ROOT (md-platform/) so this can COPY ./worker (see
# docker-compose.yml: build.context: ., dockerfile: worker/Dockerfile).
#
# The md-env base provides the scientific toolchain (GROMACS GPU build, AmberTools/acpype,
# RDKit, NumPy/SciPy/networkx, MDAnalysis). Build it first:
#   docker build -t md-platform-mdenv:latest ./md-env
FROM md-platform-mdenv:latest

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the mdworker package (with the rq extra for the queue worker).
COPY worker/ /app/worker/
RUN pip install "/app/worker[rq]"

# Storage + MD templates are bind-mounted by compose at /app/storage and /app/md-env.
# Each worker container pins its GPU via CUDA_VISIBLE_DEVICES / WORKER_GPU_ID (compose env).
WORKDIR /app/worker
CMD ["python", "worker.py"]
