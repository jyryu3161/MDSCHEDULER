"""mdworker.analysis — trajectory analysis (numpy/scipy) + Plotly figure builders.

Pure-numpy implementations compute RMSD, RMSF, Rg, SASA (Shrake-Rupley-style estimate),
hydrogen bonds, ligand-receptor distance, a synthetic-yet-consistent energy series, ligand
stability, and a residue contact map from trajectory frames read with
mdworker.pipeline.structures.read_multimodel_pdb. MDAnalysis is used opportunistically when
available but is not required.
"""
