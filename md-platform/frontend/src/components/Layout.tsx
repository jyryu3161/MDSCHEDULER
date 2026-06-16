import { useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../auth";

// Top navigation links available to every authenticated user.
const NAV_LINKS: { to: string; label: string; end?: boolean }[] = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/design", label: "Peptide Design" },
  { to: "/upload", label: "New Job" },
];

function navLinkClass({ isActive }: { isActive: boolean }): string {
  return [
    "rounded-md px-3 py-2 text-sm font-medium transition-colors",
    isActive
      ? "bg-brand-50 text-brand-700"
      : "text-slate-600 hover:bg-slate-100 hover:text-slate-900",
  ].join(" ");
}

// Persistent top navigation bar. Shows the product name, primary links, the
// Admin link (admins only), the current user, and a logout control.
export function NavBar() {
  const { user, isAdmin, logout } = useAuth();
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);

  const onLogout = async () => {
    setBusy(true);
    try {
      await logout();
    } finally {
      setBusy(false);
      navigate("/login", { replace: true });
    }
  };

  return (
    <header className="sticky top-0 z-30 border-b border-slate-200 bg-white">
      <div className="mx-auto flex h-14 max-w-7xl items-center gap-4 px-4">
        <Link to="/" className="flex items-center gap-2 text-slate-900">
          <span className="grid h-7 w-7 place-items-center rounded-md bg-brand-600 text-sm font-bold text-white">
            MD
          </span>
          <span className="text-sm font-semibold tracking-tight">
            MD Platform
          </span>
        </Link>

        <nav className="flex items-center gap-1">
          {NAV_LINKS.map((link) => (
            <NavLink
              key={link.to}
              to={link.to}
              end={link.end}
              className={navLinkClass}
            >
              {link.label}
            </NavLink>
          ))}
          {isAdmin && (
            <NavLink to="/admin" className={navLinkClass}>
              Admin
            </NavLink>
          )}
        </nav>

        <div className="ml-auto flex items-center gap-3">
          {user && (
            <span className="hidden text-sm text-slate-600 sm:inline">
              {user.username}
              <span className="ml-1 rounded bg-slate-100 px-1.5 py-0.5 text-xs font-medium uppercase tracking-wide text-slate-500">
                {user.role}
              </span>
            </span>
          )}
          <button
            type="button"
            className="btn-secondary"
            onClick={onLogout}
            disabled={busy}
          >
            {busy ? "Signing out…" : "Sign out"}
          </button>
        </div>
      </div>
    </header>
  );
}

// Page shell: NavBar above a constrained content area. Used either with an
// explicit `children` or as a router layout element via <Outlet/>.
export function Layout({ children }: { children?: ReactNode }) {
  return (
    <div className="min-h-screen bg-slate-100">
      <NavBar />
      <main className="mx-auto max-w-7xl px-4 py-6">
        {children ?? <Outlet />}
      </main>
    </div>
  );
}
