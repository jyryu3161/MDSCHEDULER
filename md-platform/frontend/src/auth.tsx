import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { Navigate, useLocation } from "react-router-dom";
import { authApi, clearToken, getToken, setToken } from "./api";
import type { Me, UserRole } from "./types";

interface AuthState {
  ready: boolean; // initial session restore finished
  token: string | null;
  user: Me | null;
  isAuthenticated: boolean;
  isAdmin: boolean;
  mustChangePassword: boolean;
}

interface AuthContextValue extends AuthState {
  login: (username: string, password: string) => Promise<Me>;
  logout: () => Promise<void>;
  changePassword: (oldPw: string, newPw: string) => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const [token, setTok] = useState<string | null>(getToken());
  const [user, setUser] = useState<Me | null>(null);

  const refresh = useCallback(async () => {
    if (!getToken()) {
      setUser(null);
      return;
    }
    const me = await authApi.me();
    setUser(me);
  }, []);

  // Restore session on first mount: if a token exists, fetch /auth/me.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (getToken()) {
        try {
          const me = await authApi.me();
          if (!cancelled) setUser(me);
        } catch {
          // Invalid/expired token; drop it.
          clearToken();
          if (!cancelled) {
            setTok(null);
            setUser(null);
          }
        }
      }
      if (!cancelled) setReady(true);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const resp = await authApi.login(username, password);
    setToken(resp.access_token);
    setTok(resp.access_token);
    // Construct a provisional user from the login response, then confirm via /me.
    const me = await authApi.me();
    setUser(me);
    return me;
  }, []);

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      // Stateless logout: ignore network errors and drop the token regardless.
    }
    clearToken();
    setTok(null);
    setUser(null);
  }, []);

  const changePassword = useCallback(
    async (oldPw: string, newPw: string) => {
      await authApi.changePassword(oldPw, newPw);
      // Server clears must_change_password; refresh local user.
      await refresh();
    },
    [refresh],
  );

  const value = useMemo<AuthContextValue>(() => {
    const role: UserRole | undefined = user?.role;
    return {
      ready,
      token,
      user,
      isAuthenticated: Boolean(token && user),
      isAdmin: role === "admin",
      mustChangePassword: Boolean(user?.must_change_password),
      login,
      logout,
      changePassword,
      refresh,
    };
  }, [ready, token, user, login, logout, changePassword, refresh]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}

// ── Route guards (CONTRACT §10 frontend) ─────────────────────────────────────

// Full-page placeholder shown while the initial session restore is in flight,
// so guards do not flash the login page for an authenticated user on reload.
function GuardPending() {
  return (
    <div className="flex min-h-screen items-center justify-center text-sm text-slate-500">
      <span className="inline-block h-5 w-5 animate-spin rounded-full border-2 border-slate-300 border-t-brand-500" />
      <span className="ml-2">Loading…</span>
    </div>
  );
}

// Gate routes behind authentication. Unauthenticated users are redirected to
// /login with a `next` param so they return to the intended page after login.
export function RequireAuth({ children }: { children: ReactNode }) {
  const { ready, isAuthenticated } = useAuth();
  const location = useLocation();
  if (!ready) return <GuardPending />;
  if (!isAuthenticated) {
    const next = `${location.pathname}${location.search}`;
    return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />;
  }
  return <>{children}</>;
}

// Gate admin-only routes. Authenticated non-admins are sent to the dashboard;
// unauthenticated users are sent to login (same as RequireAuth).
export function RequireAdmin({ children }: { children: ReactNode }) {
  const { ready, isAuthenticated, isAdmin } = useAuth();
  const location = useLocation();
  if (!ready) return <GuardPending />;
  if (!isAuthenticated) {
    const next = `${location.pathname}${location.search}`;
    return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />;
  }
  if (!isAdmin) return <Navigate to="/" replace />;
  return <>{children}</>;
}
