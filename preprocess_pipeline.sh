#!/bin/bash
###############################################################################
# preprocess_pipeline_en.sh
#   AutoDock Vina 9-pose (pdbqt) -> ready for GROMACS MD production (npt.gro).
#   Single self-contained script covering the entire preprocessing workflow.
#
#   * Newly written for sharing/reproduction; does NOT touch md/scripts/*.
#   * Python tools (build_ligand / assemble_complex) are embedded as heredocs.
#   * Force field: protein amber14sb | ligand GAFF2+AM1-BCC | water TIP3P
#
#   Usage:
#     bash preprocess_pipeline_en.sh            # full run (steps 00-03, 9 poses)
#     bash preprocess_pipeline_en.sh setup      # CPU setup only (steps 00-02)
#     bash preprocess_pipeline_en.sh equil      # equilibration only (step 03)
#
#   One-time prerequisites (skip if already done):
#     - inputs/ : pose_*.pdbqt (or split from a combined pdbqt) and peptide pdb
#     - ligand parametrization (acpype): see STEP 2 (run once after lig_ref.sdf)
###############################################################################
set -euo pipefail

#==============================================================================
# [0] Common environment
#==============================================================================
export GMX_ROOT="/data2/home/dyoh/gromacs_2026"
export PROJ="$GMX_ROOT/md"
export MDP="$GMX_ROOT/mdp"
export INPUTS="$GMX_ROOT/inputs"
export COMMON="$PROJ/common"
export LIGDIR="$PROJ/ligand"
export RUNS="$PROJ/runs"

export FF="amber14sb"
export WATER="tip3p"
export GPU_ID="3"
NT="${NT:-8}"                       # number of OpenMP threads
PEPTIDE_PDB="$INPUTS/fold_3_hdc_kccivyp_model_0.pdb"

# External scripts (GMXRC, conda) reference unset vars -> relax nounset briefly
set +u
source "$GMX_ROOT/bin/GMXRC.bash"                            # standalone GROMACS
source /data2/home/dyoh/anaconda3/etc/profile.d/conda.sh    # ligand tools
conda activate gmx_env                                      # ambertools/acpype/rdkit
set -u
mkdir -p "$COMMON" "$RUNS"

MODE="${1:-all}"                    # all | setup | equil

