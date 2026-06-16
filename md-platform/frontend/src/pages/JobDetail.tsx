import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { jobApi, normalizeError, subscribeJob } from "../api";
import { useAuth } from "../auth";
import { Card, ErrorBanner, ProgressBar, Spinner } from "../components/ui";
import { DataTable, type Column } from "../components/DataTable";
import { JobStatusBadge } from "../components/StatusBadge";
import {
  formatDateTime,
  formatDuration,
  formatNumber,
  formatScore,
  titleCase,
} from "../format";
import type {
  Job,
  JobDetail as JobDetailDto,
  JobLog,
  SubJob,
} from "../types";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

const LEVEL_STYLE: Record<JobLog["level"], string> = {
  info: "text-slate-600",
  warning: "text-amber-700",
  error: "text-red-700",
};

// Job-level SSE payload (status/progress/log stream). The backend sends one of
// these shapes per frame; we merge what is present.
interface JobEvent {
  job?: Partial<Job> & { id?: string };
  subjob?: Partial<SubJob> & { id?: string };
  subjobs?: SubJob[];
  log?: JobLog;
}

export function JobDetail() {
  const { jobId = "" } = useParams();
  const navigate = useNavigate();
  const { isAdmin, user } = useAuth();

  const [detail, setDetail] = useState<JobDetailDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [live, setLive] = useState(false);
  // `aliveRef` flips false on unmount. `epochRef` increments every time the
  // subscribe effect re-runs (i.e. when jobId changes), so async loads and SSE
  // callbacks from a previous jobId can detect they are stale and bail out.
  const aliveRef = useRef(true);
  const epochRef = useRef(0);
  const logEndRef = useRef<HTMLDivElement>(null);

  const load = useCallback(
    async (epoch: number) => {
      try {
        const d = await jobApi.get(jobId);
        if (!aliveRef.current || epoch !== epochRef.current) return;
        setDetail(d);
        setError(null);
      } catch (err) {
        if (aliveRef.current && epoch === epochRef.current) {
          setError(normalizeError(err).message);
        }
      } finally {
        if (aliveRef.current && epoch === epochRef.current) setLoading(false);
      }
    },
    [jobId],
  );

  useEffect(() => {
    aliveRef.current = true;
    const epoch = epochRef.current + 1;
    epochRef.current = epoch;
    const isCurrent = () => aliveRef.current && epoch === epochRef.current;

    setLoading(true);
    // Reset transient per-view UI state for the new job. This also clears a
    // lingering busy flag if the user navigated away mid-action on a prior job.
    setActionBusy(false);
    setLive(false);
    void load(epoch);

    let pollTimer: ReturnType<typeof setInterval> | null = null;
    const startPolling = () => {
      if (!pollTimer) pollTimer = setInterval(() => void load(epoch), 5000);
    };
    const stopPolling = () => {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    const unsubscribe = subscribeJob<JobEvent>(jobId, {
      onOpen: () => {
        if (!isCurrent()) return;
        setLive(true);
        stopPolling();
      },
      onMessage: (_event, data) => {
        if (!isCurrent() || !data) return;
        setDetail((prev) => {
          if (!prev) return prev;
          let job = prev.job;
          let subjobs = prev.subjobs;
          let logs = prev.logs;
          if (data.job) job = { ...job, ...data.job };
          if (data.subjobs) subjobs = data.subjobs;
          if (data.subjob && data.subjob.id) {
            const sid = data.subjob.id;
            subjobs = subjobs.map((s) =>
              s.id === sid ? { ...s, ...(data.subjob as Partial<SubJob>) } : s,
            );
          }
          if (data.log) {
            logs = [...logs, data.log].slice(-500);
          }
          return { job, subjobs, logs };
        });
      },
      onError: () => {
        if (!isCurrent()) return;
        setLive(false);
        startPolling();
      },
    });

    return () => {
      aliveRef.current = false;
      unsubscribe();
      stopPolling();
    };
  }, [jobId, load]);

  // Auto-scroll the log viewer to the newest entry.
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ block: "nearest" });
  }, [detail?.logs.length]);

  const onCancel = async () => {
    // Capture the epoch before awaiting; if the user navigates to another job
    // during the request, the response must not write into the new view.
    const epoch = epochRef.current;
    setActionBusy(true);
    try {
      const job = await jobApi.cancel(jobId);
      if (epoch !== epochRef.current) return;
      setDetail((prev) => (prev ? { ...prev, job } : prev));
    } catch (err) {
      if (epoch === epochRef.current) setError(normalizeError(err).message);
    } finally {
      if (epoch === epochRef.current) setActionBusy(false);
    }
  };

  const onRetry = async () => {
    const epoch = epochRef.current;
    setActionBusy(true);
    try {
      const job = await jobApi.retry(jobId);
      if (epoch !== epochRef.current) return;
      setDetail((prev) => (prev ? { ...prev, job } : prev));
      void load(epoch);
    } catch (err) {
      if (epoch === epochRef.current) setError(normalizeError(err).message);
    } finally {
      if (epoch === epochRef.current) setActionBusy(false);
    }
  };

  const onDelete = async () => {
    if (!window.confirm("Delete this job and all of its stored results?")) return;
    setActionBusy(true);
    try {
      await jobApi.remove(jobId);
      navigate("/");
    } catch (err) {
      setError(normalizeError(err).message);
      setActionBusy(false);
    }
  };

  if (loading) {
    return (
      <div className="py-10">
        <Spinner label="Loading job…" />
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="space-y-4">
        {error && <ErrorBanner message={error} />}
        <Link to="/" className="text-sm text-brand-700 hover:underline">
          ← Back to dashboard
        </Link>
      </div>
    );
  }

  const { job, subjobs, logs } = detail;
  const isOwnerOrAdmin = isAdmin || job.user_id === user?.id;
  const jobActive = !TERMINAL.has(job.status);
  const anyFailed = subjobs.some((s) => s.status === "failed");
  const anyCompleted = subjobs.some((s) => s.status === "completed");

  const subjobColumns: Column<SubJob>[] = [
    { key: "pose", header: "Pose", render: (s) => `#${s.pose_index}` },
    {
      key: "score",
      header: "Docking score",
      align: "right",
      render: (s) => formatScore(s.docking_score),
    },
    { key: "status", header: "Status", render: (s) => <JobStatusBadge status={s.status} /> },
    {
      key: "step",
      header: "Current step",
      render: (s) => (s.current_step ? titleCase(s.current_step) : "—"),
    },
    {
      key: "gpu",
      header: "GPU",
      align: "right",
      render: (s) => (s.assigned_gpu != null ? s.assigned_gpu : "—"),
    },
    {
      key: "progress",
      header: "Progress",
      render: (s) => (
        <div className="flex items-center gap-2">
          <ProgressBar value={s.progress} className="w-24" />
          <span className="w-24 text-xs tabular-nums text-slate-500">
            {formatNumber(s.completed_ns, 1)}/{job.md_length_ns} ns
          </span>
        </div>
      ),
    },
    {
      key: "speed",
      header: "ns/day",
      align: "right",
      render: (s) => (s.ns_per_day > 0 ? formatNumber(s.ns_per_day, 1) : "—"),
    },
    {
      key: "links",
      header: "",
      align: "right",
      render: (s) =>
        s.status === "completed" ? (
          <Link
            className="text-sm text-brand-700 hover:underline"
            to={`/jobs/${job.id}/results?subjob_id=${encodeURIComponent(s.id)}`}
          >
            Results
          </Link>
        ) : s.status === "failed" && s.error_message ? (
          <span className="text-xs text-red-600" title={s.error_message}>
            error
          </span>
        ) : (
          <span className="text-slate-300">—</span>
        ),
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Link to="/" className="text-sm text-brand-700 hover:underline">
            ← Dashboard
          </Link>
          <h1 className="text-xl font-semibold text-slate-900">{job.name}</h1>
          <JobStatusBadge status={job.status} />
          <span
            className={`badge ${
              live
                ? "bg-green-100 text-green-700"
                : "bg-slate-200 text-slate-600"
            }`}
          >
            {live ? "Live" : "Polling"}
          </span>
        </div>
        <div className="flex items-center gap-2">
          {anyCompleted && (
            <Link className="btn-secondary" to={`/jobs/${job.id}/results`}>
              View results
            </Link>
          )}
          {isOwnerOrAdmin && jobActive && (
            <button
              type="button"
              className="btn-secondary"
              onClick={onCancel}
              disabled={actionBusy}
            >
              Cancel
            </button>
          )}
          {isOwnerOrAdmin && anyFailed && (
            <button
              type="button"
              className="btn-secondary"
              onClick={onRetry}
              disabled={actionBusy}
            >
              Retry failed
            </button>
          )}
          {isOwnerOrAdmin && TERMINAL.has(job.status) && (
            <button
              type="button"
              className="btn-danger"
              onClick={onDelete}
              disabled={actionBusy}
            >
              Delete
            </button>
          )}
        </div>
      </div>

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

      {job.error_message && (
        <ErrorBanner message={job.error_message} code="Job failed" />
      )}

      {/* Metadata */}
      <Card title="Job metadata">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Meta label="Job ID" value={job.id} mono />
          <Meta label="Input type" value={job.input_type.toUpperCase()} />
          <Meta label="Ligand type" value={titleCase(job.ligand_type)} />
          <Meta label="Chemistry source" value={job.ligand_chem_source.toUpperCase()} />
          <Meta label="Poses" value={String(job.top_n_poses)} />
          <Meta label="MD length" value={`${job.md_length_ns} ns`} />
          <Meta label="Protein FF" value={job.force_field} />
          <Meta label="Ligand FF" value={job.ligand_force_field} />
          <Meta label="Water model" value={job.water_model} />
          <Meta label="Box type" value={titleCase(job.box_type)} />
          <Meta label="Salt" value={`${formatNumber(job.salt_concentration, 2)} M`} />
          <Meta label="Temperature" value={`${formatNumber(job.temperature, 0)} K`} />
          <Meta label="Pressure" value={`${formatNumber(job.pressure, 1)} bar`} />
          <Meta label="Priority" value={titleCase(job.priority)} />
          <Meta label="Created" value={formatDateTime(job.created_at)} />
          <Meta label="Started" value={formatDateTime(job.started_at)} />
          <Meta label="Completed" value={formatDateTime(job.completed_at)} />
        </div>
      </Card>

      {/* Per-pose status */}
      <Card title="Poses">
        <DataTable
          columns={subjobColumns}
          rows={subjobs}
          rowKey={(s) => s.id}
          empty="No sub-jobs."
        />
      </Card>

      {/* Logs */}
      <Card
        title="Logs"
        actions={
          <span className="text-xs text-slate-400">
            {logs.length} entr{logs.length === 1 ? "y" : "ies"}
          </span>
        }
      >
        {logs.length === 0 ? (
          <p className="text-sm text-slate-500">No log entries yet.</p>
        ) : (
          <div className="max-h-96 overflow-y-auto rounded-md bg-slate-900 p-3 font-mono text-xs leading-relaxed">
            {logs.map((l) => (
              <div key={l.id} className="flex gap-2">
                <span className="shrink-0 text-slate-500">
                  {formatDateTime(l.created_at)}
                </span>
                <span
                  className={`shrink-0 uppercase ${
                    l.level === "error"
                      ? "text-red-400"
                      : l.level === "warning"
                        ? "text-amber-300"
                        : "text-sky-300"
                  }`}
                >
                  {l.level}
                </span>
                <span className="shrink-0 text-violet-300">[{l.step}]</span>
                <span
                  className={`whitespace-pre-wrap break-all ${
                    LEVEL_STYLE[l.level] ? "text-slate-100" : "text-slate-100"
                  }`}
                >
                  {l.message}
                </span>
              </div>
            ))}
            <div ref={logEndRef} />
          </div>
        )}
      </Card>

      {jobActive && (
        <p className="text-center text-xs text-slate-400">
          {live
            ? "Status updates stream live."
            : `Live stream unavailable; refreshing every 5 seconds. ETA ${formatDuration(
                null,
              )}`}
        </p>
      )}
    </div>
  );
}

function Meta({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">
        {label}
      </div>
      <div className={`mt-0.5 text-sm font-medium text-slate-800 ${mono ? "font-mono" : ""}`}>
        {value}
      </div>
    </div>
  );
}
