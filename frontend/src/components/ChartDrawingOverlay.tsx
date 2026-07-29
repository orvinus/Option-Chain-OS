import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

/**
 * Transparent annotation layer rendered on top of a chart. Provides a small
 * toolbar to draw horizontal / vertical / freehand lines, an object eraser,
 * undo / redo and clear-all. Self-contained: each instance keeps its own
 * annotation history, so the two OI charts annotate independently.
 *
 * Coordinates are stored normalised (0..1) so annotations survive chart resize
 * and the 20s data poll (the overlay is a sibling of the ECharts canvas and is
 * never re-created by it). When the active tool is "cursor" the layer is fully
 * click-through, so the chart's own tooltip / hover still works.
 */

type Tool = "cursor" | "hline" | "vline" | "trend" | "ray" | "rect" | "free" | "eraser";

/** Two-point tools store both anchors; single-axis tools store one coordinate. */
type TwoPoint = { x1: number; y1: number; x2: number; y2: number };

type Shape =
  | { id: string; kind: "hline"; y: number }
  | { id: string; kind: "vline"; x: number }
  | ({ id: string; kind: "trend" } & TwoPoint)
  | ({ id: string; kind: "ray" } & TwoPoint)
  | ({ id: string; kind: "rect" } & TwoPoint)
  | { id: string; kind: "free"; pts: { x: number; y: number }[] };

let _idSeq = 0;
const nextId = () => `s${++_idSeq}`;

const PERSIST_PREFIX = "oi.drawings.v1.";

/** Load persisted shapes (normalized coords) and advance the id sequence past them. */
function loadShapes(key: string | undefined): Shape[] {
  if (!key || typeof localStorage === "undefined") return [];
  try {
    const raw = localStorage.getItem(PERSIST_PREFIX + key);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    for (const s of parsed as Shape[]) {
      const n = Number(String(s.id).replace(/^s/, ""));
      if (Number.isFinite(n) && n > _idSeq) _idSeq = n;
    }
    return parsed as Shape[];
  } catch {
    return [];
  }
}

function saveShapes(key: string | undefined, shapes: Shape[]): void {
  if (!key || typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(PERSIST_PREFIX + key, JSON.stringify(shapes));
  } catch {
    /* quota / private mode — persistence is best-effort */
  }
}

const STROKE = "#fbbf24"; // amber-400 — visible on the dark theme
const STROKE_PREVIEW = "rgba(251,191,36,0.55)";
const HIT_TOL_PX = 8;

function drawShape(
  ctx: CanvasRenderingContext2D,
  s: Shape,
  w: number,
  h: number,
  preview = false,
): void {
  ctx.strokeStyle = preview ? STROKE_PREVIEW : STROKE;
  ctx.lineWidth = s.kind === "free" ? 2 : 1.5;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.beginPath();
  if (s.kind === "hline") {
    const y = s.y * h;
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
  } else if (s.kind === "vline") {
    const x = s.x * w;
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
  } else if (s.kind === "trend") {
    ctx.moveTo(s.x1 * w, s.y1 * h);
    ctx.lineTo(s.x2 * w, s.y2 * h);
  } else if (s.kind === "ray") {
    const X1 = s.x1 * w, Y1 = s.y1 * h;
    // Extend the segment well past the second point so it reads as a ray.
    const ex = X1 + (s.x2 - s.x1) * w * 100;
    const ey = Y1 + (s.y2 - s.y1) * h * 100;
    ctx.moveTo(X1, Y1);
    ctx.lineTo(ex, ey);
  } else if (s.kind === "rect") {
    const x = Math.min(s.x1, s.x2) * w, y = Math.min(s.y1, s.y2) * h;
    ctx.rect(x, y, Math.abs(s.x2 - s.x1) * w, Math.abs(s.y2 - s.y1) * h);
  } else {
    s.pts.forEach((p, i) => {
      const X = p.x * w;
      const Y = p.y * h;
      if (i === 0) ctx.moveTo(X, Y);
      else ctx.lineTo(X, Y);
    });
  }
  ctx.stroke();
}

