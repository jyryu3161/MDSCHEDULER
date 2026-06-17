// Mirrors CONTRACT.md §2 (DB columns), §4 (enums), §6 (JobCreate), §7 (ValidationReport).
// Keep these in lock-step with the backend; the API client in api.ts returns these shapes.

// ── Enums (CONTRACT §4) ──────────────────────────────────────────────────────

export const JOB_STATUSES = [
  "uploaded",
  "validating",
  "queued",
  "preparing",
  "running_em",
  "running_nvt",
  "running_npt",
  "running_md",
  "analyzing",
  "rendering",
  "packaging",
  "completed",
  "failed",
  "cancelled",
] as const;
export type JobStatus = (typeof JOB_STATUSES)[number];

export const GPU_STATUSES = [
  "available",
  "busy",
  "disabled",
  "maintenance",
  "error",
] as const;
export type GpuStatusValue = (typeof GPU_STATUSES)[number];

export const LIGAND_TYPES = [
  "small_molecule",
  "peptide",
  "protein_partner",
  "cofactor",
  "unknown",
] as const;
export type LigandType = (typeof LIGAND_TYPES)[number];

export const CHEM_SOURCES = ["sdf", "mol2", "smiles", "meeko", "manual"] as const;
export type ChemSource = (typeof CHEM_SOURCES)[number];

export const PLOT_TYPES = [
  "rmsd",
  "rmsf",
  "rg",
  "sasa",
  "hbond",
  "energy",
  "ligand_rmsd",
  "contact_map",
  "per_residue",
] as const;
export type PlotType = (typeof PLOT_TYPES)[number];

export const MD_PRESETS = ["quick", "standard", "extended", "custom"] as const;
export type MdPreset = (typeof MD_PRESETS)[number];

export const PRIORITIES = ["low", "normal", "high"] as const;
export type Priority = (typeof PRIORITIES)[number];

export const INPUT_TYPES = ["pdbqt", "cif", "pdb", "mixed"] as const;
export type InputType = (typeof INPUT_TYPES)[number];

export const BOX_TYPES = ["dodecahedron", "cubic"] as const;
export type BoxType = (typeof BOX_TYPES)[number];

export const HETATM_DECISIONS = [
  "ligand",
  "cofactor",
  "ion",
  "water",
  "additive",
  "drop",
] as const;
export type HetatmDecision = (typeof HETATM_DECISIONS)[number];

export type UserRole = "admin" | "user";

// Hard-rule rejection codes (CONTRACT §7).
export type ValidationErrorCode =
  | "CHEMISTRY_REQUIRED"
  | "ATOM_MAPPING_FAILED"
  | "CHEMISTRY_MISMATCH";

// ── Auth ─────────────────────────────────────────────────────────────────────

export interface LoginResponse {
  access_token: string;
  token_type: "bearer";
  must_change_password: boolean;
  role: UserRole;
  username: string;
}

export interface Me {
  id: number;
  username: string;
  role: UserRole;
  must_change_password: boolean;
  is_active: boolean;
  created_at: string;
}

// ── Uploads (CONTRACT §5 Uploads, §7) ────────────────────────────────────────

export interface LigandTypeCandidate {
  ligand_type: LigandType;
  confidence?: number;
  reason?: string;
}

export interface HetatmCandidate {
  resname: string;
  count: number;
  suggested: HetatmDecision;
}

export interface UploadResponse {
  upload_id: string;
  pose_file: string;
  chemistry_file: string | null;
  receptor_file: string | null;
  detected_pose_count: number;
  detected_input_type: InputType;
  ligand_type_candidates: Array<LigandType | LigandTypeCandidate>;
  hetatm_candidates: HetatmCandidate[];
}

export interface PoseSummary {
  index: number;
  docking_score: number;
}

export interface AtomMapping {
  attempted: boolean;
  success: boolean;
  template_heavy_atoms: number | null;
  pose_heavy_atoms: number | null;
  molformula_template: string | null;
  molformula_pose: string | null;
  matched_atoms: number | null;
  message: string;
}

export interface ReceptorInfo {
  format: "pdb" | "cif";
  chains: string[];
  n_residues: number;
  n_atoms: number;
  has_hetatm: boolean;
}

export interface ValidationReport {
  ok: boolean;
  input_type: InputType;
  pose_count: number;
  poses: PoseSummary[];
  ligand_type_candidates: LigandType[];
  chem_source: ChemSource | "none";
  atom_mapping: AtomMapping;
  hetatm_candidates: HetatmCandidate[];
  receptor: ReceptorInfo | null;
  errors: string[];
  warnings: string[];
}

// ── Jobs (CONTRACT §2, §6) ───────────────────────────────────────────────────

