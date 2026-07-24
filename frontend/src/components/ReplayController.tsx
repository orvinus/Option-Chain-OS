/** Presentational replay transport bar (play/pause/restart/skip/jump/scrub/speed).
 * All state is owned by the parent; this only renders and emits callbacks.
 */
const SPEEDS = [1, 2, 5, 10, 15] as const;

interface Props {
  playing: boolean;
  index: number;
  total: number;
  speed: number;
  currentTs: string | null;
  onPlayPause: () => void;
  onRestart: () => void;
  onSeek: (index: number) => void;
  onSkip: (delta: number) => void;
  onSpeed: (speed: number) => void;
}

export function ReplayController({
  playing, index, total, speed, currentTs,
  onPlayPause, onRestart, onSeek, onSkip, onSpeed,
}: Props) {
  const atEnd = total > 0 && index >= total - 1;
  const clock = currentTs ? currentTs.slice(11, 19) : "--:--:--";

  return (
    <div className="panel px-4 py-3 flex flex-col gap-2">
      <div className="flex items-center gap-2 flex-wrap">
        <button type="button" className="pill" onClick={onRestart} title="Restart">⏮</button>
        <button type="button" className="pill" onClick={() => onSkip(-1)} title="Step back">◀</button>
        <button
          type="button"
          className="pill pill-active min-w-[70px]"
          onClick={onPlayPause}
          disabled={total === 0}
        >
          {playing ? "❚❚ Pause" : atEnd ? "↻ Replay" : "▶ Play"}
        </button>
        <button type="button" className="pill" onClick={() => onSkip(1)} title="Step forward">▶</button>

        <div className="flex items-center gap-1 ml-2">
          <span className="text-xs text-muted">Speed</span>
          {SPEEDS.map((s) => (
            <button
              key={s}
              type="button"
              className={`pill ${speed === s ? "pill-active" : ""}`}
              onClick={() => onSpeed(s)}
            >
              {s}×
            </button>
          ))}
        </div>

        <div className="ml-auto font-mono text-sm text-foreground">
          {clock} <span className="text-muted text-xs">· {total ? index + 1 : 0}/{total}</span>
        </div>
      </div>

      <input
        type="range"
        min={0}
        max={Math.max(0, total - 1)}
        value={index}
        onChange={(e) => onSeek(Number(e.target.value))}
        disabled={total === 0}
        className="w-full accent-accent"
      />
    </div>
  );
}
