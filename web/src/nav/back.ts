// Run the innermost Back handler; leave an empty stack to the caller.

import { onCleanup } from 'solid-js';

const handlers: (() => void)[] = [];

export function onBack(handler: () => void): void {
  handlers.push(handler);
  onCleanup(() => {
    const i = handlers.lastIndexOf(handler);
    if (i >= 0) handlers.splice(i, 1);
  });
}

/** Invoke the current handler, or return false if none is registered. */
export function back(): boolean {
  const handler = handlers.at(-1);
  handler?.();
  return handler !== undefined;
}