#==============================================================================
# [1] Rebuild full-atom ligand (once)  pose_*.pdbqt -> pose_*_lig.pdb + lig_ref.sdf
#   pdbqt lacks nonpolar hydrogens and has poor bond info -> define the bond
#   graph in code, place only the heavy-atom coords from each pose, and let
#   RDKit add hydrogens. All 9 poses share the same atom order (key for reuse).
#==============================================================================
build_ligand() {
  echo "==== [1] Rebuild ligand structure (build_ligand) ===="
  cd "$LIGDIR"
  python - <<'PYEOF'
import sys
from rdkit import Chem
from rdkit.Chem import AllChem
from rdkit.Chem import rdMolDescriptors as desc
from rdkit.Geometry import Point3D

B = Chem.BondType

def build_template():
    rw = Chem.RWMol()
    for _ in range(23):           # idx 0..22 : C1..C23
        rw.AddAtom(Chem.Atom(6))
    rw.AddAtom(Chem.Atom(8))      # idx 23 : O24
    rw.AddAtom(Chem.Atom(8))      # idx 24 : O26
    bonds = [
        (0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7),(7,8),(8,9),(9,10),(10,11),(11,12),(12,13),
        (0,14),(14,15),(15,16),    # C1-C15-C16-C17
        (16,17),                   # C17 - ring ipso (C18)
    ]
    for a,b in bonds:
        rw.AddBond(a,b,B.SINGLE)
    rw.AddBond(17,18,B.DOUBLE); rw.AddBond(18,19,B.SINGLE)   # benzene ring (Kekule)
    rw.AddBond(19,20,B.DOUBLE); rw.AddBond(20,21,B.SINGLE)
    rw.AddBond(21,22,B.DOUBLE); rw.AddBond(22,17,B.SINGLE)
    rw.AddBond(18,23,B.SINGLE)    # O24 on C19
    rw.AddBond(19,24,B.SINGLE)    # O26 on C20
    m = rw.GetMol()
    Chem.SanitizeMol(m)
    return m

def read_pose_heavy(pdbqt):
    """Return heavy-atom (C/O) coords from pdbqt in file order -> matches template idx."""
    heavy = []
    with open(pdbqt) as fh:
        for line in fh:
            if line.startswith(("HETATM", "ATOM")):
                t = line.split()[-1]
                if t.startswith("H"):
                    continue
                x = float(line[30:38]); y = float(line[38:46]); z = float(line[46:54])
                heavy.append((x, y, z))
    return heavy

def make_pose(template, coords):
    m = Chem.Mol(template)
    conf = Chem.Conformer(m.GetNumAtoms())
    assert len(coords) == m.GetNumAtoms(), f"expected {m.GetNumAtoms()} heavy atoms, got {len(coords)}"
    for i, (x, y, z) in enumerate(coords):
        conf.SetAtomPosition(i, Point3D(x, y, z))
    m.AddConformer(conf, assignId=True)
    return Chem.AddHs(m, addCoords=True)        # place hydrogens geometrically

def main():
    tmpl = build_template()
    assert desc.CalcMolFormula(Chem.AddHs(tmpl)) == "C23H40O2"

    # clean conformer for parametrization
    ref = Chem.AddHs(Chem.Mol(tmpl))
    AllChem.EmbedMolecule(ref, randomSeed=1)
    AllChem.MMFFOptimizeMolecule(ref)
    Chem.MolToMolFile(ref, "lig_ref.sdf")
    print("[write] lig_ref.sdf", file=sys.stderr)

    # full-atom structures for all 9 poses
    for i in range(1, 10):
        coords = read_pose_heavy(f"pose_{i}.pdbqt")
        mh = make_pose(tmpl, coords)
        assert desc.CalcMolFormula(mh) == "C23H40O2", f"pose_{i} formula mismatch"
        Chem.MolToPDBFile(mh, f"pose_{i}_lig.pdb")
        print(f"[write] pose_{i}_lig.pdb ({mh.GetNumAtoms()} atoms)", file=sys.stderr)

if __name__ == "__main__":
    main()
PYEOF
  cd - >/dev/null
}

#==============================================================================
# [2] Parametrize ligand (acpype, once)  lig_ref.sdf -> LIG.acpype/
#   Skipped if LIG.acpype/LIG_GMX.itp already exists (reuse for same molecule).
#==============================================================================
parametrize_ligand() {
  if [ -f "$LIGDIR/LIG.acpype/LIG_GMX.itp" ]; then
    echo "==== [2] acpype output present -> reuse ===="
    return
  fi
  echo "==== [2] Parametrize ligand (acpype, GAFF2 + AM1-BCC) ===="
  cd "$LIGDIR"
  acpype -i lig_ref.sdf -c bcc -a gaff2 -n 0 -b LIG
  cd - >/dev/null
}

#==============================================================================
# [3] Split ligand topology  LIG_GMX.itp -> LIG_atomtypes.itp + LIG.itp
#   acpype itp bundles [atomtypes]+[moleculetype]; GROMACS requires them split.
#==============================================================================
prep_ligand_top() {
  echo "==== [3] Split ligand topology ===="
  local ACP="$LIGDIR/LIG.acpype"
  local SRC="$ACP/LIG_GMX.itp"
  [ -f "$SRC" ] || { echo "ERROR: $SRC missing (run acpype first)"; exit 1; }

  # [atomtypes] block only (goes right after the force field include)
  awk '/^\[ *atomtypes *\]/{f=1} f && /^\[ *moleculetype *\]/{f=0} f' "$SRC" > "$LIGDIR/LIG_atomtypes.itp"
  # everything from [moleculetype] onward (goes after the protein)
  awk '/^\[ *moleculetype *\]/{f=1} f' "$SRC" > "$LIGDIR/LIG.itp"

  cat >> "$LIGDIR/LIG.itp" <<'EOF'

; ligand position restraints (equilibration, -DPOSRES_LIG)
#ifdef POSRES_LIG
#include "posre_LIG.itp"
#endif
EOF
  cp "$ACP/posre_LIG.itp" "$LIGDIR/posre_LIG.itp"
}

