import { createRoot } from 'solid-js';
import { afterEach, expect, test } from 'vitest';
import { focusable, focused, setScope } from '../src/nav/focus';

// Count connection checks, one per element each time a scope is scanned.
let checks = 0;
const element = () => {
  const classes = new Set<string>();
  return {
    classList: { add: (c: string) => classes.add(c), remove: (c: string) => classes.delete(c) },
    get isConnected() {
      checks++;
      return true;
    },
    textContent: '',
  } as unknown as HTMLElement;
};

let dispose: () => void;
afterEach(() => {
  dispose();
  setScope('main', true);
});

test('a restore queued for a scope that is no longer active leaves the focus alone', async () => {
  const background = element();
  const dialog = element();
  createRoot((cleanup) => {
    dispose = cleanup;
    focusable(background);
    focusable(dialog, { scope: 'dialog' });
    setScope('dialog');
  });
  await Promise.resolve();
  expect(focused()).toBe(dialog);
});

test('disposing a scope removes its registered elements', async () => {
  const button = element();
  createRoot((cleanup) => {
    dispose = cleanup;
    focusable(button);
  });
  await Promise.resolve();
  expect(focused()).toBe(button);
  dispose();
  await Promise.resolve();
  expect(focused()).toBeNull();
});

test('mounting many elements at once restores focus once', async () => {
  const buttons = Array.from({ length: 500 }, element);
  checks = 0;
  createRoot((cleanup) => {
    dispose = cleanup;
    for (const button of buttons) focusable(button);
  });
  await Promise.resolve();
  expect(focused()).toBe(buttons[0]);
  expect(checks).toBeLessThan(2 * buttons.length);
});

test('an autofocus element that mounts later takes focus', async () => {
  const first = element();
  const later = element();
  createRoot((cleanup) => {
    dispose = cleanup;
    focusable(first);
  });
  await Promise.resolve();
  expect(focused()).toBe(first);
  const disposeFirst = dispose;
  createRoot((cleanup) => {
    dispose = () => {
      cleanup();
      disposeFirst();
    };
    focusable(later, { autofocus: true });
  });
  await Promise.resolve();
  expect(focused()).toBe(later);
});
