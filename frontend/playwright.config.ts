import { defineConfig, devices } from '@playwright/test';

const backendEnv = {
  NOVA_ENV: 'test',
  NOVA_DB_PATH: '/tmp/nova-frontend-e2e.sqlite',
  NOVA_CONTROL_DB_PATH: '/tmp/nova-frontend-e2e-control.sqlite',
  NOVA_AUDIT_RETENTION_DAYS: '0',
};

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: 'http://127.0.0.1:5173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    ...devices['Desktop Chrome'],
  },
  webServer: [
    {
      command: 'python -m uvicorn nova.api.main:app --host 127.0.0.1 --port 8123',
      cwd: '..',
      url: 'http://127.0.0.1:8123/healthz',
      reuseExistingServer: false,
      timeout: 45_000,
      env: backendEnv,
    },
    {
      command: 'npm run dev -- --host 127.0.0.1',
      url: 'http://127.0.0.1:5173',
      reuseExistingServer: !process.env.CI,
      timeout: 45_000,
      env: { NOVA_DEV_API_TARGET: 'http://127.0.0.1:8123' },
    },
  ],
});
