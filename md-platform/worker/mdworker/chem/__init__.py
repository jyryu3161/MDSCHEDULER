"""mdworker.chem — RDKit-backed chemistry (template loading, atom-mapping bond transfer).

RDKit is imported lazily inside functions so importing this package is cheap; callers that
never touch chemistry never trigger the RDKit import.
"""
