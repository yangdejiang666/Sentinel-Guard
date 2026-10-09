const { test: base, expect } = require('@playwright/test');
const { randomUUID } = require('node:crypto');

const test = base.extend({
  browserErrors: [async ({ page }, use) => {
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
    await use(errors);
    expect(errors, 'No JavaScript, resource, or CSP errors').toEqual([]);
  }, { auto: true }],
});

async function openDashboard(page) {
  await page.goto('/');
  await expect(page.locator('#mode-banner')).toContainText('SIMULATION');
  await expect(page.locator('#value-temperature')).not.toHaveText('--');
}

async function configureToken(page) {
  await page.locator('[data-page="integrations"]').click();
  await page.locator('#control-token').fill('e2e-control-secret');
  await page.locator('#save-control-token').click();
  await expect(page.locator('#token-status')).toHaveText('当前标签页已配置控制令牌');
}

test('overview renders fresh simulated telemetry and independent-unit charts', async ({ page }) => {
  await openDashboard(page);
  await expect(page).toHaveTitle(/智卫 Sentinel/);
  await expect(page.locator('#mode-banner')).toContainText('不代表真实现场状态');
  await expect(page.locator('#quality-temperature')).toHaveText('模拟');
  await expect(page.locator('#quality-humidity')).toHaveText('模拟');
  await expect(page.locator('#quality-light')).toHaveText('模拟');
  await expect(page.locator('#connection-pill')).toContainText('实时已连接');
  await expect(page.locator('#current-state')).toHaveText(/NORMAL|WATCH|ALERT|DANGER/);
  await expect(page.locator('.live-chart-grid canvas')).toHaveCount(4);
  await expect.poll(() => page.locator('#live-temperature-chart').evaluate((canvas) => canvas.width)).toBeGreaterThan(100);
  await expect(page.locator('#current-vision-threshold')).toContainText('阈值');
  const overview = await (await page.request.get('/api/v1/overview')).json();
  expect(overview.latest_telemetry.every((item) => item.source_mode === 'simulated')).toBeTruthy();
});

test('all six platform pages and runtime configuration are available', async ({ page }) => {
  await openDashboard(page);
  const pages = { overview: '态势总览', monitor: '实时监控', events: '事件中心', devices: '设备与质量', history: '历史数据', integrations: '接入与接口' };
  for (const [id, title] of Object.entries(pages)) {
    await page.locator(`.nav-item[data-page="${id}"]`).click();
    await expect(page.locator(`#page-${id}`)).toBeVisible();
    await expect(page.locator('#page-title')).toHaveText(title);
  }
  await expect(page.locator('#config-grid')).toContainText('e2e-site');
  await expect(page.locator('#config-grid')).toContainText('安全锁关闭');
  await expect(page.locator('#config-grid')).toContainText('disabled-in-simulation');
  await expect(page.locator('#config-grid')).toContainText('未接入');
});

test('missing video is explicit and never presented as real camera footage', async ({ page }) => {
  await openDashboard(page);
  await page.locator('[data-page="monitor"]').click();
  await expect(page.locator('#monitor-mode-badge')).toContainText('视频未接入');
  await expect(page.locator('#monitor-feeds .camera-feed')).toHaveCount(1);
  await expect(page.locator('#monitor-feeds .camera-feed[data-view="k230"]')).toHaveCount(1);
  await expect(page.locator('.vision-panel .camera-feed')).toHaveCount(1);
  await expect(page.locator('#device-card-grid')).not.toContainText('MF937');
  await expect(page.locator('#detection-table')).toContainText('Orange Pi AI');
  await expect(page.locator('#monitor-feeds img.video-preview')).toHaveCount(0);
  await expect(page.locator('#page-monitor')).toContainText('K230 唯一视觉通道');
  const channels = await (await page.request.get('/api/v1/video/channels')).json();
  expect(channels.every((channel) => channel.status === 'missing')).toBeTruthy();
});

