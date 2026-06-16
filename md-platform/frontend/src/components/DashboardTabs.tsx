import { NavLink } from "react-router-dom";

// The dashboard is split into two workstreams that run on separate GPU pools: standard MD
// jobs and GA-driven peptide design. This tab strip switches between the two views.
const TABS: { to: string; label: string; end?: boolean }[] = [
  { to: "/", label: "MD", end: true },
  { to: "/design", label: "Peptide Design" },
];

function tabClass({ isActive }: { isActive: boolean }): string {
  return [
    "border-b-2 px-4 py-2 text-sm font-medium transition-colors",
    isActive
      ? "border-brand-600 text-brand-700"
      : "border-transparent text-slate-500 hover:text-slate-800",
  ].join(" ");
}

export function DashboardTabs() {
  return (
    <div className="flex gap-1 border-b border-slate-200">
      {TABS.map((t) => (
        <NavLink key={t.to} to={t.to} end={t.end} className={tabClass}>
          {t.label}
        </NavLink>
      ))}
    </div>
  );
}
