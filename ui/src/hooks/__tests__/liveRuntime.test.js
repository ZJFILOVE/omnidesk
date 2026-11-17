// @vitest-environment jsdom
// Opt-in smoke test: OMNIDESK_LIVE_SOCKET=/run/user/<uid>/omnidesk/control.sock npm test -- liveRuntime
import { createConnection } from 'node:net';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { useLiveBooks, useLiveMedia, useLiveTextFeed } from '../useLiveDomain';

const socketPath = process.env.OMNIDESK_LIVE_SOCKET;

function request(op, params) {
  return new Promise((resolve, reject) => {
    const socket = createConnection(socketPath);
    let response = '';
    socket.setEncoding('utf8');
    socket.setTimeout(40000, () => socket.destroy(new Error('Live Core RPC timed out')));
    socket.once('error', reject);
    socket.once('connect', () => socket.write(`${JSON.stringify({ id: 'live-ui-smoke', op, params })}\n`));
    socket.on('data', (chunk) => {
      response += chunk;
      const newline = response.indexOf('\n');
      if (newline < 0) return;
      socket.destroy();
      try { resolve(JSON.parse(response.slice(0, newline))); } catch (error) { reject(error); }
    });
    socket.once('end', () => reject(new Error('Core closed before returning an NDJSON frame')));
  });
}

afterEach(() => { cleanup(); delete window.__TAURI__; });

it.skipIf(!socketPath)('loads real media, text and books through Core RPC into the UI hooks', async () => {
  // Adapt only the native transport. All discovery, source reads and UI mapping use live Core data.
  window.__TAURI__ = { core: { invoke: async (command, payload) => {
    if (command !== 'call') throw new Error(`Unexpected command: ${command}`);
    const args = payload?.args;
    return await request(args.op, args.params);
  } } };
  const { result } = renderHook(() => ({
    media: useLiveMedia('', '', 'plugin-ai-curator'),
    text: useLiveTextFeed('', '', 'plugin-ai-curator'),
    book: useLiveBooks('plugin-ai-curator'),
  }));
  await waitFor(() => {
    for (const feed of Object.values(result.current)) {
      expect(feed.error).toBeNull();
      expect(feed.live).toBe(true);
      expect(feed.items.length).toBeGreaterThan(0);
    }
  }, { timeout: 45000 });
  console.info('Live UI counts:', Object.fromEntries(Object.entries(result.current).map(([domain, feed]) => [domain, feed.items.length])));
}, 50000);
