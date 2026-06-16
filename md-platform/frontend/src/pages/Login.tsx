import { useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "../auth";
import { normalizeError } from "../api";
import { ErrorBanner, Modal } from "../components/ui";

export function Login() {
  const { login, changePassword } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const next = params.get("next") || "/";

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Forced password-change flow (must_change_password).
  const [mustChange, setMustChange] = useState(false);
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [pwError, setPwError] = useState<string | null>(null);
  const [pwSubmitting, setPwSubmitting] = useState(false);
  // The just-logged-in password is needed as old_password for the change call.
  const [loginPassword, setLoginPassword] = useState("");

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const me = await login(username, password);
      setLoginPassword(password);
      if (me.must_change_password) {
        setMustChange(true);
      } else {
        navigate(next, { replace: true });
      }
    } catch (err) {
      const n = normalizeError(err);
      if (n.status === 429) {
        setError(
          "Too many failed attempts. Wait a minute before trying again.",
        );
      } else if (n.status === 401) {
        setError("Incorrect username or password.");
      } else {
        setError(n.message);
      }
    } finally {
      setSubmitting(false);
    }
  };

  const onChangePassword = async (e: FormEvent) => {
    e.preventDefault();
    setPwError(null);
    if (newPassword.length < 8) {
      setPwError("New password must be at least 8 characters.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setPwError("The new passwords do not match.");
      return;
    }
    if (newPassword === loginPassword) {
      setPwError("Choose a password different from the current one.");
      return;
    }
    setPwSubmitting(true);
    try {
      await changePassword(loginPassword, newPassword);
      setMustChange(false);
      navigate(next, { replace: true });
    } catch (err) {
      setPwError(normalizeError(err).message);
    } finally {
      setPwSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-100 px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 grid h-12 w-12 place-items-center rounded-lg bg-brand-600 text-lg font-bold text-white">
            MD
          </div>
          <h1 className="text-xl font-semibold text-slate-900">MD Platform</h1>
          <p className="mt-1 text-sm text-slate-500">
            Docking-to-MD simulation platform
          </p>
        </div>

        <form onSubmit={onSubmit} className="card space-y-4 p-6">
          {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}
          <div>
            <label className="label" htmlFor="username">
              Username
            </label>
            <input
              id="username"
              className="input"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              autoFocus
            />
          </div>
          <div>
            <label className="label" htmlFor="password">
              Password
            </label>
            <input
              id="password"
              type="password"
              className="input"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <button
            type="submit"
            className="btn-primary w-full"
            disabled={submitting}
          >
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>

      <Modal
        open={mustChange}
        title="Set a new password"
        closeOnBackdrop={false}
      >
        <form onSubmit={onChangePassword} className="space-y-4">
          <p className="text-sm text-slate-600">
            This account still uses its initial password. Set a new password to
            continue.
          </p>
          {pwError && <ErrorBanner message={pwError} onDismiss={() => setPwError(null)} />}
          <div>
            <label className="label" htmlFor="new-password">
              New password
            </label>
            <input
              id="new-password"
              type="password"
              className="input"
              autoComplete="new-password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              required
              autoFocus
            />
          </div>
          <div>
            <label className="label" htmlFor="confirm-password">
              Confirm new password
            </label>
            <input
              id="confirm-password"
              type="password"
              className="input"
              autoComplete="new-password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
            />
          </div>
          <button
            type="submit"
            className="btn-primary w-full"
            disabled={pwSubmitting}
          >
            {pwSubmitting ? "Saving…" : "Change password and continue"}
          </button>
        </form>
      </Modal>
    </div>
  );
}
