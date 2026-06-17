"""Automatic publication-style HTML reports for finished MD and design (GA) jobs.

A report bundles, in one self-contained HTML file: a publication Methods section + an exact
conditions table, the run's results with interpretation, the interactive result figures, and an
embedded trajectory viewer. The narrative prose is written by Gemini (gemini-3.5-flash by
default); if the model/key is unavailable the report still builds from deterministic templates,
so a finished job ALWAYS gets a report and report generation never fails the job.
"""
