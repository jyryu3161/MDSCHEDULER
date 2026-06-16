# =============================================================================
# md-env: the MD scientific base image for the MD Platform worker.
#
# Provides the fixed toolchain (CONTRACT §1 / PDR §5.4):
#   protein force field  : AMBER14SB
#   ligand force field   : GAFF2
#   ligand charges       : AM1-BCC
#   water model          : TIP3P
#
# Contents:
#   - Miniconda env `mdtools`: AmberTools, ACPYPE, RDKit, MDAnalysis, NumPy, SciPy.
#   - GROMACS compiled with GPU (CUDA) support.
#
# Multi-stage design (REQUIRED for the GPU build):
#   Stage `builder`  FROM nvidia/cuda:12.4.1-devel-ubuntu22.04  (has nvcc/toolkit)
#       -> installs conda env + compiles CUDA GROMACS into /opt/gromacs
#   Stage final      FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04 (smaller runtime)
#       -> copies /opt/conda and /opt/gromacs from builder; runtime libs only.
# A CUDA *runtime* base lacks nvcc and CANNOT compile CUDA GROMACS, hence the
# separate devel builder stage.
#
# Build & tag (see scripts/install.sh and the project Makefile):
#   docker build -t md-platform-mdenv:latest ./md-env
#
# The worker image (worker/Dockerfile, owned by the worker component) is built
# FROM md-platform-mdenv:latest and adds the `mdworker` Python package.
#
# NOTE ON SIZE / BUILD TIME: the GROMACS GPU build is a from-source CUDA compile
# and is HEAVY (tens of minutes, multi-GB image). To build a lighter
# mock-only image pass `--build-arg GROMACS_BUILD=OFF`; the platform still runs
# end-to-end with MD_ENGINE=mock when GROMACS is absent (CONTRACT §9).
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: builder  (CUDA devel base -> has nvcc + CUDA toolkit for the compile)
# -----------------------------------------------------------------------------
FROM nvidia/cuda:12.4.1-devel-ubuntu22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PATH=/opt/conda/bin:$PATH \
    GMX_ROOT=/opt/gromacs \
    CONDA_ENV=mdtools

