#!/usr/bin/env bash
# Source this to run the worker against a HOST GROMACS install (no Docker), e.g. on the CSBL
# lab server which already has GROMACS 2026.2 (CUDA) + an AmberTools/acpype conda env.
#
#   source scripts/lab-gromacs-env.sh
#   MD_ENGINE=gromacs python3 -m mdworker.tasks <subjob_id>     # or run the backend with these
#
# Override the two locations for a different machine:
#   GROMACS_ROOT  : dir containing bin/GMXRC.bash + share/gromacs/top/amber14sb.ff
#   AMBERTOOLS_ENV: conda env prefix providing acpype / antechamber / sqm / tleap / parmchk2
#
# gmx_env/bin is APPENDED to PATH (not prepended) so the caller's python3 (which must have the
# mdworker + rdkit + networkx deps) stays primary, while acpype/antechamber still resolve.
#
# Temporarily relax `nounset` while sourcing GMXRC/conda scripts (they reference unset vars),
# then RESTORE the caller's prior setting so we don't mutate their shell state.
_mdenv_had_nounset=0
case $- in *u*) _mdenv_had_nounset=1 ;; esac
set +u
GROMACS_ROOT="${GROMACS_ROOT:-/data2/home/dyoh/gromacs_2026}"
AMBERTOOLS_ENV="${AMBERTOOLS_ENV:-/data2/home/dyoh/anaconda3/envs/gmx_env}"

if [ -f "$GROMACS_ROOT/bin/GMXRC.bash" ]; then
  source "$GROMACS_ROOT/bin/GMXRC.bash"
else
  echo "WARN: $GROMACS_ROOT/bin/GMXRC.bash not found; set GROMACS_ROOT." >&2
fi
export AMBERHOME="$AMBERTOOLS_ENV"
export PATH="$PATH:$AMBERTOOLS_ENV/bin"
export GMXLIB="${GMXLIB:-$GROMACS_ROOT/share/gromacs/top}"   # holds amber14sb.ff
export MDP_TEMPLATE_DIR="${MDP_TEMPLATE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)/md-env/templates/gromacs}"
# OPT-IN gmx_MMPBSA support: only when ENABLE_MMPBSA=1, so a normal MD run never has its
# LD_LIBRARY_PATH (and thus MPI/library resolution for other tools) altered. gmx_MMPBSA imports
# mpi4py, which needs an MPI runtime (libmpi.so.40); point MMPBSA_MPI_LIB at an OpenMPI lib dir.
if [ "${ENABLE_MMPBSA:-0}" = "1" ] && [ -n "${MMPBSA_MPI_LIB:-}" ] && [ -e "$MMPBSA_MPI_LIB/libmpi.so.40" ]; then
  export LD_LIBRARY_PATH="$MMPBSA_MPI_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
unset _MPI_LIB 2>/dev/null || true
# Restore the caller's original nounset setting (don't force it on/off).
if [ "$_mdenv_had_nounset" -eq 1 ]; then set -u; else set +u; fi
unset _mdenv_had_nounset

command -v gmx >/dev/null 2>&1 && echo "gmx: $(command -v gmx)"
command -v acpype >/dev/null 2>&1 && echo "acpype: $(command -v acpype)"
