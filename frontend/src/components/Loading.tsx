/**
 * Shared loading indicators.
 *
 * Three pieces, used at three scales:
 *
 * - `GlobalLoadingBar` — mounted ONCE at the app root. A thin bar across the
 *   top whenever any request is in flight, so "something is loading" is always
 *   answerable no matter which page you are on.
 * - `LoadingBlock` — replaces a content area that has nothing to show yet.
 *   Says so in words, because a blank panel and a panel with no data look the
 *   same otherwise.
 * - `LoadingOverlay` — sits over a chart that already has data while it
 *   refreshes, so the old chart stays readable underneath instead of blanking.
 *
 * Two timing rules keep this from becoming visual noise, and both matter:
 *
 * - `DELAY_MS` — nothing appears for a moment. Most calls here return in a few
 *   tens of milliseconds, and a spinner that flashes on every one of those is
 *   worse than no spinner.
 * - `MIN_VISIBLE_MS` — once shown, it stays briefly. Without this a request
 *   that resolves just past the delay produces a one-frame flicker.
 */
import { useEffect, useRef, useState } from "react";

import { subscribeInflight } from "../api/inflight";

const DELAY_MS = 180;
const MIN_VISIBLE_MS = 400;

/** True while any request is in flight, debounced by the rules above. */
export function useIsLoading(): boolean {
  const [show, setShow] = useState(false);
  const shownAt = useRef(0);
  const showTimer = useRef<number | undefined>(undefined);
  const hideTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    const clear = () => {
      window.clearTimeout(showTimer.current);
      window.clearTimeout(hideTimer.current);
      showTimer.current = undefined;
      hideTimer.current = undefined;
    };
    const unsub = subscribeInflight((active) => {
      if (active > 0) {
        window.clearTimeout(hideTimer.current);
        hideTimer.current = undefined;
        if (showTimer.current === undefined) {
          showTimer.current = window.setTimeout(() => {
            shownAt.current = Date.now();
            setShow(true);
            showTimer.current = undefined;
          }, DELAY_MS);
        }
        return;
      }
      // Back to idle.
      window.clearTimeout(showTimer.current);
      showTimer.current = undefined;
      setShow((cur) => {
        if (!cur) return false;
        const left = MIN_VISIBLE_MS - (Date.now() - shownAt.current);
        if (left <= 0) return false;
        hideTimer.current = window.setTimeout(() => setShow(false), left);
        return true;
      });
    });
    return () => {
      unsub();
      clear();
    };
  }, []);

  return show;
}

/**
 * The app-wide indicator: a moving bar pinned to the very top, plus a small
 * "Loading… please wait" chip so it reads as a message and not just a stripe.
 */
export function GlobalLoadingBar() {
  const loading = useIsLoading();
  return (
    <>
      <div
        aria-hidden={!loading}
        className={`fixed top-0 left-0 right-0 z-[9999] h-[3px] overflow-hidden transition-opacity duration-200 ${
          loading ? "opacity-100" : "opacity-0"
        }`}
      >
        <div className="h-full w-1/3 rounded-full bg-accent app-loading-sweep" />
      </div>
      <div
        role="status"
        aria-live="polite"
        className={`fixed top-3 right-3 z-[9999] flex items-center gap-2 rounded-full border border-border
          bg-panel/95 px-3 py-1 text-[11px] text-gray-200 shadow-lg transition-opacity duration-200 ${
            loading ? "opacity-100" : "pointer-events-none opacity-0"
          }`}
      >
        <Spinner />
        Loading… please wait
      </div>
    </>
  );
}

/** A small ring spinner in the current text colour. */
export function Spinner({ size = 12 }: { size?: number }) {
  return (
    <span
      className="inline-block shrink-0 rounded-full border-2 border-current border-r-transparent app-loading-spin"
      style={{ width: size, height: size, opacity: 0.85 }}
    />
  );
}

/**
 * For an area with nothing to show yet. Use INSTEAD of an empty-state message
 * while a load is pending: "No data" and "not loaded yet" mean very different
 * things to someone waiting.
 */
export function LoadingBlock({
  label = "Loading… please wait",
  height,
  className = "",
}: {
  label?: string;
  height?: number | string;
  className?: string;
}) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={`flex items-center justify-center gap-2 text-xs text-muted ${className}`}
      style={height ? { height } : { padding: "1.25rem 0" }}
    >
      <Spinner />
      {label}
    </div>
  );
}

/**
 * For an area that ALREADY has data and is refreshing. The old content stays
 * visible and readable underneath; this only dims it slightly.
 */
export function LoadingOverlay({
  show,
  label = "Loading…",
}: {
  show: boolean;
  label?: string;
}) {
  if (!show) return null;
  return (
    <div className="absolute inset-0 z-20 flex items-center justify-center bg-black/25 backdrop-blur-[1px]">
      <span className="flex items-center gap-2 rounded-full border border-border bg-panel/95 px-3 py-1 text-[11px] text-gray-200">
        <Spinner />
        {label}
      </span>
    </div>
  );
}
