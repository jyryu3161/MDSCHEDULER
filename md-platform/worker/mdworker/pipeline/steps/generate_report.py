"""Step — generate_report: write a self-contained publication-style report.html for the pose.

Runs after analysis/MM-PBSA and BEFORE package_results so report.html is included in results.zip.
Non-fatal by contract: any failure (Gemini down, missing artifact) is logged and skipped — a
finished MD job is never failed by report generation. Honors settings.report_enabled."""

from __future__ import annotations

from typing import Any, Dict


def run(ctx, settings, *, md: Dict[str, Any], analysis: Dict[str, Any]) -> Dict[str, Any]:
    step = "generate_report"
    if not getattr(settings, "report_enabled", True):
        return {"skipped": True, "reason": "report_enabled is false"}
    try:
        ctx.set_status("packaging", current_step=step, progress=97.0)
        from mdworker.report.builder import build_md_report
        html = build_md_report(ctx, settings, md, analysis)
        out = ctx.pose_dir / "report.html"
        out.write_text(html, encoding="utf-8")
        from mdworker.report import gemini
        narrated = "Gemini" if gemini.available(settings) else "templates (no Gemini key)"
        ctx.info(step, f"Wrote report.html ({len(html) // 1024} KB; narrative via {narrated}).")
        return {"report_html": str(out)}
    except Exception as exc:  # noqa: BLE001 — report is optional, never fail the job
        ctx.warning(step, f"Report generation skipped: {exc}")
        return {"skipped": True, "reason": str(exc)}
