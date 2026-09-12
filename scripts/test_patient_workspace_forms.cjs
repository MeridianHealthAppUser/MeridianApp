// Checks actual rendered templates using synthetic Django test records only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.MERIDIAN_PLAYWRIGHT_PATH || 'playwright');
(async () => {
  const entries = JSON.parse(fs.readFileSync(0, 'utf8'));
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  let layouts = 0;
  try {
    const page = await browser.newPage();
    await page.route('**/*', route => route.abort());
    for (const entry of entries) {
      for (const width of [1440, 390, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        await page.setContent(entry.html, { waitUntil: 'domcontentloaded' });
        const sheets = await page.locator('link[rel="stylesheet"]').evaluateAll(nodes => nodes.map(node => node.getAttribute('href')));
        for (const sheet of sheets) {
          if (sheet.startsWith('/static/css/')) await page.addStyleTag({ content: fs.readFileSync(path.join(process.cwd(), 'static/css', path.basename(sheet)), 'utf8') });
        }
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width, `Overflow: ${entry.name} at ${width}`);
        assert.equal(await page.locator('h1').count(), 1);
        assert.equal(await page.locator('h1').innerText(), 'Example Patient');
        assert.equal(await page.locator('.patient-workspace-tabs').count(), 1);
        assert.ok((await page.locator('.patient-workspace-tabs [aria-current]').getAttribute('href')).endsWith('?tab=' + entry.tab));
        const ids = await page.locator('[id]').evaluateAll(nodes => nodes.map(node => node.id));
        assert.equal(new Set(ids).size, ids.length, `Duplicate IDs: ${entry.name}`);
        const missingLabels = await page.locator('.patient-workspace-content input:not([type="hidden"]), .patient-workspace-content select, .patient-workspace-content textarea').evaluateAll(nodes => nodes.filter(node => !node.labels.length && !node.getAttribute('aria-label')).map(node => node.name));
        assert.deepEqual(missingLabels, []);
        if (process.env.MERIDIAN_LAYOUT_OUTPUT) await page.screenshot({ path: path.join(process.env.MERIDIAN_LAYOUT_OUTPUT, `workspace-${entry.name}-${width}.png`), fullPage: true, animations: 'disabled' });
        layouts++;
      }
    }
    console.log(JSON.stringify({ layouts }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
