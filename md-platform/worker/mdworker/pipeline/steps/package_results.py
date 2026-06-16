"""Step 9 — package_results (CONTRACT §9.9, §17).

Writes the per-pose ``results.zip`` (prep/ md/ analysis/ visualization/ logs/) plus a
``pose_summary.json`` capturing the pose metadata + analysis summary.

It then (re)builds the JOB-level summary artifacts under ``summary/`` from whatever
``pose_summary.json`` files exist at that moment: ``pose_comparison.csv``, ``metadata.json``,
``summary_report.html`` (+ ``.pdf`` if weasyprint/reportlab is available), and
``all_results.zip``. This is done idempotently on every pose completion, so when the final
pose finishes the aggregates reflect all completed poses without any cross-subjob locking
(a self-healing approach safe under concurrent per-pose workers).
"""

from __future__ import annotations

import html as _html
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text to path atomically (temp file in same dir + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _atomic_replace(src: Path, dest: Path) -> None:
    os.replace(src, dest)


def run(ctx, settings, *, bond_orders: Dict[str, Any], prepared: Dict[str, Any],
        params: Dict[str, Any], md: Dict[str, Any], analysis: Dict[str, Any]) -> Dict[str, Any]:
    step = "package_results"
    ctx.set_status("packaging", current_step=step, progress=98.0)

    # Per-pose summary combining job/pose metadata with the analysis summary.
    pose_summary = {
        "job_id": ctx.job_id,
        "subjob_id": ctx.subjob_id,
        "pose_index": ctx.pose_index,
        "docking_score": ctx.docking_score,
        "ligand_type": ctx.ligand_type,
        "molformula": bond_orders.get("molformula"),
        "md_length_ns": ctx.md_length_ns,
        "engine": md.get("engine"),
        "completed_ns": md.get("completed_ns"),
        "ns_per_day": md.get("ns_per_day"),
        "n_frames": md.get("n_frames"),
        "force_field": params.get("force_field"),
        "charge_method": params.get("charge_method"),
        "analysis": analysis.get("summary"),
        "plots_available": analysis.get("plots_available", []),
    }
    (ctx.pose_dir / "pose_summary.json").write_text(json.dumps(pose_summary, indent=2))

    # Build results.zip from the pose directory contents (excluding any prior zip).
    results_zip = ctx.pose_dir / "results.zip"
    if results_zip.exists():
        results_zip.unlink()
    n_files = 0
    with zipfile.ZipFile(results_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(ctx.pose_dir.rglob("*")):
            if path.is_file() and path.resolve() != results_zip.resolve():
                zf.write(path, arcname=str(path.relative_to(ctx.pose_dir)))
                n_files += 1

    size_mb = results_zip.stat().st_size / (1024 * 1024)
    ctx.info(step, f"Packaged pose {ctx.pose_index}: {n_files} files, "
                   f"results.zip {size_mb:.2f} MB.")

    # Job-level aggregation (idempotent; reflects all pose_summary.json present now).
    try:
        _build_job_summary(ctx)
    except Exception as exc:  # noqa: BLE001 - aggregation must not fail the pose
        ctx.warning(step, f"Job-level summary aggregation deferred (will retry next pose): {exc}")

    return {
        "results_zip": str(results_zip),
        "result_path": str(ctx.pose_dir),
        "pose_summary": str(ctx.pose_dir / "pose_summary.json"),
        "n_files": n_files,
    }


def _collect_pose_summaries(ctx) -> List[Dict[str, Any]]:
    """Load every pose_summary.json present in the job dir, sorted by pose_index."""
    summaries: List[Dict[str, Any]] = []
    for pose_dir in sorted(ctx.job_dir.glob("pose_*")):
        ps = pose_dir / "pose_summary.json"
        if ps.exists():
            try:
                summaries.append(json.loads(ps.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
    summaries.sort(key=lambda s: s.get("pose_index", 0))
    return summaries


def _build_job_summary(ctx) -> None:
    """Build summary/{pose_comparison.csv, metadata.json, summary_report.html(+pdf),
    all_results.zip} from the current set of completed poses (CONTRACT §8, §9.9).

    Serialized with a job-level advisory file lock so concurrent per-pose workers do not race;
    each output is written to a temp file then atomically renamed, so a reader (or the next
    pose) never observes a partially-written aggregate.
    """
    import csv
    import io

    summary_dir = ctx.summary_dir
    summary_dir.mkdir(parents=True, exist_ok=True)

    lock_path = ctx.job_dir / ".summary.lock"
    with _JobLock(lock_path):
        summaries = _collect_pose_summaries(ctx)
        if not summaries:
            return

        # pose_comparison.csv (atomic)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "pose_index", "docking_score", "molformula", "completed_ns", "ns_per_day",
            "backbone_rmsd_mean_A", "ligand_rmsd_mean_A", "ligand_rmsd_final_A",
            "ligand_stable", "rg_mean_A", "sasa_mean_A2", "hbond_mean",
        ])
        for s in summaries:
            metrics = (s.get("analysis") or {}).get("metrics", {}) if s.get("analysis") else {}
            w.writerow([
                s.get("pose_index"),
                s.get("docking_score"),
                s.get("molformula"),
                s.get("completed_ns"),
                s.get("ns_per_day"),
                metrics.get("backbone_rmsd_mean_A"),
                metrics.get("ligand_rmsd_mean_A"),
                metrics.get("ligand_rmsd_final_A"),
                metrics.get("ligand_stable"),
                metrics.get("rg_mean_A"),
                metrics.get("sasa_mean_A2"),
                metrics.get("hbond_mean"),
            ])
        _atomic_write_text(summary_dir / "pose_comparison.csv", buf.getvalue())

        # metadata.json (job-level, atomic)
        metadata = {
            "job_id": ctx.job_id,
            "ligand_type": ctx.ligand_type,
            "md_length_ns": ctx.md_length_ns,
            "n_poses_packaged": len(summaries),
            "engine": summaries[0].get("engine") if summaries else None,
            "force_field": summaries[0].get("force_field") if summaries else None,
            "poses": [
                {
                    "pose_index": s.get("pose_index"),
                    "docking_score": s.get("docking_score"),
                    "subjob_id": s.get("subjob_id"),
                    "ligand_stable": ((s.get("analysis") or {}).get("metrics", {}) or {}).get("ligand_stable"),
                }
                for s in summaries
            ],
            "job_meta": {k: v for k, v in ctx.job_meta.items() if k != "inputs"},
        }
        _atomic_write_text(summary_dir / "metadata.json", json.dumps(metadata, indent=2))

        # summary_report.html (+ optional pdf), atomic.
        html = _render_summary_html(ctx, summaries)
        _atomic_write_text(summary_dir / "summary_report.html", html)
        _maybe_render_pdf(ctx, html, summary_dir / "summary_report.pdf")

        # all_results.zip: bundle each pose's results.zip + the job summary artifacts (atomic).
        tmp_zip = summary_dir / ".all_results.zip.tmp"
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for pose_dir in sorted(ctx.job_dir.glob("pose_*")):
                rz = pose_dir / "results.zip"
                if rz.exists():
                    zf.write(rz, arcname=f"{pose_dir.name}/results.zip")
            for name in ("pose_comparison.csv", "metadata.json", "summary_report.html", "summary_report.pdf"):
                p = summary_dir / name
                if p.exists():
                    zf.write(p, arcname=f"summary/{name}")
        _atomic_replace(tmp_zip, summary_dir / "all_results.zip")


class _JobLock:
    """Cross-process advisory lock on a job (fcntl.flock); no-op fallback if unavailable."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl

            self._fh = open(self.path, "w")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:  # noqa: BLE001 - lock is best-effort; atomic writes still protect readers
            self._fh = None
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
        return False


def _render_summary_html(ctx, summaries: List[Dict[str, Any]]) -> str:
    rows = []
    for s in summaries:
        metrics = (s.get("analysis") or {}).get("metrics", {}) if s.get("analysis") else {}
        stable = metrics.get("ligand_stable")
        verdict = "stable" if stable else ("mobile" if stable is not None else "n/a")
        rows.append(
            "<tr>"
            f"<td>{_esc(s.get('pose_index', ''))}</td>"
            f"<td>{_esc(_fmt(s.get('docking_score')))}</td>"
            f"<td>{_esc(s.get('molformula', ''))}</td>"
            f"<td>{_esc(_fmt(s.get('completed_ns')))}</td>"
            f"<td>{_esc(_fmt(metrics.get('backbone_rmsd_mean_A')))}</td>"
            f"<td>{_esc(_fmt(metrics.get('ligand_rmsd_mean_A')))}</td>"
            f"<td>{_esc(verdict)}</td>"
            "</tr>"
        )
    table = "\n".join(rows)
    engine = summaries[0].get("engine", "") if summaries else ""
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>MD Platform — Job {_esc(ctx.job_id)}</title>"
        "<style>body{font-family:system-ui,Arial,sans-serif;margin:2rem;color:#1a1a1a}"
        "h1{font-size:1.4rem}table{border-collapse:collapse;width:100%;margin-top:1rem}"
        "th,td{border:1px solid #ccc;padding:6px 10px;text-align:left;font-size:0.9rem}"
        "th{background:#f2f4f7}caption{caption-side:bottom;color:#666;font-size:0.8rem;margin-top:0.5rem}"
        "</style></head><body>"
        f"<h1>MD Platform — Job {_esc(ctx.job_id)}</h1>"
        f"<p>Ligand type: {_esc(ctx.ligand_type)} &middot; MD length: {ctx.md_length_ns:g} ns &middot; "
        f"Engine: {_esc(engine)} &middot; Poses: {len(summaries)}</p>"
        "<table><thead><tr>"
        "<th>Pose</th><th>Docking score</th><th>Formula</th><th>Completed (ns)</th>"
        "<th>Backbone RMSD mean (Å)</th><th>Ligand RMSD mean (Å)</th><th>Ligand verdict</th>"
        "</tr></thead><tbody>"
        f"{table}"
        "</tbody></table>"
        "<caption>Per-pose MD summary. Values are computed from the production trajectory; "
        "SASA/energy are estimates under the mock engine.</caption>"
        "</body></html>"
    )


def _maybe_render_pdf(ctx, html: str, dest: Path) -> None:
    """Render the HTML report to PDF if weasyprint is available; else skip.

    Rendered to a temp file then atomically renamed so a reader never sees a partial PDF.
    HTML-only output is acceptable per CONTRACT §9.9 when no PDF backend is present.
    """
    try:
        from weasyprint import HTML  # type: ignore
    except Exception:  # noqa: BLE001 - weasyprint optional / may lack system libs
        return
    tmp = dest.parent / (dest.name + ".tmp")
    try:
        HTML(string=html).write_pdf(str(tmp))
        _atomic_replace(tmp, dest)
    except Exception as exc:  # noqa: BLE001 - PDF is optional
        ctx.warning("package_results", f"PDF report rendering skipped: {exc}")
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def _esc(v) -> str:
    """HTML-escape any dynamic value before interpolating into the report."""
    return _html.escape("" if v is None else str(v), quote=True)
