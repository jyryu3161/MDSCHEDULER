// Type shims for Plotly's modular CommonJS trace bundle.
type PlotlyRegisterModule<T> = T extends readonly (infer U)[] ? U : T;
type PlotlyModule = PlotlyRegisterModule<Parameters<typeof import("plotly.js").register>[0]>;

declare module "plotly.js/lib/core" {
  const Plotly: typeof import("plotly.js");
  export default Plotly;
}

declare module "plotly.js/lib/scatter" {
  const scatter: PlotlyModule;
  export default scatter;
}

declare module "plotly.js/lib/bar" {
  const bar: PlotlyModule;
  export default bar;
}

declare module "plotly.js/lib/heatmap" {
  const heatmap: PlotlyModule;
  export default heatmap;
}

// @types/react-plotly.js declares the main entry but not the /factory subpath.
// createPlotlyComponent(plotly) returns the same component type as the default.
declare module "react-plotly.js/factory" {
  import type { PlotParams } from "react-plotly.js";
  import type * as React from "react";
  const createPlotlyComponent: (
    plotly: unknown,
  ) => React.ComponentType<PlotParams>;
  export default createPlotlyComponent;
}
