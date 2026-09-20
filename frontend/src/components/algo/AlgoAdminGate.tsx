/**
 * Real server-side admin gate for the Algo Config tab.
 *
 * Unlike LoginGate (display-only), this gate is enforced by the backend: every
 * /api/algo/* endpoint 401s without the httpOnly session cookie, so bypassing
 * this component gets you nothing. On mount it probes /api/algo/auth/me to
 * restore an existing session (cookie survives reloads).
 */
import { useCallback, useEffect, useState } from "react";
import { AlgoApiError, algoApi } from "../../api/algoRest";
import type { AlgoIdentity } from "../../types/algo";

interface Props {
  children: (identity: AlgoIdentity, signOut: () => void) => JSX.Element;
}

export function AlgoAdminGate({ children }: Props) {
  const [identity, setIdentity] = useState<AlgoIdentity | null>(null);
  const [checked, setChecked] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notConfigured, setNotConfigured] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const me = await algoApi.me();
        if (!cancelled) setIdentity(me);
      } catch (e) {
        if (!cancelled && e instanceof AlgoApiError && e.status === 503) {
          setNotConfigured(true);
          setError(e.message);
        }
        // 401 = simply not signed in — show the form silently.
      } finally {
        if (!cancelled) setChecked(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // A mid-use 401 (backend restarted, session expired) drops straight back to
  // the sign-in form with an explanation — never a half-dead page.
  useEffect(() => {
    const onUnauthorized = () => {
      setIdentity((prev) => {
        if (prev) setError("Session expired (backend restarted?) — please sign in again.");
        return null;
      });
    };
    window.addEventListener("algo:unauthorized", onUnauthorized);
    return () => window.removeEventListener("algo:unauthorized", onUnauthorized);
  }, []);

  const submit = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const me = await algoApi.login(username.trim(), password);
      setIdentity(me);
      setPassword("");
    } catch (e) {
      if (e instanceof AlgoApiError && e.status === 503) {
        setNotConfigured(true);
      }
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [username, password]);

  const signOut = useCallback(() => {
    void algoApi.logout().catch(() => undefined);
    setIdentity(null);
  }, []);

  if (identity) return children(identity, signOut);

  return (
    <div className="min-h-[60vh] flex items-center justify-center px-4">
      <div className="panel w-full max-w-sm px-6 py-6">
        <h2 className="text-lg font-bold text-gray-100 mb-1">Algo Config — Admin</h2>
        <p className="text-xs text-muted mb-4">
          Server-side session required. Every sign-in (and every failed attempt) is
          recorded in the audit log.
        </p>
        {!checked ? (
          <div className="text-sm text-muted">Checking session…</div>
        ) : notConfigured ? (
          <div className="text-sm text-amber-300/90 bg-amber-950/40 border border-amber-800/40 rounded-lg px-3 py-2">
            {error ??
              "Algo admin login is not configured. Set ALGO_ADMIN_USER and ALGO_ADMIN_PASSWORD in .env and restart the backend."}
          </div>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void submit();
            }}
            className="flex flex-col gap-3"
          >
            <input
              className="bg-panel border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent w-full"
              placeholder="Username"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
            <input
              className="bg-panel border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent w-full"
              placeholder="Password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            {error && <div className="text-xs text-ce">{error}</div>}
            <button
              type="submit"
              disabled={busy || !username.trim() || !password}
              className="pill pill-active justify-center py-2 disabled:opacity-50"
            >
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
