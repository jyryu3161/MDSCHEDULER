"""Build self-contained publication-style HTML reports for MD and design (GA) jobs.

Narrative prose (Methods, Results & interpretation, per-figure notes, limitations) is written by
Gemini; numbers come ONLY from the run's own artifacts and are also shown in deterministic tables
that are the ground truth. Figures are embedded as interactive Plotly (publication theme already
applied upstream); the MD trajectory is embedded inline and animated with 3Dmol.js. If Gemini is
unavailable the report still builds from templates — it never fails the job.
"""

from __future__ import annotations

import html as _html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import gemini

# Logical display order + human titles for the MD result figures.
_FIGURE_ORDER: List[Tuple[str, str]] = [
    ("rmsd", "RMSD vs time"),
    ("ligand_rmsd", "Ligand RMSD (binding-pose stability)"),
    ("rg", "Radius of gyration"),
    ("rmsf", "Per-residue RMSF"),
    ("hbond", "Protein–ligand hydrogen bonds"),
    ("contact_map", "Protein–ligand contact frequency"),
    ("distance", "Ligand–key-residue distances"),
    ("sasa", "Solvent-accessible surface area"),
    ("energy", "Potential energy"),
    ("per_residue", "Per-residue binding ΔG (MM-GBSA)"),
]

_MAX_TRAJ_BYTES = 12 * 1024 * 1024  # cap inline trajectory at 12 MB to keep the HTML openable


def _esc(v: Any) -> str:
    return _html.escape("" if v is None else str(v), quote=True)


def _fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _safe_json(obj: Any) -> str:
    """JSON for inline <script> embedding (neutralize any </script>)."""
    return json.dumps(obj).replace("</", "<\\/")


# ───────────────────────────── MD report ─────────────────────────────

def _md_facts(ctx, settings, md: Dict[str, Any]) -> Dict[str, Any]:
    """Exact simulation conditions for the Methods table + the Gemini prompt."""
    ff = getattr(settings, "protein_force_field", "amber19sb")
    water = getattr(settings, "water_model", "opc")
    requested_ff = ff
    ffj = ctx.md_dir / "forcefield.json"
    if ffj.exists():
        try:
            d = json.loads(ffj.read_text())
            ff = d.get("protein_force_field", ff)
            water = d.get("water_model", water)
            requested_ff = d.get("requested_force_field", requested_ff)
        except (OSError, ValueError):
            pass
    jm = ctx.job_meta or {}
    nvt_ps = int(getattr(settings, "nvt_steps", 50000)) * 0.002
    npt_ps = int(getattr(settings, "npt_steps", 125000)) * 0.002
    return {
        "engine": md.get("engine", getattr(settings, "resolved_engine", "gromacs")),
        "protein_ff": ff,
        "protein_ff_requested": requested_ff,
        "water_model": water,
        "ligand_ff": getattr(settings, "ligand_force_field", "gaff2"),
        "ligand_charges": getattr(settings, "ligand_charge_method", "am1bcc"),
        "ligand_type": ctx.ligand_type,
        "md_length_ns": ctx.md_length_ns,
        "timestep_fs": 2.0,
        "temperature_K": jm.get("temperature", 300),
        "pressure_bar": jm.get("pressure", 1.0),
        "salt_M": jm.get("salt_concentration", 0.15),
        "box_type": jm.get("box_type", "dodecahedron"),
        "box_padding_nm": getattr(settings, "box_padding_nm", 1.2),
        "nvt_ps": nvt_ps,
        "npt_ps": npt_ps,
        "cutoff_nm": 1.0,
        "electrostatics": "PME",
        "thermostat": "V-rescale",
        "barostat": "Parrinello-Rahman",
        "constraints": "h-bonds (LINCS)",
        "dispersion_correction": "EnerPres",
        "n_frames": md.get("n_frames"),
        "completed_ns": md.get("completed_ns"),
        "ns_per_day": md.get("ns_per_day"),
    }


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None
    return None


