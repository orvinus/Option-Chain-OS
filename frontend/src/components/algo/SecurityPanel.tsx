/**
 * Sub-tab — Security, Access Control & Backups.
 *
 * Version history with one-click restore (restore re-saves the old document as
 * a NEW version — the audit trail never rewinds) and the full append-only
 * audit log with user/date filtering (§11.3), plus the change-credentials form
 * for the Algo Config / Backtesting sign-in (2026-09-13).
 */
import { useCallback, useEffect, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import { algoApi } from "../../api/algoRest";
import type {
  AlgoConfigDoc,
  AlgoIdentity,
  AuditRow,
  ConfigVersionMeta,
} from "../../types/algo";
import { killExitNote } from "../../types/algo";
import { CalcNote, Card, ToggleLine } from "./controls";

const VERSION_PAGE = 100;
const AUDIT_PAGE = 200;

/** "Showing N · Load older" until the first row of the history is on screen. */
function HistoryFooter(p: {
  shown: number;
  noun: string;
  done: boolean;
  busy: boolean;
  startLabel: string;
  onMore: () => void;
}) {
  if (p.shown === 0) return null;
  const plural = p.noun === "entry" ? "entries" : `${p.noun}s`;
  return (
    <div className="flex items-center gap-2 pt-2 text-[11px] text-muted">
      <span>
        Showing {p.shown} {p.shown === 1 ? p.noun : plural}
        {p.done && p.startLabel ? ` · ${p.startLabel}` : ""}
      </span>
      {!p.done && (
        <button
          type="button"
          className="pill text-[11px] ml-auto"
          disabled={p.busy}
          onClick={p.onMore}
        >
          {p.busy ? "Loading…" : `Load older ${plural}`}
        </button>
      )}
    </div>
  );
}

export function SecurityPanel(props: {
  identity: AlgoIdentity;
  refreshKey: number;
  draft: AlgoConfigDoc;
  mutate: (fn: (d: AlgoConfigDoc) => void) => void;
  onRestored: () => void;
}) {
  const [versions, setVersions] = useState<ConfigVersionMeta[]>([]);
  const [audit, setAudit] = useState<AuditRow[]>([]);
  // Every version and every audit row back to the very first one is reachable
  // (2026-09-23: the page used to stop at the newest 25 / 200). A short page
  // means the start of the history was reached.
  const [versionsDone, setVersionsDone] = useState(false);
  const [auditDone, setAuditDone] = useState(false);
  const [paging, setPaging] = useState<"versions" | "audit" | null>(null);
  const [userFilter, setUserFilter] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busyRestore, setBusyRestore] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const [v, a] = await Promise.all([
        algoApi.versions(VERSION_PAGE),
        algoApi.audit({ username: userFilter || undefined, limit: AUDIT_PAGE }),
      ]);
      setVersions(v);
      setAudit(a);
      setVersionsDone(v.length < VERSION_PAGE);
      setAuditDone(a.length < AUDIT_PAGE);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [userFilter]);

  const loadOlderVersions = useCallback(async () => {
    const oldest = versions[versions.length - 1];
    if (!oldest) return;
    setPaging("versions");
    try {
      const more = await algoApi.versions(VERSION_PAGE, oldest.version);
      setVersions((cur) => [...cur, ...more]);
      setVersionsDone(more.length < VERSION_PAGE);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPaging(null);
    }
  }, [versions]);

  const loadOlderAudit = useCallback(async () => {
    const oldest = audit[audit.length - 1];
    if (!oldest) return;
    setPaging("audit");
    try {
      const more = await algoApi.audit({
        username: userFilter || undefined,
        limit: AUDIT_PAGE,
        before: { ts: oldest.ts, id: oldest.id },
      });
      setAudit((cur) => [...cur, ...more]);
      setAuditDone(more.length < AUDIT_PAGE);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPaging(null);
    }
  }, [audit, userFilter]);

  useEffect(() => {
    void load();
  }, [load, props.refreshKey]);

  const restore = useCallback(
    async (version: number) => {
      if (!window.confirm(`Restore configuration v${version}? It is re-saved as a new version.`)) {
        return;
      }
      setBusyRestore(version);
      try {
        const res = await algoApi.restore(version);
        const note = killExitNote(res);
        if (note) window.alert(`Restored v${version}${note}`);
        props.onRestored();
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusyRestore(null);
      }
    },
    [load, props]
  );

  return (
    <div className="grid lg:grid-cols-2 gap-4">
      <div className="flex flex-col gap-4">
        <Card title="Signed-in user">
          <div className="text-sm text-gray-100">
            <b>{props.identity.username}</b>
            <span className="ml-2 text-xs text-accent uppercase">{props.identity.role}</span>
          </div>
          <div className="text-[10.5px] text-muted mt-1">
            Multi-user role management (Admin / Editor / Viewer) arrives with the RBAC milestone —
            the users table already carries roles.
          </div>
        </Card>
        <ChangeCredentialsCard identity={props.identity} />
        <Card title="Config Lock" hint="protection against accidental edits">
          <ToggleLine
            name="Lock config during market hours"
            desc="While ON, every save is refused between 09:15–15:40 IST on trading days — except turning this lock off, which always works. Kill switches on the Daily tab are saves too, so use this deliberately."
            on={props.draft.global.config_lock?.lock_market_hours ?? false}
            danger
            onChange={(v) =>
              props.mutate((d) => void (d.global.config_lock.lock_market_hours = v))
            }
          />
          <ToggleLine
            name="Require confirmation on save"
            desc="Shows the exact field diff and asks before any change goes live (recommended). OFF = the Save button posts immediately."
            on={props.draft.global.config_lock?.require_save_confirm ?? true}
            onChange={(v) =>
              props.mutate((d) => void (d.global.config_lock.require_save_confirm = v))
            }
          />
          <div className="mt-2">
            <CalcNote>
              Both switches live in the config document itself — flip them and Save. The
              market-hours lock is enforced server-side, so nothing (including API calls)
              can slip an edit through mid-session.
            </CalcNote>
          </div>
        </Card>
        <Card title="Version History & Backups" hint="restore any point — never rewinds the log">
          <div className="max-h-[26rem] overflow-y-auto flex flex-col">
          {versions.map((v) => (
            <div
              key={v.version}
              className="flex items-center justify-between py-1.5 border-b border-border/40 last:border-b-0 text-xs"
            >
              <div>
                <span className="font-bold text-accent">v{v.version}</span>
                <span className="ml-2 text-muted">
                  {new Date(v.saved_at).toLocaleString()} · {v.saved_by}
                </span>
                {v.note && <span className="ml-2 text-gray-300">{v.note}</span>}
              </div>
              <button
                type="button"
                className="text-accent hover:text-white disabled:opacity-40"
                disabled={busyRestore !== null}
                onClick={() => void restore(v.version)}
              >
                {busyRestore === v.version ? "Restoring…" : "Restore"}
              </button>
            </div>
          ))}
          </div>
          <HistoryFooter
            shown={versions.length}
            noun="version"
            done={versionsDone}
            busy={paging === "versions"}
            startLabel={
              versions.length
                ? `back to v${versions[versions.length - 1].version}, the first saved version`
                : ""
            }
            onMore={() => void loadOlderVersions()}
          />
        </Card>
      </div>

      <Card title="Full Audit Log" hint="every login and every change, per user, timestamped">
        <div className="flex items-center gap-2 mb-2">
          <input
            className="bg-panel border border-border rounded-lg px-2 py-1 text-xs focus:outline-none focus:border-accent w-44"
            placeholder="Filter by username…"
            value={userFilter}
            onChange={(e) => setUserFilter(e.target.value)}
          />
          <button type="button" className="pill text-xs" onClick={() => void load()}>
            Refresh
          </button>
        </div>
        {error && <div className="text-xs text-ce mb-2">{error}</div>}
        <div className="max-h-[26rem] overflow-y-auto flex flex-col">
          {audit.map((row) => (
            <div
              key={row.id}
              className="py-1.5 border-b border-border/30 last:border-b-0 text-[11px] leading-relaxed"
            >
              <span className="text-muted">{new Date(row.ts).toLocaleString()}</span>{" "}
              <span className="text-gray-200 font-semibold">{row.username || "system"}</span>{" "}
              <span
                className={
                  row.event_type === "login_failed"
                    ? "text-ce"
                    : row.event_type === "login_success"
                      ? "text-pe"
                      : "text-accent"
                }
              >
                {row.event_type}
              </span>
              {row.scope && <span className="text-muted"> · {row.scope}</span>}
              {row.field && (
                <div className="text-muted pl-2">
                  {row.field}: <span className="text-ce/80">{row.old_value ?? "—"}</span> →{" "}
                  <span className="text-pe/80">{row.new_value ?? "—"}</span>
                </div>
              )}
            </div>
          ))}
          {audit.length === 0 && <div className="text-xs text-muted">No audit rows yet.</div>}
        </div>
        <HistoryFooter
          shown={audit.length}
          noun="entry"
          done={auditDone}
          busy={paging === "audit"}
          startLabel={
            audit.length
              ? `back to the first recorded entry (${new Date(audit[audit.length - 1].ts).toLocaleString()})`
              : ""
          }
          onMore={() => void loadOlderAudit()}
        />
      </Card>
    </div>
  );
}


