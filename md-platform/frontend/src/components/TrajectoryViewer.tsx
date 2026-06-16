import { useCallback, useEffect, useRef, useState } from "react";
import * as NGL from "ngl";
import { resultsApi } from "../api";
import { Spinner, ErrorBanner } from "./ui";

type ProteinStyle = "cartoon" | "surface" | "backbone" | "tube";
type LigandStyle = "licorice" | "ball+stick" | "spacefill" | "hyperball";

// NGL does not re-export the TrajectoryElement class from its top-level entry,
// so we derive the type from StructureComponent.addTrajectory's return value.
type NglTrajectoryElement = ReturnType<NGL.StructureComponent["addTrajectory"]>;

const PROTEIN_STYLES: ProteinStyle[] = ["cartoon", "surface", "backbone", "tube"];
const LIGAND_STYLES: LigandStyle[] = [
  "licorice",
  "ball+stick",
  "spacefill",
  "hyperball",
];

// Heuristic ligand selection: residue named MOL/LIG (our pipeline) or any
// hetero atom that is not water/ion. Protein selection is the complement.
const LIGAND_SELECTION =
  "(MOL or LIG or UNL or UNK) or (hetero and not (water or ion))";
const PROTEIN_SELECTION = "protein or polymer";

interface Props {
  jobId: string;
  subjobId: string;
  height?: number;
}

