import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { dashboardApi, designApi, gpuApi, normalizeError, queueApi } from "../api";
import {
  Card, DataTable, EmptyState, ErrorBanner, JobStatusBadge, ProgressBar, Spinner, StatCard,
  type Column,
} from "../components";
import { formatDuration, formatGb, formatNumber } from "../format";
import type { DashboardSummary, DesignJob, GpuStatus, QueueItem } from "../types";

const POLL_MS = 5000;
const ACTIVE = new Set(["queued", "preparing", "running_em", "running_nvt", "running_npt",
  "running_md", "analyzing", "rendering", "packaging"]);

// Combined home: a high-level view of BOTH workstreams (MD + Peptide Design) and the GPU
// pools they run on. Detailed operations live under the MD and Design menus.
export function Overview() {
  const navigate = useNavigate();
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [designs, setDesigns] = useState<DesignJob[]>([]);
  const [gpus, setGpus] = useState<GpuStatus[]>([]);
  const [mdActive, setMdActive] = useState<QueueItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    const refresh = async () => {
      try {
        const [s, d, g, q] = await Promise.all([
          dashboardApi.summary(), designApi.list(), gpuApi.list(), queueApi.get(),
        ]);
        if (!mounted.current) return;
        setSummary(s); setDesigns(d); setGpus(g);
        setMdActive([...q.running, ...q.items]);  // running first, then queued
        setError(null);
      } catch (err) {
        if (mounted.current) setError(normalizeError(err).message);
      }
    };
    refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => { mounted.current = false; clearInterval(t); };
  }, []);

  if (!summary) return error ? <ErrorBanner message={error} /> : <Spinner label="Loading…" />;

  const designRunning = designs.filter((d) => ACTIVE.has(d.status)).length;
  const designDone = designs.filter((d) => d.status === "completed").length;
  const pools: Record<string, GpuStatus[]> = { md: [], design: [], excluded: [] };
  for (const g of gpus) (pools[g.pool] ?? (pools[g.pool] = [])).push(g);

  // Recent design runs first (the API returns newest-first); cap both lists for the overview.
  const recentDesigns = designs.slice(0, 6);

  const mdCols: Column<QueueItem>[] = [
    { key: "job", header: "Job / pose", render: (q) => (
      <button className="text-left text-brand-700 hover:underline"
              onClick={() => navigate(`/jobs/${q.job_id}`)}>
        {q.job_name} <span className="text-slate-400">· pose {q.pose_index}</span>
      </button>
    ) },
    { key: "status", header: "Status", render: (q) => <JobStatusBadge status={q.status} /> },
    { key: "progress", header: "Progress", render: (q) => (
      <div className="w-36">
        <ProgressBar value={q.progress} />
        <div className="mt-0.5 text-xs text-slate-500">
          {q.completed_ns.toFixed(0)}/{q.md_length_ns} ns · {q.progress.toFixed(0)}%
        </div>
      </div>
    ) },
    { key: "gpu", header: "GPU", align: "right", render: (q) => q.assigned_gpu ?? "—" },
    { key: "eta", header: "ETA", align: "right", render: (q) => formatDuration(q.rough_eta_seconds) },
  ];

  const designCols: Column<DesignJob>[] = [
    { key: "name", header: "Run", render: (d) => (
      <button className="text-left text-brand-700 hover:underline"
              onClick={() => navigate(`/design/${d.id}`)}>{d.name}</button>
    ) },
    { key: "status", header: "Status", render: (d) => <JobStatusBadge status={d.status} /> },
    { key: "progress", header: "Progress", render: (d) => (
      <div className="w-32">
        <ProgressBar value={d.progress} />
        <div className="mt-0.5 text-xs text-slate-500">gen {d.current_generation}/{d.num_generations}</div>
      </div>
    ) },
    { key: "best", header: "Best peptide", render: (d) => (
      <span className="font-mono text-xs">{d.best_sequence ?? "—"}</span>
    ) },
    { key: "dg", header: "Best ΔG", align: "right", render: (d) =>
      d.best_md_dg != null ? d.best_md_dg.toFixed(2)
        : d.best_docking_score != null ? `${d.best_docking_score.toFixed(2)} (dock)` : "—" },
  ];

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-slate-900">Dashboard</h1>
      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

      {/* MD workstream */}
      <Card title={<Link to="/md" className="text-brand-700 hover:underline">MD jobs →</Link>}>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
          <StatCard label="Total" value={summary.total_jobs} />
          <StatCard label="Running" value={summary.running_jobs} accent="text-blue-600" />
          <StatCard label="Queued" value={summary.queued_jobs} accent="text-amber-600" />
          <StatCard label="Completed" value={summary.completed_jobs} accent="text-emerald-700" />
          <StatCard label="Failed" value={summary.failed_jobs} accent="text-rose-600" />
        </div>
      </Card>

      {/* Design workstream */}
      <Card title={<Link to="/design" className="text-brand-700 hover:underline">Peptide design runs →</Link>}>
        <div className="grid gap-4 sm:grid-cols-3">
          <StatCard label="Total runs" value={designs.length} />
          <StatCard label="Running" value={designRunning} accent="text-blue-600" />
          <StatCard label="Completed" value={designDone} accent="text-emerald-700" />
        </div>
      </Card>

      {/* GPU pools (both workstreams) + storage */}
      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="GPU pools" className="lg:col-span-2">
          {gpus.length === 0 ? (
            <p className="text-sm text-slate-500">No GPUs registered.</p>
          ) : (
            <div className="space-y-3">
              {(["md", "design", "excluded"] as const).filter((p) => pools[p]?.length).map((p) => (
                <div key={p}>
                  <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                    {p} pool ({pools[p].length} GPU{pools[p].length > 1 ? "s" : ""})
                  </div>
                  <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                    {pools[p].map((g) => (
                      <div key={g.gpu_id} className="rounded-md border border-slate-200 px-3 py-2 text-xs">
                        <div className="flex justify-between">
                          <span className="font-semibold text-slate-800">GPU {g.gpu_id}</span>
                          <span className="text-slate-500">{g.status}</span>
                        </div>
                        <div className="mt-1 flex justify-between text-slate-600">
                          <span>slots {g.running_count}/{g.capacity}</span>
                          <span>util {formatNumber(g.utilization, 0)}%</span>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
        <Card title="Resources">
          <div className="space-y-2 text-sm text-slate-600">
            <div className="flex justify-between"><span>GPUs available</span>
              <span className="tabular-nums">{summary.gpus_available} / {summary.gpus_available + summary.gpus_busy}</span></div>
            <div className="flex justify-between"><span>Storage</span>
              <span className="tabular-nums">{formatGb(summary.storage_used_gb)} / {formatGb(summary.storage_total_gb)}</span></div>
            <ProgressBar value={summary.storage_total_gb ? (summary.storage_used_gb / summary.storage_total_gb) * 100 : 0} />
          </div>
        </Card>
      </div>

      {/* Live status: active MD jobs + recent design runs */}
      <Card title={<Link to="/md" className="text-brand-700 hover:underline">MD — running &amp; queued →</Link>}>
        {mdActive.length === 0 ? (
          <EmptyState>No active MD jobs.</EmptyState>
        ) : (
          <DataTable columns={mdCols} rows={mdActive} rowKey={(q) => q.subjob_id} />
        )}
      </Card>

      <Card title={<Link to="/design" className="text-brand-700 hover:underline">Peptide design — recent runs →</Link>}>
        {recentDesigns.length === 0 ? (
          <EmptyState>No design runs yet.</EmptyState>
        ) : (
          <DataTable columns={designCols} rows={recentDesigns} rowKey={(d) => d.id} />
        )}
      </Card>
    </div>
  );
}
