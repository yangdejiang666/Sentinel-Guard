const { defineConfig } = require('@playwright/test');
const { existsSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { randomUUID } = require('node:crypto');

const port = Number(process.env.SENTINEL_E2E_PORT || 8010);
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid SENTINEL_E2E_PORT');
const installedBrowser = '/home/ydjfafafa/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome';
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || (existsSync(installedBrowser) ? installedBrowser : undefined);
const database = join(tmpdir(), `sentinel-e2e-${randomUUID()}.sqlite3`);

module.exports = defineConfig({
  testDir: './tests/e2e',
  timeout: 30000,
  expect: { timeout: 10000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    browserName: 'chromium',
    launchOptions: { executablePath },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    viewport: { width: 1440, height: 1000 },
  },
  webServer: {
    command: `uv run uvicorn backend.app.main:app --host 127.0.0.1 --port ${port}`,
    url: `http://127.0.0.1:${port}/api/v1/health`,
    reuseExistingServer: false,
    timeout: 60000,
    env: {
      SIMULATION_MODE: 'true',
      SENTINEL_ENV: 'test',
      SENTINEL_SITE_ID: 'e2e-site',
      SENTINEL_DATABASE: database,
      SENTINEL_SIMULATION_INTERVAL: '1',
      SENTINEL_CONTROL_TOKEN: 'e2e-control-secret',
      SENTINEL_DEVICE_TOKEN: 'e2e-device-secret',
      SENTINEL_MQTT_HOST: '',
      SENTINEL_ALLOW_ACTUATION: 'false',
      SENTINEL_AUTO_ACTUATION: 'false',
    },
  },
});
