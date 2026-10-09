const { chromium, expect } = require('@playwright/test');
const { existsSync, mkdirSync } = require('node:fs');
const { join } = require('node:path');

// Development-only screenshots. This script reads the running preview; it
// never creates commands or modifies the application database.
async function main() {
  const config = require('../playwright.config.js');
  const executablePath = config.use.launchOptions.executablePath;
  if (executablePath && !existsSync(executablePath)) throw new Error('Configured Chromium is unavailable');
  const output = join(__dirname, '..', 'artifacts');
  mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath });
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
    await page.goto(process.env.SENTINEL_PREVIEW_URL || 'http://127.0.0.1:8000');
    await expect(page.locator('#value-temperature')).not.toHaveText('--');
    await expect(page.locator('#connection-pill')).toContainText('实时已连接');
    await page.screenshot({ path: join(output, 'preview-desktop.png'), fullPage: true });
    await page.locator('[data-page="monitor"]').click();
    await expect(page.locator('#monitor-feeds .camera-feed')).toHaveCount(1);
    await expect(page.locator('#console-build')).toContainText('20261009');
    await page.screenshot({ path: join(output, 'preview-desktop-monitor.png'), fullPage: true });
    await page.locator('[data-page="overview"]').click();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('.sidebar').evaluate((element) => element.classList.remove('open'));
    await page.waitForTimeout(350);
    await page.screenshot({ path: join(output, 'preview-mobile.png'), fullPage: true });
    await page.locator('#mobile-menu').click();
    await page.locator('[data-page="monitor"]').click();
    await page.locator('.sidebar').evaluate((element) => element.classList.remove('open'));
    await page.waitForTimeout(350);
    await expect(page.locator('#monitor-mode-badge')).toContainText('视频未接入');
    await page.screenshot({ path: join(output, 'preview-mobile-monitor.png'), fullPage: true });
    expect(errors, 'No page, resource, or CSP errors').toEqual([]);
    console.log(JSON.stringify({ output, errors, captures: ['preview-desktop.png', 'preview-desktop-monitor.png', 'preview-mobile.png', 'preview-mobile-monitor.png'] }));
  } finally {
    await browser.close();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
