/**
 * Named watchlists for the IV scanner — persisted in localStorage.
 * Hard cap of 50 symbols per list (matches backend IV_SCANNER_MAX_SYMBOLS).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import type { SymbolEntry, SymbolSectorGroup } from "../types";

const STORAGE_KEY = "hidden.iv_watchlists.v1";
export const WATCHLIST_MAX = 50;

export interface NamedWatchlist {
  id: string;
  name: string;
  symbols: string[];
}

interface StoredState {
  lists: NamedWatchlist[];
  activeId: string;
}

function defaultState(): StoredState {
  return {
    lists: [{ id: "default", name: "My Watchlist", symbols: ["NIFTY", "BANKNIFTY", "FINNIFTY"] }],
    activeId: "default",
  };
}

function loadState(): StoredState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return defaultState();
    const parsed = JSON.parse(raw) as StoredState;
    if (!parsed.lists?.length) return defaultState();
    return parsed;
  } catch {
    return defaultState();
  }
}

function saveState(state: StoredState) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

function flatten(groups: SymbolSectorGroup[]): SymbolEntry[] {
  const out: SymbolEntry[] = [];
  for (const g of groups) {
    for (const s of g.symbols) {
      if (s.fno_eligible) out.push(s);
    }
  }
  return out;
}

interface Props {
  groups: SymbolSectorGroup[];
  onActiveSymbolsChange: (symbols: string[]) => void;
}

export function WatchlistManager({ groups, onActiveSymbolsChange }: Props) {
  const [state, setState] = useState<StoredState>(() => loadState());
  const [pickerOpen, setPickerOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [newName, setNewName] = useState("");

  const pool = useMemo(() => flatten(groups), [groups]);
  const active = state.lists.find((l) => l.id === state.activeId) ?? state.lists[0];

  const persist = useCallback((next: StoredState) => {
    setState(next);
    saveState(next);
  }, []);

  useEffect(() => {
    onActiveSymbolsChange(active?.symbols ?? []);
  }, [active?.symbols, onActiveSymbolsChange]);

  const setActiveId = (id: string) => {
    persist({ ...state, activeId: id });
  };

  const updateActiveSymbols = (symbols: string[]) => {
    const capped = symbols.slice(0, WATCHLIST_MAX);
    persist({
      ...state,
      lists: state.lists.map((l) => (l.id === active.id ? { ...l, symbols: capped } : l)),
    });
  };

  const addSymbol = (sym: string) => {
    if (active.symbols.includes(sym)) return;
    if (active.symbols.length >= WATCHLIST_MAX) return;
    updateActiveSymbols([...active.symbols, sym]);
  };

  const removeSymbol = (sym: string) => {
    updateActiveSymbols(active.symbols.filter((s) => s !== sym));
  };

  const createList = () => {
    const name = newName.trim() || `List ${state.lists.length + 1}`;
    const id = `wl_${Date.now()}`;
    persist({
      lists: [...state.lists, { id, name, symbols: [] }],
      activeId: id,
    });
    setNewName("");
  };

  const deleteList = () => {
    if (state.lists.length <= 1) return;
    const lists = state.lists.filter((l) => l.id !== active.id);
    persist({ lists, activeId: lists[0].id });
  };

  const filteredPool = useMemo(() => {
    const q = query.trim().toUpperCase();
    return pool
      .filter((s) => !active.symbols.includes(s.symbol))
      .filter((s) => !q || s.symbol.includes(q) || s.display.toUpperCase().includes(q))
      .slice(0, 80);
  }, [pool, active.symbols, query]);

  const atCap = active.symbols.length >= WATCHLIST_MAX;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-3">
        <label className="text-xs text-muted uppercase tracking-wider">Select List</label>
        <select
          className="bg-panel border border-border rounded-md px-2 py-1.5 text-sm text-gray-100 min-w-[160px]"
          value={active.id}
          onChange={(e) => setActiveId(e.target.value)}
        >
          {state.lists.map((l) => (
            <option key={l.id} value={l.id}>
              {l.name} ({l.symbols.length})
            </option>
          ))}
        </select>

        <button
          type="button"
          className="pill text-xs"
          onClick={() => setPickerOpen((v) => !v)}
        >
          {pickerOpen ? "Close editor" : "Edit symbols"}
        </button>

        <span className="text-xs text-muted">
          {active.symbols.length}/{WATCHLIST_MAX}
          {atCap && <span className="text-amber-300 ml-1">(cap reached)</span>}
        </span>
      </div>

      {pickerOpen && (
        <div className="panel p-3 flex flex-col gap-3 border border-accent/20">
          <div className="flex flex-wrap gap-2 items-center">
            <input
              className="bg-panel border border-border rounded-md px-2 py-1 text-sm w-40"
              placeholder="New list name"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
            />
            <button type="button" className="pill text-xs" onClick={createList}>
              Create list
            </button>
            <button
              type="button"
              className="pill text-xs text-red-300 border-red-500/30"
              onClick={deleteList}
              disabled={state.lists.length <= 1}
            >
              Delete list
            </button>
          </div>

          <div>
            <div className="text-xs text-muted mb-1 uppercase tracking-wider">In watchlist</div>
            <div className="flex flex-wrap gap-1.5">
              {active.symbols.length === 0 && (
                <span className="text-xs text-muted">Empty — add symbols below.</span>
              )}
              {active.symbols.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => removeSymbol(s)}
                  className="px-2 py-0.5 rounded-full text-xs border border-border bg-accent/10 hover:border-red-400/50"
                  title="Remove"
                >
                  {s} ×
                </button>
              ))}
            </div>
          </div>

          <div>
            <div className="flex items-center gap-2 mb-1">
              <span className="text-xs text-muted uppercase tracking-wider">Add from pool</span>
              <input
                className="bg-panel border border-border rounded-md px-2 py-1 text-xs flex-1 max-w-xs"
                placeholder="Search symbol…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            <div className="max-h-40 overflow-y-auto flex flex-wrap gap-1.5">
              {filteredPool.map((s) => (
                <button
                  key={s.symbol}
                  type="button"
                  disabled={atCap}
                  onClick={() => addSymbol(s.symbol)}
                  className="px-2 py-0.5 rounded-full text-xs border border-border hover:border-accent/60 disabled:opacity-40"
                >
                  + {s.symbol}
                </button>
              ))}
              {filteredPool.length === 0 && (
                <span className="text-xs text-muted">No matches.</span>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