#==============================================================================
# [4] Peptide topology  pdb -> pdb2gmx (amber14sb/tip3p)
#==============================================================================
prep_peptide() {
  echo "==== [4] Peptide topology (pdb2gmx) ===="
  local PEP="$COMMON/peptide.pdb"
  tr -d '\r' < "$PEPTIDE_PDB" | awk '/^ATOM|^TER|^END/' > "$PEP"     # strip CRLF + ATOM only
  cd "$COMMON"
  gmx pdb2gmx -f "$PEP" \
              -o peptide_processed.gro \
              -p topol.top \
              -i posre.itp \
              -ff "$FF" -water "$WATER" -ignh                # regenerate H; auto termini/disulfide
  cd - >/dev/null
}

#==============================================================================
# [5] Per-pose complex -> box -> solvate -> ions -> index
#   (5a) assemble_complex.py builds coordinates + topology
#   (5b) editconf / solvate / genion / make_ndx
#==============================================================================
assemble_one() {                    # $1 = pose, $2 = run dir
  PROJ="$PROJ" COMMON="$COMMON" LIGDIR="$LIGDIR" python - "$1" "$2" <<'PYEOF'
import os, shutil, sys

def read_gro_atoms(path):
    L = open(path).read().splitlines()
    n = int(L[1])
    return L[0], L[2:2+n], L[2+n]

def main():
    pose = int(sys.argv[1]); run = sys.argv[2]
    common = os.environ["COMMON"]; ligdir = os.environ["LIGDIR"]
    acp = os.path.join(ligdir, "LIG.acpype")
    os.makedirs(run, exist_ok=True)

    # --- coordinates: peptide .gro + ligand pose coords ---
    _, pep_atoms, pep_box = read_gro_atoms(os.path.join(common, "peptide_processed.gro"))
    _, lig_gro_atoms, _   = read_gro_atoms(os.path.join(acp, "LIG_GMX.gro"))   # for atom names

    lig_xyz = []
    for line in open(os.path.join(ligdir, f"pose_{pose}_lig.pdb")):
        if line.startswith(("ATOM", "HETATM")):
            lig_xyz.append((float(line[30:38]), float(line[38:46]), float(line[46:54])))
    assert len(lig_xyz) == len(lig_gro_atoms), \
        f"ligand atom count mismatch: pdb {len(lig_xyz)} vs gro {len(lig_gro_atoms)}"

    lig_resnum = int(pep_atoms[-1][0:5]) + 1
    out_atoms = list(pep_atoms)
    serial = len(pep_atoms)
    for (gro_line, (x, y, z)) in zip(lig_gro_atoms, lig_xyz):
        atomname = gro_line[10:15]
        serial += 1
        out_atoms.append("%5d%-5s%5s%5d%8.3f%8.3f%8.3f" %
                         (lig_resnum, "MOL", atomname, serial % 100000, x/10.0, y/10.0, z/10.0))

    with open(os.path.join(run, "complex.gro"), "w") as fh:
        fh.write(f"3-HDC complex pose {pose}\n{len(out_atoms)}\n")
        fh.write("\n".join(out_atoms) + "\n" + pep_box + "\n")
    print(f"  complex.gro: peptide {len(pep_atoms)} + ligand {len(lig_xyz)} = {len(out_atoms)} atoms")

    # --- topology: atomtypes after forcefield / moleculetype before solvent / molecules at end ---
    top = open(os.path.join(common, "topol.top")).read().splitlines()
    out = []; injected_at = injected_mol = False
    for line in top:
        out.append(line)
        if (not injected_at) and line.strip().startswith("#include") and "forcefield.itp" in line:
            out += ['; Include ligand atomtypes', '#include "LIG_atomtypes.itp"']
            injected_at = True
    final = []
    for line in out:
        if (not injected_mol) and line.strip().startswith("#include") and \
           (("tip3p" in line) or ("/ions.itp" in line) or ("water" in line.lower())):
            final += ['; Include ligand topology', '#include "LIG.itp"', '']
            injected_mol = True
        final.append(line)
    text = "\n".join(final)
    if not injected_mol:
        text = text.replace("[ system ]", '; Include ligand topology\n#include "LIG.itp"\n\n[ system ]', 1)
    if not text.endswith("\n"):
        text += "\n"
    text += "LIG                 1\n"                       # add ligand to [molecules]

    with open(os.path.join(run, "topol.top"), "w") as fh:
        fh.write(text)
    print("  topol.top: injected ligand atomtypes/moleculetype/molecules")

    for f in ["LIG_atomtypes.itp", "LIG.itp", "posre_LIG.itp"]:
        shutil.copy(os.path.join(ligdir, f), os.path.join(run, f))
    shutil.copy(os.path.join(common, "posre.itp"), os.path.join(run, "posre.itp"))

if __name__ == "__main__":
    main()
PYEOF
}

