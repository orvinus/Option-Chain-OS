/**
 * Draft/save machinery for an AlgoConfigDoc, parameterized on its storage —
 * extracted from AlgoConfigPage so the LIVE config page and the Backtesting
 * workspace's SANDBOX config share one battle-tested flow:
 *
 *   load → clone into draft → mutate() edits → dirty/changes memos →
 *   requestSave (honors config_lock.require_save_confirm) → ConfirmSaveModal
 *   → doSave → toast → reload + refreshKey bump.
 *
 * NOTE: pass STABLE `load`/`save` functions (module-level or useCallback) —
 * they sit in dependency arrays.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { AlgoApiError, algoApi } from "../api/algoRest";
import type { AlgoConfigDoc } from "../types/algo";
import { cloneDoc, diffDocs, isDirty } from "../components/algo/algoDraft";

export interface DraftLoadResult<M> {
  config: AlgoConfigDoc;
  meta: M;
}

export function useConfigDraft<M>(
  load: () => Promise<DraftLoadResult<M>>,
  // `meta` is the load-time metadata (the live page passes its version as
  // the optimistic-concurrency base; the sandbox ignores it).
  save: (config: AlgoConfigDoc, note: string, meta: M | null) => Promise<string>,
) {
  const [meta, setMeta] = useState<M | null>(null);
  const [saved, setSaved] = useState<AlgoConfigDoc | null>(null);
  const [draft, setDraft] = useState<AlgoConfigDoc | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [showConfirm, setShowConfirm] = useState(false);
  const [saveBusy, setSaveBusy] = useState(false);
  const [saveErrors, setSaveErrors] = useState<string[] | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  // Bumped after every successful save so dependent panels refetch.
  const [refreshKey, setRefreshKey] = useState(0);
  // Appendix-A reference doc — the source every "Reset to Default" copies from.
  const [defaults, setDefaults] = useState<AlgoConfigDoc | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        setDefaults(await algoApi.configDefaults());
      } catch {
        /* reset buttons stay disabled without it */
      }
    })();
  }, []);

  const reload = useCallback(async () => {
    try {
      const res = await load();
      setMeta(res.meta);
      setSaved(res.config);
      setDraft(cloneDoc(res.config));
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, [load]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const mutate = useCallback((fn: (d: AlgoConfigDoc) => void) => {
    setDraft((prev) => {
      if (!prev) return prev;
      const next = cloneDoc(prev);
      fn(next);
      return next;
    });
  }, []);

  const dirty = useMemo(
    () => (saved && draft ? isDirty(saved, draft) : false),
    [saved, draft]
  );
  const changes = useMemo(
    () => (saved && draft ? diffDocs(saved, draft) : []),
    [saved, draft]
  );

  const doSave = useCallback(
    async (note: string) => {
      if (!draft) return;
      setSaveBusy(true);
      setSaveErrors(null);
      try {
        const msg = await save(draft, note, meta);
        setShowConfirm(false);
        setToast(msg);
        setTimeout(() => setToast(null), 3500);
        await reload();
        setRefreshKey((k) => k + 1);
      } catch (e) {
        if (e instanceof AlgoApiError && e.status === 409) {
          // The live document moved on (paper reset, restore, another tab):
          // never let this stale draft overwrite it — surface it and reload
          // the saved document so the user re-applies their edits on top.
          setSaveErrors(e.errors.length > 0 ? e.errors : [e.message]);
          setShowConfirm(false);
          await reload();
        } else if (e instanceof AlgoApiError && e.errors.length > 0) {
          setSaveErrors(e.errors);
        } else {
          setSaveErrors([e instanceof Error ? e.message : String(e)]);
        }
      } finally {
        setSaveBusy(false);
      }
    },
    [draft, meta, reload, save]
  );

  // One save entry-point for header buttons AND per-engine "Save All Engine
  // Settings". Honors the Config Lock's require_save_confirm: ON (default) →
  // diff modal; OFF → post directly.
  const requestSave = useCallback(() => {
    setSaveErrors(null);
    // Decide from the SAVED document: the save that switches confirmation
    // off must itself still be confirmed (QA 2026-09-02 H4).
    if (saved && saved.global.config_lock?.require_save_confirm === false) {
      void doSave("");
    } else {
      setShowConfirm(true);
    }
  }, [saved, doSave]);

  const discard = useCallback(() => {
    if (saved) setDraft(cloneDoc(saved));
  }, [saved]);

  return {
    meta,
    saved,
    draft,
    loadError,
    reload,
    mutate,
    dirty,
    changes,
    doSave,
    requestSave,
    discard,
    showConfirm,
    setShowConfirm,
    saveBusy,
    saveErrors,
    setSaveErrors,
    toast,
    refreshKey,
    setRefreshKey,
    defaults,
  };
}
