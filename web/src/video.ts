// VideoApp state and command contracts; implemented in addon/lib/oriel/video.py.

import type { BaseState } from './bridge';

/** browse shows the app; osd overlays Kodi video; video hides the page. */
export type Mode = 'browse' | 'osd' | 'video';

/** Audio tracks in the active file, identified by Kodi index. */
export interface AudioTrack {
  index: number;
  language: string;
  name: string;
  /** FFmpeg codec name. */
  codec: string;
  channels: number;
}

export interface SubtitleTrack {
  index: number;
  language: string;
  name: string;
  forced: boolean;
  impaired: boolean;
}

/** All fields are present; null represents an unknown or absent value. */
export interface Player<Meta = unknown> {
  state: 'idle' | 'loading' | 'playing' | 'paused';
  /** Position in seconds on the item timeline. */
  time: number;
  /** Duration in seconds; null until supplied by the page or Kodi. */
  duration: number | null;
  session: string | null;
  /** End reason, null until playback finishes. */
  ended: 'ended' | 'stopped' | 'error' | null;
  /** App metadata retained for restoring a page reloaded during playback. */
  meta: Meta | null;
  /** Track lists are null until this session's file starts, then arrays;
   * an empty array means no tracks of that kind. */
  audio: AudioTrack[] | null;
  subtitles: SubtitleTrack[] | null;
  audioIndex: number | null;
  /** Active subtitle index; null when subtitles are disabled. */
  subtitleIndex: number | null;
}

export interface VideoState<Meta = unknown> extends BaseState {
  mode: Mode;
  player: Player<Meta>;
  /** Hardware video codecs by FFmpeg name; null when the driver cannot report them. */
  decoders: string[] | null;
}

/** Setting an already active state succeeds without further action.
 * play, stop, seek-to, audio and subtitle reply after requesting the Kodi
 * operation; observe state for completion. Other commands complete before
 * replying. Optional fields accept omission or null for their defaults. */
export type VideoCommand<Meta = unknown> =
  /** Base URL for relative media paths and headers added by the media proxy. */
  | { cmd: 'media-source'; base: string; headers?: Record<string, string> | null }
  | {
      cmd: 'play';
      /** Relative media-source path or absolute playback URL. */
      path: string;
      title?: string | null;
      session?: string | null;
      /** Starting position in seconds on the item timeline. */
      position?: number | null;
      /** Stream start in item-timeline seconds: 0 for a complete file, or the
       * offset of a transcode beginning partway through it. */
      streamOffset?: number | null;
      /** Item duration in seconds when Kodi cannot determine it, such as a transcode. */
      duration?: number | null;
      /** Kodi audio index to apply after the file starts. */
      audio?: number | null;
      /** Kodi subtitle index; -1 disables subtitles. */
      subtitle?: number | null;
      subtitleFile?: string | null;
      meta?: Meta | null;
    }
  /** Target position on the item timeline, in seconds; must be within the stream. */
  | { cmd: 'seek-to'; seconds: number }
  | { cmd: 'audio'; index: number }
  | { cmd: 'subtitle'; index?: number | null; file?: string | null }
  /** Show the page over playback, including for app prompts. */
  | { cmd: 'osd-on' }
  | { cmd: 'osd-off' }
  | { cmd: 'set-paused'; paused: boolean }
  | { cmd: 'stop' };

export const IDLE: Player<never> = {
  state: 'idle',
  time: 0,
  duration: null,
  session: null,
  ended: null,
  meta: null,
  audio: null,
  subtitles: null,
  audioIndex: null,
  subtitleIndex: null,
};
