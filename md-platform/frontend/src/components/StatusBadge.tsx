import type { GpuStatusValue, JobStatus } from "../types";

const JOB_STATUS_STYLES: Record<JobStatus, string> = {
  uploaded: "bg-slate-100 text-slate-700",
  validating: "bg-sky-100 text-sky-700",
  queued: "bg-amber-100 text-amber-800",
  preparing: "bg-indigo-100 text-indigo-700",
  running_em: "bg-blue-100 text-blue-700",
  running_nvt: "bg-blue-100 text-blue-700",
  running_npt: "bg-blue-100 text-blue-700",
  running_md: "bg-brand-100 text-brand-700",
  analyzing: "bg-violet-100 text-violet-700",
  rendering: "bg-fuchsia-100 text-fuchsia-700",
  packaging: "bg-teal-100 text-teal-700",
  completed: "bg-green-100 text-green-700",
  failed: "bg-red-100 text-red-700",
  cancelled: "bg-slate-200 text-slate-600",
};

const JOB_STATUS_LABEL: Record<JobStatus, string> = {
  uploaded: "Uploaded",
  validating: "Validating",
  queued: "Queued",
  preparing: "Preparing",
  running_em: "Running EM",
  running_nvt: "Running NVT",
  running_npt: "Running NPT",
  running_md: "Running MD",
  analyzing: "Analyzing",
  rendering: "Rendering",
  packaging: "Packaging",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

export function JobStatusBadge({ status }: { status: JobStatus }) {
  const style = JOB_STATUS_STYLES[status] ?? "bg-slate-100 text-slate-700";
  const label = JOB_STATUS_LABEL[status] ?? status;
  return <span className={`badge ${style}`}>{label}</span>;
}

const GPU_STATUS_STYLES: Record<GpuStatusValue, string> = {
  available: "bg-green-100 text-green-700",
  busy: "bg-brand-100 text-brand-700",
  disabled: "bg-slate-200 text-slate-600",
  maintenance: "bg-amber-100 text-amber-800",
  error: "bg-red-100 text-red-700",
};

export function GpuStatusBadge({ status }: { status: GpuStatusValue }) {
  const style = GPU_STATUS_STYLES[status] ?? "bg-slate-100 text-slate-700";
  return (
    <span className={`badge ${style}`}>
      {status.charAt(0).toUpperCase() + status.slice(1)}
    </span>
  );
}
