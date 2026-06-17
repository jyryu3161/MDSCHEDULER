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
import type { DesignDockEngine, DesignEvalMode, DesignJob, DesignStrategy, GpuStatus } from "../types";

const POLL_MS = 4000;

export function DesignDashboard({ strategy = "ga" }: { strategy?: DesignStrategy } = {}) {
  const navigate = useNavigate();
  const isAS = strategy === "autoscientist";
  const base = isAS ? "/autoscientist" : "/design";
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
      setError(null); // a successful refresh clears a stale error banner from a prior failure
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
      <button className="text-brand-700 hover:underline" onClick={() => navigate(`${base}/${d.id}`)}>
        {d.name}
      </button>
    ) },
    { key: "compound_name", header: "Compound", render: (d) => d.compound_name },
    { key: "status", header: "Status", render: (d) => <JobStatusBadge status={d.status} /> },
    { key: "progress", header: "Progress", render: (d) => (
      <div className="w-32">
        <ProgressBar value={d.progress} />
        <div className="mt-0.5 text-xs text-slate-500">
          {isAS ? "round" : "gen"} {d.current_generation}/{d.num_generations} · {d.progress.toFixed(0)}%
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
      <div>
        <h1 className="text-xl font-semibold text-slate-900">
          {isAS ? "AutoScientist Design" : "Peptide Design"}
        </h1>
        {isAS && (
          <p className="mt-1 text-sm text-slate-500">
            A self-organizing LLM agent team (AutoScientists, arXiv:2605.28655) designs the peptide:
            agents propose research directions, an analyst proposes candidate sequences along each
            direction (filtering weak ideas before they cost compute), candidates are evaluated by
            docking (+ MD/MM-GBSA), and the team reorganizes around productive directions — retiring
            dead-ends — when progress stalls. Requires a Gemini key (set in Admin).
          </p>
        )}
      </div>

      {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

      <CreateDesignForm strategy={strategy} onCreated={() => refresh()} />

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

      <Card title={isAS ? "AutoScientist runs" : "Design runs"}>
        {jobs === null ? (
          <Spinner label="Loading design runs…" />
        ) : (() => {
          const rows = jobs.filter((d) => (d.strategy ?? "ga") === strategy);
          return rows.length === 0 ? (
            <EmptyState>No {isAS ? "AutoScientist" : "design"} runs yet. Start one above.</EmptyState>
          ) : (
            <DataTable columns={columns} rows={rows} rowKey={(d) => d.id} />
          );
        })()}
      </Card>
    </div>
  );
}

function CreateDesignForm({ onCreated, strategy = "ga" }: { onCreated: () => void; strategy?: DesignStrategy }) {
  const navigate = useNavigate();
  const isAS = strategy === "autoscientist";
  const base = isAS ? "/autoscientist" : "/design";
  const [name, setName] = useState("");
  const [sequences, setSequences] = useState("");
  const [smiles, setSmiles] = useState("");
  const [compound, setCompound] = useState<File | null>(null);
  const [compoundName, setCompoundName] = useState("compound");
  const [population, setPopulation] = useState(10);
  const [generations, setGenerations] = useState(5);
  const [dockOversample, setDockOversample] = useState(4);
  const [mdNs, setMdNs] = useState(10);
  const [mdReplicas, setMdReplicas] = useState(1);
  const [exhaustiveness, setExhaustiveness] = useState(8);
  const [evalMode, setEvalMode] = useState<DesignEvalMode>("hybrid");
  const [dockEngine, setDockEngine] = useState<DesignDockEngine>("vina");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    // Client-side validation mirroring the backend (DesignJobCreate) so users get immediate,
    // friendly feedback instead of a post-submit 422.
    const seqs = sequences.split(/[\s,]+/).map((s) => s.trim().toUpperCase()).filter(Boolean);
    if (seqs.length === 0) {
      setError("Enter at least one initial peptide sequence.");
      return;
    }
    const lengths = new Set(seqs.map((s) => s.length));
    if (lengths.size !== 1) {
      setError(`All peptide sequences must have the same length (got lengths ${[...lengths].sort((a, b) => a - b).join(", ")}).`);
      return;
    }
    const badAa = seqs.find((s) => /[^ARNDCQEGHILKMFPSTWYV]/.test(s));
    if (badAa) {
      setError(`Sequence "${badAa}" contains non-standard amino acids (use the 20 standard one-letter codes).`);
      return;
    }
    if (!compound && !smiles.trim()) {
      setError("Provide a target compound: a SMILES string or a structure file.");
      return;
    }
    setSubmitting(true);
    try {
      const job = await designApi.create({
        name, initial_sequences: sequences, population_size: population,
        num_generations: generations, dock_oversample: dockOversample, md_length_ns: mdNs,
        n_replicas: mdReplicas, exhaustiveness, eval_mode: evalMode, dock_engine: dockEngine,
        strategy, compound_name: compoundName, smiles: smiles.trim() || undefined, compound,
      });
      onCreated();
      navigate(`${base}/${job.id}`);
    } catch (err) {
      setError(normalizeError(err).message);
    } finally {
      setSubmitting(false);
    }
  }

  const labelCls = "block text-xs font-medium text-slate-600";
  const inputCls = "mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm";

  return (
    <Card title={isAS ? "New AutoScientist design run" : "New peptide-design run"}>
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
          <textarea className={`${inputCls} font-mono`} rows={4} value={sequences}
                    onChange={(e) => setSequences(e.target.value)}
                    placeholder={"One sequence per line (commas/spaces also OK):\nKCCIVYP\nAAAAAAA\nGGGGGGG"} required />
          <p className="mt-1 text-xs text-slate-500">
            Separate sequences by newline, comma, or space — all accepted. All must share one length.
          </p>
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

        <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <NumField label={isAS ? "Candidates/round" : "Population"} value={population} setValue={setPopulation} min={2} max={200} />
          <NumField label={isAS ? "Rounds" : "Generations"} value={generations} setValue={setGenerations} min={1} max={100} />
          <NumField label={isAS ? "Research directions" : "Dock ×N (hybrid)"} value={dockOversample} setValue={setDockOversample} min={1} max={20} />
          <NumField label="MD length (ns)" value={mdNs} setValue={setMdNs} min={1} max={1000} />
          <NumField label="Replicas/cand." value={mdReplicas} setValue={setMdReplicas} min={1} max={5} />
          <NumField label="Exhaustiveness" value={exhaustiveness} setValue={setExhaustiveness} min={1} max={64} />
        </div>
        <p className="-mt-2 text-xs text-slate-500">
          {isAS ? (
            <>The agent team runs <b>Rounds</b> discussion→execution cycles, proposing up to{" "}
            <b>Candidates/round</b> sequences spread across <b>Research directions</b> (mechanistic
            hypotheses). Each candidate is docked (+ MD-refined in hybrid mode); the champion is
            promoted only past a noise-aware gate, and directions are retired when they stop helping.</>
          ) : (
            <>Dock ×N (hybrid only): each generation docks Population × N candidates and MD-refines the
            top Population by docking score (N=1 ⇒ MD all docked). Replicas/cand.: independent MD
            repeats per candidate; fitness uses the mean ΔG (multiplies MD cost).</>
          )}
        </p>

        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className={labelCls}>Evaluation strategy</label>
            <select className={inputCls} aria-label="Evaluation strategy" value={evalMode}
                    onChange={(e) => setEvalMode(e.target.value as DesignEvalMode)}>
              <option value="hybrid">Docking → MD top-k (hybrid, efficient)</option>
              <option value="md_only">MD on every candidate (most accurate, slowest)</option>
            </select>
          </div>
          <div>
            <label className={labelCls}>Docking engine</label>
            <select className={inputCls} aria-label="Docking engine" value={dockEngine}
                    onChange={(e) => setDockEngine(e.target.value as DesignDockEngine)}>
              <option value="vina">AutoDock Vina 1.2.7 (rigid, default)</option>
              <option value="smina">Smina (flexible receptor side chains)</option>
              <option value="gnina">Gnina (CNN scoring, GPU — requires install)</option>
              <option value="auto">Auto (Smina if installed, else Vina)</option>
            </select>
          </div>
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
      <input type="number" min={min} max={max} step={1} value={value}
             onChange={(e) =>
               setValue(Math.max(min, Math.min(max, Math.floor(Number(e.target.value)) || min)))
             }
             className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
    </div>
  );
}
