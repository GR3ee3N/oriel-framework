import { afterEach, expect, test, vi } from 'vitest';
import { CommandFailure, createBridge } from '../src/bridge';

afterEach(() => vi.unstubAllGlobals());

test('a cut-off reply is a failure, not a success', async () => {
  vi.stubGlobal('fetch', async () => new Response('{"result": 4', { status: 200 }));
  const failure = await createBridge({}).call({ cmd: 'ready' }).catch((e: unknown) => e);
  expect(failure).toBeInstanceOf(CommandFailure);
  expect((failure as CommandFailure).code).toBe('unreachable');
});

test('an error without a readable body keeps the HTTP status', async () => {
  vi.stubGlobal('fetch', async () => new Response('', { status: 500 }));
  await expect(createBridge({}).call({ cmd: 'ready' })).rejects.toMatchObject({ message: 'ready: HTTP 500', code: 'failed' });
});

test('commands keep their order and a failed request does not block the next one', async () => {
  let rejectFirst: (reason: Error) => void;
  const fetch = vi.fn()
    .mockImplementationOnce(() => new Promise<Response>((_, reject) => { rejectFirst = reject; }))
    .mockResolvedValueOnce(new Response('{"result": 42}', { status: 200 }));
  vi.stubGlobal('fetch', fetch);
  const bridge = createBridge({});
  const first = bridge.call({ cmd: 'ready' }).catch((e: unknown) => e);
  const second = bridge.call({ cmd: 'exit' });
  await Promise.resolve();
  expect(fetch).toHaveBeenCalledTimes(1);
  rejectFirst!(new Error('connection lost'));
  expect(await first).toMatchObject({ code: 'unreachable' });
  expect(await second).toBe(42);
  expect(fetch.mock.calls.map(([, options]) => JSON.parse(options.body).cmd)).toEqual(['ready', 'exit']);
});

test('each state from the add-on reaches listeners as a new object; a lost stream clears connected', () => {
  let stream!: { listeners: Record<string, (e: { data: string }) => void>; onerror: () => void };
  vi.stubGlobal(
    'EventSource',
    class {
      listeners: Record<string, (e: { data: string }) => void> = {};
      onerror = () => {};
      constructor() {
        stream = this;
      }
      addEventListener(kind: string, listener: (e: { data: string }) => void) {
        this.listeners[kind] = listener;
      }
    },
  );
  const bridge = createBridge({});
  const seen: unknown[] = [];
  const stop = bridge.onState((s) => seen.push(s));
  const before = bridge.state();
  bridge.connect();
  stream.listeners.state({ data: '{"cpuRendering": true, "screensaver": null}' });
  expect(bridge.state()).toEqual({ cpuRendering: true, screensaver: null, connected: true });
  expect(bridge.state()).not.toBe(before);
  stream.onerror();
  expect(bridge.state().connected).toBe(false);
  stop();
  stream.listeners.state({ data: '{"cpuRendering": false, "screensaver": null}' });
  expect(seen).toHaveLength(2);
});
