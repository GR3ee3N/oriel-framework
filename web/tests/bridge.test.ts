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
