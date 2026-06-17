// Publication-quality Plotly styling — TypeScript mirror of
// worker/mdworker/analysis/theme.py. Keep the two in sync. Used for figures built in the
// browser (e.g. the design convergence curve) and for shared chart defaults.

// Okabe–Ito colorblind-safe qualitative palette, ordered for lines/categories on white.
export const PALETTE: string[] = [
  "#0072B2", // blue
  "#D55E00", // vermillion
  "#009E73", // bluish green
  "#CC79A7", // reddish purple
  "#E69F00", // orange
  "#56B4E9", // sky blue
  "#555555", // gray
  "#F0E442", // yellow (use sparingly)
];

// Named semantic colors (mirror theme.py).
export const C = {
  backbone: "#0072B2",
  ligand: "#D55E00",
  rg: "#009E73",
  hbond: "#CC79A7",
  sasa: "#E69F00",
  favorable: "#0072B2",
  unfavorable: "#D55E00",
  accent: "#0072B2",
};

export const FONT_FAMILY = "Inter, Helvetica Neue, Helvetica, Arial, sans-serif";
const INK = "#1a1a1a";
const GRID = "#e8ecf1";
export const LINE_WIDTH = 2.2;

function axis(title: string): Record<string, unknown> {
  return {
    title: { text: title, font: { size: 15, color: INK } },
    showline: true,
    linecolor: INK,
    linewidth: 1.2,
    mirror: false,
    ticks: "outside",
    tickcolor: INK,
    ticklen: 5,
    tickwidth: 1.1,
    tickfont: { size: 13, color: INK },
    gridcolor: GRID,
    zeroline: false,
    automargin: true,
  };
}

// A complete publication layout (font, axes, white bg, legend, palette). Returns a plain
// object assignable to PlotlyFigure["layout"].
export function pubLayout(
  title: string,
  xaxis: string,
  yaxis: string,
  opts: { legend?: boolean; extra?: Record<string, unknown> } = {},
): Record<string, unknown> {
  return {
    title: { text: title, x: 0.02, xanchor: "left", font: { size: 17, color: INK } },
    xaxis: axis(xaxis),
    yaxis: axis(yaxis),
    font: { family: FONT_FAMILY, size: 13, color: INK },
    paper_bgcolor: "white",
    plot_bgcolor: "white",
    margin: { l: 72, r: 26, t: 54, b: 58 },
    colorway: PALETTE,
    hovermode: "x unified",
    showlegend: opts.legend ?? true,
    legend: {
      font: { size: 12, color: INK },
      bgcolor: "rgba(255,255,255,0.65)",
      borderwidth: 0,
    },
    ...(opts.extra ?? {}),
  };
}