build_complex() {                   # $1 = pose
  local POSE="$1"
  local RUN="$RUNS/pose_$POSE"
  mkdir -p "$RUN"
  echo "==== [5] pose $POSE : complex setup ===="

  assemble_one "$POSE" "$RUN"       # (a) assemble
  cd "$RUN"

  # (b) box: rhombic dodecahedron, solute-wall distance >= 1.0 nm
  gmx editconf -f complex.gro -o boxed.gro -bt dodecahedron -d 1.0 -c
  # (c) solvate (TIP3P)
  gmx solvate -cp boxed.gro -cs spc216.gro -o solv.gro -p topol.top
  # (d) ions: neutralize + 0.15 M NaCl
  gmx grompp -f "$MDP/ions.mdp" -c solv.gro -p topol.top -o ions.tpr -maxwarn 2
  printf "SOL\n" | gmx genion -s ions.tpr -o solv_ions.gro -p topol.top \
          -pname NA -nname CL -neutral -conc 0.15
  # (e) index group for temperature coupling (Protein_MOL)
  printf "\"Protein\" | \"MOL\"\nq\n" | gmx make_ndx -f solv_ions.gro -o index.ndx

  cd - >/dev/null
}

#==============================================================================
# [6] Per-pose minimization + equilibration  EM -> NVT -> NPT  (uses GPU)
#==============================================================================
em_equil() {                        # $1 = pose
  local POSE="$1"
  local RUN="$RUNS/pose_$POSE"; cd "$RUN"
  export CUDA_VISIBLE_DEVICES="$GPU_ID"
  echo "==== [6] pose $POSE : EM -> NVT -> NPT ===="

  # energy minimization
  gmx grompp -f "$MDP/em.mdp" -c solv_ions.gro -p topol.top -o em.tpr -maxwarn 2
  gmx mdrun -v -deffnm em -ntmpi 1 -ntomp "$NT"

  # NVT 100 ps (300 K)
  gmx grompp -f "$MDP/nvt.mdp" -c em.gro -r em.gro -p topol.top -n index.ndx -o nvt.tpr -maxwarn 2
  gmx mdrun -v -deffnm nvt -ntmpi 1 -ntomp "$NT" -nb gpu -update gpu

  # NPT 100 ps (1 bar)
  gmx grompp -f "$MDP/npt.mdp" -c nvt.gro -r nvt.gro -t nvt.cpt -p topol.top -n index.ndx -o npt.tpr -maxwarn 2
  gmx mdrun -v -deffnm npt -ntmpi 1 -ntomp "$NT" -nb gpu -update gpu

  cd - >/dev/null
}

#==============================================================================
# Main flow
#==============================================================================
run_setup() {                       # CPU stage: ligand/peptide/9-pose setup
  build_ligand
  parametrize_ligand
  prep_ligand_top
  prep_peptide
  for P in 1 2 3 4 5 6 7 8 9; do build_complex "$P"; done
}

run_equil() {                       # GPU stage: equilibrate 9 poses
  for P in 1 2 3 4 5 6 7 8 9; do em_equil "$P"; done
}

case "$MODE" in
  setup) run_setup ;;
  equil) run_equil ;;
  all)   run_setup; run_equil ;;
  *) echo "Usage: bash preprocess_pipeline_en.sh [all|setup|equil]"; exit 1 ;;
esac

echo "================================================================"
echo "[done] mode=$MODE"
echo "  next step: 04_production.sh (GPU ${GPU_ID}, 50 ns x 9 poses)"
echo "================================================================"
