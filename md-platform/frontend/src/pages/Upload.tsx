import { useMemo, useState, type ChangeEvent } from "react";
import { useNavigate } from "react-router-dom";
import { jobApi, normalizeError, uploadApi } from "../api";
import { useAuth } from "../auth";
import { Card, ErrorBanner, Spinner } from "../components/ui";
import { DataTable, type Column } from "../components/DataTable";
import { formatScore, titleCase } from "../format";
import {
  BOX_TYPES,
  HETATM_DECISIONS,
  LIGAND_TYPES,
  MD_PRESETS,
  PRIORITIES,
  type BoxType,
  type ChemSource,
  type HetatmCandidate,
  type HetatmDecision,
  type LigandType,
  type MdPreset,
  type PoseSummary,
  type Priority,
  type UploadResponse,
  type ValidationReport,
} from "../types";

// Preset → production MD length (ns). custom keeps the user-entered length.
const PRESET_NS: Record<Exclude<MdPreset, "custom">, number> = {
  quick: 10,
  standard: 50,
  extended: 100,
};

// Rough storage estimate: ~2.5 GB of trajectory+analysis artifacts per pose
// at the standard 100 ps interval, scaled by MD length relative to 50 ns.
function estimateStorageGb(topN: number, mdLengthNs: number): number {
  const perPoseAt50ns = 2.5;
  const scaled = perPoseAt50ns * (mdLengthNs / 50);
  return Math.max(0.2, topN * scaled);
}

function FileField({
  id,
  label,
  hint,
  accept,
  required,
  file,
  onChange,
}: {
  id: string;
  label: string;
  hint: string;
  accept: string;
  required?: boolean;
  file: File | null;
  onChange: (f: File | null) => void;
}) {
  return (
    <div>
      <label className="label" htmlFor={id}>
        {label}
        {required && <span className="ml-1 text-red-500">*</span>}
      </label>
      <input
        id={id}
        type="file"
        accept={accept}
        className="block w-full text-sm text-slate-600 file:mr-3 file:rounded-md file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:text-sm file:font-medium file:text-brand-700 hover:file:bg-brand-100"
        onChange={(e: ChangeEvent<HTMLInputElement>) =>
          onChange(e.target.files?.[0] ?? null)
        }
      />
      <p className="mt-1 text-xs text-slate-500">
        {hint}
        {file && (
          <span className="ml-1 font-medium text-slate-700">
            · {file.name}
          </span>
        )}
      </p>
    </div>
  );
}

