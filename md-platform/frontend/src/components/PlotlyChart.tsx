import { useMemo } from "react";
import createPlotlyComponent from "react-plotly.js/factory";
import Plotly from "plotly.js-dist-min";
import type { PlotlyFigure } from "../types";

// Bind react-plotly.js to the prebuilt dist-min bundle (smaller than the full
// source build and avoids bundling Plotly twice).
const Plot = createPlotlyComponent(Plotly);

const BASE_LAYOUT: Partial<Plotly.Layout> = {
  margin: { l: 56, r: 16, t: 36, b: 44 },
  font: { family: "Inter, system-ui, sans-serif", size: 12, color: "#334155" },
  paper_bgcolor: "rgba(0,0,0,0)",
  plot_bgcolor: "rgba(0,0,0,0)",
  legend: { orientation: "h", y: -0.2 },
};

const CONFIG: Partial<Plotly.Config> = {
  responsive: true,
  displaylogo: false,
  // Plotly's image-export buttons (PNG/SVG) satisfy the "graphs downloadable"
  // requirement (PDR §14.2 / §15.2) directly from the chart toolbar.
  modeBarButtonsToRemove: ["lasso2d", "select2d"],
  toImageButtonOptions: { format: "png", scale: 2 },
};

export function PlotlyChart({
  figure,
  title,
  height = 320,
}: {
  figure: PlotlyFigure;
  title?: string;
  height?: number;
}) {
  const layout = useMemo<Partial<Plotly.Layout>>(() => {
    return {
      ...BASE_LAYOUT,
      ...(figure.layout as Partial<Plotly.Layout>),
      ...(title ? { title: { text: title } } : {}),
      autosize: true,
      height,
    };
  }, [figure.layout, title, height]);

  return (
    <Plot
      data={figure.data as Plotly.Data[]}
      layout={layout}
      config={CONFIG}
      useResizeHandler
      style={{ width: "100%", height }}
    />
  );
}
