// Settings saved by the add-on, not WebKit, so they work from the dev server too.

import { ORIGIN } from './bridge';

export interface BaseSaved {
  /** Not saved: changes when the app starts, but not when only the shell restarts. */
  run?: string;
}

export async function load<T extends BaseSaved>(): Promise<T> {
  const r = await fetch(`${ORIGIN}/api/store`);
  if (!r.ok) throw new Error(`store: HTTP ${r.status}`);
  return r.json();
}

/** null removes a key. */
export async function save<T extends BaseSaved>(changes: { [K in keyof T]?: T[K] | null }): Promise<void> {
  const r = await fetch(`${ORIGIN}/api/store`, { method: 'POST', body: JSON.stringify(changes) });
  if (!r.ok) throw new Error(`store: HTTP ${r.status}`);
}
