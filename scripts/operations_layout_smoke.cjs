// Optional local browser check. Receives only synthetic test-page HTML on stdin.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.MERIDIAN_PLAYWRIGHT_PATH || 'playwright');

(async () => {
  const pages = JSON.parse(fs.readFileSync(0, 'utf8'));
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const results = [];
  try {
    const page = await browser.newPage();
    await page.route('**/*', route => route.abort());
    for (const entry of pages) {
      for (const width of [1440, 390, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        await page.setContent(entry.html, { waitUntil: 'domcontentloaded' });
        const sheets = await page.locator('link[rel="stylesheet"]').evaluateAll(nodes => nodes.map(node => node.getAttribute('href')));
        for (const sheet of sheets) {
          if (sheet.startsWith('/static/css/')) {
            await page.addStyleTag({ content: fs.readFileSync(path.join(process.cwd(), 'static/css', path.basename(sheet)), 'utf8') });
          }
        }
        // Inserting stylesheet text triggers the console's short colour
        // transition. Inspect the settled paint, not the intermediate frame.
        await page.waitForTimeout(250);
        const metrics = await page.evaluate(() => ({
          width: innerWidth, contentWidth: document.documentElement.scrollWidth,
          headings: document.querySelectorAll('h1').length,
          primaryColors: [...document.querySelectorAll('.console-button--primary')].map(node => getComputedStyle(node).color),
          overflowing: [...document.querySelectorAll('body *')].filter(node => {
            const rect = node.getBoundingClientRect();
            return rect.width > 0 && (rect.right > innerWidth + 1 || rect.left < -1) &&
              !node.closest('.patient-navigation, .console-sidebar, .staff-mobile-nav');
          }).slice(0, 8).map(node => ({ tag: node.tagName, class: node.className })),
        }));
        results.push({ page: entry.name, ...metrics });
        assert.equal(metrics.headings, 1, `${entry.name}: one page heading`);
        assert.ok(metrics.contentWidth <= width + 1, `${entry.name} overflows at ${width}: ${JSON.stringify(metrics)}`);
        assert.ok(metrics.primaryColors.every(color => color === 'rgb(255, 255, 255)'), `${entry.name}: primary button contrast`);
        if (process.env.MERIDIAN_LAYOUT_OUTPUT) {
          await page.screenshot({ path: path.join(process.env.MERIDIAN_LAYOUT_OUTPUT, `${entry.name}-${width}.png`), fullPage: true });
        }
      }
    }
    console.log(JSON.stringify({ checked: results.length, results }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
