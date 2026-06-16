import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { normalizeError, resultsApi } from "../api";
import { Card, EmptyState, ErrorBanner, Spinner } from "../components/ui";
import { DataTable, type Column } from "../components/DataTable";
import { JobStatusBadge } from "../components/StatusBadge";
import { PlotlyChart } from "../components/PlotlyChart";
import { TrajectoryViewer } from "../components/TrajectoryViewer";
import { formatScore, titleCase } from "../format";
import { PLOT_TYPES } from "../types";
import type {
  JobResults,
  PlotlyFigure,
  PlotType,
  SubJobResult,
} from "../types";

// Human-friendly labels for each Plotly analysis chart (CONTRACT §4 PlotType).
const PLOT_LABELS: Record<PlotType, string> = {
  rmsd: "RMSD",
  rmsf: "RMSF",
  rg: "Radius of gyration",
  sasa: "SASA",
  hbond: "Hydrogen bonds",
  energy: "Energy",
  ligand_rmsd: "Ligand RMSD",
  contact_map: "Contact map",
  per_residue: "Per-residue ΔG (peptide)",
};

// Render a scalar analysis-summary value. Non-scalars (objects/arrays) are never rendered as
// React children — they are summarized so a nested summary shape can never crash the page.
function formatSummaryValue(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(3);
  }
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.join(", ");
  return "—";
}

// Only scalar metric entries are shown as cards / comparison columns.
function isScalar(value: unknown): boolean {
  return value == null || typeof value !== "object";
}

export function Results() {
  const { jobId = "" } = useParams<{ jobId: string }>();
  const [params, setParams] = useSearchParams();

  const [results, setResults] = useState<JobResults | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [downloadBusy, setDownloadBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await resultsApi.job(jobId);
      setResults(data);
      setError(null);
    } catch (err) {
      setError(normalizeError(err).message);
    } finally {
      setLoading(false);
    }
  }, [jobId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Only completed sub-jobs have results worth viewing.
  const completedSubjobs = useMemo(
    () =>
      (results?.subjobs ?? [])
        .filter((s) => s.status === "completed")
        .sort((a, b) => a.pose_index - b.pose_index),
    [results],
  );

  // The selected pose comes from ?subjob_id=, defaulting to the first completed.
  const selectedSubjobId = params.get("subjob_id");
  const selected = useMemo<SubJobResult | null>(() => {
    if (completedSubjobs.length === 0) return null;
    const found = completedSubjobs.find((s) => s.id === selectedSubjobId);
    return found ?? completedSubjobs[0];
  }, [completedSubjobs, selectedSubjobId]);

  const selectSubjob = (subjobId: string) => {
    const next = new URLSearchParams(params);
    next.set("subjob_id", subjobId);
    setParams(next, { replace: true });
  };

  const onDownloadJob = async () => {
    setDownloadBusy("job");
    setError(null);
    try {
      await resultsApi.downloadJobZip(jobId);
    } catch (err) {
      setError(normalizeError(err).message);
    } finally {
      setDownloadBusy(null);
    }
  };

  const onDownloadPose = async (subjobId: string) => {
    setDownloadBusy(subjobId);
    setError(null);
    try {
      await resultsApi.downloadSubjobZip(jobId, subjobId);
    } catch (err) {
      setError(normalizeError(err).message);
    } finally {
      setDownloadBusy(null);
    }
  };

  if (loading && !results) {
    return (
      <div className="py-12">
        <Spinner label="Loading results…" />
      </div>
    );
  }

  if (!results) {
    return (
      <div className="space-y-4">
        {error && <ErrorBanner message={error} />}
        <Card title="Results">
          <p className="text-sm text-slate-500">
            Results for this job could not be loaded.{" "}
            <Link className="text-brand-700 hover:underline" to="/">
              Back to dashboard
            </Link>
            .
          </p>
        </Card>
      </div>
    );
  }

  const { job } = results;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Link
            to={`/jobs/${job.id}`}
            className="text-sm text-brand-700 hover:underline"
          >
            ← Job
          </Link>
          <h1 className="text-xl font-semibold text-slate-900">
            {job.name} · results
          </h1>
          <JobStatusBadge status={job.status} />
        </div>
        <button
          type="button"
          className="btn-primary"
          onClick={onDownloadJob}
          disabled={downloadBusy !== null}
        >
          {downloadBusy === "job" ? "Preparing…" : "Download all results (.zip)"}
        </button>
      </div>

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

      {completedSubjobs.length === 0 ? (
        <Card title="Poses">
          <EmptyState>
            No completed poses are available yet. Results appear here once a
            sub-job finishes.
          </EmptyState>
        </Card>
      ) : (
        <>
          {/* Pose selector */}
          <Card title="Poses">
            <div className="flex flex-wrap gap-2">
              {completedSubjobs.map((s) => {
                const active = selected?.id === s.id;
                return (
                  <button
                    key={s.id}
                    type="button"
                    onClick={() => selectSubjob(s.id)}
                    className={[
                      "rounded-md border px-3 py-2 text-sm font-medium transition-colors",
                      active
                        ? "border-brand-500 bg-brand-50 text-brand-700"
                        : "border-slate-300 bg-white text-slate-600 hover:border-brand-400",
                    ].join(" ")}
                    aria-pressed={active}
                  >
                    Pose {s.pose_index}
                    <span className="ml-2 text-xs text-slate-400">
                      {formatScore(s.docking_score)} kcal/mol
                    </span>
                  </button>
                );
              })}
            </div>
          </Card>

          {/* Selected pose detail */}
          {selected && (
            <PoseResults
              jobId={job.id}
              subjob={selected}
              downloadBusy={downloadBusy === selected.id}
              onDownload={() => onDownloadPose(selected.id)}
            />
          )}

          {/* Pose comparison (only meaningful with 2+ completed poses) */}
          {completedSubjobs.length > 1 && (
            <PoseComparison jobId={job.id} subjobs={completedSubjobs} />
          )}
        </>
      )}
    </div>
  );
}

