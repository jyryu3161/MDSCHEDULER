"""mdworker.io — import-light file parsers (PDBQT poses, receptor PDB/CIF).

These modules deliberately avoid RDKit and heavy dependencies so the backend can reuse
them in the upload-validate path without paying import costs.
"""