export interface JobCreate {
  upload_id: string;
  name?: string;
  ligand_type: LigandType;
  ligand_chem_source: ChemSource;
  top_n_poses: number;
  n_replicas: number;
  md_length_ns: number;
  md_preset: MdPreset;
  force_field: string;
  ligand_force_field: string;
  water_model: string;
  box_type: BoxType;
  salt_concentration: number;
  temperature: number;
  pressure: number;
  use_gpu: boolean;
  priority: Priority;
  hetatm_decisions: Record<string, HetatmDecision>;
  cif_options: {
    keep_waters: boolean;
    keep_ions: boolean;
    select_chain: string;
  };
}

export interface Job {
  id: string;
  user_id: number;
  name: string;
  input_type: InputType;
  ligand_type: LigandType;
  status: JobStatus;
  md_length_ns: number;
  top_n_poses: number;
  n_replicas: number;
  force_field: string;
  ligand_force_field: string;
  ligand_chem_source: ChemSource;
  water_model: string;
  salt_concentration: number;
  temperature: number;
  pressure: number;
  box_type: BoxType;
  priority: Priority;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  result_path: string | null;
  error_message: string | null;
}

export interface SubJob {
  id: string;
  job_id: string;
  pose_index: number;
  replica_index: number;
  docking_score: number;
  status: JobStatus;
  assigned_gpu: number | null;
  progress: number;
  completed_ns: number;
  ns_per_day: number;
  current_step: string;
  started_at: string | null;
  completed_at: string | null;
  result_path: string | null;
  error_message: string | null;
}

export interface JobLog {
  id: number;
  job_id: string;
  subjob_id: string | null;
  level: "info" | "warning" | "error";
  step: string;
  message: string;
  created_at: string;
}

export interface ReplicaStat {
  n: number;
  mean: number | null;
  sem: number | null;
  std: number | null;
  min: number | null;
  max: number | null;
}

export interface ReplicaResult {
  replica_index: number;
  subjob_id: string;
  status: JobStatus;
  gbsa_dg_kcal_mol: number | null;
  pbsa_dg_kcal_mol: number | null;
  pose_occupancy: number | null;
}

export interface PoseReplicaAggregate {
  pose_index: number;
  n_replicas: number;
  gbsa: ReplicaStat;
  pbsa: ReplicaStat;
  pose_occupancy: ReplicaStat;
  replicas: ReplicaResult[];
}

export interface JobDetail {
  job: Job;
  subjobs: SubJob[];
  logs: JobLog[];
  replica_aggregates?: PoseReplicaAggregate[];
}

// ── Queue (CONTRACT §5 Queue) ────────────────────────────────────────────────

export interface QueueItem {
  job_id: string;
  subjob_id: string;
  job_name: string;
  user: string;
  pose_index: number;
  status: JobStatus;
  queue_position: number | null;
  assigned_gpu: number | null;
  progress: number;
  completed_ns: number;
  md_length_ns: number;
  ns_per_day: number;
  rough_eta_seconds: number | null;
}

export interface QueueResponse {
  items: QueueItem[];
  running: QueueItem[];
}

// ── GPU (CONTRACT §2 gpustatus) ──────────────────────────────────────────────

export const GPU_POOLS = ["md", "design", "excluded"] as const;
export type GpuPool = (typeof GPU_POOLS)[number];

export interface GpuStatus {
  gpu_id: number;
  name: string;
  status: GpuStatusValue;
  utilization: number;
  memory_used: number;
  memory_total: number;
  temperature: number;
  assigned_subjob_id: string | null;
  pool: GpuPool;
  capacity: number;
  running_count: number;
  updated_at: string;
}

// ── Dashboard (CONTRACT §5 Dashboard summary) ────────────────────────────────

export interface DashboardSummary {
  total_jobs: number;
  running_jobs: number;
  queued_jobs: number;
  completed_jobs: number;
  failed_jobs: number;
  gpus_available: number;
  gpus_busy: number;
  storage_used_gb: number;
  storage_total_gb: number;
}

// SSE /api/events/dashboard payload (CONTRACT §5 Realtime).
export interface DashboardEvent {
  summary: DashboardSummary;
  gpus: GpuStatus[];
  queue: QueueResponse;
}

// ── Results (CONTRACT §5 Results) ────────────────────────────────────────────

export interface AnalysisSummary {
  // Free-form summary metrics produced by the worker analyze step.
  // Common keys: rmsd_mean, rmsd_final, ligand_rmsd_mean, hbond_mean,
  // rg_mean, sasa_mean, stable (bool). Kept open to avoid coupling to
  // exact worker output while staying typed at call sites.
  [key: string]: number | string | boolean | null | undefined;
}

export interface SubJobResult {
  id: string;
  job_id: string;
  pose_index: number;
  docking_score: number;
  status: JobStatus;
  analysis_summary: AnalysisSummary | null;
  plots_available: PlotType[];
  has_trajectory: boolean;
  has_movie: boolean;
  mmpbsa: MmpbsaResult | null;
  per_residue: PerResidueDecomp | null;
  bound_window: BoundWindow | null;
  hotspots: Hotspot[];
}

