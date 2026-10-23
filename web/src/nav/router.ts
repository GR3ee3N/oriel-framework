// Keep screens below the top entry mounted so Back restores their focus
// and scroll position within the current tab.
//
//     export const { stack, top, root, push, pop, tab } = createRouter<Route>({ screen: 'home' });

import { createSignal } from 'solid-js';

export interface Entry<R> {
  route: R;
  scope: string;
}

export function createRouter<R>(home: R) {
  let next = 0;
  const entry = (route: R): Entry<R> => ({ route, scope: `screen-${next++}` });

  const [stack, setStack] = createSignal<Entry<R>[]>([entry(home)]);

  const top = () => stack()[stack().length - 1];
  const root = () => stack()[0];

  function push(route: R): void {
    setStack((s) => [...s, entry(route)]);
  }

  /** Return false at the tab root; otherwise remove the top screen. */
  function pop(): boolean {
    if (stack().length <= 1) return false;
    setStack((s) => s.slice(0, -1));
    return true;
  }

  function tab(route: R): void {
    const current = root().route;
    if (stack().length === 1 && JSON.stringify(current) === JSON.stringify(route)) return;
    setStack([entry(route)]);
  }

  return { stack, top, root, push, pop, tab };
}
