import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  dashboardApi,
  gpuApi,
  jobApi,
  normalizeError,
  queueApi,
  subscribeDashboard,
} from "../api";
import { useAuth } from "../auth";
import { Card, ErrorBanner, ProgressBar, StatCard } from "../components/ui";
import { DataTable, type Column } from "../components/DataTable";
import { GpuStatusBadge, JobStatusBadge } from "../components/StatusBadge";
import { formatDuration, formatGb, formatNumber, formatRelative } from "../format";
import type {
  DashboardEvent,
  DashboardSummary,
  GpuStatus,
  Job,
  QueueItem,
  QueueResponse,
} from "../types";

const POLL_INTERVAL_MS = 5000;

export function Dashboard() {
  const { isAdmin } = useAuth();
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [gpus, setGpus] = useState<GpuStatus[]>([]);
  const [queue, setQueue] = useState<QueueResponse>({ items: [], running: [] });
  const [recent, setRecent] = useState<Job[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const [gpuBusy, setGpuBusy] = useState<number | null>(null);
  const [mdConc, setMdConc] = useState<number | null>(null);  // null = follow current md capacity
  const [selected, setSelected] = useState<Set<string>>(new Set());  // finished jobs picked for delete
  const [deleting, setDeleting] = useState(false);

  // Avoid overlapping polls / stale writes after unmount.
  const mounted = useRef(true);

  const loadRecent = useCallback(async () => {
    try {
      // mine=true: the API returns the caller's jobs (admins still see their own
      // here; the full cross-user view lives in the queue/Admin surfaces).
      const jobs = await jobApi.list(true);
      if (!mounted.current) return;
      // Finished jobs (completed/failed/cancelled) so results can be cleaned up — including
      // failed runs, not just completed ones.
      const TERMINAL = new Set(["completed", "failed", "cancelled"]);
      const finished = jobs
        .filter((j) => TERMINAL.has(j.status))
        .sort((a, b) =>
          (b.completed_at ?? b.created_at ?? "").localeCompare(a.completed_at ?? a.created_at ?? ""),
        )
        .slice(0, 12);
      setRecent(finished);
      // Drop any selections that no longer exist (e.g. after a delete elsewhere).
      const ids = new Set(finished.map((j) => j.id));
      setSelected((prev) => new Set([...prev].filter((id) => ids.has(id))));
    } catch {
      /* recent list is best-effort */
    }
  }, []);

  const removeJobs = useCallback(async (ids: string[]) => {
    if (ids.length === 0) return;
    const msg = ids.length === 1
      ? "Delete this job and all of its stored results?"
      : `Delete ${ids.length} jobs and all of their stored results?`;
    if (!window.confirm(msg)) return;
    setDeleting(true);
    try {
      const results = await Promise.allSettled(ids.map((id) => jobApi.remove(id)));
      const failed = results.filter((r) => r.status === "rejected").length;
      if (failed && mounted.current) {
        setError(`${failed} of ${ids.length} deletions failed.`);
      }
      setSelected(new Set());
      await loadRecent();
    } finally {
      if (mounted.current) setDeleting(false);
    }
  }, [loadRecent]);

  const toggleOne = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const pollOnce = useCallback(async () => {
    try {
      const [s, g, q] = await Promise.all([
        dashboardApi.summary(),
        gpuApi.list(),
        queueApi.get(),
      ]);
      if (!mounted.current) return;
      setSummary(s);
      setGpus(g);
      setQueue(q);
      setError(null);
    } catch (err) {
      if (mounted.current) setError(normalizeError(err).message);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    // Initial fetch so the page is populated before SSE/poll kicks in.
    void pollOnce();
    void loadRecent();

    // Primary: SSE stream. Fallback: 5s polling when SSE is not connected.
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    const startPolling = () => {
      if (pollTimer) return;
      pollTimer = setInterval(() => void pollOnce(), POLL_INTERVAL_MS);
    };
    const stopPolling = () => {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    const unsubscribe = subscribeDashboard<DashboardEvent>({
      onOpen: () => {
        if (!mounted.current) return;
        setLive(true);
        setError(null);
        stopPolling();
      },
      onMessage: (_event, data) => {
        if (!mounted.current || !data) return;
        if (data.summary) setSummary(data.summary);
        if (data.gpus) setGpus(data.gpus);
        if (data.queue) setQueue(data.queue);
      },
      onError: () => {
        if (!mounted.current) return;
        setLive(false);
        startPolling();
      },
    });

    // Refresh the recent-completed list periodically (not part of the SSE payload).
    const recentTimer = setInterval(() => void loadRecent(), 15000);

    return () => {
      mounted.current = false;
      unsubscribe();
      stopPolling();
      clearInterval(recentTimer);
    };
  }, [pollOnce, loadRecent]);

  const onGpuAction = async (
    gpuId: number,
    action: "enable" | "disable" | "maintenance",
  ) => {
    setGpuBusy(gpuId);
    try {
      const updated =
        action === "enable"
          ? await gpuApi.enable(gpuId)
          : action === "disable"
            ? await gpuApi.disable(gpuId)
            : await gpuApi.maintenance(gpuId);
      setGpus((prev) =>
        prev.map((g) => (g.gpu_id === gpuId ? updated : g)),
      );
    } catch (err) {
      setError(normalizeError(err).message);
    } finally {
      setGpuBusy(null);
    }
  };

  const onSetPool = async (gpuId: number, pool: "md" | "design" | "excluded") => {
    setGpuBusy(gpuId);
    try {
      const updated = await gpuApi.setPool(gpuId, pool);
      setGpus((prev) => prev.map((g) => (g.gpu_id === gpuId ? updated : g)));
    } catch (err) {
      setError(normalizeError(err).message); // 409 if the GPU is still running a job
    } finally {
      setGpuBusy(null);
    }
  };

  const onSetMdConcurrency = async (n: number) => {
    try {
      const updated = await gpuApi.setConcurrency("md", n);
      const byId = new Map(updated.map((g) => [g.gpu_id, g]));
      setGpus((prev) => prev.map((g) => byId.get(g.gpu_id) ?? g));
    } catch (err) {
      setError(normalizeError(err).message);
    }
  };

  const queueColumns: Column<QueueItem>[] = [
    {
      key: "job",
      header: "Job / pose",
      render: (q) => (
        <Link className="font-medium text-brand-700 hover:underline" to={`/jobs/${q.job_id}`}>
          {q.job_name}
          <span className="ml-1 text-slate-400">
            · pose {q.pose_index}{q.replica_index > 1 ? ` · rep ${q.replica_index}` : ""}
          </span>
        </Link>
      ),
    },
    { key: "user", header: "User", render: (q) => q.user },
    { key: "status", header: "Status", render: (q) => <JobStatusBadge status={q.status} /> },
    {
      key: "pos",
      header: "Queue #",
      align: "right",
      render: (q) => (q.queue_position != null ? q.queue_position : "—"),
    },
    {
      key: "len",
      header: "MD length",
      align: "right",
      render: (q) => `${q.md_length_ns} ns`,
    },
  ];

  const runningColumns: Column<QueueItem>[] = [
    {
      key: "job",
      header: "Job / pose",
      render: (q) => (
        <Link className="font-medium text-brand-700 hover:underline" to={`/jobs/${q.job_id}`}>
          {q.job_name}
          <span className="ml-1 text-slate-400">
            · pose {q.pose_index}{q.replica_index > 1 ? ` · rep ${q.replica_index}` : ""}
          </span>
        </Link>
      ),
    },
    { key: "user", header: "User", render: (q) => q.user },
    { key: "status", header: "Step", render: (q) => <JobStatusBadge status={q.status} /> },
    {
      key: "gpu",
      header: "GPU",
      align: "right",
      render: (q) => (q.assigned_gpu != null ? q.assigned_gpu : "—"),
    },
    {
      key: "progress",
      header: "Progress",
      render: (q) => (
        <div className="flex items-center gap-2">
          <ProgressBar value={q.progress} className="w-24" />
          <span className="w-28 text-xs tabular-nums text-slate-500">
            {formatNumber(q.completed_ns, 1)}/{q.md_length_ns} ns
          </span>
        </div>
      ),
    },
    {
      key: "speed",
      header: "ns/day",
      align: "right",
      render: (q) =>
        q.ns_per_day > 0 ? formatNumber(q.ns_per_day, 1) : "—",
    },
    {
      key: "eta",
      header: "ETA",
      align: "right",
      render: (q) => formatDuration(q.rough_eta_seconds),
    },
  ];

  const allSelected = recent.length > 0 && recent.every((j) => selected.has(j.id));
  const recentColumns: Column<Job>[] = [
    {
      key: "select",
      header: (
        <input
          type="checkbox"
          aria-label="Select all finished jobs"
          checked={allSelected}
          onChange={(e) =>
            setSelected(e.target.checked ? new Set(recent.map((j) => j.id)) : new Set())
          }
        />
      ),
      render: (j) => (
        <input
          type="checkbox"
          aria-label={`Select ${j.name}`}
          checked={selected.has(j.id)}
          onChange={() => toggleOne(j.id)}
        />
      ),
    },
    {
      key: "name",
      header: "Job",
      render: (j) => (
        <Link className="font-medium text-brand-700 hover:underline" to={`/jobs/${j.id}/results`}>
          {j.name}
        </Link>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (j) => <JobStatusBadge status={j.status} />,
    },
    {
      key: "poses",
      header: "Poses",
      align: "right",
      render: (j) => j.top_n_poses,
    },
    {
      key: "len",
      header: "MD length",
      align: "right",
      render: (j) => `${j.md_length_ns} ns`,
    },
    {
      key: "completed",
      header: "Completed",
      align: "right",
      render: (j) => formatRelative(j.completed_at),
    },
    {
      key: "view",
      header: "",
      align: "right",
      render: (j) => (
        <div className="flex items-center justify-end gap-3">
          <Link className="text-sm text-brand-700 hover:underline" to={`/jobs/${j.id}/results`}>
            View results
          </Link>
          <button
            type="button"
            className="text-sm text-rose-600 hover:underline disabled:opacity-50"
            disabled={deleting}
            onClick={() => void removeJobs([j.id])}
          >
            Remove
          </button>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-900">MD</h1>
        <div className="flex items-center gap-3">
          <span
            className={`badge ${
              live ? "bg-green-100 text-green-700" : "bg-slate-200 text-slate-600"
            }`}
            title={live ? "Live updates via server-sent events" : "Polling every 5s"}
          >
            {live ? "Live" : "Polling"}
          </span>
          <Link
            to="/upload"
            className="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700"
          >
            New MD Job
          </Link>
        </div>
      </div>

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

      {/* Summary cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Total jobs" value={summary?.total_jobs ?? "—"} />
        <StatCard
          label="Running"
          value={summary?.running_jobs ?? "—"}
          accent="text-brand-700"
        />
        <StatCard
          label="Queued"
          value={summary?.queued_jobs ?? "—"}
          accent="text-amber-700"
        />
        <StatCard
          label="Completed"
          value={summary?.completed_jobs ?? "—"}
          accent="text-green-700"
          sub={
            summary?.failed_jobs
              ? `${summary.failed_jobs} failed`
              : undefined
          }
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <StatCard
          label="GPUs available"
          value={
            summary
              ? `${summary.gpus_available} / ${
                  summary.gpus_available + summary.gpus_busy
                }`
              : "—"
          }
          sub={summary ? `${summary.gpus_busy} busy` : undefined}
        />
        <StatCard
          label="Storage used"
          value={formatGb(summary?.storage_used_gb)}
          sub={
            summary
              ? `of ${formatGb(summary.storage_total_gb, 0)}`
              : undefined
          }
        />
        <StatCard
          label="Failed jobs"
          value={summary?.failed_jobs ?? "—"}
          accent={summary?.failed_jobs ? "text-red-600" : "text-slate-900"}
        />
      </div>

      {/* GPU panel */}
      <Card
        title="GPUs"
        actions={
          isAdmin && gpus.some((g) => g.pool === "md") ? (
            <div className="flex items-center gap-2 text-xs">
              <span className="text-slate-500">MD per GPU</span>
              <input
                type="number"
                min={1}
                max={16}
                aria-label="Concurrent MD jobs per GPU"
                className="w-16 rounded-md border border-slate-300 px-2 py-1 text-xs"
                value={mdConc ?? Math.max(1, ...gpus.filter((g) => g.pool === "md").map((g) => g.capacity), 1)}
                onChange={(e) => setMdConc(Number(e.target.value))}
              />
              <button
                type="button"
                className="btn-secondary !px-2 !py-1 !text-xs"
                onClick={() => {
                  const fallback = Math.max(1, ...gpus.filter((g) => g.pool === "md").map((g) => g.capacity), 1);
                  const raw = mdConc ?? fallback;
                  // Clamp to the API's 1..16 (HTML min/max don't enforce typed values).
                  const n = Math.min(16, Math.max(1, Math.round(Number.isFinite(raw) ? raw : fallback)));
                  void onSetMdConcurrency(n).then(() => setMdConc(null));
                }}
              >
                Apply
              </button>
            </div>
          ) : undefined
        }
      >
        {gpus.length === 0 ? (
          <p className="text-sm text-slate-500">No GPUs registered.</p>
        ) : (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {gpus.map((g) => (
              <div
                key={g.gpu_id}
                className="rounded-md border border-slate-200 p-4"
              >
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-sm font-semibold text-slate-800">
                      GPU {g.gpu_id}
                    </div>
                    <div className="text-xs text-slate-500">{g.name}</div>
                  </div>
                  <GpuStatusBadge status={g.status} />
                </div>
                <div className="mt-3 space-y-1.5 text-xs text-slate-600">
                  <div className="flex justify-between">
                    <span>Utilization</span>
                    <span className="tabular-nums">
                      {formatNumber(g.utilization, 0)}%
                    </span>
                  </div>
                  <ProgressBar value={g.utilization} />
                  <div className="flex justify-between pt-1">
                    <span>Memory</span>
                    <span className="tabular-nums">
                      {formatNumber(g.memory_used, 0)} /{" "}
                      {formatNumber(g.memory_total, 0)} MiB
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span>Temperature</span>
                    <span className="tabular-nums">
                      {formatNumber(g.temperature, 0)} °C
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span>Assigned</span>
                    <span className="truncate pl-2 text-right font-mono">
                      {g.assigned_subjob_id ?? "—"}
                    </span>
                  </div>
                  <div className="flex justify-between pt-1">
                    <span>Pool</span>
                    <span className="font-medium uppercase">{g.pool}</span>
                  </div>
                  <div className="flex justify-between">
                    <span>Slots (running / capacity)</span>
                    <span className="tabular-nums">{g.running_count} / {g.capacity}</span>
                  </div>
                </div>
                {isAdmin && (
                  <div className="mt-3 flex flex-wrap gap-2">
                    <button
                      type="button"
                      className="btn-secondary !px-2 !py-1 !text-xs"
                      disabled={gpuBusy === g.gpu_id || g.status === "available"}
                      onClick={() => onGpuAction(g.gpu_id, "enable")}
                    >
                      Enable
                    </button>
                    <button
                      type="button"
                      className="btn-secondary !px-2 !py-1 !text-xs"
                      disabled={gpuBusy === g.gpu_id || g.status === "disabled"}
                      onClick={() => onGpuAction(g.gpu_id, "disable")}
                    >
                      Disable
                    </button>
                    <button
                      type="button"
                      className="btn-secondary !px-2 !py-1 !text-xs"
                      disabled={
                        gpuBusy === g.gpu_id || g.status === "maintenance"
                      }
                      onClick={() => onGpuAction(g.gpu_id, "maintenance")}
                    >
                      Maintenance
                    </button>
                    <label className="flex items-center gap-1 text-xs text-slate-500">
                      Pool
                      <select
                        aria-label={`GPU ${g.gpu_id} pool`}
                        className="rounded-md border border-slate-300 px-1.5 py-1 text-xs"
                        disabled={gpuBusy === g.gpu_id || g.running_count > 0}
                        title={g.running_count > 0 ? "Drain the GPU before reassigning its pool" : undefined}
                        value={g.pool}
                        onChange={(e) => onSetPool(g.gpu_id, e.target.value as "md" | "design" | "excluded")}
                      >
                        <option value="md">md</option>
                        <option value="design">design</option>
                        <option value="excluded">excluded</option>
                      </select>
                    </label>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Running */}
      <Card title="Running">
        <DataTable
          columns={runningColumns}
          rows={queue.running}
          rowKey={(q) => q.subjob_id}
          empty="No sub-jobs are currently running."
        />
      </Card>

      {/* Queue */}
      <Card title="Queue">
        <DataTable
          columns={queueColumns}
          rows={queue.items}
          rowKey={(q) => q.subjob_id}
          empty="The queue is empty."
        />
      </Card>

      {/* Finished jobs (completed/failed) — selectable for result cleanup */}
      <Card
        title="Finished jobs"
        actions={
          selected.size > 0 ? (
            <button
              type="button"
              className="rounded-md bg-rose-600 px-3 py-1 text-xs font-medium text-white hover:bg-rose-700 disabled:opacity-50"
              disabled={deleting}
              onClick={() => void removeJobs([...selected])}
            >
              {deleting ? "Deleting…" : `Delete selected (${selected.size})`}
            </button>
          ) : undefined
        }
      >
        <DataTable
          columns={recentColumns}
          rows={recent}
          rowKey={(j) => j.id}
          empty="No finished jobs yet."
        />
      </Card>
    </div>
  );
}