def _md_prompt(facts: Dict[str, Any], metrics: Dict[str, Any], mmpbsa: Optional[Dict[str, Any]],
               figures: List[Tuple[str, str]]) -> str:
    fig_keys = [k for k, _ in figures]
    payload = {
        "conditions": facts,
        "metrics": metrics,
        "mmpbsa": mmpbsa or "not computed",
        "available_figures": fig_keys,
    }
    return (
        "You are a computational chemist writing the figures-and-methods part of a paper about a "
        "molecular-dynamics study of a protein/peptide–ligand complex (a docked pose refined by "
        "explicit-solvent MD; binding assessed by pose stability and relative MM-PBSA/GBSA ΔG).\n\n"
        "Using ONLY the data below (do not invent numbers, residues, or conditions), return a JSON "
        "object with EXACTLY these string keys:\n"
        '  "methods": a publication-ready Methods paragraph (third person, past tense) describing the '
        "force fields, water model, box/ions, equilibration, and production settings from `conditions`.\n"
        '  "results": 2–4 sentences interpreting the run from `metrics`/`mmpbsa` — is the pose stable, '
        "what do RMSD/contacts/ΔG indicate. State MM-PBSA/GBSA values are a RELATIVE ranking score, not "
        "an absolute affinity, and flag low reliability if pose_occupancy < 0.5.\n"
        '  "figures": an object mapping each key in available_figures to a ONE-sentence interpretation.\n'
        '  "limitations": one short sentence on caveats (e.g. single trajectory, estimate-sourced '
        "metrics if any data_source says 'estimate').\n\n"
        "Be precise and sober; no marketing language. DATA:\n" + json.dumps(payload, default=str)
    )


