import { createRoot } from 'solid-js';
import { afterEach, expect, test } from 'vitest';
import { focusable, focused, setScope } from '../src/nav/focus';

const element = () => {
  const classes = new Set<string>();
  return {
    classList: { add: (c: string) => classes.add(c), remove: (c: string) => classes.delete(c) },
    isConnected: true,
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
