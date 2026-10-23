// Persist settings through the add-on so bundled and development pages share them.

import { ORIGIN } from './bridge';

export interface BaseSaved {
  /** App-run id supplied at load time; survives shell restarts and is not persisted. */
  run?: string;
}

export async function load<T extends BaseSaved>(): Promise<T> {
  const r = await fetch(`${ORIGIN}/api/store`);
  if (!r.ok) throw new Error(`store: HTTP ${r.status}`);
  return r.json();
}

/** Merge settings; null deletes a saved key. */
export async function save<T extends BaseSaved>(changes: { [K in keyof T]?: T[K] | null }): Promise<void> {
  const r = await fetch(`${ORIGIN}/api/store`, { method: 'POST', body: JSON.stringify(changes) });
  if (!r.ok) throw new Error(`store: HTTP ${r.status}`);
}
