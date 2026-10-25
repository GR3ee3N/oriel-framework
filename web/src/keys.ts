// Map DOM keys to the action names in addon/lib/oriel/app.py's KEYS table.
// Input passes through Kodi actions, evdev codes and the xkb keymap first.

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
