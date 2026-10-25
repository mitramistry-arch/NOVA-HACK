import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

function localApiTarget(): URL {
  const raw = process.env.NOVA_DEV_API_TARGET ?? 'http://127.0.0.1:8000';
  let target: URL;
  try {
    target = new URL(raw);
  } catch {
    throw new Error('NOVA_DEV_API_TARGET must be a valid local URL.');
  }
  const loopbackHosts = new Set(['127.0.0.1', 'localhost', '[::1]', '::1']);
  if (!['http:', 'https:'].includes(target.protocol) || !loopbackHosts.has(target.hostname)) {
    throw new Error(
      'The development proxy only permits a loopback API target. Use a trusted server-side proxy for hosted deployments.',
    );
  }
  if (target.username || target.password) {
    throw new Error('Do not put credentials in NOVA_DEV_API_TARGET.');
  }
  if (target.pathname !== '/' || target.search || target.hash) {
    throw new Error('NOVA_DEV_API_TARGET must contain only the local API origin, without path, query, or fragment.');
  }
  return target;
}

const target = localApiTarget();

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    strictPort: true,
    proxy: {
      '/api': {
        target: target.origin,
        changeOrigin: false,
        rewrite: (path) => path.replace(/^\/api(?=\/|$)/, '') || '/',
        configure: (proxy) => {
          // This key is read only by the Node-side dev server and is never defined in client code.
          // Local development only; hosted deployments need authenticated same-origin BFF wiring.
          const apiKey = process.env.NOVA_API_KEY;
          if (apiKey) {
            proxy.on('proxyReq', (proxyReq) => {
              proxyReq.setHeader('X-API-Key', apiKey);
            });
          }
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
    restoreMocks: true,
    clearMocks: true,
  },
  build: {
    sourcemap: false,
    emptyOutDir: true,
  },
});