export function TrajectoryViewer({ jobId, subjobId, height = 460 }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<NGL.Stage | null>(null);
  const componentRef = useRef<NGL.StructureComponent | null>(null);
  const trajRef = useRef<NglTrajectoryElement | null>(null);
  const playerRef = useRef<NGL.TrajectoryPlayer | null>(null);
  const proteinReprRef = useRef<NGL.RepresentationElement | null>(null);
  const ligandReprRef = useRef<NGL.RepresentationElement | null>(null);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [frameCount, setFrameCount] = useState(0);
  const [currentFrame, setCurrentFrame] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [proteinStyle, setProteinStyle] = useState<ProteinStyle>("cartoon");
  const [ligandStyle, setLigandStyle] = useState<LigandStyle>("licorice");
  const [centerLigand, setCenterLigand] = useState(true);
  const [xtcNotice, setXtcNotice] = useState(false);

  // Apply protein representation, replacing the previous one.
  const applyProteinRepr = useCallback((style: ProteinStyle) => {
    const comp = componentRef.current;
    if (!comp) return;
    proteinReprRef.current?.dispose();
    proteinReprRef.current = comp.addRepresentation(style, {
      sele: PROTEIN_SELECTION,
      colorScheme: "chainindex",
    });
  }, []);

  const applyLigandRepr = useCallback((style: LigandStyle) => {
    const comp = componentRef.current;
    if (!comp) return;
    ligandReprRef.current?.dispose();
    ligandReprRef.current = comp.addRepresentation(style, {
      sele: LIGAND_SELECTION,
      colorScheme: "element",
      multipleBond: "symmetric",
    });
  }, []);

  // Initialize the stage and load the trajectory blob once per subjob.
  useEffect(() => {
    let disposed = false;
    setLoading(true);
    setError(null);
    setXtcNotice(false);
    setPlaying(false);
    setCurrentFrame(0);
    setFrameCount(0);

    const el = containerRef.current;
    if (!el) return;

    const stage = new NGL.Stage(el, { backgroundColor: "white" });
    stageRef.current = stage;
    const handleResize = () => stage.handleResize();
    window.addEventListener("resize", handleResize);

    (async () => {
      try {
        const payload = await resultsApi.trajectory(jobId, subjobId);
        if (disposed) return;

        if (payload.format === "xtc") {
          // The MVP viewer ingests multi-model PDB. A raw .xtc needs its
          // companion topology to render; surface a clear notice instead of a
          // blank canvas. Real-engine results also ship trajectory.pdb.
          setXtcNotice(true);
          setLoading(false);
          return;
        }

        const file = new File([payload.blob], "trajectory.pdb", {
          type: "chemical/x-pdb",
        });
        const comp = (await stage.loadFile(file, {
          ext: "pdb",
          asTrajectory: true,
        })) as NGL.StructureComponent;
        if (disposed) {
          return;
        }
        componentRef.current = comp;

        applyProteinRepr(proteinStyle);
        applyLigandRepr(ligandStyle);

        // Attach the in-memory frames as a trajectory.
        const traj = comp.addTrajectory();
        trajRef.current = traj;
        const nFrames = traj.trajectory.frameCount;
        setFrameCount(nFrames);

        traj.signals.frameChanged.add((frame: number) => {
          if (!disposed) setCurrentFrame(frame);
        });

        const player = new NGL.TrajectoryPlayer(traj.trajectory, {
          step: 1,
          timeout: 80,
          interpolateType: "linear",
          mode: "loop",
        } as Partial<ConstructorParameters<typeof NGL.TrajectoryPlayer>[1]>);
        playerRef.current = player;

        comp.autoView();
        setLoading(false);
      } catch (err) {
        if (!disposed) {
          setError(
            err instanceof Error
              ? err.message
              : "Failed to load the trajectory.",
          );
          setLoading(false);
        }
      }
    })();

    return () => {
      disposed = true;
      window.removeEventListener("resize", handleResize);
      try {
        playerRef.current?.pause?.();
      } catch {
        /* ignore */
      }
      playerRef.current = null;
      trajRef.current = null;
      proteinReprRef.current = null;
      ligandReprRef.current = null;
      componentRef.current = null;
      stage.dispose();
      stageRef.current = null;
    };
    // Re-init only when the target subjob changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, subjobId]);

  // React to representation style changes after init.
  useEffect(() => {
    if (!loading) applyProteinRepr(proteinStyle);
  }, [proteinStyle, loading, applyProteinRepr]);

  useEffect(() => {
    if (!loading) applyLigandRepr(ligandStyle);
  }, [ligandStyle, loading, applyLigandRepr]);

  // Center on ligand (binding-site view) or whole complex.
  useEffect(() => {
    const comp = componentRef.current;
    if (!comp || loading) return;
    if (centerLigand) {
      comp.autoView(LIGAND_SELECTION, 2000);
    } else {
      comp.autoView(2000);
    }
  }, [centerLigand, loading]);

  const togglePlay = useCallback(() => {
    const player = playerRef.current;
    if (!player) return;
    if (playing) {
      player.pause();
      setPlaying(false);
    } else {
      player.play();
      setPlaying(true);
    }
  }, [playing]);

  const seekFrame = useCallback((frame: number) => {
    const traj = trajRef.current;
    if (!traj) return;
    playerRef.current?.pause?.();
    setPlaying(false);
    traj.trajectory.setFrame(frame);
    setCurrentFrame(frame);
  }, []);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-1 text-sm text-slate-600">
          Protein
          <select
            className="input !w-auto !py-1"
            value={proteinStyle}
            onChange={(e) => setProteinStyle(e.target.value as ProteinStyle)}
            disabled={loading}
          >
            {PROTEIN_STYLES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1 text-sm text-slate-600">
          Ligand
          <select
            className="input !w-auto !py-1"
            value={ligandStyle}
            onChange={(e) => setLigandStyle(e.target.value as LigandStyle)}
            disabled={loading}
          >
            {LIGAND_STYLES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1.5 text-sm text-slate-600">
          <input
            type="checkbox"
            checked={centerLigand}
            onChange={(e) => setCenterLigand(e.target.checked)}
            disabled={loading}
          />
          Ligand-centered view
        </label>
      </div>

      <div
        ref={containerRef}
        className="ngl-viewport relative w-full overflow-hidden rounded-md border border-slate-200 bg-white"
        style={{ height }}
      >
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-white/70">
            <Spinner label="Loading trajectory…" />
          </div>
        )}
      </div>

      {error && <ErrorBanner message={error} />}
      {xtcNotice && (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          The server returned a compressed XTC trajectory. The in-browser viewer
          renders multi-model PDB; download the results package to inspect the
          XTC with its topology, or use the PDB trajectory when available.
        </div>
      )}

      {!loading && !xtcNotice && frameCount > 0 && (
        <div className="flex items-center gap-3">
          <button
            type="button"
            className="btn-secondary !px-2.5"
            onClick={togglePlay}
            aria-label={playing ? "Pause" : "Play"}
          >
            {playing ? "Pause" : "Play"}
          </button>
          <input
            type="range"
            min={0}
            max={Math.max(0, frameCount - 1)}
            value={currentFrame}
            onChange={(e) => seekFrame(Number(e.target.value))}
            className="flex-1 accent-brand-600"
            aria-label="Trajectory frame"
          />
          <span className="w-24 text-right text-xs tabular-nums text-slate-500">
            frame {currentFrame + 1} / {frameCount}
          </span>
        </div>
      )}
    </div>
  );
}
