// Barrel export for reusable UI pieces. Pages import from "../components".
export {
  Card,
  StatCard,
  ProgressBar,
  Spinner,
  ErrorBanner,
  EmptyState,
  Modal,
} from "./ui";
export { JobStatusBadge, GpuStatusBadge } from "./StatusBadge";
export { Layout, NavBar } from "./Layout";
export { DataTable, type Column } from "./DataTable";
export { LogViewer } from "./LogViewer";
export { FileInput } from "./FileInput";
// NOTE: PlotlyChart (Plotly ~4.8 MB) and TrajectoryViewer (NGL ~1.3 MB) are intentionally NOT
// re-exported here — they are lazy-imported directly in the pages that use them (Results,
// DesignDetail) so these heavy deps stay code-split out of the main bundle.