// ── Single-pose results: 3D trajectory, movie, per-type plots ────────────────

function PoseResults({
  jobId,
  subjob,
  downloadBusy,
  onDownload,
}: {
  jobId: string;
  subjob: SubJobResult;
  downloadBusy: boolean;
  onDownload: () => void;
}) {
  const [movieUrl, setMovieUrl] = useState<string | null>(null);
  const [movieChecked, setMovieChecked] = useState(false);

  // Probe for a movie once per pose; revoke the object URL on cleanup.
  useEffect(() => {
    let revoked: string | null = null;
    let cancelled = false;
    setMovieUrl(null);
    setMovieChecked(false);
    if (!subjob.has_movie) {
      setMovieChecked(true);
      return;
    }
    (async () => {
      try {
        const url = await resultsApi.movieUrl(jobId, subjob.id);
        if (cancelled) {
          if (url) URL.revokeObjectURL(url);
          return;
        }
        revoked = url;
        setMovieUrl(url);
      } catch {
        /* movie is optional; ignore */
      } finally {
        if (!cancelled) setMovieChecked(true);
      }
    })();
    return () => {
      cancelled = true;
      if (revoked) URL.revokeObjectURL(revoked);
    };
  }, [jobId, subjob.id, subjob.has_movie]);

  const availablePlots = subjob.plots_available ?? [];
  const summaryEntries = subjob.analysis_summary
    ? Object.entries(subjob.analysis_summary).filter(([, v]) => isScalar(v))
    : [];

  return (
    <Card
      title={`Pose ${subjob.pose_index}`}
      actions={
        <button
          type="button"
          className="btn-secondary !px-2.5 !py-1 !text-xs"
          onClick={onDownload}
          disabled={downloadBusy}
        >
          {downloadBusy ? "Preparing…" : "Download pose (.zip)"}
        </button>
      }
    >
      {summaryEntries.length > 0 && (
        <div className="mb-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {summaryEntries.map(([key, value]) => (
            <div key={key} className="rounded-md bg-slate-50 p-3">
              <div className="text-xs font-medium uppercase tracking-wide text-slate-500">
                {titleCase(key)}
              </div>
              <div className="mt-0.5 text-sm font-semibold text-slate-800">
                {formatSummaryValue(value)}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Binding free energy (MM/PBSA & MM/GBSA), shown only when computed */}
      {subjob.mmpbsa && (
        <div className="mb-5">
          <h3 className="mb-2 text-sm font-semibold text-slate-700">
            Binding free energy (ΔG)
          </h3>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div className="rounded-md bg-emerald-50 p-3">
              <div className="text-xs font-medium uppercase tracking-wide text-emerald-700">
                MM/GBSA ΔG
              </div>
              <div className="mt-0.5 text-sm font-semibold text-slate-800">
                {typeof subjob.mmpbsa.gbsa_dg_kcal_mol === "number"
                  ? `${subjob.mmpbsa.gbsa_dg_kcal_mol.toFixed(2)} kcal/mol`
                  : "—"}
              </div>
            </div>
            <div className="rounded-md bg-emerald-50 p-3">
              <div className="text-xs font-medium uppercase tracking-wide text-emerald-700">
                MM/PBSA ΔG
              </div>
              <div className="mt-0.5 text-sm font-semibold text-slate-800">
                {typeof subjob.mmpbsa.pbsa_dg_kcal_mol === "number"
                  ? `${subjob.mmpbsa.pbsa_dg_kcal_mol.toFixed(2)} kcal/mol`
                  : "—"}
              </div>
            </div>
            {subjob.mmpbsa.frames && (
              <div className="rounded-md bg-slate-50 p-3">
                <div className="text-xs font-medium uppercase tracking-wide text-slate-500">
                  Frames
                </div>
                <div className="mt-0.5 text-sm font-semibold text-slate-800">
                  {String(subjob.mmpbsa.frames)}
                </div>
              </div>
            )}
          </div>
          <p className="mt-1.5 text-xs text-slate-500">
            {subjob.mmpbsa.method ? `${subjob.mmpbsa.method}. ` : ""}
            Relative ranking / experimental-trend comparison (no entropy term); absolute ΔG is
            not quantitatively accurate.
          </p>
        </div>
      )}

      {/* Unified binding-hotspot table — which peptide residues drive binding.
          Merges MM/PBSA per-residue ΔG with geometric contact frequency + mean H-bonds
          (computed over the auto-detected bound window). */}
      {subjob.hotspots && subjob.hotspots.length > 0 && (
        <div className="mb-5">
          <h3 className="mb-2 text-sm font-semibold text-slate-700">
            Binding hotspots — per-residue ΔG, contacts &amp; H-bonds
          </h3>
          {subjob.bound_window && (
            <p className="mb-2 text-xs text-slate-600">
              Bound window:{" "}
              <span className="font-medium text-slate-800">
                {subjob.bound_window.start_ns.toFixed(2)}–{subjob.bound_window.end_ns.toFixed(2)} ns
              </span>{" "}
              ({subjob.bound_window.n_bound_frames}/{subjob.bound_window.n_total_frames} frames
              {typeof subjob.bound_window.ligand_rmsd_cutoff_A === "number"
                ? `, ligand RMSD < ${subjob.bound_window.ligand_rmsd_cutoff_A} Å`
                : ""}
              ).{" "}
              {subjob.bound_window.fully_bound
                ? "Ligand stayed bound throughout the trajectory."
                : "Contacts/H-bonds and ΔG decomposition are computed over this window only (frames after dissociation would be meaningless)."}
            </p>
          )}
          <div className="overflow-x-auto rounded-md border border-slate-200">
            <table className="min-w-full text-sm">
              <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-3 py-2 text-left">Residue</th>
                  <th className="px-3 py-2 text-right">ΔG (kcal/mol)</th>
                  <th className="px-3 py-2 text-right">Contact %</th>
                  <th className="px-3 py-2 text-right">H-bonds</th>
                  <th className="px-3 py-2 text-right">vdW</th>
                  <th className="px-3 py-2 text-right">Elec.</th>
                </tr>
              </thead>
              <tbody>
                {[...subjob.hotspots]
                  .sort((a, b) => {
                    // Mirror the backend ordering so the "Sorted by ΔG" caption holds regardless
                    // of payload order: ΔG rows first (ascending = most favorable), then
                    // ΔG-less rows by contact frequency (descending).
                    const ad = a.total_dg;
                    const bd = b.total_dg;
                    if (ad != null && bd != null) return ad - bd;
                    if (ad != null) return -1;
                    if (bd != null) return 1;
                    return (b.contact_frequency ?? 0) - (a.contact_frequency ?? 0);
                  })
                  .map((h) => (
                  <tr key={`${h.chain}:${h.residue}`} className="border-t border-slate-100">
                    <td className="px-3 py-1.5 font-medium text-slate-800">{h.residue}</td>
                    <td
                      className={[
                        "px-3 py-1.5 text-right font-semibold",
                        h.total_dg == null
                          ? "text-slate-400"
                          : h.total_dg < 0
                          ? "text-emerald-700"
                          : "text-rose-600",
                      ].join(" ")}
                    >
                      {h.total_dg == null ? "—" : h.total_dg.toFixed(2)}
                    </td>
                    <td className="px-3 py-1.5 text-right text-slate-600">
                      {h.contact_frequency == null
                        ? "—"
                        : `${(h.contact_frequency * 100).toFixed(0)}%`}
                    </td>
                    <td className="px-3 py-1.5 text-right text-slate-600">
                      {h.hbond_mean == null ? "—" : h.hbond_mean.toFixed(2)}
                    </td>
                    <td className="px-3 py-1.5 text-right text-slate-500">
                      {h.vdw == null ? "—" : h.vdw.toFixed(2)}
                    </td>
                    <td className="px-3 py-1.5 text-right text-slate-500">
                      {h.eel == null ? "—" : h.eel.toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-1.5 text-xs text-slate-500">
            Sorted by ΔG (most favorable first). ΔG/vdW/Elec from MM/GBSA per-residue
            decomposition (kcal/mol; negative = favorable). Contact % = fraction of bound-window
            frames with a residue atom within 4.5 Å of the ligand; H-bonds = mean polar contacts
            (≤3.5 Å) per frame. "—" = metric not computed for that residue. See the "Per-residue
            ΔG (peptide)" bar chart below for the sequence view.
          </p>
        </div>
      )}

      {/* 3D trajectory */}
      <div className="mb-6">
        <h3 className="mb-2 text-sm font-semibold text-slate-700">
          Trajectory
        </h3>
        {subjob.has_trajectory ? (
          <TrajectoryViewer jobId={jobId} subjobId={subjob.id} />
        ) : (
          <EmptyState>No trajectory file is available for this pose.</EmptyState>
        )}
      </div>

      {/* Movie */}
      {movieChecked && movieUrl && (
        <div className="mb-6">
          <h3 className="mb-2 text-sm font-semibold text-slate-700">Movie</h3>
          <video
            src={movieUrl}
            controls
            loop
            className="w-full max-w-2xl rounded-md border border-slate-200 bg-black"
          >
            Your browser does not support embedded video playback. Download the
            results package to view the movie.
          </video>
        </div>
      )}

      {/* Plots */}
      <div>
        <h3 className="mb-2 text-sm font-semibold text-slate-700">
          Analysis plots
        </h3>
        {availablePlots.length === 0 ? (
          <EmptyState>No analysis plots are available for this pose.</EmptyState>
        ) : (
          <div className="grid gap-5 lg:grid-cols-2">
            {availablePlots.map((plotType) => (
              <PlotPanel
                key={plotType}
                jobId={jobId}
                subjobId={subjob.id}
                plotType={plotType}
              />
            ))}
          </div>
        )}
      </div>
    </Card>
  );
}

// ── Lazy-loaded single plot panel (per pose) ─────────────────────────────────

function PlotPanel({
  jobId,
  subjobId,
  plotType,
}: {
  jobId: string;
  subjobId: string;
  plotType: PlotType;
}) {
  const [figure, setFigure] = useState<PlotlyFigure | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setFigure(null);
    setError(null);
    (async () => {
      try {
        const fig = await resultsApi.plot(jobId, plotType, subjobId);
        if (!cancelled) setFigure(fig);
      } catch (err) {
        if (!cancelled) setError(normalizeError(err).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [jobId, subjobId, plotType]);

  return (
    <div className="rounded-md border border-slate-200 p-3">
      <div className="mb-2 text-sm font-medium text-slate-700">
        {PLOT_LABELS[plotType]}
      </div>
      {error ? (
        <ErrorBanner message={error} />
      ) : figure ? (
        <PlotlyChart figure={figure} />
      ) : (
        <div className="flex h-40 items-center justify-center">
          <Spinner label="Loading chart…" />
        </div>
      )}
    </div>
  );
}

// ── Pose comparison: overlay plots + summary table ───────────────────────────

function PoseComparison({
  jobId,
  subjobs,
}: {
  jobId: string;
  subjobs: SubJobResult[];
}) {
  // The overlay endpoints are the same plot routes with subjob_id omitted; only
  // offer the plot types that at least one completed pose produced.
  const overlayPlots = useMemo<PlotType[]>(() => {
    const present = new Set<PlotType>();
    for (const s of subjobs) {
      for (const p of s.plots_available ?? []) present.add(p);
    }
    return PLOT_TYPES.filter((p) => present.has(p));
  }, [subjobs]);

  // Summary keys that appear across poses, in stable first-seen order.
  const summaryKeys = useMemo<string[]>(() => {
    const keys: string[] = [];
    const seen = new Set<string>();
    for (const s of subjobs) {
      if (!s.analysis_summary) continue;
      for (const [k, v] of Object.entries(s.analysis_summary)) {
        if (isScalar(v) && !seen.has(k)) {
          seen.add(k);
          keys.push(k);
        }
      }
    }
    return keys;
  }, [subjobs]);

  const comparisonColumns = useMemo<Column<SubJobResult>[]>(() => {
    const cols: Column<SubJobResult>[] = [
      {
        key: "pose",
        header: "Pose",
        render: (s) => (
          <Link
            className="font-medium text-brand-700 hover:underline"
            to={`/jobs/${jobId}/results?subjob_id=${encodeURIComponent(s.id)}`}
          >
            Pose {s.pose_index}
          </Link>
        ),
      },
      {
        key: "score",
        header: "Docking score (kcal/mol)",
        align: "right",
        render: (s) => formatScore(s.docking_score),
      },
    ];
    for (const key of summaryKeys) {
      cols.push({
        key,
        header: titleCase(key),
        align: "right",
        render: (s) =>
          formatSummaryValue(s.analysis_summary?.[key] ?? null),
      });
    }
    return cols;
  }, [jobId, summaryKeys]);

  return (
    <Card title="Pose comparison">
      <div className="mb-5">
        <DataTable
          columns={comparisonColumns}
          rows={subjobs}
          rowKey={(s) => s.id}
          empty="No comparable poses."
        />
      </div>

      {overlayPlots.length > 0 && (
        <div>
          <h3 className="mb-2 text-sm font-semibold text-slate-700">
            Overlay plots
          </h3>
          <div className="grid gap-5 lg:grid-cols-2">
            {overlayPlots.map((plotType) => (
              <OverlayPlotPanel
                key={plotType}
                jobId={jobId}
                plotType={plotType}
              />
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

// Overlay plot: same endpoint as a per-pose plot, but with subjob_id omitted so
// the backend returns all poses overlaid in one figure (CONTRACT §5 Results).
function OverlayPlotPanel({
  jobId,
  plotType,
}: {
  jobId: string;
  plotType: PlotType;
}) {
  const [figure, setFigure] = useState<PlotlyFigure | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setFigure(null);
    setError(null);
    (async () => {
      try {
        const fig = await resultsApi.plot(jobId, plotType);
        if (!cancelled) setFigure(fig);
      } catch (err) {
        if (!cancelled) setError(normalizeError(err).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [jobId, plotType]);

  return (
    <div className="rounded-md border border-slate-200 p-3">
      <div className="mb-2 text-sm font-medium text-slate-700">
        {PLOT_LABELS[plotType]} (all poses)
      </div>
      {error ? (
        <ErrorBanner message={error} />
      ) : figure ? (
        <PlotlyChart figure={figure} />
      ) : (
        <div className="flex h-40 items-center justify-center">
          <Spinner label="Loading chart…" />
        </div>
      )}
    </div>
  );
}
