// Synthetic renders only; assets come from the locally collected static directory.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.MERIDIAN_PLAYWRIGHT_PATH || 'playwright');

(async () => {
  const pages = JSON.parse(fs.readFileSync(0, 'utf8'));
  const staticRoot = path.resolve('staticfiles');
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const errors = [], results = [];
  try {
    const page = await browser.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.hostname === 'meridian.test' && url.pathname.startsWith('/static/')) {
        const file = path.resolve(staticRoot, decodeURIComponent(url.pathname.slice('/static/'.length)));
        if (file.startsWith(staticRoot + path.sep) && fs.existsSync(file)) return route.fulfill({ path: file });
        errors.push(`Missing asset: ${url.pathname}`);
      }
      if (url.hostname === 'meridian.test' && !url.pathname.startsWith('/static/')) {
        const entry = pages.find(item => item.path === url.pathname);
        if (entry) return route.fulfill({ contentType: 'text/html', body: entry.html });
      }
      return route.abort();
    });
    for (const entry of pages) {
      for (const width of [1440, 390, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        // Empty and populated overview share a URL, so route this render directly.
        await page.route(`http://meridian.test${entry.path}`, route => route.fulfill({ contentType: 'text/html', body: entry.html }));
        await page.goto(`http://meridian.test${entry.path}`, { waitUntil: 'networkidle' });
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
        assert.equal(overflow, false, `${entry.name} has horizontal overflow at ${width}`);
        if (entry.name === 'overview') {
          assert.equal(await page.locator('#meridian-activity-chart svg').count(), 1);
          assert.ok(await page.locator('.md-chart-point').count() > 0);
          const toggle = page.locator('[data-chart-series="patients"]');
          await toggle.click();
          assert.equal(await toggle.getAttribute('aria-pressed'), 'false');
          await toggle.click();
          await page.locator('#meridian-directory-search').fill('definitely-not-a-section');
          assert.equal(await page.locator('#meridian-directory-empty').isVisible(), true);
          await page.locator('#meridian-directory-search').fill('patient');
          assert.ok(await page.locator('.md-directory-group:not([hidden]) li:not([hidden])').count() > 0);
          await page.locator('#meridian-directory-search').fill('');
          assert.equal(await page.locator('.practice-role-switcher button').count(), 3);
        }
        if (entry.name === 'login') {
          assert.equal(await page.getByLabel('Email address').count(), 1);
          // Jazzmin appends a screen-reader-only '(required)' to this label.
          assert.equal(await page.getByLabel(/^Password/).count(), 1);
        }
        if (process.env.MERIDIAN_LAYOUT_OUTPUT) {
          await page.screenshot({ path: path.join(process.env.MERIDIAN_LAYOUT_OUTPUT, `${entry.name}-${width}.png`), fullPage: true });
        }
        results.push(`${entry.name}:${width}`);
        await page.unroute(`http://meridian.test${entry.path}`);
      }
    }
    assert.deepEqual(errors, [], 'Browser errors or missing assets');
    console.log(JSON.stringify({ checked: results.length, pages: results, errors }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