# Build/runtime dependencies for the GROMACS compile and conda packages.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        curl \
        git \
        libfftw3-dev \
        ninja-build \
        wget \
    && rm -rf /var/lib/apt/lists/*

# Miniconda (pinned version + SHA256 verified).
# NOTE: MINICONDA_SHA256 must match the pinned installer. Verify/refresh against
# https://repo.anaconda.com/miniconda/ (the published sha256) when bumping
# MINICONDA_VERSION; an empty/placeholder value will fail `sha256sum -c` by
# design so a stale checksum cannot pass silently.
ARG MINICONDA_VERSION=py310_24.5.0-0
ARG MINICONDA_SHA256=4b3b3b1b99215e85fd73fb2c2d7ebf2dbf3f5b5d6e2f5cdb23e8eebd5805f4dd
RUN set -eux; \
    curl -fsSL "https://repo.anaconda.com/miniconda/Miniconda3-${MINICONDA_VERSION}-Linux-x86_64.sh" -o /tmp/miniconda.sh; \
    echo "${MINICONDA_SHA256}  /tmp/miniconda.sh" | sha256sum -c -; \
    bash /tmp/miniconda.sh -b -p /opt/conda; \
    rm /tmp/miniconda.sh; \
    /opt/conda/bin/conda config --system --set always_yes true; \
    /opt/conda/bin/conda config --system --set channel_priority strict; \
    /opt/conda/bin/conda config --system --add channels conda-forge

# Create the `mdtools` env. md-env/conda-linux-64.lock is always present in the
# build context (committed as a comment-only sentinel by default). If it is a
# REAL explicit lock (contains an "@EXPLICIT" line, generated via conda-lock;
# see environment.yml), build from it for a transitively-reproducible env.
# Otherwise fall back to the version-pinned environment.yml (direct packages
# pinned; solver resolves transitive deps at build time).
COPY environment.yml /tmp/environment.yml
COPY conda-linux-64.lock /tmp/conda-linux-64.lock
RUN set -eux; \
    if grep -q '^@EXPLICIT' /tmp/conda-linux-64.lock; then \
        echo "Using explicit lock file (transitively reproducible)"; \
        conda create -n ${CONDA_ENV} --file /tmp/conda-linux-64.lock; \
    else \
        echo "No explicit lock; using version-pinned environment.yml"; \
        conda env create -f /tmp/environment.yml; \
    fi; \
    conda clean -afy; \
    rm -f /tmp/environment.yml /tmp/conda-linux-64.lock

# -----------------------------------------------------------------------------
# GROMACS with GPU (CUDA) support  --  HEAVY BUILD (clearly marked).
# Compiles GROMACS from source against CUDA into ${GMX_ROOT}. Skip with
# --build-arg GROMACS_BUILD=OFF for a mock-only image.
# -----------------------------------------------------------------------------
ARG GROMACS_VERSION=2024.3
ARG GROMACS_BUILD=ON
# SHA256 of gromacs-${GROMACS_VERSION}.tar.gz. Verify/refresh against the
# published checksum at https://manual.gromacs.org/ / ftp.gromacs.org when
# bumping GROMACS_VERSION; a stale value fails `sha256sum -c` by design.
ARG GROMACS_SHA256=cdd95b2cf78fbe9d5d0e9c7c2cb7eb53ee7fd5f1d11c70f4a4d6e7a6f43d2c9b

RUN if [ "$GROMACS_BUILD" = "ON" ]; then \
        set -eux; \
        cd /tmp; \
        wget -q "https://ftp.gromacs.org/gromacs/gromacs-${GROMACS_VERSION}.tar.gz"; \
        echo "${GROMACS_SHA256}  gromacs-${GROMACS_VERSION}.tar.gz" | sha256sum -c -; \
        tar xf "gromacs-${GROMACS_VERSION}.tar.gz"; \
        cd "gromacs-${GROMACS_VERSION}"; \
        mkdir build && cd build; \
        cmake .. \
            -DGMX_BUILD_OWN_FFTW=ON \
            -DGMX_GPU=CUDA \
            -DGMX_MPI=OFF \
            -DGMX_DOUBLE=OFF \
            -DCMAKE_INSTALL_PREFIX=${GMX_ROOT} \
            -DGMX_DEFAULT_SUFFIX=ON \
            -DCMAKE_BUILD_TYPE=Release; \
        make -j"$(nproc)"; \
        make install; \
        cd /tmp; rm -rf "gromacs-${GROMACS_VERSION}" "gromacs-${GROMACS_VERSION}.tar.gz"; \
    else \
        echo "GROMACS build skipped (GROMACS_BUILD=$GROMACS_BUILD): worker will use MD_ENGINE=mock"; \
        mkdir -p ${GMX_ROOT}/bin; \
    fi

# =============================================================================
# Stage 2: final  (CUDA runtime base -> smaller; no compilers)
# =============================================================================
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

LABEL org.opencontainers.image.title="md-platform-mdenv" \
      org.opencontainers.image.description="MD Platform scientific base: GROMACS(GPU) + AmberTools + ACPYPE + RDKit + MDAnalysis" \
      maintainer="csbl"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    GMX_ROOT=/opt/gromacs \
    CONDA_ENV=mdtools

# Runtime libraries GROMACS and the chemistry stack need at execution time.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        libfftw3-3 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Bring the compiled GROMACS install and the conda env over from the builder.
COPY --from=builder /opt/conda /opt/conda
COPY --from=builder /opt/gromacs /opt/gromacs

# Put the conda env and gmx on PATH so the worker's auto-detect works:
#   `gmx` on PATH -> real GROMACS engine; absent -> mock engine.
ENV PATH=/opt/conda/envs/${CONDA_ENV}/bin:${GMX_ROOT}/bin:/opt/conda/bin:$PATH \
    CONDA_DEFAULT_ENV=${CONDA_ENV} \
    LD_LIBRARY_PATH=${GMX_ROOT}/lib:${GMX_ROOT}/lib64:$LD_LIBRARY_PATH

# Convenience profile snippet (interactive shells).
RUN if [ -f "${GMX_ROOT}/bin/GMXRC" ]; then echo "source ${GMX_ROOT}/bin/GMXRC" > /etc/profile.d/gromacs.sh; fi

# -----------------------------------------------------------------------------
# GROMACS bundles its own force-field data (amber14sb.ff, tip3p) so no extra
# copy is required for the standard toolchain. md-env/forcefields/ is mounted /
# available for any additional/override force fields a deployment supplies.
# -----------------------------------------------------------------------------
WORKDIR /app

# Sanity surface for `docker run md-platform-mdenv:latest`.
CMD ["bash", "-lc", "python -c 'import rdkit, MDAnalysis, numpy, scipy; print(\"chem stack OK\")'; (command -v gmx && gmx --version | head -1) || echo 'gmx not present (mock engine)'"]
