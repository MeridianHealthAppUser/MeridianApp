// Optional browser layout test. Only synthetic test-page HTML is supplied.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.MERIDIAN_PLAYWRIGHT_PATH || 'playwright');

(async () => {
  const entries = JSON.parse(fs.readFileSync(0, 'utf8'));
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  let checked = 0;
  try {
    const page = await browser.newPage();
    await page.route('**/*', route => route.abort());
    for (const entry of entries) {
      for (const width of [1440, 390, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        await page.setContent(entry.html, { waitUntil: 'domcontentloaded' });
        for (const sheet of await page.locator('link[rel="stylesheet"]').evaluateAll(nodes => nodes.map(node => node.getAttribute('href')))) {
          if (sheet.startsWith('/static/css/')) await page.addStyleTag({ content: fs.readFileSync(path.join(process.cwd(), 'static/css', path.basename(sheet)), 'utf8') });
        }
        // A long neighbouring conversation must never stretch the weight card.
        await page.addStyleTag({ content: '#messages { min-height: 1600px; }' });
        const metrics = await page.locator('#weights').evaluate(card => {
          const rect = card.getBoundingClientRect();
          return {
            height: rect.height, right: rect.right, left: rect.left,
            align: getComputedStyle(card).alignSelf,
            chartCount: card.querySelectorAll('svg').length,
            rows: card.querySelectorAll('tbody tr').length,
            overflow: [...card.querySelectorAll('*')].some(node => {
              const box = node.getBoundingClientRect();
              return box.width > 0 && (box.right > rect.right + 1 || box.left < rect.left - 1);
            }),
          };
        });
        assert.equal(metrics.align, 'start');
        assert.ok(metrics.height < (entry.name === 'empty' ? 250 : 1100), `${entry.name} unexpectedly tall: ${metrics.height}px`);
        assert.ok(metrics.right <= width + 1 && metrics.left >= 0, `card overflows at ${width}px`);
        assert.equal(metrics.overflow, false, `weight content overflows at ${width}px`);
        assert.equal(metrics.chartCount, entry.name === 'empty' ? 0 : 1);
        assert.equal(metrics.rows, entry.name === 'empty' ? 0 : 4);
        if (process.env.MERIDIAN_LAYOUT_OUTPUT) {
          await page.locator('#weights').screenshot({
            path: path.join(process.env.MERIDIAN_LAYOUT_OUTPUT, `record-weight-${entry.name}-${width}.png`),
            style: '.site-header, .staff-mobile-nav { visibility: hidden !important; }',
          });
        }
        if (entry.name === 'populated') {
          await page.locator('.record-weight-note summary').first().click();
          assert.ok(await page.locator('.record-weight-note').first().evaluate(node => node.open));
          assert.ok((await page.locator('.record-weight-note p').first().innerText()).includes('x'.repeat(160)));
        }
        checked++;
      }
    }
    console.log(JSON.stringify({ checked }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
