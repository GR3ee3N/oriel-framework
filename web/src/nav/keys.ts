// Remote keys as the page sees them, after Kodi action -> evdev -> xkb; the
// names are the add-on's (KEYS in addon/lib/oriel/app.py).

export type Digit = '0' | '1' | '2' | '3' | '4' | '5' | '6' | '7' | '8' | '9';

export type Action =
  | 'left' | 'right' | 'up' | 'down'
  | 'select' | 'back' | 'info' | 'menu'
  | 'playpause' | 'stop' | 'next' | 'previous' | 'fastforward' | 'rewind'
  | 'pageup' | 'pagedown'
  | Digit;

const ACTIONS: Record<string, Action> = {
  ArrowLeft: 'left',
  ArrowRight: 'right',
  ArrowUp: 'up',
  ArrowDown: 'down',
  Enter: 'select',
  Escape: 'back',
  Backspace: 'back',
  F1: 'info',
  ContextMenu: 'menu',
  MediaPlay: 'playpause',
  MediaStop: 'stop',
  MediaTrackNext: 'next',
  MediaTrackPrevious: 'previous',
  MediaFastForward: 'fastforward',
  MediaRewind: 'rewind',
  PageUp: 'pageup',
  PageDown: 'pagedown',
  ...Object.fromEntries([...'0123456789'].map((d) => [d, d as Digit])),
};

export function action(e: KeyboardEvent): Action | undefined {
  return ACTIONS[e.key];
}
