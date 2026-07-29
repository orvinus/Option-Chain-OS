import type { UTCTimestamp } from "lightweight-charts";

/**
 * Convert an IST ISO timestamp ("...T10:25:00+05:30") to a lightweight-charts
 * time value that renders as the IST wall-clock.
 *
 * lightweight-charts renders the time axis in UTC. Our data is IST-only, so we
 * add the fixed +5:30 offset to the real epoch: the chart then labels the point
 * with its IST wall-clock time regardless of the viewer's local timezone.
 */
const IST_OFFSET_SEC = 5.5 * 3600;

export function istIsoToChartTime(iso: string): UTCTimestamp {
  const epoch = Date.parse(iso);
  return (Math.floor(epoch / 1000) + IST_OFFSET_SEC) as UTCTimestamp;
}
