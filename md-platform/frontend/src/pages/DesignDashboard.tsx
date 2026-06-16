import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { designApi, gpuApi, normalizeError } from "../api";
import {
  Card,
  DataTable,
  EmptyState,
  ErrorBanner,
  JobStatusBadge,
  ProgressBar,
  Spinner,
  type Column,
} from "../components";
import { DashboardTabs } from "../components/DashboardTabs";
import type { DesignJob, GpuStatus } from "../types";

const POLL_MS = 4000;

export function DesignDashboard() {
  const navigate = useNavigate();
  const [jobs, setJobs] = useState<DesignJob[] | null>(null);
  const [gpus, setGpus] = useState<GpuStatus[]>([]);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);

  async function refresh() {
    try {
      const [j, g] = await Promise.all([designApi.list(), gpuApi.list()]);
      if (!mounted.current) return;
      setJobs(j);
      setGpus(g);
    } catch (err) {
      if (mounted.current) setError(normalizeError(err).message);
    }
  }

  useEffect(() => {
    mounted.current = true;
    refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => {
      mounted.current = false;
      clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const designGpus = gpus.filter((g) => g.pool === "design");

  const columns: Column<DesignJob>[] = [
    { key: "name", header: "Name", render: (d) => (
      <button className="text-brand-700 hover:underline" onClick={() => navigate(`/design/${d.id}`)}>
        {d.name}
      </button>
    ) },
    { key: "compound_name", header: "Compound", render: (d) => d.compound_name },
    { key: "status", header: "Status", render: (d) => <JobStatusBadge status={d.status} /> },
    { key: "progress", header: "Progress", render: (d) => (
      <div className="w-32">
        <ProgressBar value={d.progress} />
        <div className="mt-0.5 text-xs text-slate-500">
          gen {d.current_generation}/{d.num_generations} · {d.progress.toFixed(0)}%
        </div>
      </div>
    ) },
    { key: "best_sequence", header: "Best peptide", render: (d) => (
      <span className="font-mono text-xs">{d.best_sequence ?? "—"}</span>
    ) },
    { key: "best_md_dg", header: "Best ΔG", render: (d) => (
      <span className="text-xs">{d.best_md_dg != null ? `${d.best_md_dg.toFixed(2)}` :
        d.best_docking_score != null ? `${d.best_docking_score.toFixed(2)} (dock)` : "—"}</span>
    ) },
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-900">Dashboard</h1>
      </div>
      <DashboardTabs />

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

      <CreateDesignForm onCreated={() => refresh()} />

      <Card title="Design GPU pool">
        {designGpus.length === 0 ? (
          <EmptyState>
            No GPUs are assigned to the design pool. Set <code>DESIGN_GPU_IDS</code> to reserve
            a GPU for peptide design (it then runs separately from MD jobs).
          </EmptyState>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {designGpus.map((g) => (
              <div key={g.gpu_id} className="rounded-md border border-slate-200 p-3">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-semibold text-slate-800">GPU {g.gpu_id}</span>
                  <span className="text-xs text-slate-500">{g.status}</span>
                </div>
                <div className="mt-1 text-xs text-slate-500">{g.name}</div>
                <div className="mt-2 text-xs text-slate-600">
                  slots {g.running_count}/{g.capacity} · util {g.utilization.toFixed(0)}%
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="Design runs">
        {jobs === null ? (
          <Spinner label="Loading design runs…" />
        ) : jobs.length === 0 ? (
          <EmptyState>No design runs yet. Start one above.</EmptyState>
        ) : (
          <DataTable columns={columns} rows={jobs} rowKey={(d) => d.id} />
        )}
      </Card>
    </div>
  );
}

function CreateDesignForm({ onCreated }: { onCreated: () => void }) {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [sequences, setSequences] = useState("");
  const [smiles, setSmiles] = useState("");
  const [compound, setCompound] = useState<File | null>(null);
  const [compoundName, setCompoundName] = useState("compound");
  const [population, setPopulation] = useState(10);
  const [generations, setGenerations] = useState(5);
  const [topK, setTopK] = useState(2);
  const [mdNs, setMdNs] = useState(10);
  const [exhaustiveness, setExhaustiveness] = useState(8);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const job = await designApi.create({
        name, initial_sequences: sequences, population_size: population,
        num_generations: generations, top_k_md: topK, md_length_ns: mdNs,
        exhaustiveness, compound_name: compoundName,
        smiles: smiles.trim() || undefined, compound,
      });
      onCreated();
      navigate(`/design/${job.id}`);
    } catch (err) {
      setError(normalizeError(err).message);
    } finally {
      setSubmitting(false);
    }
  }

  const labelCls = "block text-xs font-medium text-slate-600";
  const inputCls = "mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm";

  return (
    <Card title="New peptide-design run">
      <form onSubmit={submit} className="space-y-4">
        {error && <ErrorBanner message={error} code="Could not start design" />}
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className={labelCls}>Run name</label>
            <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)}
                   placeholder="e.g. 3-HDC binder design" required />
          </div>
          <div>
            <label className={labelCls}>Compound name</label>
            <input className={inputCls} value={compoundName}
                   onChange={(e) => setCompoundName(e.target.value)} />
          </div>
        </div>

        <div>
          <label className={labelCls}>Initial peptide sequences (same length, standard AAs)</label>
          <textarea className={`${inputCls} font-mono`} rows={2} value={sequences}
                    onChange={(e) => setSequences(e.target.value)}
                    placeholder="KCCIVYP, AAAAAAA, GGGGGGG" required />
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className={labelCls}>Target compound — SMILES</label>
            <input className={`${inputCls} font-mono`} value={smiles}
                   onChange={(e) => setSmiles(e.target.value)}
                   placeholder="CC(=O)Oc1ccccc1C(=O)O (or upload a file)" />
          </div>
          <div>
            <label className={labelCls}>…or upload .sdf/.mol/.mol2/.pdb</label>
            <input className={inputCls} type="file" accept=".sdf,.mol,.mol2,.pdb,.smi"
                   onChange={(e) => setCompound(e.target.files?.[0] ?? null)} />
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-5">
          <NumField label="Population" value={population} setValue={setPopulation} min={2} max={200} />
          <NumField label="Generations" value={generations} setValue={setGenerations} min={1} max={100} />
          <NumField label="Top-k MD" value={topK} setValue={setTopK} min={1} max={50} />
          <NumField label="MD length (ns)" value={mdNs} setValue={setMdNs} min={1} max={1000} />
          <NumField label="Exhaustiveness" value={exhaustiveness} setValue={setExhaustiveness} min={1} max={64} />
        </div>

        <div className="flex items-center gap-3">
          <button type="submit" disabled={submitting}
                  className="rounded-md bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50">
            {submitting ? "Starting…" : "Start design"}
          </button>
          <span className="text-xs text-slate-500">
            Each generation docks all candidates, then MD-refines the top-k on the design GPU.
          </span>
        </div>
      </form>
    </Card>
  );
}

function NumField({ label, value, setValue, min, max }: {
  label: string; value: number; setValue: (n: number) => void; min: number; max: number;
}) {
  return (
    <div>
      <label className="block text-xs font-medium text-slate-600">{label}</label>
      <input type="number" min={min} max={max} value={value}
             onChange={(e) => setValue(Number(e.target.value))}
             className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
    </div>
  );
}
