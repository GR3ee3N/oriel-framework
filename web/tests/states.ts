// Written by tests/test_contract.py from the states the add-on publishes;
// `npm run check` holds them to the page's types.

import type { BaseState } from '../src/bridge';
import type { VideoState } from '../src/video';

export const base = {
  "run": "run",
  "cpuRendering": false,
  "screensaver": null
} satisfies Omit<BaseState, 'connected'>;

export const video = [
  {
    "run": "run",
    "cpuRendering": false,
    "screensaver": 10,
    "mode": "browse",
    "player": {
      "state": "idle",
      "time": 0,
      "duration": null,
      "session": null,
      "meta": null,
      "ended": null,
      "audio": null,
      "subtitles": null,
      "audioIndex": null,
      "subtitleIndex": null
    },
    "decoders": [
      "h264",
      "hevc"
    ]
  },
  {
    "run": "run",
    "cpuRendering": false,
    "screensaver": 10,
    "mode": "video",
    "player": {
      "state": "loading",
      "time": 120,
      "duration": 5400,
      "session": "one",
      "meta": {
        "title": "Movie"
      },
      "ended": null,
      "audio": null,
      "subtitles": null,
      "audioIndex": null,
      "subtitleIndex": null
    },
    "decoders": [
      "h264",
      "hevc"
    ]
  },
  {
    "run": "run",
    "cpuRendering": false,
    "screensaver": 10,
    "mode": "video",
    "player": {
      "state": "loading",
      "time": 120,
      "duration": 5400,
      "session": "one",
      "meta": {
        "title": "Movie"
      },
      "ended": null,
      "audio": null,
      "subtitles": null,
      "audioIndex": null,
      "subtitleIndex": null
    },
    "decoders": [
      "h264",
      "hevc"
    ]
  },
  {
    "run": "run",
    "cpuRendering": false,
    "screensaver": 10,
    "mode": "video",
    "player": {
      "state": "playing",
      "time": 30,
      "duration": 5400,
      "session": "one",
      "meta": {
        "title": "Movie"
      },
      "ended": null,
      "audio": null,
      "subtitles": null,
      "audioIndex": null,
      "subtitleIndex": null
    },
    "decoders": [
      "h264",
      "hevc"
    ]
  },
  {
    "run": "run",
    "cpuRendering": false,
    "screensaver": 10,
    "mode": "video",
    "player": {
      "state": "playing",
      "time": 30,
      "duration": 5400,
      "session": "one",
      "meta": {
        "title": "Movie"
      },
      "ended": null,
      "audio": [
        {
          "index": 0,
          "language": "de",
          "name": "E-AC3 5.1",
          "codec": "eac3",
          "channels": 6
        }
      ],
      "subtitles": [
        {
          "index": 0,
          "language": "de",
          "name": "Forced",
          "forced": true,
          "impaired": false
        }
      ],
      "audioIndex": 0,
      "subtitleIndex": null
    },
    "decoders": [
      "h264",
      "hevc"
    ]
  },
  {
    "run": "run",
    "cpuRendering": false,
    "screensaver": 10,
    "mode": "browse",
    "player": {
      "state": "idle",
      "time": 30,
      "duration": 5400,
      "session": "one",
      "meta": {
        "title": "Movie"
      },
      "ended": "ended",
      "audio": null,
      "subtitles": null,
      "audioIndex": null,
      "subtitleIndex": null
    },
    "decoders": [
      "h264",
      "hevc"
    ]
  },
  {
    "run": "run",
    "cpuRendering": false,
    "screensaver": 10,
    "mode": "video",
    "player": {
      "state": "loading",
      "time": 600,
      "duration": null,
      "session": "two",
      "meta": null,
      "ended": null,
      "audio": null,
      "subtitles": null,
      "audioIndex": null,
      "subtitleIndex": null
    },
    "decoders": [
      "h264",
      "hevc"
    ]
  },
  {
    "run": "run",
    "cpuRendering": false,
    "screensaver": 10,
    "mode": "video",
    "player": {
      "state": "loading",
      "time": 600,
      "duration": null,
      "session": "two",
      "meta": null,
      "ended": null,
      "audio": null,
      "subtitles": null,
      "audioIndex": null,
      "subtitleIndex": null
    },
    "decoders": [
      "h264",
      "hevc"
    ]
  },
  {
    "run": "run",
    "cpuRendering": false,
    "screensaver": 10,
    "mode": "video",
    "player": {
      "state": "playing",
      "time": 630,
      "duration": null,
      "session": "two",
      "meta": null,
      "ended": null,
      "audio": null,
      "subtitles": null,
      "audioIndex": null,
      "subtitleIndex": null
    },
    "decoders": [
      "h264",
      "hevc"
    ]
  },
  {
    "run": "run",
    "cpuRendering": false,
    "screensaver": 10,
    "mode": "browse",
    "player": {
      "state": "idle",
      "time": 630,
      "duration": null,
      "session": "two",
      "meta": null,
      "ended": "stopped",
      "audio": null,
      "subtitles": null,
      "audioIndex": null,
      "subtitleIndex": null
    },
    "decoders": [
      "h264",
      "hevc"
    ]
  }
] satisfies Omit<VideoState<{ title: string }>, 'connected'>[];
