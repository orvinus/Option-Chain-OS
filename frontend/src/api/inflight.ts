/**
 * Global in-flight request counter.
 *
 * Both API layers funnel every call through exactly one function — `getJSON`
 * in `rest.ts` and `request` in `algoRest.ts` — so counting there covers the
 * WHOLE app in one place: every page, every panel, every poll, including
 * fetches added later, with nothing to remember to wire up.
 *
 * The counter is deliberately dumb: begin/end and a subscriber list. The delay
 * before anything is shown, and the minimum time it stays up, are the
 * consumer's business (see `useIsLoading`).
 */

type Listener = (active: number) => void;

let active = 0;
const listeners = new Set<Listener>();

function emit() {
  for (const fn of listeners) fn(active);
}

export function beginRequest(): void {
  active += 1;
  emit();
}

export function endRequest(): void {
  // Never go negative: a double-end would otherwise wedge the counter below
  // zero and the indicator would stop showing for every later request.
  active = Math.max(0, active - 1);
  emit();
}

/** Count + end() in one call, so callers cannot forget the decrement. */
export async function trackRequest<T>(run: () => Promise<T>): Promise<T> {
  beginRequest();
  try {
    return await run();
  } finally {
    endRequest();
  }
}

export function subscribeInflight(fn: Listener): () => void {
  listeners.add(fn);
  fn(active);
  return () => {
    listeners.delete(fn);
  };
}

export function inflightCount(): number {
  return active;
}