/** Distance (px) from point P to segment AB, in canvas pixel space. */
function segDistPx(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax, dy = by - ay;
  const len2 = dx * dx + dy * dy;
  if (len2 === 0) return Math.hypot(px - ax, py - ay);
  let t = ((px - ax) * dx + (py - ay) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

interface ToolBtnProps {
  active?: boolean;
  disabled?: boolean;
  title: string;
  onClick: () => void;
  children: React.ReactNode;
}

function ToolBtn({ active, disabled, title, onClick, children }: ToolBtnProps) {
  return (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={`p-1.5 rounded-md border transition-colors disabled:opacity-30 disabled:cursor-not-allowed ${
        active
          ? "border-accent text-accent bg-accent/10"
          : "border-border text-muted hover:text-foreground hover:border-accent/50"
      }`}
    >
      {children}
    </button>
  );
}

const ICON = "w-4 h-4";
const svg = (d: string) => (
  <svg className={ICON} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
    {d.split("|").map((p, i) => <path key={i} d={p} />)}
  </svg>
);

interface Props {
  /** Maps a pointer pixel (relative to the chart) to a readable value/level + time. */
  describeAt?: (px: number, py: number) => { value: string; time: string | null } | null;
  /** When set, committed drawings are persisted to localStorage under this key. */
  persistKey?: string;
  /**
   * When provided, the toolbar is portaled into this element (e.g. a slot beside
   * the chart heading) instead of floating over the top-right of the canvas — so
   * it never overlaps chart data. The drawing canvas stays over the chart.
   */
  toolbarContainer?: HTMLElement | null;
}

export function ChartDrawingOverlay({ describeAt, persistKey, toolbarContainer }: Props) {
  const rootRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const cursorTagRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });

  const [tool, setTool] = useState<Tool>("cursor");
  const toolRef = useRef(tool);
  toolRef.current = tool;

  // Undo/redo history: an immutable stack of shape-arrays + a cursor. Seeded from
  // any persisted drawings for this key.
  const [hist, setHist] = useState<{ stack: Shape[][]; index: number }>(() => ({
    stack: [loadShapes(persistKey)],
    index: 0,
  }));
  const shapes = hist.stack[hist.index];
  const shapesRef = useRef<Shape[]>(shapes);
  shapesRef.current = shapes;

  // Reload persisted drawings when the persist key changes (e.g. symbol/expiry swap).
  const persistKeyRef = useRef(persistKey);
  useEffect(() => {
    if (persistKeyRef.current === persistKey) return;
    persistKeyRef.current = persistKey;
    setHist({ stack: [loadShapes(persistKey)], index: 0 });
  }, [persistKey]);

  // Persist the current committed drawing set whenever it changes.
  useEffect(() => {
    saveShapes(persistKey, shapes);
  }, [shapes, persistKey]);

  const commit = (next: Shape[]) =>
    setHist((h) => {
      const stack = [...h.stack.slice(0, h.index + 1), next];
      return { stack, index: stack.length - 1 };
    });
  const undo = () => setHist((h) => ({ ...h, index: Math.max(0, h.index - 1) }));
  const redo = () => setHist((h) => ({ ...h, index: Math.min(h.stack.length - 1, h.index + 1) }));
  const clearAll = () => { if (shapesRef.current.length) commit([]); };
  const canUndo = hist.index > 0;
  const canRedo = hist.index < hist.stack.length - 1;

  // Transient drawing state (kept in refs so pointer moves don't re-render React).
  const draggingRef = useRef(false);
  const draftPtsRef = useRef<{ x: number; y: number }[] | null>(null);
  const previewRef = useRef<Shape | null>(null);
  const eraseRemovedRef = useRef<Set<string> | null>(null);

  const redraw = () => {
    const cv = canvasRef.current;
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size.w, size.h);
    const removed = eraseRemovedRef.current;
    for (const s of shapesRef.current) {
      if (removed && removed.has(s.id)) continue;
      drawShape(ctx, s, size.w, size.h);
    }
    if (draftPtsRef.current && draftPtsRef.current.length > 1) {
      drawShape(ctx, { id: "_d", kind: "free", pts: draftPtsRef.current }, size.w, size.h, true);
    }
    if (previewRef.current) drawShape(ctx, previewRef.current, size.w, size.h, true);
  };
  const redrawRef = useRef(redraw);
  redrawRef.current = redraw;

  // Track the chart area size; keep the canvas backing store in sync (DPR-aware).
  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0].contentRect;
      setSize({ w: Math.round(r.width), h: Math.round(r.height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const cv = canvasRef.current;
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.max(1, Math.round(size.w * dpr));
    cv.height = Math.max(1, Math.round(size.h * dpr));
    redrawRef.current();
  }, [size]);

  // Redraw whenever the committed annotation set changes (commit/undo/redo/clear).
  useEffect(() => { redrawRef.current(); }, [hist]);

  // Drop the cursor readout when leaving draw mode.
  useEffect(() => { if (tool === "cursor") hideCursorLabel(); }, [tool]);

  const getPt = (e: React.PointerEvent): { x: number; y: number } => {
    const cv = canvasRef.current!;
    const r = cv.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width;
    const y = (e.clientY - r.top) / r.height;
    return { x: Math.min(1, Math.max(0, x)), y: Math.min(1, Math.max(0, y)) };
  };

  const eraseAt = (p: { x: number; y: number }) => {
    const removed = eraseRemovedRef.current;
    if (!removed) return;
    const { w, h } = size;
    const PX = p.x * w, PY = p.y * h;
    for (const s of shapesRef.current) {
      if (removed.has(s.id)) continue;
      let hit = false;
      if (s.kind === "hline") hit = Math.abs(s.y * h - PY) <= HIT_TOL_PX;
      else if (s.kind === "vline") hit = Math.abs(s.x * w - PX) <= HIT_TOL_PX;
      else if (s.kind === "trend") hit = segDistPx(PX, PY, s.x1 * w, s.y1 * h, s.x2 * w, s.y2 * h) <= HIT_TOL_PX;
      else if (s.kind === "ray") {
        const ex = s.x1 * w + (s.x2 - s.x1) * w * 100;
        const ey = s.y1 * h + (s.y2 - s.y1) * h * 100;
        hit = segDistPx(PX, PY, s.x1 * w, s.y1 * h, ex, ey) <= HIT_TOL_PX;
      } else if (s.kind === "rect") {
        const x1 = s.x1 * w, y1 = s.y1 * h, x2 = s.x2 * w, y2 = s.y2 * h;
        hit = Math.min(
          segDistPx(PX, PY, x1, y1, x2, y1),
          segDistPx(PX, PY, x2, y1, x2, y2),
          segDistPx(PX, PY, x2, y2, x1, y2),
          segDistPx(PX, PY, x1, y2, x1, y1),
        ) <= HIT_TOL_PX;
      } else {
        hit = s.pts.some((q) => Math.hypot((q.x - p.x) * w, (q.y - p.y) * h) <= HIT_TOL_PX);
      }
      if (hit) removed.add(s.id);
    }
  };

  // Floating readout of the value/level at the cursor while a draw tool is active.
  const showCursorLabel = (e: React.PointerEvent) => {
    const tag = cursorTagRef.current;
    const cv = canvasRef.current;
    if (!tag || !cv) return;
    if (toolRef.current === "cursor" || !describeAt) { tag.style.display = "none"; return; }
    const r = cv.getBoundingClientRect();
    const px = e.clientX - r.left;
    const py = e.clientY - r.top;
    const info = describeAt(px, py);
    if (!info) { tag.style.display = "none"; return; }
    tag.textContent = info.time ? `${info.value}  ·  ${info.time}` : info.value;
    tag.style.display = "block";
    tag.style.left = `${Math.min(px + 12, Math.max(0, size.w - 96))}px`;
    tag.style.top = `${Math.max(2, py - 24)}px`;
  };
  const hideCursorLabel = () => { if (cursorTagRef.current) cursorTagRef.current.style.display = "none"; };

  const onPointerDown = (e: React.PointerEvent) => {
    const t = toolRef.current;
    if (t === "cursor") return;
    e.preventDefault();
    canvasRef.current?.setPointerCapture(e.pointerId);
    draggingRef.current = true;
    const p = getPt(e);
    if (t === "free") draftPtsRef.current = [p];
    else if (t === "hline") previewRef.current = { id: "_p", kind: "hline", y: p.y };
    else if (t === "vline") previewRef.current = { id: "_p", kind: "vline", x: p.x };
    else if (t === "trend" || t === "ray" || t === "rect")
      previewRef.current = { id: "_p", kind: t, x1: p.x, y1: p.y, x2: p.x, y2: p.y } as Shape;
    else if (t === "eraser") { eraseRemovedRef.current = new Set(); eraseAt(p); }
    redrawRef.current();
  };

  const onPointerMove = (e: React.PointerEvent) => {
    showCursorLabel(e); // update on hover too, not only while dragging
    if (!draggingRef.current) return;
    const t = toolRef.current;
    const p = getPt(e);
    if (t === "free") draftPtsRef.current?.push(p);
    else if (t === "hline") { const pv = previewRef.current; if (pv && pv.kind === "hline") pv.y = p.y; }
    else if (t === "vline") { const pv = previewRef.current; if (pv && pv.kind === "vline") pv.x = p.x; }
    else if (t === "trend" || t === "ray" || t === "rect") {
      const pv = previewRef.current;
      if (pv && (pv.kind === "trend" || pv.kind === "ray" || pv.kind === "rect")) { pv.x2 = p.x; pv.y2 = p.y; }
    }
    else if (t === "eraser") eraseAt(p);
    redrawRef.current();
  };

  const onPointerUp = (e: React.PointerEvent) => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    try { canvasRef.current?.releasePointerCapture(e.pointerId); } catch { /* already released */ }
    const t = toolRef.current;
    if (t === "free") {
      const pts = draftPtsRef.current;
      draftPtsRef.current = null;
      if (pts && pts.length > 1) commit([...shapesRef.current, { id: nextId(), kind: "free", pts }]);
      else redrawRef.current();
    } else if (t === "hline") {
      const pv = previewRef.current;
      previewRef.current = null;
      if (pv && pv.kind === "hline") commit([...shapesRef.current, { id: nextId(), kind: "hline", y: pv.y }]);
    } else if (t === "vline") {
      const pv = previewRef.current;
      previewRef.current = null;
      if (pv && pv.kind === "vline") commit([...shapesRef.current, { id: nextId(), kind: "vline", x: pv.x }]);
    } else if (t === "trend" || t === "ray" || t === "rect") {
      const pv = previewRef.current;
      previewRef.current = null;
      if (pv && (pv.kind === "trend" || pv.kind === "ray" || pv.kind === "rect")) {
        // Ignore a click with no drag (zero-size shape).
        if (Math.hypot((pv.x2 - pv.x1) * size.w, (pv.y2 - pv.y1) * size.h) > 3) {
          commit([
            ...shapesRef.current,
            { id: nextId(), kind: pv.kind, x1: pv.x1, y1: pv.y1, x2: pv.x2, y2: pv.y2 } as Shape,
          ]);
        } else redrawRef.current();
      }
    } else if (t === "eraser") {
      const removed = eraseRemovedRef.current;
      eraseRemovedRef.current = null;
      if (removed && removed.size) commit(shapesRef.current.filter((s) => !removed.has(s.id)));
      else redrawRef.current();
    }
  };

  const interactive = tool !== "cursor";

  // Toolbar element — floated in the chart's top-right corner by default, or (when
  // a container is supplied) portaled into that slot beside the heading so it never
  // overlaps chart data. State stays here, so all tools/undo/redo work unchanged.
  const toolbar = (
    <div
      className={
        "flex items-center gap-1 rounded-lg border border-border bg-surface/90 backdrop-blur-sm px-1.5 py-1 shadow-lg" +
        (toolbarContainer ? "" : " absolute top-2 right-2")
      }
      style={{ pointerEvents: "auto" }}
    >
      <ToolBtn active={tool === "cursor"} title="Cursor — interact with chart" onClick={() => setTool("cursor")}>
        {svg("M5 3l13 7-5 1.5L11 17z")}
      </ToolBtn>
      <ToolBtn active={tool === "hline"} title="Horizontal line" onClick={() => setTool("hline")}>
        {svg("M3 12h18")}
      </ToolBtn>
      <ToolBtn active={tool === "vline"} title="Vertical line" onClick={() => setTool("vline")}>
        {svg("M12 3v18")}
      </ToolBtn>
      <ToolBtn active={tool === "trend"} title="Trend line (2 points)" onClick={() => setTool("trend")}>
        {svg("M4 20L20 4")}
      </ToolBtn>
      <ToolBtn active={tool === "ray"} title="Ray (extends from the first point)" onClick={() => setTool("ray")}>
        {svg("M4 20L20 4|M20 4h-5|M20 4v5")}
      </ToolBtn>
      <ToolBtn active={tool === "rect"} title="Rectangle" onClick={() => setTool("rect")}>
        {svg("M4 6h16v12H4z")}
      </ToolBtn>
      <ToolBtn active={tool === "free"} title="Freehand pen" onClick={() => setTool("free")}>
        {svg("M4 20l3.5-1L18 8.5 15.5 6 5 16.5z|M14 7l3 3")}
      </ToolBtn>
      <ToolBtn active={tool === "eraser"} title="Eraser — click/drag over a line to remove it" onClick={() => setTool("eraser")}>
        {svg("M15 4l5 5-9 9H6l-3-3z|M9 11l5 5")}
      </ToolBtn>

      <span className="w-px h-5 bg-border mx-0.5" />

      <ToolBtn disabled={!canUndo} title="Undo" onClick={undo}>
        {svg("M9 7L4 12l5 5|M4 12h10a5 5 0 010 10h-2")}
      </ToolBtn>
      <ToolBtn disabled={!canRedo} title="Redo" onClick={redo}>
        {svg("M15 7l5 5-5 5|M20 12H10a5 5 0 000 10h2")}
      </ToolBtn>
      <ToolBtn disabled={shapes.length === 0} title="Clear all annotations" onClick={clearAll}>
        {svg("M4 7h16|M9 7V4h6v3|M6 7l1 13h10l1-13")}
      </ToolBtn>
    </div>
  );

  return (
    <div ref={rootRef} className="absolute inset-0 z-10" style={{ pointerEvents: "none" }}>
      <canvas
        ref={canvasRef}
        className="absolute inset-0"
        style={{
          width: "100%",
          height: "100%",
          pointerEvents: interactive ? "auto" : "none",
          cursor: interactive ? "crosshair" : "default",
          touchAction: "none",
        }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={(e) => { onPointerUp(e); hideCursorLabel(); }}
      />

      <div
        ref={cursorTagRef}
        className="absolute px-1.5 py-0.5 rounded text-[11px] font-mono tabular-nums bg-slate-900/90 border border-amber-400/40 text-amber-300 shadow"
        style={{ display: "none", pointerEvents: "none", whiteSpace: "nowrap", zIndex: 20 }}
      />

      {toolbarContainer ? createPortal(toolbar, toolbarContainer) : toolbar}
    </div>
  );
}
