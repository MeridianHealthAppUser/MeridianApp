// Uses a Django-rendered synthetic record only; no application or patient DB writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.MERIDIAN_PLAYWRIGHT_PATH || 'playwright');

(async () => {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  let passed = 0;
  try {
    const scenario = async (width, mode = 'success', noObserver = false) => {
      const context = await browser.newContext({ viewport: { width, height: 1000 } });
      const page = await context.newPage();
      let requests = 0;
      let answer;
      const failures = [];
      page.on('pageerror', error => failures.push(error.message));
      await page.addInitScript(({ noObserver }) => {
        window.__observerRoot = null;
        if (noObserver) { delete window.IntersectionObserver; return; }
        const NativeObserver = window.IntersectionObserver;
        window.IntersectionObserver = class extends NativeObserver {
          constructor(callback, options) { super(callback, options); if (options?.root) window.__observerRoot = options.root.id; }
        };
      }, { noObserver });
      await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.pathname === '/example-record/') {
          const html = input.html.replace(/data-next-url="[^"]*"/, 'data-next-url="/history/older/?scope=current&amp;category=all&amp;cursor=example-signed-cursor"');
          return route.fulfill({ contentType: 'text/html', body: html });
        }
        if (url.pathname.startsWith('/static/')) {
          const file = path.join(process.cwd(), 'static', url.pathname.slice('/static/'.length));
          if (fs.existsSync(file)) return route.fulfill({
            contentType: file.endsWith('.css') ? 'text/css' : 'application/javascript', body: fs.readFileSync(file),
          });
        }
        if (url.pathname === '/history/older/') {
          requests++;
          assert.equal(url.searchParams.get('scope'), 'current');
          assert.equal(url.searchParams.get('cursor'), 'example-signed-cursor');
          if (mode === '401') return route.fulfill({ status: 401, contentType: 'application/json', body: '{}' });
          if (mode === 'redirect') return route.fulfill({ status: 302, headers: { location: '/login/' } });
          if (mode === 'html') return route.fulfill({ contentType: 'text/html', body: '<h1>Sign in</h1>' });
          if (mode === 'retry' && requests === 1) return route.fulfill({ status: 503, contentType: 'application/json', body: '{}' });
          if (mode === 'held') await new Promise(resolve => { answer = resolve; });
          const oldRow = await page.locator('[data-entry-key]').first().evaluate(row => row.outerHTML);
          return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
            html: mode === 'empty' ? '' : oldRow + input.older,
            next_url: mode === 'empty' ? '/history/next/' : (mode === 'unsafe' ? 'https://other.invalid/history/' : null),
            count: 45,
          }) });
        }
        return route.abort();
      });
      await page.goto('http://meridian.test/example-record/');
      await page.locator('[data-timeline-load]').waitFor({ state: 'visible' });
      const originalCount = await page.locator('[data-entry-key]').count();
      assert.equal(originalCount, 20);
      const dimensions = await page.evaluate(() => {
        const card = document.querySelector('.record-timeline-card').getBoundingClientRect();
        const side = document.querySelector('.record-sidebar').getBoundingClientRect();
        const scroll = document.querySelector('[data-timeline-scroll]');
        return {
          bottomDifference: Math.abs(card.bottom - side.bottom), topDifference: Math.abs(card.top - side.top),
          height: card.height, width: document.documentElement.scrollWidth,
          root: window.__observerRoot, scrollHeight: scroll.scrollHeight, clientHeight: scroll.clientHeight,
        };
      });
      assert.ok(dimensions.width <= width + 1, `Page overflow at ${width}: ${JSON.stringify(dimensions)}`);
      if (width > 1100) assert.ok(dimensions.bottomDifference <= 2 && dimensions.topDifference <= 2, `Panel alignment: ${JSON.stringify(dimensions)}`);
      else assert.ok(dimensions.height >= 416 && dimensions.height <= 760, `Mobile bounded height: ${dimensions.height}`);
      assert.ok(dimensions.scrollHeight > dimensions.clientHeight, 'History must scroll internally');
      if (!noObserver) assert.equal(dimensions.root, 'record-timeline-scroll');
      if (width > 1100) {
        const resize = await page.evaluate(() => {
          const side = document.querySelector('.record-sidebar');
          const extra = document.createElement('div');
          extra.style.height = '100px';
          side.append(extra);
          const difference = Math.abs(side.getBoundingClientRect().bottom - document.querySelector('.record-timeline-card').getBoundingClientRect().bottom);
          extra.remove();
          return difference;
        });
        assert.ok(resize <= 2, 'The timeline must follow changes in sidebar height');
      }
      const ids = await page.locator('[id]').evaluateAll(nodes => nodes.map(node => node.id));
      assert.equal(new Set(ids).size, ids.length, 'Duplicate element IDs');
      if (mode === 'success' && process.env.MERIDIAN_LAYOUT_OUTPUT) {
        await page.screenshot({ path: path.join(process.env.MERIDIAN_LAYOUT_OUTPUT, `record-timeline-${width}.png`), fullPage: true });
      }
      if (noObserver) await page.locator('[data-timeline-load]').click();
      else await page.locator('[data-timeline-scroll]').evaluate(node => { node.scrollTop = node.scrollHeight; });
      if (mode === 'held') {
        await page.waitForFunction(() => document.querySelector('[data-timeline-scroll]').getAttribute('aria-busy') === 'true');
        await page.locator('[data-timeline-load]').evaluate(button => { button.click(); button.click(); });
        assert.equal(requests, 1, 'Only one request may run at once');
        answer();
      }
      if (['401', 'redirect', 'html'].includes(mode)) {
        await page.locator('[data-timeline-reload]').waitFor({ state: 'visible' });
        assert.equal(await page.locator('[data-timeline-load]').isVisible(), false);
        assert.equal(await page.locator('[data-entry-key]').count(), originalCount);
        assert.equal(requests, 1);
      } else if (mode === 'retry' || mode === 'unsafe') {
        await page.getByRole('button', { name: 'Try again' }).waitFor();
        assert.equal(await page.locator('[data-entry-key]').count(), originalCount);
        await page.waitForTimeout(200);
        assert.equal(requests, 1, 'Errors must not cause automatic retry loops');
        if (mode === 'retry') {
          await page.getByRole('button', { name: 'Try again' }).click();
          await page.waitForFunction(count => document.querySelectorAll('[data-entry-key]').length === count, originalCount + 20);
          assert.equal(requests, 2);
        }
      } else if (mode === 'empty') {
        await page.waitForFunction(() => document.querySelector('[data-timeline-status]').textContent.includes('No additional visible entries'));
        await page.waitForTimeout(200);
        assert.equal(requests, 1, 'An empty batch must not auto-load repeatedly');
        assert.equal(await page.locator('[data-entry-key]').count(), originalCount);
      } else {
        await page.waitForFunction(count => document.querySelectorAll('[data-entry-key]').length === count, originalCount + 20);
        assert.equal(requests, 1);
        assert.equal(await page.locator('[data-timeline-status]').innerText(), 'End of matching history.');
      }
      const keys = await page.locator('[data-entry-key]').evaluateAll(nodes => nodes.map(node => node.dataset.entryKey));
      assert.equal(new Set(keys).size, keys.length, 'Duplicate entries appended');
      const finalHeight = await page.locator('.record-timeline-card').evaluate(node => node.getBoundingClientRect().height);
      assert.ok(Math.abs(finalHeight - dimensions.height) <= 1, 'Appending history must not grow the page');
      if (noObserver) assert.equal(await page.evaluate(() => document.activeElement.dataset.entryKey), 'event:10000', 'Manual loading should focus the first new entry');
      assert.deepEqual(failures, []);
      await context.close();
      passed++;
    };
    for (const width of [1440, 390, 320]) await scenario(width);
    for (const mode of ['401', 'redirect', 'html', 'retry', 'held', 'unsafe', 'empty']) await scenario(390, mode);
    await scenario(320, 'success', true);
    console.log(JSON.stringify({ passed }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
