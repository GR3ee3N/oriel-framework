// Navigate by element position with remembered focus per scope and group.
// Horizontal moves stay within the row. Apply the focused class independently
// of native DOM focus.

import { createSignal, onCleanup, untrack } from 'solid-js';
import { useScope } from './scope';

export type Direction = 'left' | 'right' | 'up' | 'down';

export interface Options {
  scope?: string;
  group?: string;
  onSelect?: () => unknown;
  /** Return true to consume an arrow without moving focus, such as during scrubbing. */
  onArrow?: (direction: Direction) => boolean;
  onPlay?: () => unknown;
  onFocus?: () => void;
  autofocus?: boolean;
  /** Default target when entering a group without remembered focus. */
  entry?: boolean;
  /** Disable group focus recall to enter at the nearest element. */
  recall?: boolean;
}

interface Entry extends Options {
  scope: string;
}

const entries = new Map<HTMLElement, Entry>();
const last = new Map<string, HTMLElement>();
const lastInGroup = new Map<string, HTMLElement>();
const [current, setCurrent] = createSignal<HTMLElement | null>(null);
const [activeScope, setActiveScope] = createSignal('main');
// Implicit focus may yield to an autofocus element that mounts later.
let implicit = false;

export { current as focused, activeScope };

function focus(el: HTMLElement | null): void {
  const prev = current();
  if (prev === el) return;
  prev?.classList.remove('focused');
  el?.classList.add('focused');
  if (el) {
    const e = entries.get(el)!;
    last.set(e.scope, el);
    if (e.group && e.recall !== false) lastInGroup.set(`${e.scope}/${e.group}`, el);
  }
  setCurrent(el);
  if (import.meta.env.DEV && el) console.debug(`focus: ${(el.textContent ?? '').trim().slice(0, 40) || el.className}`);
  if (el) entries.get(el)!.onFocus?.();
}

function inScope(scope: string): HTMLElement[] {
  const out: HTMLElement[] = [];
  for (const [el, e] of entries) if (e.scope === scope && el.isConnected) out.push(el);
  return out;
}

function restore(scope: string): void {
  const remembered = last.get(scope);
  if (remembered?.isConnected && entries.has(remembered) && !implicit) return focus(remembered);
  const els = inScope(scope);
  const auto = els.find((el) => entries.get(el)!.autofocus);
  const entry = els.find((el) => entries.get(el)!.entry);
  focus(auto ?? entry ?? els[0] ?? null);
  implicit = !auto;
}

/** Register from a Solid ref; remove the entry on owner cleanup. */
export function focusable(el: HTMLElement, options: Options = {}): void {
  const entry = { ...options, scope: options.scope ?? useScope() };
  entries.set(el, entry);
  if (entry.scope === activeScope() && (!current() || (entry.autofocus && implicit))) {
    queueMicrotask(() => {
      if (entry.scope === activeScope()) restore(entry.scope);
    });
  }
  onCleanup(() => {
    entries.delete(el);
    if (last.get(entry.scope) === el) last.delete(entry.scope);
    const key = `${entry.scope}/${entry.group}`;
    if (entry.group && lastInGroup.get(key) === el) lastInGroup.delete(key);
    if (current() === el) {
      setCurrent(null);
      queueMicrotask(() => {
        if (!current()) restore(activeScope());
      });
    }
  });
}

/** Enter a focus scope; fresh clears its remembered focus. */
export function setScope(scope: string, fresh = false): void {
  // Do not subscribe calling effects to focus changes made during scope restoration.
  untrack(() => {
    if (fresh) last.delete(scope);
    if (scope === activeScope() && !fresh) return;
    setActiveScope(scope);
    restore(scope);
  });
}

export function focusOn(el: HTMLElement): void {
  if (entries.get(el)?.scope !== activeScope()) return;
  implicit = false;
  focus(el);
}

export function move(direction: Direction): boolean {
  const from = current();
  if (!from?.isConnected) {
    restore(activeScope());
    return current() !== null;
  }
  const fromEntry = entries.get(from);
  if (fromEntry?.onArrow?.(direction)) return true;
  const a = from.getBoundingClientRect();
  let best: HTMLElement | null = null;
  let bestScore = Infinity;
  for (const el of inScope(activeScope())) {
    if (el === from) continue;
    const b = el.getBoundingClientRect();
    if (b.width === 0 && b.height === 0) continue;
    // Weight cross-axis distance twice to favor candidates aligned with the current row.
    let ahead: number;
    let aside: number;
    switch (direction) {
      case 'right':
        ahead = b.left - a.right;
        aside = Math.abs(b.top + b.height / 2 - (a.top + a.height / 2));
        break;
      case 'left':
        ahead = a.left - b.right;
        aside = Math.abs(b.top + b.height / 2 - (a.top + a.height / 2));
        break;
      case 'down':
        ahead = b.top - a.bottom;
        aside = Math.abs(b.left + b.width / 2 - (a.left + a.width / 2));
        break;
      case 'up':
        ahead = a.top - b.bottom;
        aside = Math.abs(b.left + b.width / 2 - (a.left + a.width / 2));
        break;
    }
    if (ahead < -1) continue;
    if ((direction === 'left' || direction === 'right') && (b.bottom <= a.top || b.top >= a.bottom)) continue;
    const score = Math.max(ahead, 0) + 2 * aside;
    if (score < bestScore) {
      bestScore = score;
      best = el;
    }
  }
  if (!best) return false;
  const to = entries.get(best)!;
  if (to.group && to.group !== fromEntry?.group) {
    const remembered = lastInGroup.get(`${to.scope}/${to.group}`);
    if (remembered?.isConnected) best = remembered;
    else {
      for (const [el, e] of entries) {
        if (e.entry && e.group === to.group && e.scope === to.scope && el.isConnected) {
          best = el;
          break;
        }
      }
    }
  }
  implicit = false;
  focus(best);
  return true;
}

export function playFocused(): boolean {
  const el = current();
  const handler = el && entries.get(el)?.onPlay;
  if (!handler) return false;
  Promise.resolve(handler()).catch((e) => console.error(`play: ${e?.stack ?? e}`));
  return true;
}

export function select(): void {
  implicit = false;
  const el = current();
  if (!el) return;
  Promise.resolve(entries.get(el)?.onSelect?.()).catch((e) => console.error(`select: ${e?.stack ?? e}`));
}
