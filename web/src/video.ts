// State and commands of a video app; see addon/lib/oriel/video.py.

import type { BaseState } from './bridge';

/** 'osd' is the page over Kodi's video; 'video' hides the page. */
export type Mode = 'browse' | 'osd' | 'video';

/** Kodi's tracks of the playing file, by Kodi's index. */
export interface AudioTrack {
  index: number;
  language: string;
  name: string;
  /** ffmpeg's name. */
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

/** Every field is always there; null is unknown or none. */
export interface Player<Meta = unknown> {
  state: 'idle' | 'loading' | 'playing' | 'paused';
  /** Seconds, in the item's timeline. */
  time: number;
  /** null while neither the page nor Kodi knows it. */
  duration: number | null;
  session: string | null;
  /** null until playback ends. */
  ended: 'ended' | 'stopped' | 'error' | null;
  /** Given back to a page reloaded during playback. */
  meta: Meta | null;
  /** Kodi's track lists: null until the session's file has started, [] for
   * a file without tracks of the kind. */
  audio: AudioTrack[] | null;
  subtitles: SubtitleTrack[] | null;
  audioIndex: number | null;
  /** null while subtitles are off. */
  subtitleIndex: number | null;
}

export interface VideoState<Meta = unknown> extends BaseState {
  mode: Mode;
  player: Player<Meta>;
  /** Video codecs (ffmpeg's names) the box decodes in hardware; null if it
   * doesn't say. */
  decoders: string[] | null;
}

/** Commands set a state; one that already holds succeeds without doing
 * anything. A success means it's done, except for play, stop, seek-to, audio
 * and subtitle: Kodi was asked, and the state reports what came of it. An
 * optional field may be left out or null. */
export type VideoCommand<Meta = unknown> =
  /** Where relative paths play from, and the headers (tokens, say) the
   * add-on's proxy adds to their requests. */
  | { cmd: 'media-source'; base: string; headers?: Record<string, string> | null }
  | {
      cmd: 'play';
      /** A path on the media source, or an absolute URL. */
      path: string;
      title?: string | null;
      session?: string | null;
      /** Where playback starts, in the item's timeline. */
      position?: number | null;
      /** Where the stream begins in the item's timeline: 0 for the file,
       * the start of a transcode that begins into it. */
      streamOffset?: number | null;
      /** The item's duration, when Kodi can't tell (a transcode). */
      duration?: number | null;
      /** Kodi's index; applied once the file starts. */
      audio?: number | null;
      /** Kodi's index; -1 for none. */
      subtitle?: number | null;
      subtitleFile?: string | null;
      meta?: Meta | null;
    }
  /** In the item's timeline; refused outside the stream. */
  | { cmd: 'seek-to'; seconds: number }
  | { cmd: 'audio'; index: number }
  | { cmd: 'subtitle'; index?: number | null; file?: string | null }
  /** Show the page during playback, e.g. for a prompt. */
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
