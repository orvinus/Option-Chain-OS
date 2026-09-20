/**
 * Read-only mirror mode (/seceretdashboard).
 *
 * The server is the real boundary: a viewer session already gets 403 on every
 * mutating route. This module exists so the UI does not *pretend* to be
 * editable — a dashboard whose Save button looks alive but always errors is
 * worse than one that plainly cannot be edited.
 *
 * The marker is the `?mirror=1` on the entry redirect, captured once at boot
 * into sessionStorage so it survives in-app navigation without being sticky
 * across tabs. Faking it only removes controls; it grants nothing, because
 * every read still needs the httpOnly viewer cookie the server issued.
 */
const KEY = "oi.viewerMirror";

function detect(): boolean {
  try {
    if (new URLSearchParams(window.location.search).has("mirror")) {
      sessionStorage.setItem(KEY, "1");
      return true;
    }
    return sessionStorage.getItem(KEY) === "1";
  } catch {
    // Private mode / storage blocked: fall back to the URL alone.
    try {
      return new URLSearchParams(window.location.search).has("mirror");
    } catch {
      return false;
    }
  }
}

let cached: boolean | null = null;

/** True when this tab was opened through the read-only mirror URL. */
export function isViewerMirror(): boolean {
  if (cached === null) cached = detect();
  return cached;
}

/** Thrown instead of sending a write while in mirror mode. */
export class ReadOnlyError extends Error {
  constructor() {
    super("Read-only view — this dashboard cannot change anything.");
    this.name = "ReadOnlyError";
  }
}

/**
 * Guard for the API layer. Blocks every non-GET before it leaves the browser,
 * so a write cannot be fired from a stray keyboard shortcut, an auto-save, or
 * a control this build has not thought to disable.
 */
export function assertWritable(method: string): void {
  if (method.toUpperCase() !== "GET" && isViewerMirror()) throw new ReadOnlyError();
}
