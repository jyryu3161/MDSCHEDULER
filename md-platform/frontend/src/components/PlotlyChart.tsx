import { useMemo } from "react";
import createPlotlyComponent from "react-plotly.js/factory";
import Plotly from "plotly.js/lib/core";
import bar from "plotly.js/lib/bar";
import heatmap from "plotly.js/lib/heatmap";
import scatter from "plotly.js/lib/scatter";
import type { PlotlyFigure } from "../types";
import { FONT_FAMILY, PALETTE } from "../plotTheme";

Plotly.register([scatter, bar, heatmap]);

// Bind react-plotly.js to a custom Plotly bundle with only the trace modules produced by the
// worker (scatter, bar, heatmap). This keeps the lazy plot chunk much smaller than the full dist.
const Plot = createPlotlyComponent(Plotly);

// Publication defaults for any figure that doesn't carry its own full layout. Worker figures
// set a complete publication layout via mdworker.analysis.theme; this covers browser-built
// figures (e.g. the design convergence curve) and provides a consistent font/palette/background.
const BASE_LAYOUT: Partial<Plotly.Layout> = {
  margin: { l: 72, r: 26, t: 54, b: 58 },
  font: { family: FONT_FAMILY, size: 13, color: "#1a1a1a" },
  paper_bgcolor: "white",
  plot_bgcolor: "white",
  colorway: PALETTE,
  legend: { orientation: "h", y: -0.2 },
};

// Publication export: the toolbar camera writes a vector SVG (ideal for figures); a second
// button writes a high-resolution 4× PNG. Both satisfy "downloadable figures" (PDR §14.2/§15.2).
const CONFIG: Partial<Plotly.Config> = {
  responsive: true,
  displaylogo: false,
  modeBarButtonsToRemove: ["lasso2d", "select2d"],
  toImageButtonOptions: { format: "svg", filename: "md-figure", scale: 1 },
  modeBarButtonsToAdd: [
    {
      name: "downloadHiResPng",
      title: "Download high-resolution PNG (4×)",
      icon: (Plotly as unknown as { Icons: { camera: unknown } }).Icons.camera,
      click: (gd: unknown) =>
        (Plotly as unknown as { downloadImage: (g: unknown, o: object) => void }).downloadImage(
          gd,
          { format: "png", scale: 4, filename: "md-figure" },
        ),
    },
  ] as unknown as Plotly.ModeBarButtonAny[],
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