test('simulated bounding box is honestly labelled as metadata without video', async ({ page }) => {
  await openDashboard(page);
  await page.locator('[data-page="monitor"]').click();
  const response = await page.request.post('/api/v1/ingest/detections', {
    headers: { 'X-Device-Token': 'e2e-device-secret' },
    data: {
      event_id: `e2e-detection-${randomUUID()}`, site_id: 'e2e-site', device_id: 'k230-001',
      camera_id: 'k230-001', source_view: 'k230', target: 'person', confidence: 0.91,
      bbox: [0.1, 0.2, 0.3, 0.4], event_time: new Date().toISOString(), sequence: 1,
      boot_id: randomUUID(), quality: 'simulated', source_mode: 'simulated', model_version: 'e2e-metadata-model',
    },
  });
  expect(response.status()).toBe(202);
  await expect(page.locator('#monitor-k230-status')).toContainText('模拟元数据框');
  await expect(page.locator('#monitor-meta-k230')).toContainText('不代表画面检测');
  await expect(page.locator('#monitor-bbox-k230')).toHaveClass(/simulated-metadata/);
  await expect(page.locator('#monitor-feeds img.video-preview')).toHaveCount(0);
});

test('single-camera console assets are versioned and revalidated', async ({ page }) => {
  const response = await page.goto('/');
  expect(response.headers()['cache-control']).toBe('no-cache, must-revalidate');
  await expect(page.locator('#console-build')).toHaveText('单摄像头控制台 · 20261009');
  for (const selector of ['script[src]', 'link[rel="stylesheet"]']) {
    const path = await page.locator(selector).getAttribute(selector.startsWith('script') ? 'src' : 'href');
    expect(path).toContain('v=single-camera-20261009');
    const asset = await page.request.get(path);
    expect(asset.status()).toBe(200);
    expect(asset.headers()['cache-control']).toBe('no-cache, must-revalidate');
  }
});

test('legacy dynamic notices do not imply a second camera and retain inference identity', async ({ page }) => {
  await openDashboard(page);
  const result = await page.evaluate(() => {
    const legacy = '环境或双路视觉数据缺失/过期；不能视为完整现场结论';
    updateSystemNotice({ system_mode: 'DEGRADED', system_message: legacy });
    updateState({ ...state.overview, state_reason: legacy });
    handleRealtime({ type: 'event', data: {
      event_id: 'legacy-ui-regression', title: 'MAIN 视角检测到person',
      message: legacy, occurred_at: new Date().toISOString(),
      severity: 'critical', quality: 'simulated', source_mode: 'simulated',
      acknowledged: false,
    } });
    return state.events.find((event) => event.event_id === 'legacy-ui-regression');
  });
  // Rendering must not rewrite persisted/raw records or conflate inference with a camera.
  expect(result.title).toBe('MAIN 视角检测到person');
  expect(result.message).toContain('双路视觉');
  await expect(page.locator('#overview-events')).toContainText('Orange Pi AI 推理检测到person');
  await expect(page.locator('#toast-stack')).toContainText('Orange Pi AI 推理检测到person');
  await expect(page.locator('#system-message')).toContainText('视觉采集或 AI 推理');
  await expect(page.locator('#state-reason')).toContainText('视觉采集或 AI 推理');
  for (const id of ['overview', 'monitor', 'events', 'devices', 'history', 'integrations']) {
    await page.locator(`.nav-item[data-page="${id}"]`).click();
    const visibleText = await page.locator('body').innerText();
    expect(visibleText).not.toMatch(/MF937|前置|侦察|双路视觉|MAIN 视角|主控视角/);
  }
  await expect(page.locator('#monitor-feeds .camera-feed')).toHaveCount(1);
  await expect(page.locator('#detection-table')).toContainText('Orange Pi AI');
});

test('control token is tab-scoped and can be cleared without storing plaintext in the UI', async ({ page }) => {
  await openDashboard(page);
  await configureToken(page);
  await expect(page.locator('#control-token')).toHaveValue('');
  expect(await page.evaluate(() => sessionStorage.getItem('sentinel-control-token'))).toBe('e2e-control-secret');
  expect(await page.evaluate(() => localStorage.getItem('sentinel-control-token'))).toBeNull();
  await page.reload();
  await page.locator('[data-page="integrations"]').click();
  await expect(page.locator('#token-status')).toContainText('已配置');
  await page.locator('#clear-control-token').click();
  await expect(page.locator('#token-status')).toHaveText('当前未配置浏览器控制令牌');
  expect(await page.evaluate(() => sessionStorage.getItem('sentinel-control-token'))).toBeNull();
});

