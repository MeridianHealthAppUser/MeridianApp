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
      for (const theme of ['light', 'dark']) {
      for (const width of [1440, 390, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        await page.setContent(entry.html, { waitUntil: 'domcontentloaded' });
        await page.evaluate(theme => document.documentElement.setAttribute('data-theme', theme), theme);
        for (const sheet of await page.locator('link[rel="stylesheet"]').evaluateAll(nodes => nodes.map(n => n.getAttribute('href')))) {
          if (sheet.startsWith('/static/css/')) {
            const filename = path.basename(new URL(sheet, 'http://meridian.test').pathname);
            await page.addStyleTag({ content: fs.readFileSync(path.join(process.cwd(), 'static/css', filename), 'utf8') });
          }
        }
        await page.waitForTimeout(250);
        const layout = await page.evaluate(() => {
          const home = document.querySelector('.patient-home');
          const header = home.querySelector('.patient-home__heading');
          const style = selector => getComputedStyle(home.querySelector(selector));
          const status = home.querySelector('.patient-home__status').getBoundingClientRect();
          const chart = home.querySelector('.patient-home__chart-card').getBoundingClientRect();
          return {
            overflow: document.documentElement.scrollWidth > innerWidth + 1,
            duplicateStrip: !!document.querySelector('.patient-page-topbar'),
            headingSize: parseFloat(style('h1').fontSize),
            headingHeight: header.getBoundingClientRect().height,
            displaySize: parseFloat(style('.patient-home__display').fontSize),
            subheadingSizes: [...home.querySelectorAll('h2:not(.patient-home__display)')].map(n => parseFloat(getComputedStyle(n).fontSize)),
            hasStatus: !!home.querySelector('.patient-home__status .patient-kv'),
            hasWeightLog: !!home.querySelector('#weight-log form'),
            cardTopDifference: Math.abs(status.top - chart.top),
            cardHeightDifference: Math.abs(status.height - chart.height),
            chartReference: !!home.querySelector('.patient-weight-chart__reference'),
            textColor: style('.patient-home__lead').color,
            cardColor: style('.patient-home__chart-card').backgroundColor,
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
        // Home: a welcome with three actions and status pills, then the weight headline.
        assert.ok(layout.headingSize <= 40);
        assert.ok(layout.displaySize <= 32);
        assert.ok(layout.subheadingSizes.every(size => size <= 16));
        assert.ok(layout.headingHeight < (width > 832 ? 200 : 360));
        assert.ok(layout.hasStatus && layout.hasWeightLog);
        assert.ok(layout.buttons.every(b => b.height >= (width > 832 ? 39 : 43) && b.weight <= 600));
        assert.ok(layout.buttons.filter(b => b.primary).every(b => b.color === (theme === 'light' ? 'rgb(255, 255, 255)' : 'rgb(29, 74, 94)')));
        if (width >= 960) {
          assert.ok(layout.cardTopDifference <= 1, 'Home cards should align at the top');
          assert.ok(layout.cardHeightDifference <= 1, 'Home cards should have equal height');
        }
        if (entry.name === 'patient-home-populated') assert.ok(layout.chartReference);
        if (process.env.MERIDIAN_LAYOUT_OUTPUT) {
          await page.screenshot({ path: path.join(process.env.MERIDIAN_LAYOUT_OUTPUT, `${entry.name}-${theme}-${width}.png`), fullPage: true });
        }
        results.push({ page: entry.name, theme, width, ...layout });
      }
      }
    }
    console.log(JSON.stringify({ checked: results.length, results }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
