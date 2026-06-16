// Type shim for the prebuilt Plotly bundle. We import the dist-min build to keep
// the bundle small and avoid pulling Plotly's full source through the TS layer.
declare module "plotly.js-dist-min" {
  const Plotly: typeof import("plotly.js");
  export default Plotly;
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
