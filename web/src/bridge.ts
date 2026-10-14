// State and commands between the page and its add-on.
//
//     export const { state, send, call, connect, ready } = createBridge<State, Command>({ ... });

import { createStore, reconcile } from 'solid-js/store';

export interface BaseState {
  connected: boolean;
  /** Graphics white in nits on HDR output, the framework's setting for
   * every app; 203 (BT.2408's) unless changed. */
  hdrWhite: number;
  /** WebKit draws the page on the CPU instead of the GPU, the framework's
   * setting for every app; false unless changed. */
  cpuRendering: boolean;
  /** Kodi's screensaver delay in minutes, for an app with `own_screensaver`;
   * null for other apps, or when Kodi's screensaver is off. */
  screensaver: number | null;
}

/** A success means it's done. */
export type BaseCommand =
  | { cmd: 'ready' }
  | { cmd: 'exit' }
  /** Sets and saves `hdrWhite`, 80 to 1000; WebKit uses it from its next frame. */
  | { cmd: 'hdr-white'; nits: number }
  /** Sets and saves `cpuRendering`. A change restarts the page, which loses
   * its state, as WebKit takes it only when it starts. */
  | { cmd: 'cpu-rendering'; on: boolean };

/** Why a command failed (docs/apps.md). invalid: it can't work as sent;
 * unavailable: not now, e.g. without playback; failed: the add-on broke;
 * timeout: no reply in time, though it may have run; unreachable: no answer
 * from the add-on, or one cut off. */
export type FailureCode = 'invalid' | 'unavailable' | 'failed' | 'timeout' | 'unreachable';

export class CommandFailure extends Error {
  code: FailureCode;

  constructor(message: string, code: FailureCode) {
    super(message);
    this.code = code;
  }
}

// Loaded from the dev server, the page still runs in WebKit on the box, next to its add-on.
export const ORIGIN = import.meta.env.DEV ? 'http://127.0.0.1:32499' : '';

export interface Bridge<S extends BaseState, C extends { cmd: string }> {
  state: S;
  /** Fire and forget: a failure is logged, not thrown. Commands from send
   * and call reach the add-on one at a time, in the order they were sent. */
  send(command: C | BaseCommand): void;
  /** The command's result; rejects with a CommandFailure, for commands
   * whose failure the caller handles. */
  call<R = unknown>(command: C | BaseCommand): Promise<R>;
  /** Call once at start. */
  connect(): void;
  /** Call after the first frame. */
  ready(): void;
}

export function createBridge<S extends BaseState, C extends { cmd: string }>(
  initial: Omit<S, keyof BaseState>,
): Bridge<S, C> {
  // Solid's setters don't take a generic store's keys.
  const [state, setState] = createStore<BaseState>({ ...initial, connected: false, hdrWhite: 203, cpuRendering: false, screensaver: null });

  const post = async <R>(command: C | BaseCommand): Promise<R> => {
    // text/plain avoids a CORS preflight from the dev server.
    const r = await fetch(`${ORIGIN}/api/cmd`, { method: 'POST', body: JSON.stringify(command) }).catch((e) => {
      throw new CommandFailure(`${command.cmd}: ${e}`, 'unreachable');
    });
    const reply = (await r.json().catch(() => null)) as { result?: R; error?: string; code?: FailureCode } | null;
    if (!r.ok) throw new CommandFailure(`${command.cmd}: ${reply?.error ?? `HTTP ${r.status}`}`, reply?.code ?? 'failed');
    // A cut-off reply leaves the outcome unknown.
    if (!reply) throw new CommandFailure(`${command.cmd}: unreadable reply`, 'unreachable');
    return reply.result as R;
  };

  // One command at a time: requests in parallel can reach the add-on in
  // another order than they were sent.
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
      setState(reconcile({ ...next, connected: true }));
    });
    events.onerror = () => setState('connected', false);
  };

  const ready = (): void => {
    requestAnimationFrame(() => requestAnimationFrame(() => send({ cmd: 'ready' })));
  };

  return { state: state as S, send, call, connect, ready };
}