/**
 * Change the Algo Config / Backtesting sign-in ID and password.
 *
 * Scope is stated on the card because it is the thing people get wrong: this
 * pair does NOT gate the main dashboard, which has its own separate credential.
 *
 * The new values are written to the database, so they survive a backend
 * restart. On success the server clears the session, so the page reloads
 * straight into the sign-in gate and the new credentials are proven at once.
 */
function ChangeCredentialsCard({ identity }: { identity: AlgoIdentity }) {
  const [currentUsername, setCurrentUsername] = useState(identity.username);
  const [currentPassword, setCurrentPassword] = useState("");
  const [confirmCurrent, setConfirmCurrent] = useState("");
  const [newUsername, setNewUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmNew, setConfirmNew] = useState("");
  const [showCurrent, setShowCurrent] = useState(false);
  const [showNew, setShowNew] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const localProblem = (): string | null => {
    if (!currentUsername.trim()) return "Enter your current ID.";
    if (!currentPassword) return "Enter your current password.";
    if (currentPassword !== confirmCurrent) return "The current passwords do not match.";
    if (!newUsername.trim()) return "Enter the new ID.";
    if (newPassword.length < 8) return "The new password must be at least 8 characters.";
    if (newPassword !== confirmNew) return "The new passwords do not match.";
    if (newUsername.trim() === currentUsername.trim() && newPassword === currentPassword) {
      return "The new ID and password are the same as the current ones.";
    }
    return null;
  };

  const submit = async () => {
    const problem = localProblem();
    if (problem) {
      setErr(problem);
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await algoApi.changeCredentials({
        current_username: currentUsername.trim(),
        current_password: currentPassword,
        new_username: newUsername.trim(),
        new_password: newPassword,
      });
      setDone(true);
      // The server has already cleared the session; reload straight into the
      // gate so the new credentials are used immediately.
      window.setTimeout(() => window.location.reload(), 1600);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const newPwOk = newPassword.length >= 8;
  const newPwMatch = confirmNew.length > 0 && newPassword === confirmNew;
  const curPwMatch = confirmCurrent.length > 0 && currentPassword === confirmCurrent;
  const problem = localProblem();
  const locked = busy || done;

  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Enter" && !locked && !problem) void submit();
  };

  return (
    // `id` is the jump target for the header's "Change password" button — the
    // form is three clicks deep otherwise, which is why it was hard to find.
    <div id="change-credentials" className="scroll-mt-24">
      <Card title="Change sign-in ID & password" hint="Algo Config + Backtesting only">
        <div className="text-[10.5px] text-muted leading-relaxed mb-3">
          These credentials gate <b className="text-gray-300">Algo Config</b> and{" "}
          <b className="text-gray-300">Backtesting</b>. The main dashboard sign-in is
          separate and is not affected. New values are stored in the database, so they
          survive a backend restart.
        </div>

        <div onKeyDown={onKeyDown} className="flex flex-col gap-4">
          <FieldGroup
            step="1"
            title="Confirm who you are"
            desc="Your current sign-in, typed twice so a mistyped password cannot lock you out."
          >
            <Field
              label="Current ID"
              value={currentUsername}
              onChange={setCurrentUsername}
              disabled={locked}
              autoComplete="username"
              className="sm:col-span-2"
            />
            <Field
              label="Current password"
              value={currentPassword}
              onChange={setCurrentPassword}
              disabled={locked}
              type={showCurrent ? "text" : "password"}
              autoComplete="current-password"
              action={
                <RevealButton shown={showCurrent} onClick={() => setShowCurrent((v) => !v)} />
              }
            />
            <Field
              label="Confirm current password"
              value={confirmCurrent}
              onChange={setConfirmCurrent}
              disabled={locked}
              type={showCurrent ? "text" : "password"}
              autoComplete="current-password"
              state={confirmCurrent.length === 0 ? null : curPwMatch ? "ok" : "bad"}
              note={
                confirmCurrent.length === 0
                  ? null
                  : curPwMatch
                    ? "Matches"
                    : "Does not match the password above"
              }
            />
          </FieldGroup>

          <FieldGroup
            step="2"
            title="Choose the new sign-in"
            desc="The ID and the password change together — you will sign in with this pair from now on."
          >
            <Field
              label="New ID"
              value={newUsername}
              onChange={setNewUsername}
              disabled={locked}
              autoComplete="off"
              className="sm:col-span-2"
            />
            <Field
              label="New password"
              value={newPassword}
              onChange={setNewPassword}
              disabled={locked}
              type={showNew ? "text" : "password"}
              autoComplete="new-password"
              state={newPassword.length === 0 ? null : newPwOk ? "ok" : "bad"}
              note={
                newPassword.length === 0
                  ? "At least 8 characters"
                  : newPwOk
                    ? "Long enough"
                    : `${8 - newPassword.length} more character${
                        8 - newPassword.length === 1 ? "" : "s"
                      } needed`
              }
              action={<RevealButton shown={showNew} onClick={() => setShowNew((v) => !v)} />}
            />
            <Field
              label="Confirm new password"
              value={confirmNew}
              onChange={setConfirmNew}
              disabled={locked}
              type={showNew ? "text" : "password"}
              autoComplete="new-password"
              state={confirmNew.length === 0 ? null : newPwMatch ? "ok" : "bad"}
              note={
                confirmNew.length === 0
                  ? null
                  : newPwMatch
                    ? "Matches"
                    : "Does not match the password above"
              }
            />
          </FieldGroup>
        </div>

        {err && (
          <div className="mt-3 flex items-start gap-2 rounded-lg border border-ce/40 bg-ce/10 px-3 py-2 text-[11px] text-ce">
            <span aria-hidden>⚠</span>
            <span>{err}</span>
          </div>
        )}
        {done && (
          <div className="mt-3 flex items-start gap-2 rounded-lg border border-pe/40 bg-pe/10 px-3 py-2 text-[11px] text-pe">
            <span aria-hidden>✓</span>
            <span>Changed. Signing you out so the new credentials take effect…</span>
          </div>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-border/50 pt-3">
          <button
            type="button"
            className="pill pill-active disabled:opacity-40"
            disabled={locked || problem !== null}
            onClick={() => void submit()}
          >
            {busy ? "Changing…" : done ? "Changed" : "Change password"}
          </button>
          {/* Why the button is disabled, shown BEFORE the click rather than after
              it — the old card only revealed the reason on submit. */}
          {!locked && problem && <span className="text-[10.5px] text-muted">{problem}</span>}
          {!locked && !problem && (
            <span className="text-[10.5px] text-muted">
              You will be signed out and asked for the new ID and password.
            </span>
          )}
        </div>
      </Card>
    </div>
  );
}

/** One labelled step of the form: a numbered heading plus a 2-column field grid. */
function FieldGroup(p: {
  step: string;
  title: string;
  desc: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-xl border border-border/60 bg-black/15 p-3">
      <div className="flex items-baseline gap-2 mb-0.5">
        <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-accent/20 text-[9px] font-bold text-accent">
          {p.step}
        </span>
        <h4 className="text-[11.5px] font-semibold text-gray-200">{p.title}</h4>
      </div>
      <p className="text-[10.5px] text-muted mb-2.5 pl-6">{p.desc}</p>
      <div className="grid sm:grid-cols-2 gap-x-3 gap-y-2.5">{p.children}</div>
    </section>
  );
}

/**
 * A single labelled input.
 *
 * Declared at module scope ON PURPOSE. It used to live inside
 * `ChangeCredentialsCard`, which makes a NEW component type on every render, so
 * React unmounted and remounted the input on every keystroke and the caret left
 * the field after each character.
 */
function Field(p: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
  type?: string;
  autoComplete?: string;
  /** Validation tint: green when satisfied, red when not, none while empty. */
  state?: "ok" | "bad" | null;
  /** Small helper line under the field. */
  note?: string | null;
  /** Trailing control inside the field (the reveal toggle). */
  action?: ReactNode;
  className?: string;
}) {
  return (
    <label className={`flex flex-col gap-1 ${p.className ?? ""}`}>
      <span className="text-[10.5px] text-muted">{p.label}</span>
      <div className="relative">
        <input
          className={`input text-xs ${p.action ? "pr-14" : ""} ${
            p.state === "ok" ? "input-ok" : p.state === "bad" ? "input-bad" : ""
          }`}
          type={p.type ?? "text"}
          value={p.value}
          // The helper note lives inside the <label>, so the implicit accessible
          // name would read "New password At least 8 characters". Name the field
          // explicitly and let the note be supplementary.
          aria-label={p.label}
          autoComplete={p.autoComplete ?? "off"}
          disabled={p.disabled}
          onChange={(e) => p.onChange(e.target.value)}
        />
        {p.action && (
          <span className="absolute inset-y-0 right-1.5 flex items-center">{p.action}</span>
        )}
      </div>
      {p.note && (
        <span
          className={`text-[10px] ${
            p.state === "ok" ? "text-pe/80" : p.state === "bad" ? "text-ce/80" : "text-muted"
          }`}
        >
          {p.note}
        </span>
      )}
    </label>
  );
}

function RevealButton({ shown, onClick }: { shown: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      tabIndex={-1}
      onClick={onClick}
      className="text-[10px] text-muted hover:text-accent px-1.5 py-0.5 rounded transition"
      aria-label={shown ? "Hide password" : "Show password"}
    >
      {shown ? "Hide" : "Show"}
    </button>
  );
}