test('simulated command returns an auditable completed receipt without claiming hardware actuation', async ({ page }) => {
  await openDashboard(page);
  await configureToken(page);
  const responsePromise = page.waitForResponse((response) => response.url().endsWith('/api/v1/commands') && response.request().method() === 'POST');
  await page.locator('[data-command="fan_on"]').click();
  const response = await responsePromise;
  expect(response.status()).toBe(201);
  const command = await response.json();
  expect(command.status).toBe('completed');
  expect(command.source_mode).toBe('simulated');
  expect(command.execution_message).toContain('未驱动物理硬件');
  await expect(page.locator('#last-command')).toContainText('未驱动物理硬件');
  await page.locator('[data-page="events"]').click();
  await expect(page.locator('#command-table')).toContainText(command.command_id);
});

test('event acknowledgement remains synchronized through refresh and audit views', async ({ page }) => {
  await openDashboard(page);
  await configureToken(page);
  await page.request.post('/api/v1/commands', { headers: { 'X-Control-Token': 'e2e-control-secret' }, data: {
    command_type: 'alarm_on', reason: 'E2E acknowledgement test', requested_by: 'e2e', request_id: randomUUID(),
  } });
  await page.locator('[data-page="events"]').click();
  await page.locator('#refresh-events').click();
  const button = page.locator('#all-events [data-ack-event]').first();
  await expect(button).toBeVisible();
  const eventId = await button.getAttribute('data-ack-event');
  const acknowledgement = page.waitForResponse((response) => response.url().includes(`/events/${eventId}/acknowledge`));
  await button.click();
  expect((await acknowledgement).status()).toBe(200);
  await expect(page.locator(`#all-events [data-ack-event="${eventId}"]`)).toHaveCount(0);
  const events = await (await page.request.get('/api/v1/events?limit=100')).json();
  expect(events.find((event) => event.event_id === eventId).acknowledged).toBeTruthy();
});

test('history can query raw values, units, source mode, device, and timestamps', async ({ page }) => {
  await openDashboard(page);
  await page.locator('[data-page="history"]').click();
  await expect(page.locator('#history-table tr')).not.toHaveCount(0);
  await expect(page.locator('#history-table')).toContainText('°C');
  await page.locator('#history-metric').selectOption('humidity');
  await page.locator('#history-device').selectOption('esp32-s3-001');
  await page.locator('#history-source').selectOption('simulated');
  await page.locator('#refresh-history').click();
  await expect(page.locator('#history-title')).toHaveText('湿度历史 / %');
  await expect(page.locator('#history-table')).toContainText('模拟');
  await expect(page.locator('#history-table')).toContainText('接收');
  await page.locator('#history-source').selectOption('live');
  await expect(page.locator('#history-table')).toContainText('暂无历史数据');
});

test('server resync signal and sequence gaps trigger authoritative recovery', async ({ page }) => {
  await openDashboard(page);
  const resync = page.waitForResponse((response) => response.url().includes('/api/v1/events?limit=100'));
  await page.evaluate(() => handleRealtime({ type: 'resync_required' }));
  expect((await resync).status()).toBe(200);
  await expect.poll(() => page.evaluate(() => state.needsResync)).toBeFalsy();
  const gapResync = page.waitForResponse((response) => response.url().includes('/api/v1/state/transitions?limit=100'));
  await page.evaluate(() => handleRealtime({ type: 'health', data: state.overview.devices, stream_id: state.streamId, stream_sequence: state.streamSequence + 10 }));
  expect((await gapResync).status()).toBe(200);
  await expect.poll(() => page.evaluate(() => state.needsResync)).toBeFalsy();
});

test('disconnected data ages to stale instead of silently remaining fresh', async ({ page }) => {
  await openDashboard(page);
  await page.evaluate(() => {
    state.socket.onclose = null;
    state.socket.close();
    const oldTime = new Date(Date.now() + state.serverOffsetMs - 20000).toISOString();
    state.overview.latest_telemetry.forEach((record) => { record.event_time = oldTime; });
    updateSensors(state.overview);
  });
  await expect(page.locator('#quality-temperature')).toHaveText('过期');
  await expect(page.locator('#value-temperature')).not.toHaveText('--');
});

test('phone layout has no horizontal main-page overflow on any page', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openDashboard(page);
  for (const id of ['overview', 'monitor', 'events', 'devices', 'history', 'integrations']) {
    await page.locator('#mobile-menu').click();
    await page.locator(`.nav-item[data-page="${id}"]`).click();
    await expect(page.locator(`#page-${id}`)).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow, `page ${id}`).toBeLessThanOrEqual(1);
  }
});
