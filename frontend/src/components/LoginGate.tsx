import { useState, type FormEvent, type ReactNode } from "react";
import { api } from "../api/rest";

/**
 * Fixed-credential login gate for the MAIN dashboard (served at "/"). Mirrors the
 * /hidden gate: the username + password are verified server-side against
 * HIDDEN_USER / HIDDEN_PASSWORD (the same single credential pair) via
 * POST /api/auth/hidden-login — never checked in the browser. A successful sign-in
 * in live mode also starts the broker feed, so it can block up to ~45s.
 *
 * The gate is display-only and in-memory: a full page reload re-prompts, and the
 * backend does not enforce it on /api routes (same behavior as /hidden).
 */
export function LoginGate({ children }: { children: ReactNode }) {
  const [unlocked, setUnlocked] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (unlocked) return <>{children}</>;

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!username.trim() || !password) {
      setError("Enter your username and password.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await api.gateLogin({ username: username.trim(), password });
      setUnlocked(true);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen w-full flex items-center justify-center px-4">
      <div className="panel p-8 flex flex-col items-center gap-6 border border-accent/30 max-w-md w-full">
        <div className="w-16 h-16 rounded-full bg-accent/10 flex items-center justify-center">
          <svg className="w-8 h-8 text-accent" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 10.5V6.75a4.5 4.5 0 119 0v3.75M3.75 21.75h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H3.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
          </svg>
        </div>

        <div className="text-center">
          <h2 className="text-xl font-semibold text-foreground">Sign in to Dashboard</h2>
          <p className="mt-1 text-sm text-muted">
            Enter your username and password to open the dashboard.
          </p>
        </div>

        <form onSubmit={(e) => void handleSubmit(e)} className="w-full flex flex-col gap-4">
          <div className="flex flex-col gap-1">
            <label className="text-xs text-muted font-medium uppercase tracking-wide">Username</label>
            <input
              type="text"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="username"
              className="bg-panel border border-border rounded-lg px-3 py-2 text-sm
                         focus:outline-none focus:border-accent w-full"
              required
            />
          </div>

          <div className="flex flex-col gap-1">
            <label className="text-xs text-muted font-medium uppercase tracking-wide">Password</label>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              className="bg-panel border border-border rounded-lg px-3 py-2 text-sm
                         focus:outline-none focus:border-accent w-full"
              required
            />
          </div>

          {error && (
            <div className="rounded-lg bg-red-500/10 border border-red-500/30 px-3 py-2 text-sm text-red-400">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full px-6 py-2.5 rounded-lg bg-accent text-black font-semibold
                       hover:bg-accent/80 active:scale-95 transition-all
                       disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