def _md_fallback(facts: Dict[str, Any], metrics: Dict[str, Any],
                 mmpbsa: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Deterministic narrative used when Gemini is unavailable."""
    methods = (
        f"Molecular dynamics was performed in GROMACS ({facts['engine']} engine) with the AMBER "
        f"{facts['protein_ff']} protein force field and the {facts['water_model'].upper()} water model; "
        f"the ligand was parameterized with {facts['ligand_ff'].upper()} and {facts['ligand_charges'].upper()} "
        f"charges. The complex was solvated in a {facts['box_type']} box ({facts['box_padding_nm']} nm "
        f"padding) and neutralized with {facts['salt_M']} M NaCl. After energy minimization the system was "
        f"equilibrated for {facts['nvt_ps']:.0f} ps NVT and {facts['npt_ps']:.0f} ps NPT with position "
        f"restraints, followed by {facts['md_length_ns']:g} ns production at {facts['timestep_fs']:.0f} fs, "
        f"{facts['temperature_K']} K ({facts['thermostat']}) and {facts['pressure_bar']} bar "
        f"({facts['barostat']}), using PME with {facts['cutoff_nm']} nm cutoffs, h-bond constraints, and "
        f"dispersion correction."
    )
    stable = metrics.get("ligand_stable")
    verdict = ("the ligand remained bound" if stable else
               "the ligand was mobile/dissociated" if stable is not None else "stability was not determined")
    results = (
        f"Over {facts['md_length_ns']:g} ns the backbone RMSD averaged "
        f"{_fmt(metrics.get('backbone_rmsd_mean_A'))} Å and the ligand RMSD "
        f"{_fmt(metrics.get('ligand_rmsd_mean_A'))} Å, indicating {verdict}."
    )
    if mmpbsa and not mmpbsa.get("skipped"):
        gb = mmpbsa.get("gbsa_dg_kcal_mol")
        results += (f" MM-GBSA binding ΔG was {_fmt(gb)} kcal/mol (relative ranking score, not an "
                    f"absolute affinity; pose occupancy {_fmt(mmpbsa.get('pose_occupancy'))}).")
    return {
        "methods": methods,
        "results": results,
        "figures": {},
        "limitations": ("Single-trajectory estimate; MM-PBSA/GBSA values are for relative ranking only. "
                        "SASA and energy are estimates under the mock engine."),
    }


def build_md_report(ctx, settings, md: Dict[str, Any], analysis: Dict[str, Any]) -> str:
    facts = _md_facts(ctx, settings, md)
    summary = _read_json(ctx.analysis_dir / "summary.json") or (analysis or {})
    metrics = summary.get("metrics", {}) if isinstance(summary, dict) else {}
    data_source = summary.get("data_source", {}) if isinstance(summary, dict) else {}
    bound_window = summary.get("bound_window") if isinstance(summary, dict) else None
    mmpbsa = _read_json(ctx.analysis_dir / "mmpbsa.json")

    # Figures present on disk, in logical order.
    figs: List[Tuple[str, str, Dict[str, Any]]] = []
    for key, title in _FIGURE_ORDER:
        fig = _read_json(ctx.plots_dir / f"{key}.json")
        if fig and fig.get("data"):
            figs.append((key, title, fig))
    fig_titles = [(k, t) for k, t, _ in figs]

    narrative = gemini.generate_json(
        _md_prompt(facts, {**metrics, "data_source": data_source}, mmpbsa, fig_titles),
        settings=settings) or _md_fallback(facts, metrics, mmpbsa)

    traj = ctx.viz_dir / "trajectory.pdb"
    header = {
        "title": f"MD report — {ctx.job_id} · pose {ctx.pose_index}"
                 + (f" · replica {ctx.replica_index}" if getattr(ctx, "replica_index", 1) > 1 else ""),
        "subtitle": f"{facts['ligand_type']} ligand · {facts['md_length_ns']:g} ns · "
                    f"{facts['protein_ff']} / {facts['water_model'].upper()} · engine {facts['engine']}",
    }
    metric_rows = [
        ("Backbone RMSD (mean)", f"{_fmt(metrics.get('backbone_rmsd_mean_A'))} Å"),
        ("Ligand RMSD (mean / final)",
         f"{_fmt(metrics.get('ligand_rmsd_mean_A'))} / {_fmt(metrics.get('ligand_rmsd_final_A'))} Å"),
        ("Radius of gyration (mean)", f"{_fmt(metrics.get('rg_mean_A'))} Å"),
        ("H-bonds (mean)", _fmt(metrics.get("hbond_mean"))),
        ("Ligand verdict", "stable" if metrics.get("ligand_stable") else
            ("mobile" if metrics.get("ligand_stable") is not None else "n/a")),
    ]
    if mmpbsa and not mmpbsa.get("skipped"):
        metric_rows.append(("MM-GBSA ΔG (relative)", f"{_fmt(mmpbsa.get('gbsa_dg_kcal_mol'))} kcal/mol"))
        metric_rows.append(("MM-PBSA ΔG (relative)", f"{_fmt(mmpbsa.get('pbsa_dg_kcal_mol'))} kcal/mol"))
        metric_rows.append(("Pose occupancy", _fmt(mmpbsa.get("pose_occupancy"))))
    return _assemble_html(header, _md_conditions_rows(facts), narrative, figs, metric_rows,
                          traj if traj.exists() else None,
                          extra_caveats=_estimate_caveats(data_source))


def _md_conditions_rows(facts: Dict[str, Any]) -> List[Tuple[str, str]]:
    return [
        ("Engine", str(facts["engine"])),
        ("Protein force field", str(facts["protein_ff"])
            + ("" if facts["protein_ff"] == facts["protein_ff_requested"]
               else f" (requested {facts['protein_ff_requested']})")),
        ("Water model", str(facts["water_model"]).upper()),
        ("Ligand parameters", f"{facts['ligand_ff'].upper()} / {facts['ligand_charges'].upper()}"),
        ("Box", f"{facts['box_type']}, {facts['box_padding_nm']} nm padding"),
        ("Ions", f"neutralized + {facts['salt_M']} M NaCl"),
        ("Equilibration", f"EM → {facts['nvt_ps']:.0f} ps NVT → {facts['npt_ps']:.0f} ps NPT (restrained)"),
        ("Production", f"{facts['md_length_ns']:g} ns @ {facts['timestep_fs']:.0f} fs"),
        ("Thermostat / barostat",
         f"{facts['thermostat']} {facts['temperature_K']} K / {facts['barostat']} {facts['pressure_bar']} bar"),
        ("Electrostatics / cutoff",
         f"{facts['electrostatics']}, {facts['cutoff_nm']} nm; {facts['constraints']}; DispCorr {facts['dispersion_correction']}"),
        ("Frames analyzed", _fmt(facts.get("n_frames"), 0)),
    ]


def _estimate_caveats(data_source: Dict[str, Any]) -> List[str]:
    est = [k for k, v in (data_source or {}).items() if isinstance(v, str) and "estimate" in v]
    if est:
        return [f"The following are estimates (not parsed from GROMACS output): {', '.join(sorted(est))}."]
    return []


# ───────────────────────────── design (GA) report ─────────────────────────────

def _design_facts(config: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    pop = int(config.get("population_size", 10))
    over = max(1, int(config.get("dock_oversample", 4)))
    eval_mode = str(config.get("eval_mode", "hybrid"))
    return {
        "eval_mode": eval_mode,
        "population_size": pop,
        "num_generations": int(config.get("num_generations", 5)),
        "dock_oversample": over,
        "dock_pool_per_gen": pop * over if eval_mode == "hybrid" else pop,
        "md_per_gen": pop,
        "dock_engine": str(settings.get("DOCK_ENGINE", "vina")),
        "md_engine": str(settings.get("MD_ENGINE", "mock")),
        "md_length_ns": float(config.get("md_length_ns", 10.0)),
        "n_replicas": int(config.get("n_replicas", 1) or 1),
        "exhaustiveness": int(config.get("exhaustiveness", 16)),
        "protein_ff": settings.get("PROTEIN_FORCE_FIELD", "amber19sb"),
        "water_model": settings.get("WATER_MODEL", "opc"),
        "ligand_ff": settings.get("LIGAND_FORCE_FIELD", "gaff2"),
    }


def _design_prompt(facts: Dict[str, Any], result: Dict[str, Any]) -> str:
    gens = result.get("generations", [])
    payload = {
        "design_setup": facts,
        "best_sequence": result.get("best_sequence"),
        "best_fitness": result.get("best_fitness"),
        "best_docking_score": result.get("best_docking_score"),
        "best_md_dg": result.get("best_md_dg"),
        "n_generations_run": len(gens),
        "convergence": [{"generation": g.get("generation"), "best_fitness": g.get("best_fitness")}
                        for g in gens],
    }
    return (
        "You are a computational chemist writing up a genetic-algorithm peptide-design run that "
        "screened candidates by docking and refined the best by MD + MM-GBSA. Using ONLY the data "
        "below (invent nothing), return a JSON object with EXACTLY these string keys:\n"
        '  "methods": a publication-ready Methods paragraph describing the GA (population, generations, '
        "docking oversample/MD-refinement scheme), docking engine, and MD/force-field settings from "
        "`design_setup`.\n"
        '  "results": 2–4 sentences on the outcome — the best peptide, its fitness/ΔG, and whether the '
        "GA converged (from `convergence`). MM-GBSA ΔG is a relative ranking score, not absolute affinity.\n"
        '  "figures": an object with key "convergence" mapping to a one-sentence interpretation.\n'
        '  "limitations": one short sentence on caveats.\n\nDATA:\n' + json.dumps(payload, default=str)
    )


def _design_fallback(facts: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    methods = (
        f"Peptide design used a genetic algorithm (population {facts['population_size']}, "
        f"{facts['num_generations']} generations). In each generation "
        f"{facts['dock_pool_per_gen']} candidates were screened by {facts['dock_engine']} docking "
        f"(exhaustiveness {facts['exhaustiveness']}) and the top {facts['md_per_gen']} were refined by "
        f"{facts['md_engine']} MD ({facts['md_length_ns']:g} ns, {facts['n_replicas']} replica(s)) with "
        f"the {facts['protein_ff']}/{facts['water_model'].upper()} force field and MM-GBSA scoring."
    )
    results = (
        f"The best peptide was {result.get('best_sequence')} (fitness "
        f"{_fmt(result.get('best_fitness'))}; docking {_fmt(result.get('best_docking_score'))}; "
        f"MM-GBSA ΔG {_fmt(result.get('best_md_dg'))} kcal/mol, a relative ranking score)."
    )
    return {"methods": methods, "results": results, "figures": {},
            "limitations": "Relative ranking only; designs warrant experimental validation."}


def build_design_report(workdir: Path, config: Dict[str, Any], settings: Dict[str, Any],
                        result: Dict[str, Any]) -> str:
    facts = _design_facts(config, settings)
    narrative = gemini.generate_json(_design_prompt(facts, result), settings=settings) \
        or _design_fallback(facts, result)

    gens = result.get("generations", [])
    conv_fig = None
    if gens:
        from mdworker.analysis import theme
        x = [g.get("generation") for g in gens]
        conv_fig = theme.figure(
            [{"x": x, "y": [g.get("best_fitness") for g in gens], "type": "scatter",
              "mode": "lines+markers", "name": "Best fitness", "line": {"color": theme.PALETTE[0]}}],
            "Design convergence", "Generation", "Best fitness (−energy)")
    figs = [("convergence", "Design convergence", conv_fig)] if conv_fig else []

    header = {
        "title": f"Peptide design report — {workdir.name}",
        "subtitle": f"GA {facts['population_size']}×{facts['num_generations']} · "
                    f"{facts['eval_mode']} · dock {facts['dock_engine']} · MD {facts['md_engine']}",
    }
    cond_rows = [
        ("Evaluation mode", facts["eval_mode"]),
        ("Population × generations", f"{facts['population_size']} × {facts['num_generations']}"),
        ("Docking screen", f"{facts['dock_pool_per_gen']}/gen ({facts['dock_engine']}, "
                            f"exhaustiveness {facts['exhaustiveness']}) → MD top {facts['md_per_gen']}"),
        ("MD refinement", f"{facts['md_engine']}, {facts['md_length_ns']:g} ns, "
                          f"{facts['n_replicas']} replica(s)"),
        ("Force field", f"{facts['protein_ff']}/{facts['water_model'].upper()}, ligand {facts['ligand_ff'].upper()}"),
    ]
    metric_rows = [
        ("Best peptide", str(result.get("best_sequence"))),
        ("Best fitness", _fmt(result.get("best_fitness"))),
        ("Best docking score", f"{_fmt(result.get('best_docking_score'))} kcal/mol"),
        ("Best MM-GBSA ΔG (relative)", f"{_fmt(result.get('best_md_dg'))} kcal/mol"),
        ("Generations run", _fmt(len(gens), 0)),
    ]
    return _assemble_html(header, cond_rows, narrative, figs, metric_rows, trajectory=None,
                          extra_caveats=[])


# ───────────────────────────── HTML assembly ─────────────────────────────

def _assemble_html(header: Dict[str, str], conditions: List[Tuple[str, str]],
                   narrative: Dict[str, Any], figures: List[Tuple[str, str, Dict[str, Any]]],
                   metrics: List[Tuple[str, str]], trajectory: Optional[Path],
                   extra_caveats: List[str]) -> str:
    def table(rows: List[Tuple[str, str]]) -> str:
        return ("<table>" + "".join(
            f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in rows) + "</table>")

    fig_notes = narrative.get("figures", {}) if isinstance(narrative.get("figures"), dict) else {}
    fig_blocks = []
    fig_scripts = []
    for i, (key, title, fig) in enumerate(figures):
        div_id = f"fig{i}"
        note = fig_notes.get(key)
        fig_blocks.append(
            f"<figure><div class='plot' id='{div_id}'></div>"
            f"<figcaption><b>{_esc(title)}.</b> {_esc(note) if note else ''}</figcaption></figure>")
        fig_scripts.append(
            f"Plotly.newPlot('{div_id}', {_safe_json(fig.get('data', []))}, "
            f"{_safe_json(fig.get('layout', {}))}, {{responsive:true, displaylogo:false}});")

    traj_section = ""
    traj_script = ""
    if trajectory is not None:
        try:
            size = trajectory.stat().st_size
        except OSError:
            size = _MAX_TRAJ_BYTES + 1
        if size <= _MAX_TRAJ_BYTES:
            # Neutralize any script terminator so a malformed artifact cannot break out of the
            # <script> container. Valid PDB has no "</", so textContent is byte-identical and 3Dmol
            # parses it unchanged; only an injected "</script>" is defanged to "<\/script>".
            pdb_text = trajectory.read_text(errors="replace").replace("</", "<\\/")
            traj_section = (
                "<section><h2>Trajectory</h2>"
                "<p class='muted'>Solute (protein + ligand); drag to rotate, scroll to zoom. "
                "Frames animate automatically.</p>"
                "<div id='viewer' class='viewer'></div>"
                f"<script type='application/x-pdb' id='trajdata'>{pdb_text}</script></section>")
            traj_script = (
                "(function(){var t=document.getElementById('trajdata').textContent;"
                "var v=$3Dmol.createViewer('viewer',{backgroundColor:'white'});"
                "v.addModelsAsFrames(t,'pdb');"
                "v.setStyle({},{cartoon:{color:'spectrum'}});"
                "v.setStyle({hetflag:true},{stick:{radius:0.18,colorscheme:'greenCarbon'}});"
                "v.zoomTo();v.render();v.animate({loop:'forward',interval:160});})();")
        else:
            traj_section = ("<section><h2>Trajectory</h2><p class='muted'>Trajectory omitted from the "
                            "inline report (too large); see trajectory.pdb in the results archive.</p></section>")

    caveats = list(extra_caveats)
    lim = narrative.get("limitations")
    gen_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    model_name = gemini.model(None)

    html = _TEMPLATE
    html = html.replace("__TITLE__", _esc(header["title"]))
    html = html.replace("__SUBTITLE__", _esc(header["subtitle"]))
    html = html.replace("__METHODS__", _esc(narrative.get("methods", "")))
    html = html.replace("__CONDITIONS__", table(conditions))
    html = html.replace("__RESULTS__", _esc(narrative.get("results", "")))
    html = html.replace("__METRICS__", table(metrics))
    html = html.replace("__FIGURES__", "\n".join(fig_blocks) or "<p class='muted'>No figures.</p>")
    html = html.replace("__TRAJECTORY__", traj_section)
    html = html.replace("__LIMITATIONS__",
                        _esc(lim) + ("<br>" + "<br>".join(_esc(c) for c in caveats) if caveats else ""))
    html = html.replace("__GENERATED__", f"Narrative by {_esc(model_name)} · generated {gen_at}")
    html = html.replace("__FIG_SCRIPTS__", "\n".join(fig_scripts))
    html = html.replace("__TRAJ_SCRIPT__", traj_script)
    return html


_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<script src="https://3Dmol.org/build/3Dmol-min.js"></script>
<style>
:root{--ink:#1a1a1a;--muted:#667085;--line:#e4e8ee;--accent:#0072B2}
*{box-sizing:border-box}
body{font-family:Inter,'Helvetica Neue',Helvetica,Arial,sans-serif;color:var(--ink);
  max-width:980px;margin:0 auto;padding:2.5rem 1.5rem;line-height:1.55}
h1{font-size:1.5rem;margin:0 0 .25rem}
h2{font-size:1.15rem;margin:2rem 0 .6rem;border-bottom:2px solid var(--accent);padding-bottom:.25rem}
.subtitle{color:var(--muted);margin:0 0 1.5rem;font-size:.95rem}
p{margin:.4rem 0}
.muted{color:var(--muted);font-size:.85rem}
table{border-collapse:collapse;width:100%;margin:.5rem 0 1rem;font-size:.9rem}
th,td{border:1px solid var(--line);padding:7px 11px;text-align:left;vertical-align:top}
th{background:#f6f8fa;width:38%;font-weight:600}
figure{margin:1.2rem 0;border:1px solid var(--line);border-radius:8px;padding:.5rem .5rem 0}
.plot{width:100%;height:360px}
figcaption{font-size:.85rem;color:var(--ink);padding:.5rem .25rem .75rem}
.viewer{width:100%;height:460px;position:relative;border:1px solid var(--line);border-radius:8px}
footer{margin-top:2.5rem;padding-top:1rem;border-top:1px solid var(--line);color:var(--muted);font-size:.8rem}
.note{background:#fff8ec;border:1px solid #f0d9a8;border-radius:8px;padding:.75rem 1rem;font-size:.85rem}
</style></head>
<body>
<h1>__TITLE__</h1>
<p class="subtitle">__SUBTITLE__</p>

<section><h2>Methods</h2><p>__METHODS__</p>__CONDITIONS__</section>
<section><h2>Results &amp; interpretation</h2><p>__RESULTS__</p>__METRICS__</section>
<section><h2>Figures</h2>__FIGURES__</section>
__TRAJECTORY__
<section><h2>Limitations</h2><div class="note">__LIMITATIONS__</div></section>
<footer>__GENERATED__. Conditions tables are derived directly from the run; narrative text is
AI-generated and should be checked before publication.</footer>

<script>
window.addEventListener('load', function(){
  try{ __FIG_SCRIPTS__ }catch(e){console.error('figure render',e);}
  try{ __TRAJ_SCRIPT__ }catch(e){console.error('trajectory render',e);}
});
</script>
</body></html>
"""
