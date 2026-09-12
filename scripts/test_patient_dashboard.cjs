// Render only synthetic Django test responses, with repository-owned CSS.
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
        for (const sheet of await page.locator('link[rel="stylesheet"]').evaluateAll(nodes => nodes.map(n => n.getAttribute('href')))) {
          if (sheet.startsWith('/static/css/')) {
            await page.addStyleTag({ content: fs.readFileSync(path.join(process.cwd(), 'static/css', path.basename(sheet)), 'utf8') });
          }
        }
        await page.waitForTimeout(250);
        const layout = await page.evaluate(() => {
          const home = document.querySelector('.patient-home');
          const header = home.querySelector('.patient-home__heading');
          const style = selector => getComputedStyle(home.querySelector(selector));
          return {
            overflow: document.documentElement.scrollWidth > innerWidth + 1,
            duplicateStrip: !!document.querySelector('.patient-page-topbar'),
            headingSize: parseFloat(style('h1').fontSize),
            headingHeight: header.getBoundingClientRect().height,
            metricHeights: [...home.querySelectorAll('.metric-card')].map(n => n.getBoundingClientRect().height),
            subheadingSizes: [...home.querySelectorAll('h2')].map(n => parseFloat(getComputedStyle(n).fontSize)),
            buttons: [...home.querySelectorAll('.console-button')].map(n => ({
              height: n.getBoundingClientRect().height,
              weight: Number(getComputedStyle(n).fontWeight),
              color: getComputedStyle(n).color,
              primary: n.classList.contains('console-button--primary'),
            })),
          };
        });
        assert.equal(await page.locator('h1').count(), 1);
        assert.equal(layout.duplicateStrip, false);
        assert.equal(layout.overflow, false, `${entry.name} overflows at ${width}`);
        assert.ok(layout.headingSize <= 30);
        assert.ok(layout.subheadingSizes.every(size => size <= 16));
        assert.ok(layout.headingHeight < (width > 832 ? 125 : 190));
        assert.ok(layout.metricHeights.every(height => height < 155));
        assert.ok(layout.buttons.every(b => b.height >= (width > 832 ? 39 : 43) && b.weight <= 600));
        assert.ok(layout.buttons.filter(b => b.primary).every(b => b.color === 'rgb(255, 255, 255)'));
        if (process.env.MERIDIAN_LAYOUT_OUTPUT) {
          await page.screenshot({ path: path.join(process.env.MERIDIAN_LAYOUT_OUTPUT, `${entry.name}-${width}.png`), fullPage: true });
        }
        results.push({ page: entry.name, width, ...layout });
      }
    }
    console.log(JSON.stringify({ checked: results.length, results }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
