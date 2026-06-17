import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  gpuApi,
  jobApi,
  normalizeError,
  queueApi,
  subscribeDashboard,
} from "../api";
import { Card, ErrorBanner, ProgressBar } from "../components/ui";
import { DataTable, type Column } from "../components/DataTable";
import { GpuStatusBadge, JobStatusBadge } from "../components/StatusBadge";
import { formatDuration, formatNumber, formatRelative } from "../format";
import { PRIORITIES } from "../types";
import type {
  DashboardEvent,
  GpuStatus,
  Job,
  Priority,
  QueueItem,
  QueueResponse,
} from "../types";

const POLL_INTERVAL_MS = 5000;

export function Admin() {
  const [gpus, setGpus] = useState<GpuStatus[]>([]);
  const [queue, setQueue] = useState<QueueResponse>({ items: [], running: [] });
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const [gpuBusy, setGpuBusy] = useState<number | null>(null);
  const [priorityBusy, setPriorityBusy] = useState<string | null>(null);
  const [mdConc, setMdConc] = useState<number | null>(null);  // null = follow current md capacity

  const mounted = useRef(true);

  const loadJobs = useCallback(async () => {
    try {
      // mine=false: admin-wide view of every user's jobs (CONTRACT §5 Jobs).
      const all = await jobApi.list(false);
      if (!mounted.current) return;
      const sorted = [...all].sort((a, b) =>
        b.created_at.localeCompare(a.created_at),
      );
      setJobs(sorted);
    } catch (err) {
      if (mounted.current) setError(normalizeError(err).message);
    }
  }, []);

  const pollOnce = useCallback(async () => {
    try {
      const [g, q] = await Promise.all([gpuApi.list(), queueApi.get()]);
      if (!mounted.current) return;
      setGpus(g);
      setQueue(q);
      setError(null);
    } catch (err) {
      if (mounted.current) setError(normalizeError(err).message);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void pollOnce();
    void loadJobs();

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
        if (data.gpus) setGpus(data.gpus);
        if (data.queue) setQueue(data.queue);
      },
      onError: () => {
        if (!mounted.current) return;
        setLive(false);
        startPolling();
      },
    });

    // The job overview is not part of the SSE payload; refresh on an interval.
    const jobsTimer = setInterval(() => void loadJobs(), 15000);

    return () => {
      mounted.current = false;
      unsubscribe();
      stopPolling();
      clearInterval(jobsTimer);
    };
  }, [pollOnce, loadJobs]);

  const onGpuAction = async (
    gpuId: number,
    action: "enable" | "disable" | "maintenance",
  ) => {
    setGpuBusy(gpuId);
    setError(null);
    try {
      const updated =
        action === "enable"
          ? await gpuApi.enable(gpuId)
          : action === "disable"
            ? await gpuApi.disable(gpuId)
            : await gpuApi.maintenance(gpuId);
      setGpus((prev) => prev.map((g) => (g.gpu_id === gpuId ? updated : g)));
    } catch (err) {
      setError(normalizeError(err).message);
    } finally {
      setGpuBusy(null);
    }
  };

  const onSetMdConcurrency = async (n: number) => {
    setError(null);
    try {
      const updated = await gpuApi.setConcurrency("md", n);
      const byId = new Map(updated.map((g) => [g.gpu_id, g]));
      setGpus((prev) => prev.map((g) => byId.get(g.gpu_id) ?? g));
      setMdConc(null);  // follow the freshly-applied capacity
    } catch (err) {
      setError(normalizeError(err).message);
    }
  };

  const onSetPool = async (gpuId: number, pool: "md" | "design" | "excluded") => {
    setGpuBusy(gpuId);
    setError(null);
    try {
      const updated = await gpuApi.setPool(gpuId, pool);
      setGpus((prev) => prev.map((g) => (g.gpu_id === gpuId ? updated : g)));
    } catch (err) {
      setError(normalizeError(err).message);  // 409 if the GPU is still running a job
    } finally {
      setGpuBusy(null);
    }
  };

  const onPriorityChange = async (jobId: string, priority: Priority) => {
    setPriorityBusy(jobId);
    setError(null);
    try {
      const updated = await queueApi.setPriority(jobId, priority);
      setJobs((prev) => prev.map((j) => (j.id === jobId ? updated : j)));
    } catch (err) {
      setError(normalizeError(err).message);
    } finally {
      setPriorityBusy(null);
    }
  };

  // Priority controls operate on the queued/active sub-jobs. Dedupe to one row
  // per job (priority is a job-level field) using the first sub-job seen.
  const priorityRows = (() => {
    const byJob = new Map<string, QueueItem>();
    for (const item of [...queue.items, ...queue.running]) {
      if (!byJob.has(item.job_id)) byJob.set(item.job_id, item);
    }
    return Array.from(byJob.values()).sort((a, b) => {
      const posA = a.queue_position ?? Number.MAX_SAFE_INTEGER;
      const posB = b.queue_position ?? Number.MAX_SAFE_INTEGER;
      return posA - posB;
    });
  })();

  // Look up the current priority for a queued job from the jobs overview.
  const jobPriority = (jobId: string): Priority => {
    const job = jobs.find((j) => j.id === jobId);
    return job?.priority ?? "normal";
  };

  const priorityColumns: Column<QueueItem>[] = [
    {
      key: "job",
      header: "Job / pose",
      render: (q) => (
        <Link
          className="font-medium text-brand-700 hover:underline"
          to={`/jobs/${q.job_id}`}
        >
          {q.job_name}
          <span className="ml-1 text-slate-400">· pose {q.pose_index}</span>
        </Link>
      ),
    },
    { key: "user", header: "User", render: (q) => q.user },
    {
      key: "status",
      header: "Status",
      render: (q) => <JobStatusBadge status={q.status} />,
    },
    {
      key: "pos",
      header: "Queue #",
      align: "right",
      render: (q) => (q.queue_position != null ? q.queue_position : "—"),
    },
    {
      key: "eta",
      header: "ETA",
      align: "right",
      render: (q) => formatDuration(q.rough_eta_seconds),
    },
    {
      key: "priority",
      header: "Priority",
      render: (q) => (
        <select
          className="input !w-auto !py-1"
          value={jobPriority(q.job_id)}
          disabled={priorityBusy === q.job_id}
          onChange={(e) =>
            onPriorityChange(q.job_id, e.target.value as Priority)
          }
          aria-label={`Priority for ${q.job_name}`}
        >
          {PRIORITIES.map((p) => (
            <option key={p} value={p}>
              {p.charAt(0).toUpperCase() + p.slice(1)}
            </option>
          ))}
        </select>
      ),
    },
  ];

  const jobColumns: Column<Job>[] = [
    {
      key: "name",
      header: "Job",
      render: (j) => (
        <Link
          className="font-medium text-brand-700 hover:underline"
          to={
            j.status === "completed"
              ? `/jobs/${j.id}/results`
              : `/jobs/${j.id}`
          }
        >
          {j.name}
        </Link>
      ),
    },
    {
      key: "id",
      header: "ID",
      render: (j) => <span className="font-mono text-xs">{j.id}</span>,
    },
    {
      key: "user",
      header: "User ID",
      align: "right",
      render: (j) => j.user_id,
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
      key: "priority",
      header: "Priority",
      render: (j) => (
        <span className="text-sm text-slate-700">
          {j.priority.charAt(0).toUpperCase() + j.priority.slice(1)}
        </span>
      ),
    },
    {
      key: "created",
      header: "Created",
      align: "right",
      render: (j) => formatRelative(j.created_at),
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-900">Administration</h1>
        <span
          className={`badge ${
            live ? "bg-green-100 text-green-700" : "bg-slate-200 text-slate-600"
          }`}
          title={
            live ? "Live updates via server-sent events" : "Polling every 5s"
          }
        >
          {live ? "Live" : "Polling"}
        </span>
      </div>

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

      {/* GPU control */}
      <Card
        title="GPU control"
        actions={
          gpus.some((g) => g.pool === "md") ? (
            <div className="flex items-center gap-2 text-xs">
              <label htmlFor="md-conc" className="text-slate-600">MD jobs / GPU</label>
              <input
                id="md-conc"
                type="number"
                min={1}
                max={16}
                step={1}
                className="w-16 rounded-md border border-slate-300 px-2 py-1"
                value={
                  mdConc ??
                  Math.max(1, ...gpus.filter((g) => g.pool === "md").map((g) => g.capacity), 1)
                }
                onChange={(e) =>
                  setMdConc(Math.max(1, Math.min(16, Math.floor(Number(e.target.value)) || 1)))
                }
              />
              <button
                type="button"
                className="rounded-md bg-brand-600 px-2 py-1 font-medium text-white hover:bg-brand-700"
                onClick={() => {
                  const fallback = Math.max(
                    1, ...gpus.filter((g) => g.pool === "md").map((g) => g.capacity), 1);
                  void onSetMdConcurrency(mdConc ?? fallback);
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
                  <div className="flex justify-between">
                    <span>Pool / slots</span>
                    <span className="tabular-nums">
                      {g.pool} · {g.running_count}/{g.capacity}
                    </span>
                  </div>
                </div>
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <label className="sr-only" htmlFor={`pool-${g.gpu_id}`}>GPU {g.gpu_id} pool</label>
                  <select
                    id={`pool-${g.gpu_id}`}
                    className="rounded-md border border-slate-300 px-1.5 py-1 text-xs"
                    title={g.running_count > 0 ? "Drain the GPU before reassigning its pool" : "Assign pool"}
                    value={g.pool}
                    disabled={gpuBusy === g.gpu_id || g.running_count > 0}
                    onChange={(e) =>
                      onSetPool(g.gpu_id, e.target.value as "md" | "design" | "excluded")
                    }
                  >
                    <option value="md">MD pool</option>
                    <option value="design">Design pool</option>
                    <option value="excluded">Excluded</option>
                  </select>
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
                    disabled={gpuBusy === g.gpu_id || g.status === "maintenance"}
                    onClick={() => onGpuAction(g.gpu_id, "maintenance")}
                  >
                    Maintenance
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Queue priority control */}
      <Card title="Queue priority">
        <DataTable
          columns={priorityColumns}
          rows={priorityRows}
          rowKey={(q) => q.job_id}
          empty="No queued or running jobs."
        />
      </Card>

      {/* All jobs overview */}
      <Card title="All jobs">
        <DataTable
          columns={jobColumns}
          rows={jobs}
          rowKey={(j) => j.id}
          empty="No jobs found."
        />
      </Card>
    </div>
  );
}
