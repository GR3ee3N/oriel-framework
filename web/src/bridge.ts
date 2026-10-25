// Synchronize page state and commands with the Kodi add-on.
//
//     const bridge = createBridge<State, Command>({ ... });
//     bridge.onState((state) => render(state));

export interface BaseState {
  connected: boolean;
  /** Shared framework CPU rendering setting; defaults to false (GPU). */
  cpuRendering: boolean;
  /** Kodi idle delay in minutes for apps using own_screensaver; null for
   * other apps or when Kodi's screensaver is disabled. */
  screensaver: number | null;
}

/** These commands complete before replying, except the subsequent rendering restart. */
export type BaseCommand =
  | { cmd: 'ready' }
  | { cmd: 'exit' }
  /** Save cpuRendering. A changed value restarts WebKit to apply the setting,
   * reloading the page and discarding its in-memory state. */
  | { cmd: 'cpu-rendering'; on: boolean };

/** Failure codes from docs/apps.md: invalid request, unavailable app state,
 * unexpected handler failure, response timeout with unknown outcome, or
 * unreachable add-on/unreadable response. */
export type FailureCode = 'invalid' | 'unavailable' | 'failed' | 'timeout' | 'unreachable';

export class CommandFailure extends Error {
  code: FailureCode;

  constructor(message: string, code: FailureCode) {
    super(message);
    this.code = code;
  }
}

// The add-on serves the bundled page on its API port. A page from a
// development server still runs in the box's WebKit and reaches the same API.
export const ORIGIN = globalThis.location?.port === '32499' ? '' : 'http://127.0.0.1:32499';

export interface Bridge<S extends BaseState, C extends { cmd: string }> {
  /** The latest state. Each update replaces the object rather than changing it. */
  state(): S;
  /** Call listener with each new state; the returned function removes it. */
  onState(listener: (state: S) => void): () => void;
  /** Queue a command and log any failure. send and call share an ordered queue. */
  send(command: C | BaseCommand): void;
  /** Queue a command and return its result, rejecting with CommandFailure on error. */
  call<R = unknown>(command: C | BaseCommand): Promise<R>;
  /** Open the state event stream once at app startup. */
  connect(): void;
  /** Notify the add-on after the initial page render. */
  ready(): void;
}

export function createBridge<S extends BaseState, C extends { cmd: string }>(
  initial: Omit<S, keyof BaseState>,
): Bridge<S, C> {
  let state = { ...initial, connected: false, cpuRendering: false, screensaver: null } as S;
  const listeners = new Set<(state: S) => void>();
  const update = (next: S) => {
    state = next;
    for (const listener of listeners) listener(state);
  };

  const post = async <R>(command: C | BaseCommand): Promise<R> => {
    // A string body defaults to text/plain, permitting requests without CORS preflight.
    const r = await fetch(`${ORIGIN}/api/cmd`, { method: 'POST', body: JSON.stringify(command) }).catch((e) => {
      throw new CommandFailure(`${command.cmd}: ${e}`, 'unreachable');
    });
    const reply = (await r.json().catch(() => null)) as { result?: R; error?: string; code?: FailureCode } | null;
    if (!r.ok) throw new CommandFailure(`${command.cmd}: ${reply?.error ?? `HTTP ${r.status}`}`, reply?.code ?? 'failed');
    // An unreadable reply cannot establish whether the command completed.
    if (!reply) throw new CommandFailure(`${command.cmd}: unreadable reply`, 'unreachable');
    return reply.result as R;
  };

  // Serialize requests so network arrival order cannot reorder page commands.
  let last: Promise<unknown> = Promise.resolve();
  const call = <R>(command: C | BaseCommand): Promise<R> => {
    const reply = last.then(() => post<R>(command));
    last = reply.catch(() => undefined);
    return reply;
  };

  const send = (command: C | BaseCommand): void => {
    call(command).catch((e: Error) => console.error(`bridge: ${e.message}`));
  };

  const connect = (): void => {
    const events = new EventSource(`${ORIGIN}/api/events`);
    events.addEventListener('state', (e) => {
      const next = JSON.parse((e as MessageEvent<string>).data) as Omit<S, 'connected'>;
      update({ ...next, connected: true } as S);
    });
    events.onerror = () => {
      if (state.connected) update({ ...state, connected: false });
    };
  };

  const ready = (): void => {
    requestAnimationFrame(() => requestAnimationFrame(() => send({ cmd: 'ready' })));
  };

  const onState = (listener: (state: S) => void) => {
    listeners.add(listener);
    return () => listeners.delete(listener);
  };

  return { state: () => state, onState, send, call, connect, ready };
}
