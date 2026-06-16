import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";

import { designApi, normalizeError } from "../api";
import {
  Card,
  DataTable,
  EmptyState,
  ErrorBanner,
  JobStatusBadge,
  PlotlyChart,
  ProgressBar,
  Spinner,
  StatCard,
  type Column,
} from "../components";
import type { DesignCandidate, DesignJobDetail, PlotlyFigure } from "../types";

const POLL_MS = 4000;
const TERMINAL = new Set(["completed", "failed", "cancelled"]);

export function DesignDetail() {
  const { designId = "" } = useParams();
  const [detail, setDetail] = useState<DesignJobDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);

  useEffect(() => {
    // Per-effect guard: a response for a previous designId (or after unmount) is ignored, so
    // switching runs never lets a stale fetch clobber the current one.
    let active = true;
    setDetail(null);
    setError(null);

    const load = async () => {
      try {
        const d = await designApi.get(designId);
        if (!active) return;
        setDetail(d);
        setError(null); // a successful refresh clears a prior transient error
      } catch (err) {
        if (active) setError(normalizeError(err).message);
      }
    };

    load();
    const t = setInterval(() => {
      if (detail && TERMINAL.has(detail.job.status)) return;
      load();
    }, POLL_MS);
    return () => {
      active = false;
      clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [designId, detail?.job.status]);

  async function refresh() {
    try {
      setDetail(await designApi.get(designId));
      setError(null);
    } catch (err) {
      setError(normalizeError(err).message);
    }
  }

  const figure = useMemo<PlotlyFigure | null>(() => {
    if (!detail || detail.generations.length === 0) return null;
    const gens = detail.generations.map((p) => p.generation);
    return {
      data: [
        {
          x: gens, y: detail.generations.map((p) => p.best_fitness),
          type: "scatter", mode: "lines+markers", name: "Best fitness (−ΔG / −dock)",
          line: { color: "#2563eb" },
        },
        {
          x: gens, y: detail.generations.map((p) => p.best_md_dg),
          type: "scatter", mode: "lines+markers", name: "Best MM/GBSA ΔG",
          line: { color: "#16a34a", dash: "dot" }, connectgaps: true,
        },
      ],
      layout: {
        xaxis: { title: { text: "Generation" }, dtick: 1 },
        yaxis: { title: { text: "kcal/mol (fitness = −energy)" } },
        margin: { l: 60, r: 20, t: 30, b: 45 }, template: "plotly_white",
        legend: { orientation: "h", y: -0.2 },
      },
    };
  }, [detail]);

  if (!detail) return error ? <ErrorBanner message={error} /> : <Spinner label="Loading design run…" />;

  const { job, candidates } = detail;
  const canCancel = !TERMINAL.has(job.status);

  async function cancel() {
    setCancelling(true);
    try {
      await designApi.cancel(designId);
      await refresh();
    } catch (err) {
      setError(normalizeError(err).message);
    } finally {
      setCancelling(false);
    }
  }

  const columns: Column<DesignCandidate>[] = [
    { key: "rank", header: "#", render: (_c, i) => <span className="text-slate-400">{i + 1}</span> },
    { key: "sequence", header: "Peptide", render: (c) => <span className="font-mono">{c.sequence}</span> },
    { key: "fitness", header: "Fitness", align: "right", render: (c) => c.fitness.toFixed(3) },
    { key: "md_dg", header: "MM/GBSA ΔG", align: "right",
      render: (c) => (c.md_dg != null ? <span className="font-semibold text-emerald-700">{c.md_dg.toFixed(2)}</span> : "—") },
    { key: "docking_score", header: "Docking", align: "right",
      render: (c) => (c.docking_score != null ? c.docking_score.toFixed(2) : "—") },
    { key: "generation", header: "Gen", align: "right", render: (c) => c.generation },
    { key: "refined", header: "MD?", align: "center",
      render: (c) => (c.refined ? <span className="text-emerald-600">✓</span> : <span className="text-slate-300">—</span>) },
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">{job.name}</h1>
          <p className="text-sm text-slate-500">
            Target: {job.compound_name} · peptide length {job.peptide_length} ·
            population {job.population_size} · {job.num_generations} generations · top-{job.top_k_md} MD @ {job.md_length_ns} ns
          </p>
        </div>
        <div className="flex items-center gap-3">
          <JobStatusBadge status={job.status} />
          {canCancel && (
            <button onClick={cancel} disabled={cancelling}
                    className="rounded-md border border-rose-300 px-3 py-1.5 text-sm font-medium text-rose-700 hover:bg-rose-50 disabled:opacity-50">
              {cancelling ? "Cancelling…" : "Cancel"}
            </button>
          )}
        </div>
      </div>

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}
      {job.error_message && <ErrorBanner message={job.error_message} code="Run error" />}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Best peptide" value={<span className="font-mono text-lg">{job.best_sequence ?? "—"}</span>} />
        <StatCard label="Best MM/GBSA ΔG"
                  value={job.best_md_dg != null ? `${job.best_md_dg.toFixed(2)}` : "—"}
                  sub="kcal/mol (negative = stronger)" accent="text-emerald-700" />
        <StatCard label="Best docking score"
                  value={job.best_docking_score != null ? `${job.best_docking_score.toFixed(2)}` : "—"}
                  sub="kcal/mol (Vina)" />
        <StatCard label="Progress"
                  value={`gen ${job.current_generation}/${job.num_generations}`}
                  sub={<ProgressBar value={job.progress} />} />
      </div>

      <Card title="Convergence (best-so-far per generation)">
        {figure ? <PlotlyChart figure={figure} height={320} /> :
          <EmptyState>No generations recorded yet.</EmptyState>}
      </Card>

      <Card title="Candidate leaderboard">
        {candidates.length === 0 ? (
          <EmptyState>No candidates evaluated yet.</EmptyState>
        ) : (
          <DataTable columns={columns} rows={candidates} rowKey={(c) => `${c.generation}:${c.sequence}`} />
        )}
      </Card>
    </div>
  );
}
