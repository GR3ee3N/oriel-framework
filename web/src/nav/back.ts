// Back handlers run innermost first; the caller handles an empty stack.

import { onCleanup } from 'solid-js';

const handlers: (() => void)[] = [];

export function onBack(handler: () => void): void {
  handlers.push(handler);
  onCleanup(() => {
    const i = handlers.lastIndexOf(handler);
    if (i >= 0) handlers.splice(i, 1);
  });
}

/** False if there is no handler. */
export function back(): boolean {
  const handler = handlers.at(-1);
  handler?.();
  return handler !== undefined;
}
