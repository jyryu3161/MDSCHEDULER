import axios, { AxiosError, AxiosInstance } from "axios";
import type {
  ApiErrorDetail,
  DashboardSummary,
  DesignJob,
  DesignJobCreate,
  DesignJobDetail,
  GpuStatus,
  Job,
  JobCreate,
  JobDetail,
  JobResults,
  LoginResponse,
  Me,
  PlotlyFigure,
  PlotType,
  Priority,
  QueueResponse,
  SubJobResultDetail,
  TrajectoryPayload,
  UploadResponse,
  ValidationReport,
} from "./types";

// ── Token storage (shared with auth.tsx) ─────────────────────────────────────

const TOKEN_KEY = "md_platform_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

// ── Axios instance ───────────────────────────────────────────────────────────

// All requests go through the Vite/nginx proxy at /api.
export const http: AxiosInstance = axios.create({
  baseURL: "/api",
  headers: { "Content-Type": "application/json" },
});

http.interceptors.request.use((config) => {
  const token = getToken();
  if (token) {
    config.headers = config.headers ?? {};
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// On 401 (other than the login call itself), drop the token and bounce to login.
http.interceptors.response.use(
  (resp) => resp,
  (error: AxiosError) => {
    const status = error.response?.status;
    const url = error.config?.url ?? "";
    const isLogin = url.includes("/auth/login");
    if (status === 401 && !isLogin) {
      clearToken();
      const here = window.location.pathname + window.location.search;
      if (!window.location.pathname.startsWith("/login")) {
        window.location.assign(
          `/login?next=${encodeURIComponent(here)}`,
        );
      }
    }
    return Promise.reject(error);
  },
);

// Extract a factual, actionable message + structured detail from an Axios error.
export interface NormalizedApiError {
  status: number | null;
  code: string | null;
  message: string;
  detail: ApiErrorDetail | null;
  report: ValidationReport | null;
}

export function normalizeError(err: unknown): NormalizedApiError {
  if (axios.isAxiosError(err)) {
    const status = err.response?.status ?? null;
    const data = err.response?.data as
      | { detail?: ApiErrorDetail | string }
      | undefined;
    const raw = data?.detail;
    if (raw && typeof raw === "object") {
      return {
        status,
        code: raw.code ?? null,
        message: raw.message ?? err.message,
        detail: raw,
        report: raw.report ?? null,
      };
    }
    return {
      status,
      code: null,
      message: typeof raw === "string" ? raw : err.message,
      detail: null,
      report: null,
    };
  }
  return {
    status: null,
    code: null,
    message: err instanceof Error ? err.message : "Unexpected error.",
    detail: null,
    report: null,
  };
}

// ── Auth (CONTRACT §5 Auth) ──────────────────────────────────────────────────

export const authApi = {
  async login(username: string, password: string): Promise<LoginResponse> {
    const { data } = await http.post<LoginResponse>("/auth/login", {
      username,
      password,
    });
    return data;
  },
  async logout(): Promise<void> {
    await http.post("/auth/logout");
  },
  async changePassword(
    old_password: string,
    new_password: string,
  ): Promise<void> {
    await http.post("/auth/change-password", { old_password, new_password });
  },
  async me(): Promise<Me> {
    const { data } = await http.get<Me>("/auth/me");
    return data;
  },
};

// ── Uploads (CONTRACT §5 Uploads) ────────────────────────────────────────────

export interface UploadInputArgs {
  poseFile: File;
  chemistryFile?: File | null;
  receptorFile?: File | null;
  smiles?: string;
}

export const uploadApi = {
  async createInput(args: UploadInputArgs): Promise<UploadResponse> {
    const form = new FormData();
    form.append("pose_file", args.poseFile);
    if (args.chemistryFile) form.append("chemistry_file", args.chemistryFile);
    if (args.receptorFile) form.append("receptor_file", args.receptorFile);
    if (args.smiles && args.smiles.trim()) {
      form.append("smiles", args.smiles.trim());
    }
    const { data } = await http.post<UploadResponse>("/uploads/input", form, {
      headers: { "Content-Type": "multipart/form-data" },
    });
    return data;
  },
  async validate(uploadId: string): Promise<ValidationReport> {
    const { data } = await http.get<ValidationReport>(
      `/uploads/${encodeURIComponent(uploadId)}/validate`,
    );
    return data;
  },
};

// ── Jobs (CONTRACT §5 Jobs) ──────────────────────────────────────────────────

export const jobApi = {
  async create(payload: JobCreate): Promise<Job> {
    const { data } = await http.post<Job>("/jobs", payload);
    return data;
  },
  async list(mine: boolean): Promise<Job[]> {
    const { data } = await http.get<Job[]>("/jobs", { params: { mine } });
    return data;
  },
  async get(jobId: string): Promise<JobDetail> {
    const { data } = await http.get<JobDetail>(
      `/jobs/${encodeURIComponent(jobId)}`,
    );
    return data;
  },
  async cancel(jobId: string): Promise<Job> {
    const { data } = await http.post<Job>(
      `/jobs/${encodeURIComponent(jobId)}/cancel`,
    );
    return data;
  },
  async retry(jobId: string): Promise<Job> {
    const { data } = await http.post<Job>(
      `/jobs/${encodeURIComponent(jobId)}/retry`,
    );
    return data;
  },
  async remove(jobId: string): Promise<void> {
    await http.delete(`/jobs/${encodeURIComponent(jobId)}`);
  },
};

// ── Queue (CONTRACT §5 Queue) ────────────────────────────────────────────────

export const queueApi = {
  async get(): Promise<QueueResponse> {
    const { data } = await http.get<QueueResponse>("/queue");
    return data;
  },
  async setPriority(jobId: string, priority: Priority): Promise<Job> {
    const { data } = await http.post<Job>(
      `/queue/${encodeURIComponent(jobId)}/priority`,
      { priority },
    );
    return data;
  },
};

// ── GPU (CONTRACT §5 GPU) ────────────────────────────────────────────────────

export const gpuApi = {
  async list(): Promise<GpuStatus[]> {
    const { data } = await http.get<GpuStatus[]>("/gpus");
    return data;
  },
  async enable(gpuId: number): Promise<GpuStatus> {
    const { data } = await http.post<GpuStatus>(`/gpus/${gpuId}/enable`);
    return data;
  },
  async disable(gpuId: number): Promise<GpuStatus> {
    const { data } = await http.post<GpuStatus>(`/gpus/${gpuId}/disable`);
    return data;
  },
  async maintenance(gpuId: number): Promise<GpuStatus> {
    const { data } = await http.post<GpuStatus>(`/gpus/${gpuId}/maintenance`);
    return data;
  },
  async setConcurrency(pool: "md" | "design", concurrency: number): Promise<GpuStatus[]> {
    const { data } = await http.patch<GpuStatus[]>("/gpus/concurrency", { pool, concurrency });
    return data;
  },
};

// ── Peptide design (GA) (CONTRACT §5 Design) ─────────────────────────────────

export const designApi = {
  async list(): Promise<DesignJob[]> {
    const { data } = await http.get<DesignJob[]>("/design");
    return data;
  },
  async get(designId: string): Promise<DesignJobDetail> {
    const { data } = await http.get<DesignJobDetail>(`/design/${designId}`);
    return data;
  },
  async create(payload: DesignJobCreate): Promise<DesignJob> {
    const form = new FormData();
    form.append("name", payload.name);
    form.append("initial_sequences", payload.initial_sequences);
    form.append("population_size", String(payload.population_size));
    form.append("num_generations", String(payload.num_generations));
    form.append("top_k_md", String(payload.top_k_md));
    form.append("md_length_ns", String(payload.md_length_ns));
    form.append("exhaustiveness", String(payload.exhaustiveness));
    form.append("compound_name", payload.compound_name);
    if (payload.smiles) form.append("smiles", payload.smiles);
    if (payload.compound) form.append("compound", payload.compound);
    const { data } = await http.post<DesignJob>("/design", form, {
      headers: { "Content-Type": "multipart/form-data" },
    });
    return data;
  },
  async cancel(designId: string): Promise<DesignJob> {
    const { data } = await http.post<DesignJob>(`/design/${designId}/cancel`);
    return data;
  },
};

// ── Dashboard summary (CONTRACT §5 Dashboard summary) ────────────────────────

export const dashboardApi = {
  async summary(): Promise<DashboardSummary> {
    const { data } = await http.get<DashboardSummary>("/dashboard/summary");
    return data;
  },
};

// ── Results (CONTRACT §5 Results) ────────────────────────────────────────────

export const resultsApi = {
  async job(jobId: string): Promise<JobResults> {
    const { data } = await http.get<JobResults>(
      `/jobs/${encodeURIComponent(jobId)}/results`,
    );
    return data;
  },
  async subjob(jobId: string, subjobId: string): Promise<SubJobResultDetail> {
    const { data } = await http.get<SubJobResultDetail>(
      `/jobs/${encodeURIComponent(jobId)}/subjobs/${encodeURIComponent(
        subjobId,
      )}/results`,
    );
    return data;
  },
  async plot(
    jobId: string,
    plotType: PlotType,
    subjobId?: string,
  ): Promise<PlotlyFigure> {
    const { data } = await http.get<PlotlyFigure>(
      `/jobs/${encodeURIComponent(jobId)}/plots/${plotType}`,
      { params: subjobId ? { subjob_id: subjobId } : {} },
    );
    return data;
  },
  // Trajectory: fetch as a blob and report the X-Trajectory-Format header so the
  // viewer knows how to parse it (multi-model PDB for MVP/mock; xtc for real).
  async trajectory(jobId: string, subjobId: string): Promise<TrajectoryPayload> {
    const resp = await http.get(
      `/jobs/${encodeURIComponent(jobId)}/trajectory`,
      {
        params: { subjob_id: subjobId },
        responseType: "blob",
      },
    );
    const fmt = (resp.headers["x-trajectory-format"] ?? "pdb")
      .toString()
      .toLowerCase();
    return {
      format: fmt === "xtc" ? "xtc" : "pdb",
      blob: resp.data as Blob,
    };
  },
  // Returns a Blob URL if a movie exists, otherwise null (404).
  // The caller owns the returned object URL and must revokeObjectURL it.
  async movieUrl(jobId: string, subjobId: string): Promise<string | null> {
    try {
      const resp = await http.get(`/jobs/${encodeURIComponent(jobId)}/movie`, {
        params: { subjob_id: subjobId },
        responseType: "blob",
      });
      return URL.createObjectURL(resp.data as Blob);
    } catch (err) {
      if (axios.isAxiosError(err) && err.response?.status === 404) return null;
      throw err;
    }
  },
  // Authenticated download of the whole-job zip. The token stays in the
  // Authorization header (never in the URL); the file is streamed into a Blob
  // and saved via a transient object URL.
  async downloadJobZip(jobId: string): Promise<void> {
    const resp = await http.get(
      `/jobs/${encodeURIComponent(jobId)}/download`,
      { responseType: "blob" },
    );
    saveBlob(
      resp.data as Blob,
      filenameFromDisposition(resp.headers["content-disposition"]) ??
        `${jobId}_all_results.zip`,
    );
  },
  // Authenticated download of a single pose's results zip.
  async downloadSubjobZip(jobId: string, subjobId: string): Promise<void> {
    const resp = await http.get(
      `/jobs/${encodeURIComponent(jobId)}/subjobs/${encodeURIComponent(
        subjobId,
      )}/download`,
      { responseType: "blob" },
    );
    saveBlob(
      resp.data as Blob,
      filenameFromDisposition(resp.headers["content-disposition"]) ??
        `${subjobId}_results.zip`,
    );
  },
};

// Parse a filename out of a Content-Disposition header, if present.
function filenameFromDisposition(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const star = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(value);
  if (star?.[1]) {
    try {
      return decodeURIComponent(star[1].replace(/"/g, "").trim());
    } catch {
      /* fall through */
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(value);
  return plain?.[1]?.trim() ?? null;
}

// Trigger a browser download for an in-memory Blob.
function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoke after the click has been dispatched.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

// ── Realtime: header-authenticated SSE (CONTRACT §5 Realtime) ────────────────

// EventSource cannot set an Authorization header, and putting the JWT in the
// URL would leak it via history/logs/referrers. Instead we open the SSE stream
// with fetch (which carries the bearer header) and parse the event/data frames
// from the response body reader.
//
// This client replicates the EventSource semantics that matter here:
//  - automatic reconnect after a transient disconnect or network error,
//  - exponential backoff (capped) with reset on a successful read,
//  - honoring the server's `retry:` hint as the reconnect delay,
//  - propagating `Last-Event-ID` on reconnect.
// On a 401 it stops and redirects to login (the token is gone/expired).
// Returns an unsubscribe function.
export interface SseHandlers<T> {
  onMessage: (event: string, data: T) => void;
  onError?: (err: unknown) => void;
  onOpen?: () => void;
}

const SSE_BACKOFF_MIN_MS = 1000;
const SSE_BACKOFF_MAX_MS = 30000;

export function subscribeSse<T = unknown>(
  path: string,
  handlers: SseHandlers<T>,
): () => void {
  let closed = false;
  let controller: AbortController | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let backoffMs = SSE_BACKOFF_MIN_MS;
  let retryHintMs: number | null = null;
  let lastEventId = "";

  const scheduleReconnect = () => {
    if (closed) return;
    const delay = retryHintMs ?? backoffMs;
    backoffMs = Math.min(backoffMs * 2, SSE_BACKOFF_MAX_MS);
    reconnectTimer = setTimeout(connect, delay);
  };

  const connect = async () => {
    if (closed) return;
    controller = new AbortController();
    const token = getToken();
    try {
      const resp = await fetch(`/api${path}`, {
        method: "GET",
        headers: {
          Accept: "text/event-stream",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          ...(lastEventId ? { "Last-Event-ID": lastEventId } : {}),
        },
        signal: controller.signal,
        cache: "no-store",
      });

      if (resp.status === 401) {
        closed = true;
        clearToken();
        if (!window.location.pathname.startsWith("/login")) {
          window.location.assign("/login");
        }
        return;
      }
      if (!resp.ok || !resp.body) {
        throw new Error(`SSE connection failed (${resp.status})`);
      }

      handlers.onOpen?.();
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      // Read frames separated by a blank line; each frame may carry `event:`,
      // `id:`, `retry:`, and (possibly multi-line) `data:` fields.
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        // A successful read means the connection is healthy: reset backoff.
        backoffMs = SSE_BACKOFF_MIN_MS;
        buffer += decoder.decode(value, { stream: true });
        let sepIndex: number;
        while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, sepIndex);
          buffer = buffer.slice(sepIndex + 2);
          let eventName = "message";
          const dataLines: string[] = [];
          for (const line of frame.split("\n")) {
            if (line.startsWith("event:")) {
              eventName = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              dataLines.push(line.slice(5).replace(/^ /, ""));
            } else if (line.startsWith("id:")) {
              lastEventId = line.slice(3).trim();
            } else if (line.startsWith("retry:")) {
              const ms = parseInt(line.slice(6).trim(), 10);
              if (Number.isFinite(ms) && ms >= 0) retryHintMs = ms;
            }
            // ignore comment lines (starting with ':')
          }
          if (dataLines.length === 0) continue;
          const raw = dataLines.join("\n");
          try {
            handlers.onMessage(eventName, JSON.parse(raw) as T);
          } catch {
            handlers.onMessage(eventName, raw as unknown as T);
          }
        }
      }

      // Stream ended cleanly (server closed) — reconnect unless we're done.
      if (!closed) scheduleReconnect();
    } catch (err) {
      if (closed) return;
      if (err instanceof DOMException && err.name === "AbortError") return;
      handlers.onError?.(err);
      scheduleReconnect();
    }
  };

  void connect();

  return () => {
    closed = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    controller?.abort();
  };
}

export function subscribeDashboard<T = unknown>(
  handlers: SseHandlers<T>,
): () => void {
  return subscribeSse<T>("/events/dashboard", handlers);
}

export function subscribeJob<T = unknown>(
  jobId: string,
  handlers: SseHandlers<T>,
): () => void {
  return subscribeSse<T>(
    `/events/jobs/${encodeURIComponent(jobId)}`,
    handlers,
  );
}