// Auto-detected bound window: the leading trajectory segment where the ligand stays in the
// pocket (ligand RMSD < cutoff). MM/PBSA decomposition is restricted to this window.
export interface BoundWindow {
  start_ns: number;
  end_ns: number;
  n_bound_frames: number;
  n_total_frames: number;
  ligand_rmsd_cutoff_A?: number;
  criterion?: string;
  fully_bound?: boolean;
}

// One row of the unified binding-hotspot table: per-residue ΔG (MM/PBSA) merged with
// geometric contact frequency + mean H-bonds over the bound window. Any metric may be null
// when its source did not run.
export interface Hotspot {
  residue: string;
  chain: string;
  resname: string;
  resnum: number;
  total_dg: number | null;
  vdw: number | null;
  eel: number | null;
  contact_frequency: number | null;
  hbond_mean: number | null;
}

// MM/PBSA & MM/GBSA binding free energy (ΔG, kcal/mol), present only when computed.
export interface MmpbsaResult {
  gbsa_dg_kcal_mol?: number;
  pbsa_dg_kcal_mol?: number;
  method?: string;
  frames?: string;
  score_type?: string;          // "relative_ranking" — not an absolute ΔG
  pose_occupancy?: number | null; // fraction of trajectory the ligand stayed bound
  reliable?: boolean;           // false when occupancy < 0.5 (score untrustworthy)
  warning?: string;
  [k: string]: unknown;
}

// Per-residue ΔG decomposition: which peptide residues contribute most to binding.
export interface ResidueContribution {
  chain: string;
  resname: string;
  resnum: number;
  total_dg: number;
  vdw: number;
  eel: number;
  polar?: number;
  nonpolar?: number;
}
export interface PerResidueDecomp {
  method?: string;
  frames?: string;
  residues: ResidueContribution[];
  hotspots?: { residue: string; total_dg: number }[];
}

export interface JobResults {
  job: Job;
  subjobs: SubJobResult[];
}

export interface SubJobResultDetail {
  subjob: SubJob;
  analysis_summary: AnalysisSummary | null;
  plots_available: PlotType[];
  has_trajectory: boolean;
  has_movie: boolean;
  pose_comparison_entry: Record<string, number | string | boolean | null> | null;
}

// Plotly figure JSON returned by /plots/{plot_type}.
export interface PlotlyFigure {
  data: Array<Record<string, unknown>>;
  layout: Record<string, unknown>;
}

// Trajectory format header value (X-Trajectory-Format).
export type TrajectoryFormat = "pdb" | "xtc";

export interface TrajectoryPayload {
  format: TrajectoryFormat;
  blob: Blob;
}

// ── Peptide design (GA) ──────────────────────────────────────────────────────

export type DesignEvalMode = "hybrid" | "md_only";
export type DesignDockEngine = "vina" | "smina" | "gnina" | "auto";

export interface DesignJob {
  id: string;
  user_id: number;
  name: string;
  status: JobStatus;
  compound_name: string;
  peptide_length: number;
  population_size: number;
  num_generations: number;
  top_k_md: number;
  md_length_ns: number;
  eval_mode: DesignEvalMode;
  dock_engine: DesignDockEngine;
  current_generation: number;
  progress: number;
  assigned_gpu: number | null;
  best_sequence: string | null;
  best_fitness: number | null;
  best_docking_score: number | null;
  best_md_dg: number | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
}

export interface DesignCandidate {
  generation: number;
  sequence: string;
  docking_score: number | null;
  md_dg: number | null;
  fitness: number;
  refined: boolean;
}

export interface DesignGenerationPoint {
  generation: number;
  best_fitness: number;
  best_sequence: string;
  best_docking_score: number | null;
  best_md_dg: number | null;
}

export interface DesignJobDetail {
  job: DesignJob;
  candidates: DesignCandidate[];      // leaderboard, fitness desc
  generations: DesignGenerationPoint[]; // best-so-far convergence curve
}

export interface DesignJobCreate {
  name: string;
  initial_sequences: string;          // comma/space/newline-separated
  population_size: number;
  num_generations: number;
  top_k_md: number;
  md_length_ns: number;
  exhaustiveness: number;
  eval_mode: DesignEvalMode;     // hybrid (dock→top-k MD) | md_only (MD all)
  dock_engine: DesignDockEngine; // vina | smina | gnina | auto
  compound_name: string;
  smiles?: string;
  compound?: File | null;
}

// Generic backend error body for rejected job creation (CONTRACT §7).
export interface ApiErrorDetail {
  code?: ValidationErrorCode | string;
  message?: string;
  report?: ValidationReport;
}
