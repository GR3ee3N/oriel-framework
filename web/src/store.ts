// Persist settings through the add-on so bundled and development pages share them.

import { ORIGIN } from './bridge';

export async function load<T extends object>(): Promise<T> {
  const r = await fetch(`${ORIGIN}/api/store`);
  if (!r.ok) throw new Error(`store: HTTP ${r.status}`);
  return r.json();
}

/** Merge settings; null deletes a saved key. */
export async function save<T extends object>(changes: { [K in keyof T]?: T[K] | null }): Promise<void> {
  const r = await fetch(`${ORIGIN}/api/store`, { method: 'POST', body: JSON.stringify(changes) });
  if (!r.ok) throw new Error(`store: HTTP ${r.status}`);
}
