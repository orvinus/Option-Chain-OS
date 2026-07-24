import { useState } from "react";
import { api } from "../api/rest";

interface Props {
  onAuthenticated?: () => void;
}

export function ConnectBanner({ onAuthenticated }: Props) {
  const [mpin, setMpin] = useState("");
  const [totp, setTotp] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!mpin.trim()) {
      setError("MPIN is required.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await api.login({ mpin: mpin.trim(), totp_code: totp.trim() });
      setSuccess(true);
      onAuthenticated?.();
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  };

  if (success) {
    return (
      <div className="panel p-8 text-center flex flex-col items-center gap-4 border border-green-500/30">
        <div className="w-12 h-12 rounded-full bg-green-500/10 flex items-center justify-center">
          <svg className="w-6 h-6 text-green-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
          </svg>
        </div>
        <h2 className="text-lg font-semibold text-green-400">Logged in — ingestion starting…</h2>
        <p className="text-sm text-muted">Dashboard will activate in a moment.</p>
      </div>
    );
  }

  return (
    <div className="panel p-8 flex flex-col items-center gap-6 border border-accent/30 max-w-md mx-auto w-full">
      <div className="w-16 h-16 rounded-full bg-accent/10 flex items-center justify-center">
        <svg className="w-8 h-8 text-accent" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 10.5V6.75a4.5 4.5 0 119 0v3.75M3.75 21.75h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H3.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
        </svg>
      </div>

      <div className="text-center">
        <h2 className="text-xl font-semibold text-foreground">Connect to Broker</h2>
        <p className="mt-1 text-sm text-muted">
          Click Connect to start live option-chain ingestion via XTS credentials from .env.
        </p>
      </div>

      <form onSubmit={(e) => void handleLogin(e)} className="w-full flex flex-col gap-4">
        <div className="flex flex-col gap-1">
          <label className="text-xs text-muted font-medium uppercase tracking-wide">
            MPIN (4-digit SmartAPI PIN)
          </label>
          <input
            type="password"
            inputMode="numeric"
            maxLength={6}
            value={mpin}
            onChange={(e) => setMpin(e.target.value)}
            placeholder="••••"
            className="bg-panel border border-border rounded-lg px-3 py-2 text-sm
                       focus:outline-none focus:border-accent w-full"
            required
          />
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-xs text-muted font-medium uppercase tracking-wide">
            TOTP code (6-digit, from authenticator app)
          </label>
          <input
            type="text"
            inputMode="numeric"
            maxLength={6}
            value={totp}
            onChange={(e) => setTotp(e.target.value)}
            placeholder="123456"
            className="bg-panel border border-border rounded-lg px-3 py-2 text-sm
                       focus:outline-none focus:border-accent w-full tracking-widest"
          />
          <p className="text-xs text-muted">
            Leave blank to auto-generate from your <code className="text-accent">ANGEL_TOTP_SECRET</code> in .env.
            Override here only if your authenticator and .env are out of sync.
          </p>
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
          {loading ? "Logging in…" : "Login & Start Ingestion"}
        </button>
      </form>

      <p className="text-xs text-muted text-center">
        Client code is pre-configured in <code className="text-accent">.env</code>.
        TOTP codes expire every 30 s — enter just before clicking Login.
      </p>
    </div>
  );
}