export function Upload() {
  const navigate = useNavigate();
  const { isAdmin } = useAuth();

  // Step 1 — files
  const [poseFile, setPoseFile] = useState<File | null>(null);
  const [chemistryFile, setChemistryFile] = useState<File | null>(null);
  const [receptorFile, setReceptorFile] = useState<File | null>(null);
  const [smiles, setSmiles] = useState("");

  // Step 2 — upload + validation
  const [upload, setUpload] = useState<UploadResponse | null>(null);
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [validating, setValidating] = useState(false);
  const [validateError, setValidateError] = useState<string | null>(null);

  // Step 3 — job options
  const [name, setName] = useState("");
  const [ligandType, setLigandType] = useState<LigandType>("small_molecule");
  const [topN, setTopN] = useState(3);
  const [nReplicas, setNReplicas] = useState(1);
  const [preset, setPreset] = useState<MdPreset>("standard");
  const [mdLengthNs, setMdLengthNs] = useState(50);
  const [boxType, setBoxType] = useState<BoxType>("dodecahedron");
  const [salt, setSalt] = useState(0.15);
  const [temperature, setTemperature] = useState(300);
  const [pressure, setPressure] = useState(1.0);
  const [priority, setPriority] = useState<Priority>("normal");
  const [useGpu, setUseGpu] = useState(true);
  const [keepWaters, setKeepWaters] = useState(false);
  const [keepIons, setKeepIons] = useState(true);
  const [selectChain, setSelectChain] = useState("All");
  const [hetatmDecisions, setHetatmDecisions] = useState<
    Record<string, HetatmDecision>
  >({});

  // Submit
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitCode, setSubmitCode] = useState<string | null>(null);

  const effectiveMdLength =
    preset === "custom" ? mdLengthNs : PRESET_NS[preset];

  // Complex-CIF mode: no docked poses, but a protein+ligand complex (CIF/PDB) in the receptor
  // field together with the ligand SMILES. The server splits the complex into a protein-only
  // receptor + the ligand's bound coordinates as a single pose, so the pipeline runs unchanged.
  const complexMode = !poseFile && Boolean(receptorFile) && smiles.trim().length > 0;
  const canUpload = (Boolean(poseFile) || complexMode) && !validating;

  // The job is blocked when validation has a hard-rule failure (CONTRACT §7).
  const blockingReason = useMemo<string | null>(() => {
    if (!report) return null;
    if (report.ok) return null;
    if (!report.atom_mapping.success && report.atom_mapping.attempted) {
      return "Atom mapping between the chemistry template and the docked poses failed. Provide the original SDF/MOL2 used for docking, or a protonation-resolved ligand definition.";
    }
    if (report.chem_source === "none") {
      return "A ligand chemistry definition is required. Upload an SDF/MOL2 file, a valid isomeric SMILES, or a Meeko mapping. Bond orders cannot be derived from PDBQT alone.";
    }
    if (report.errors.length > 0) {
      return report.errors.join(" ");
    }
    return "Validation did not pass. Review the report below.";
  }, [report]);

  const canSubmit =
    Boolean(upload) &&
    Boolean(report) &&
    report?.ok === true &&
    blockingReason === null &&
    !submitting;

  const resetValidation = () => {
    setUpload(null);
    setReport(null);
    setValidateError(null);
    setSubmitError(null);
    setSubmitCode(null);
  };

  const onUploadAndValidate = async () => {
    if (!poseFile && !complexMode) return;
    setValidating(true);
    setValidateError(null);
    setReport(null);
    setUpload(null);
    try {
      const up = await uploadApi.createInput({
        poseFile,
        chemistryFile,
        receptorFile,
        smiles,
      });
      setUpload(up);
      // Seed defaults from detection.
      setTopN(Math.min(up.detected_pose_count || 3, 3) || 1);
      const seededDecisions: Record<string, HetatmDecision> = {};
      for (const h of up.hetatm_candidates) {
        seededDecisions[h.resname] = h.suggested;
      }
      setHetatmDecisions(seededDecisions);

      const rep = await uploadApi.validate(up.upload_id);
      setReport(rep);
      if (rep.poses.length > 0) {
        setTopN(Math.min(rep.poses.length, topN || 3));
      }
      if (rep.ligand_type_candidates.length > 0) {
        setLigandType(rep.ligand_type_candidates[0]);
      }
      // Refresh decisions from the richer validate report if present.
      if (rep.hetatm_candidates.length > 0) {
        const merged: Record<string, HetatmDecision> = { ...seededDecisions };
        for (const h of rep.hetatm_candidates) {
          if (!(h.resname in merged)) merged[h.resname] = h.suggested;
        }
        setHetatmDecisions(merged);
      }
    } catch (err) {
      setValidateError(normalizeError(err).message);
    } finally {
      setValidating(false);
    }
  };

  const chemSourceForJob = (): ChemSource => {
    const src = report?.chem_source;
    if (src && src !== "none") return src;
    if (smiles.trim()) return "smiles";
    return "manual";
  };

  const onSubmit = async () => {
    if (!upload || !report || !canSubmit) return;
    setSubmitting(true);
    setSubmitError(null);
    setSubmitCode(null);
    try {
      const job = await jobApi.create({
        upload_id: upload.upload_id,
        name: name.trim() || undefined,
        ligand_type: ligandType,
        ligand_chem_source: chemSourceForJob(),
        top_n_poses: topN,
        n_replicas: nReplicas,
        md_length_ns: effectiveMdLength,
        md_preset: preset,
        force_field: "ff19SB",
        ligand_force_field: "gaff2",
        water_model: "opc",
        box_type: boxType,
        salt_concentration: salt,
        temperature,
        pressure,
        use_gpu: useGpu,
        priority,
        hetatm_decisions: hetatmDecisions,
        cif_options: {
          keep_waters: keepWaters,
          keep_ions: keepIons,
          select_chain: selectChain,
        },
      });
      navigate(`/jobs/${job.id}`);
    } catch (err) {
      const n = normalizeError(err);
      setSubmitCode(n.code);
      setSubmitError(n.message);
      if (n.report) setReport(n.report);
    } finally {
      setSubmitting(false);
    }
  };

  const poseColumns: Column<PoseSummary>[] = [
    { key: "index", header: "Pose", render: (p) => `#${p.index}` },
    {
      key: "score",
      header: "Docking score (kcal/mol)",
      align: "right",
      render: (p) => formatScore(p.docking_score),
    },
    {
      key: "selected",
      header: "Included",
      align: "center",
      render: (p) =>
        p.index <= topN ? (
          <span className="badge bg-green-100 text-green-700">top-{topN}</span>
        ) : (
          <span className="text-slate-400">—</span>
        ),
    },
  ];

  const hetatmColumns: Column<HetatmCandidate>[] = [
    { key: "resname", header: "Residue", render: (h) => h.resname },
    { key: "count", header: "Count", align: "right", render: (h) => h.count },
    {
      key: "suggested",
      header: "Suggested",
      render: (h) => titleCase(h.suggested),
    },
    {
      key: "decision",
      header: "Decision",
      render: (h) => (
        <select
          className="input !w-auto !py-1"
          value={hetatmDecisions[h.resname] ?? h.suggested}
          onChange={(e) =>
            setHetatmDecisions((prev) => ({
              ...prev,
              [h.resname]: e.target.value as HetatmDecision,
            }))
          }
        >
          {HETATM_DECISIONS.map((d) => (
            <option key={d} value={d}>
              {titleCase(d)}
            </option>
          ))}
        </select>
      ),
    },
  ];

  const storageGb = estimateStorageGb(topN, effectiveMdLength);
  const isCif = report?.input_type === "cif" || report?.receptor?.format === "cif";

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">New MD job</h1>
        <p className="mt-1 text-sm text-slate-500">
          Upload a docking pose file with a ligand chemistry definition and a
          receptor structure. Coordinates come from the poses; chemistry comes
          from the definition file. Alternatively, upload a protein+ligand
          complex (CIF/PDB) in the receptor field with a SMILES and no pose
          file — it is split into a receptor + a single bound pose.
        </p>
      </div>

      {/* Step 1 — inputs */}
      <Card title="1 · Input files">
        <div className="grid gap-5 md:grid-cols-2">
          <FileField
            id="pose-file"
            label="Docking poses (PDBQT)"
            hint={
              complexMode
                ? "Optional in complex mode — the pose is derived from the complex below."
                : "AutoDock Vina multi-pose PDBQT. Used for coordinates only. Omit if you upload a protein+ligand complex below."
            }
            accept=".pdbqt,.txt"
            required={!complexMode}
            file={poseFile}
            onChange={(f) => {
              setPoseFile(f);
              resetValidation();
            }}
          />
          <FileField
            id="chemistry-file"
            label="Ligand chemistry (SDF / MOL2)"
            hint="Defines bond orders, formal charges, and protonation state."
            accept=".sdf,.mol2,.mol"
            file={chemistryFile}
            onChange={(f) => {
              setChemistryFile(f);
              resetValidation();
            }}
          />
          <FileField
            id="receptor-file"
            label="Receptor (PDB / CIF)"
            hint="Receptor structure — or a protein+ligand COMPLEX (CIF/PDB). With no pose file and a SMILES, the complex is split into receptor + a single bound pose."
            accept=".pdb,.cif,.mmcif,.ent"
            file={receptorFile}
            onChange={(f) => {
              setReceptorFile(f);
              resetValidation();
            }}
          />
          <div>
            <label className="label" htmlFor="smiles">
              SMILES (optional)
            </label>
            <input
              id="smiles"
              className="input font-mono"
              placeholder="Isomeric, protonation-resolved SMILES"
              value={smiles}
              onChange={(e) => {
                setSmiles(e.target.value);
                resetValidation();
              }}
            />
            <p className="mt-1 text-xs text-slate-500">
              {complexMode
                ? "Supplies bond orders for the ligand extracted from the complex."
                : "Accepted only if atom mapping to the poses succeeds."}
            </p>
          </div>
        </div>

        <div className="mt-5 flex items-center gap-3">
          <button
            type="button"
            className="btn-primary"
            disabled={!canUpload}
            onClick={onUploadAndValidate}
          >
            {validating ? "Validating…" : "Upload and validate"}
          </button>
          {validating && <Spinner />}
        </div>

        {validateError && (
          <div className="mt-4">
            <ErrorBanner
              message={validateError}
              onDismiss={() => setValidateError(null)}
            />
          </div>
        )}
      </Card>

      {/* Step 2 — validation report */}
      {report && (
        <Card
          title="2 · Validation report"
          actions={
            report.ok ? (
              <span className="badge bg-green-100 text-green-700">Passed</span>
            ) : (
              <span className="badge bg-red-100 text-red-700">
                Action required
              </span>
            )
          }
        >
          {blockingReason && (
            <div className="mb-4">
              <ErrorBanner message={blockingReason} />
            </div>
          )}

          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Detail label="Input type" value={report.input_type.toUpperCase()} />
            <Detail label="Poses detected" value={String(report.pose_count)} />
            <Detail
              label="Chemistry source"
              value={
                report.chem_source === "none"
                  ? "None"
                  : report.chem_source.toUpperCase()
              }
            />
            <Detail
              label="Ligand candidates"
              value={
                report.ligand_type_candidates.map(titleCase).join(", ") || "—"
              }
            />
          </div>

          {/* Atom mapping */}
          <div className="mt-5 rounded-md border border-slate-200 bg-slate-50 p-4">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-sm font-semibold text-slate-700">
                Atom mapping
              </span>
              {report.atom_mapping.attempted ? (
                report.atom_mapping.success ? (
                  <span className="badge bg-green-100 text-green-700">
                    Success
                  </span>
                ) : (
                  <span className="badge bg-red-100 text-red-700">Failed</span>
                )
              ) : (
                <span className="badge bg-slate-200 text-slate-600">
                  Not attempted
                </span>
              )}
            </div>
            <p className="text-sm text-slate-600">{report.atom_mapping.message}</p>
            {report.atom_mapping.attempted && (
              <div className="mt-3 grid gap-3 text-sm sm:grid-cols-3">
                <Detail
                  label="Template formula"
                  value={report.atom_mapping.molformula_template ?? "—"}
                  small
                />
                <Detail
                  label="Pose formula"
                  value={report.atom_mapping.molformula_pose ?? "—"}
                  small
                />
                <Detail
                  label="Matched heavy atoms"
                  value={
                    report.atom_mapping.matched_atoms != null
                      ? `${report.atom_mapping.matched_atoms} / ${
                          report.atom_mapping.template_heavy_atoms ?? "?"
                        }`
                      : "—"
                  }
                  small
                />
              </div>
            )}
          </div>

          {/* Poses */}
          <div className="mt-5">
            <h3 className="mb-2 text-sm font-semibold text-slate-700">
              Poses
            </h3>
            <DataTable
              columns={poseColumns}
              rows={report.poses}
              rowKey={(p) => p.index}
              empty="No poses parsed."
            />
          </div>

          {/* Receptor */}
          {report.receptor && (
            <div className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
              <Detail
                label="Receptor format"
                value={report.receptor.format.toUpperCase()}
              />
              <Detail
                label="Chains"
                value={report.receptor.chains.join(", ") || "—"}
              />
              <Detail
                label="Residues"
                value={String(report.receptor.n_residues)}
              />
              <Detail label="Atoms" value={String(report.receptor.n_atoms)} />
              <Detail
                label="HETATM present"
                value={report.receptor.has_hetatm ? "Yes" : "No"}
              />
            </div>
          )}

          {/* HETATM review */}
          {report.hetatm_candidates.length > 0 && (
            <div className="mt-5">
              <h3 className="mb-1 text-sm font-semibold text-slate-700">
                HETATM review
              </h3>
              <p className="mb-2 text-xs text-slate-500">
                HETATM records are never treated as ligand automatically.
                Confirm how each residue should be handled.
              </p>
              <DataTable
                columns={hetatmColumns}
                rows={report.hetatm_candidates}
                rowKey={(h) => h.resname}
              />
            </div>
          )}

          {report.warnings.length > 0 && (
            <ul className="mt-4 list-inside list-disc space-y-1 text-sm text-amber-700">
              {report.warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          )}
        </Card>
      )}

      {/* Step 3 — options + submit */}
      {report && (
        <Card title="3 · Job options">
          <div className="grid gap-5 lg:grid-cols-2">
            <div>
              <label className="label" htmlFor="job-name">
                Job name
              </label>
              <input
                id="job-name"
                className="input"
                placeholder="Auto-generated if blank"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>

            <div>
              <label className="label" htmlFor="ligand-type">
                Ligand type
              </label>
              <select
                id="ligand-type"
                className="input"
                value={ligandType}
                onChange={(e) => setLigandType(e.target.value as LigandType)}
              >
                {LIGAND_TYPES.map((lt) => (
                  <option key={lt} value={lt}>
                    {titleCase(lt)}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="label" htmlFor="top-n">
                Top poses (sub-jobs)
              </label>
              <input
                id="top-n"
                type="number"
                min={1}
                max={Math.max(1, report.pose_count)}
                className="input"
                value={topN}
                onChange={(e) =>
                  setTopN(
                    Math.max(
                      1,
                      Math.min(
                        report.pose_count || 1,
                        Number(e.target.value) || 1,
                      ),
                    ),
                  )
                }
              />
              <p className="mt-1 text-xs text-slate-500">
                Each selected pose runs as an independent MD sub-job.
              </p>
            </div>

            <div>
              <label className="label" htmlFor="n-replicas">
                Replicas per pose
              </label>
              <input
                id="n-replicas"
                type="number"
                min={1}
                max={10}
                step={1}
                className="input"
                value={nReplicas}
                onChange={(e) =>
                  setNReplicas(Math.max(1, Math.min(10, Math.floor(Number(e.target.value)) || 1)))
                }
              />
              <p className="mt-1 text-xs text-slate-500">
                Independent MD repeats (different random seeds) per pose. &gt;1 reports
                mean ± SEM of the binding score; multiplies compute by this factor.
              </p>
            </div>

            <div>
              <label className="label" htmlFor="preset">
                MD preset
              </label>
              <select
                id="preset"
                className="input"
                value={preset}
                onChange={(e) => setPreset(e.target.value as MdPreset)}
              >
                {MD_PRESETS.map((p) => (
                  <option
                    key={p}
                    value={p}
                    disabled={p === "custom" && !isAdmin}
                  >
                    {p === "quick" && "Quick — 10 ns"}
                    {p === "standard" && "Standard — 50 ns (default)"}
                    {p === "extended" && "Extended — 100 ns"}
                    {p === "custom" &&
                      (isAdmin ? "Custom" : "Custom (admin only)")}
                  </option>
                ))}
              </select>
            </div>

            {preset === "custom" && isAdmin && (
              <div>
                <label className="label" htmlFor="md-length">
                  MD length (ns)
                </label>
                <input
                  id="md-length"
                  type="number"
                  min={1}
                  className="input"
                  value={mdLengthNs}
                  onChange={(e) =>
                    setMdLengthNs(Math.max(1, Number(e.target.value) || 1))
                  }
                />
              </div>
            )}

            <div>
              <label className="label" htmlFor="priority">
                Priority
              </label>
              <select
                id="priority"
                className="input"
                value={priority}
                onChange={(e) => setPriority(e.target.value as Priority)}
                disabled={!isAdmin && priority === "normal"}
              >
                {PRIORITIES.map((p) => (
                  <option key={p} value={p} disabled={p !== "normal" && !isAdmin}>
                    {titleCase(p)}
                    {p !== "normal" && !isAdmin ? " (admin only)" : ""}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {/* Advanced options */}
          <details className="mt-5 rounded-md border border-slate-200">
            <summary className="cursor-pointer select-none px-4 py-3 text-sm font-medium text-slate-700">
              Advanced options
            </summary>
            <div className="border-t border-slate-200 p-4">
              <div className="grid gap-5 lg:grid-cols-2">
                <Readonly label="Protein force field" value="AMBER ff19SB (fixed)" />
                <Readonly label="Ligand force field" value="GAFF2 / AM1-BCC (fixed)" />
                <Readonly label="Water model" value="OPC (4-point, fixed)" />
                <div>
                  <label className="label" htmlFor="box-type">
                    Box type
                  </label>
                  <select
                    id="box-type"
                    className="input"
                    value={boxType}
                    onChange={(e) => setBoxType(e.target.value as BoxType)}
                  >
                    {BOX_TYPES.map((b) => (
                      <option key={b} value={b}>
                        {titleCase(b)}
                      </option>
                    ))}
                  </select>
                </div>
                <NumberField
                  id="salt"
                  label="Salt concentration (M)"
                  value={salt}
                  step={0.01}
                  min={0}
                  onChange={setSalt}
                />
                <NumberField
                  id="temperature"
                  label="Temperature (K)"
                  value={temperature}
                  step={1}
                  min={0}
                  onChange={setTemperature}
                />
                <NumberField
                  id="pressure"
                  label="Pressure (bar)"
                  value={pressure}
                  step={0.1}
                  min={0}
                  onChange={setPressure}
                />
                <label className="flex items-center gap-2 self-end text-sm text-slate-700">
                  <input
                    type="checkbox"
                    checked={useGpu}
                    onChange={(e) => setUseGpu(e.target.checked)}
                  />
                  Use GPU
                </label>
              </div>

              <p className="mt-3 text-xs text-slate-500">
                ff19SB + OPC is the platform default. If the ff19SB GROMACS port is
                unavailable on the compute node, the run falls back to AMBER ff14SB +
                TIP3P; the force field actually used is recorded per job and shown on
                the job detail page.
              </p>

              {isCif && (
                <div className="mt-5 border-t border-slate-200 pt-4">
                  <h4 className="mb-3 text-sm font-semibold text-slate-700">
                    CIF / PDB receptor options
                  </h4>
                  <div className="grid gap-4 lg:grid-cols-3">
                    <label className="flex items-center gap-2 text-sm text-slate-700">
                      <input
                        type="checkbox"
                        checked={keepWaters}
                        onChange={(e) => setKeepWaters(e.target.checked)}
                      />
                      Keep crystallographic waters
                    </label>
                    <label className="flex items-center gap-2 text-sm text-slate-700">
                      <input
                        type="checkbox"
                        checked={keepIons}
                        onChange={(e) => setKeepIons(e.target.checked)}
                      />
                      Keep ions
                    </label>
                    <div>
                      <label className="label" htmlFor="select-chain">
                        Select chain
                      </label>
                      <input
                        id="select-chain"
                        className="input"
                        value={selectChain}
                        onChange={(e) => setSelectChain(e.target.value)}
                        placeholder="All"
                      />
                    </div>
                  </div>
                </div>
              )}
            </div>
          </details>

          {/* Storage estimate + submit */}
          <div className="mt-5 flex flex-col gap-4 border-t border-slate-200 pt-5 sm:flex-row sm:items-center sm:justify-between">
            <div className="text-sm text-slate-600">
              <span className="font-medium text-slate-800">
                Estimated storage:
              </span>{" "}
              ~{storageGb.toFixed(1)} GB
              <span className="text-slate-400">
                {" "}
                ({topN} pose{topN === 1 ? "" : "s"} · {effectiveMdLength} ns)
              </span>
            </div>
            <div className="flex items-center gap-3">
              <button
                type="button"
                className="btn-primary"
                disabled={!canSubmit}
                onClick={onSubmit}
                title={blockingReason ?? undefined}
              >
                {submitting ? "Submitting…" : "Create job"}
              </button>
            </div>
          </div>

          {!report.ok && (
            <p className="mt-2 text-right text-xs text-red-600">
              Submission is disabled until validation passes.
            </p>
          )}

          {submitError && (
            <div className="mt-4">
              <ErrorBanner
                message={submitError}
                code={submitCode}
                onDismiss={() => setSubmitError(null)}
              />
            </div>
          )}
        </Card>
      )}
    </div>
  );
}

function Detail({
  label,
  value,
  small,
}: {
  label: string;
  value: string;
  small?: boolean;
}) {
  return (
    <div>
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">
        {label}
      </div>
      <div
        className={`mt-0.5 font-medium text-slate-800 ${
          small ? "text-sm font-mono" : ""
        }`}
      >
        {value}
      </div>
    </div>
  );
}

function Readonly({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <label className="label">{label}</label>
      <input className="input" value={value} readOnly disabled />
    </div>
  );
}

function NumberField({
  id,
  label,
  value,
  step,
  min,
  onChange,
}: {
  id: string;
  label: string;
  value: number;
  step: number;
  min: number;
  onChange: (n: number) => void;
}) {
  return (
    <div>
      <label className="label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        type="number"
        className="input"
        value={value}
        step={step}
        min={min}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  );
}
