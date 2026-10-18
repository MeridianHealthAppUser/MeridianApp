/* Django-rendered synthetic appointment table, local assets, mocked status replies. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.MERIDIAN_PLAYWRIGHT_PATH || 'playwright');
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
const opens = Date.parse(fixture.opensAt);

(async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  let checked = 0;
  try {
    async function scenario(width) {
      const context = await browser.newContext({ viewport: { width, height: 1000 } });
      const page = await context.newPage();
      let now = opens - 1000;
      let mode = 'future';
      let statusRequests = 0;
      let navigations = 0;
      let release = null;
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.clock.install({ time: new Date(now) });
      await page.route('https://appointments.test/**', async route => {
        const url = new URL(route.request().url());
        if (url.pathname === '/patient/appointments/') {
          navigations++;
          return route.fulfill({ contentType: 'text/html', body: fixture.html });
        }
        if (url.pathname.startsWith('/static/')) {
          const file = path.join(process.cwd(), url.pathname.slice(1));
          if (file.endsWith('.js') && !file.endsWith('appointment_video.js')) return route.fulfill({ contentType: 'application/javascript', body: '' });
          if (!fs.existsSync(file)) return route.abort();
          return route.fulfill({ contentType: file.endsWith('.css') ? 'text/css' : file.endsWith('.js') ? 'application/javascript' : 'font/woff2', body: fs.readFileSync(file) });
        }
        if (url.pathname === '/patient/appointments/video-status/') {
          statusRequests++;
          const sampledAt = now;
          assert.deepEqual(url.searchParams.getAll('id'), [String(fixture.id)]);
          assert.equal(route.request().headers()['x-meridian-scope'], fixture.scope);
          if (mode === 'failed') return route.abort();
          if (mode === 'held') await new Promise(resolve => { release = resolve; });
          const row = { id: fixture.id, opensAt: new Date(mode === 'moved' ? opens + 60000 : opens).toISOString(),
            closesAt: new Date(opens + 4000).toISOString(), message: mode === 'future' || mode === 'moved' ? 'The video room opens shortly before your appointment.' : '',
            joinUrl: ['open', 'held'].includes(mode) ? `/video/appointments/${fixture.id}/` : null };
          if (mode === 'cancelled') { row.opensAt = null; row.closesAt = null; }
          return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
            scope: mode === 'wrong-scope' ? 'patient:999:999:999' : fixture.scope,
            serverTime: new Date(sampledAt).toISOString(), appointments: [row],
          }) });
        }
        return route.abort();
      });
      const initial = page.waitForResponse(response => response.url().includes('/video-status/'));
      await page.goto('https://appointments.test/patient/appointments/');
      await initial;
      const join = page.getByRole('link', { name: 'Join video call', exact: true });
      assert.equal(await join.isVisible(), false, 'Join is hidden before the opening boundary');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `Layout overflow at ${width}`);
      await page.evaluate(() => { window.sameDocumentMarker = 'kept'; });
      mode = 'open'; now = opens + 100;
      const boundary = page.waitForResponse(response => response.url().includes('/video-status/'));
      await page.clock.runFor(1100);
      await boundary;
      await join.waitFor({ state: 'visible' });
      assert.equal(await join.getAttribute('href'), `https://appointments.test/video/appointments/${fixture.id}/`);
      assert.equal(await join.evaluate(node => node.closest('td').dataset.label), 'Actions');
      assert.equal(await page.locator('[data-appointment-video-announcement]').textContent(),
        'Your video appointment is ready to join. Use Join video call in the Actions column.');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `Join layout overflow at ${width}`);

      async function refresh(nextMode) {
        mode = nextMode;
        const response = page.waitForResponse(item => item.url().includes('/video-status/'));
        await page.evaluate(() => window.dispatchEvent(new Event('online')));
        await response;
      }

      await refresh('cancelled');
      await join.waitFor({ state: 'hidden' });
      assert.equal(await page.locator('[data-appointment-video-join]').getAttribute('href'), null);
      await refresh('moved');
      assert.equal(await join.isVisible(), false, 'A rescheduled appointment stays closed');
      assert.equal(await page.locator('[data-video-appointment-id]').getAttribute('data-video-opens-at'), new Date(opens + 60000).toISOString());
      await refresh('open');
      await join.waitFor({ state: 'visible' });

      // Expiry must hide the action even while the next server request is held.
      mode = 'held';
      await page.evaluate(() => window.dispatchEvent(new Event('online')));
      for (let attempts = 0; !release && attempts < 20; attempts++) await new Promise(resolve => setTimeout(resolve, 10));
      assert.ok(release, 'The simulated status request is in flight');
      await page.clock.runFor(4500);
      assert.equal(await join.isVisible(), false, 'The call closes locally during a network delay');
      now = opens + 5000;
      release();
      await page.waitForResponse(item => item.url().includes('/video-status/'));
      assert.equal(await join.isVisible(), false, 'A response after expiry cannot restore the link');
      await refresh('wrong-scope');
      const stoppedAt = statusRequests;
      await page.clock.runFor(31000);
      assert.equal(statusRequests, stoppedAt, 'A changed account or practice stops this table');
      assert.equal(navigations, 1, 'Live changes never reload the page');
      assert.equal(await page.evaluate(() => window.sameDocumentMarker), 'kept');
      assert.deepEqual(errors, []);
      checked++;
      await context.close();
    }
    for (const width of [1440, 390, 320]) await scenario(width);
    console.log(`Passed ${checked} appointment browser scenarios (opening, cancellation, reschedule, expiry during delay, scope changes; desktop and mobile).`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
