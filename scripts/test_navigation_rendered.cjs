/* Real Django test responses and repository assets, without a live server or private data. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.MERIDIAN_PLAYWRIGHT_PATH || 'playwright');
const datasets = JSON.parse(fs.readFileSync(0, 'utf8'));
const origin = 'https://meridian.test';
const mime = { '.js': 'application/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.woff': 'font/woff',
  '.png': 'image/png', '.svg': 'image/svg+xml', '.jpg': 'image/jpeg', '.ico': 'image/x-icon' };

async function eventually(check, label) {
  const deadline = Date.now() + 10000;
  while (!check()) {
    if (Date.now() > deadline) throw new Error(`Timed out: ${label}`);
    await new Promise(resolve => setTimeout(resolve, 20));
  }
}

async function run(browser, fixture, width) {
  const context = await browser.newContext({ viewport: { width, height: 900 } });
  const page = await context.newPage();
  const state = { errors: [], unexpected: [], documents: [], requests: [], held: new Set(), releases: new Map() };
  page.on('pageerror', error => state.errors.push(error.message));
  page.on('dialog', dialog => dialog.dismiss());
  await context.addInitScript(() => {
    window.renderedSockets = [];
    window.socketMax = {};
    class FixtureSocket extends EventTarget {
      static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
      constructor(url) {
        super(); this.url = String(url); this.readyState = 0; renderedSockets.push(this);
        const active = renderedSockets.filter(item => item.url === this.url && item.readyState < 2).length;
        socketMax[this.url] = Math.max(socketMax[this.url] || 0, active);
        setTimeout(() => {
          if (this.readyState !== 0) return;
          this.readyState = 1; this.onopen?.({});
          if (this.url.endsWith('/ws/messages/')) this.onmessage?.({ data: JSON.stringify({ type: 'ready' }) });
        }, 0);
      }
      close(code = 1000) { if (this.readyState === 3) return; this.readyState = 3; this.onclose?.({ code }); }
      send() {}
    }
    window.WebSocket = FixtureSocket;
    window.navigationEvents = [];
    document.addEventListener('workspace:navigated', () => navigationEvents.push(location.pathname + location.search));
  });
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    const fulfill = options => route.fulfill(options).catch(() => {});
    if (url.origin !== origin) { state.unexpected.push(url.href); return fulfill({ status: 404, body: '' }); }
    if (url.pathname.startsWith('/static/')) {
      const relative = decodeURIComponent(url.pathname.slice('/static/'.length));
      const filename = path.resolve('static', relative);
      if (!filename.startsWith(path.resolve('static') + path.sep) || !fs.existsSync(filename)) {
        state.unexpected.push(url.pathname); return fulfill({ status: 404, body: '' });
      }
      return fulfill({ contentType: mime[path.extname(filename)] || 'application/octet-stream', body: fs.readFileSync(filename) });
    }
    if (url.pathname === '/favicon.ico') return fulfill({ status: 204, body: '' });
    const address = url.pathname + url.search;
    const headers = request.headers();
    if (request.isNavigationRequest()) {
      state.documents.push(address);
      return fulfill({ contentType: 'text/html', body: fixture.shell });
    }
    state.requests.push({ url: address, method: request.method(), headers, body: request.postData() });
    if (headers['x-meridian-navigation'] === 'section') {
      const section = headers['x-meridian-section'];
      if (fixture.sections[section]) return fulfill({ json: fixture.sections[section] });
    }
    if (headers['x-meridian-navigation'] === 'content') {
      if (state.held.has(address)) await new Promise(resolve => state.releases.set(address, resolve));
      if (fixture.pages[address]) return fulfill({ json: fixture.pages[address].content });
    }
    if (fixture.endpoints[url.pathname]) return fulfill({ json: fixture.endpoints[url.pathname] });
    if (headers['x-meridian-realtime'] === 'snapshot' && fixture.pages[address]) {
      return fulfill({ contentType: 'text/html', body: fixture.pages[address].snapshot });
    }
    state.unexpected.push(`${request.method()} ${address}`);
    return fulfill({ status: 404, json: { error: 'Unexpected synthetic endpoint' } });
  });
  const release = url => { state.held.delete(url); state.releases.get(url)?.(); state.releases.delete(url); };
  const arrive = async url => {
    const expected = fixture.pages[url].content;
    await page.waitForFunction(({ url, title }) => location.pathname + location.search === url
      && document.title === title && !document.querySelector('[data-navigation-loading]')
      && document.querySelector('#workspace-content h1'), { url, title: expected.title });
    await page.evaluate(() => document.fonts.ready);
    const result = await page.evaluate(html => {
      const expected = new DOMParser().parseFromString(html, 'text/html');
      return { actual: document.querySelector('#workspace-content h1')?.textContent.trim(),
        expected: expected.querySelector('h1')?.textContent.trim(),
        header: window.keptHeader === document.querySelector('.site-header'),
        bodyWidth: document.documentElement.scrollWidth, width: window.innerWidth };
    }, expected.html);
    assert.equal(result.actual, result.expected, `${fixture.role} ${width} ${url}: destination heading`);
    assert.ok(result.header, `${fixture.role} ${width}: persistent site header`);
    if (result.bodyWidth > result.width + 2) {
      const offenders = await page.evaluate(() => [...document.querySelectorAll('body *')]
        .filter(el => el.getBoundingClientRect().right > innerWidth + 2).slice(0, 18)
        .map(el => ({ tag: el.tagName, class: el.className, width: el.getBoundingClientRect().width, right: el.getBoundingClientRect().right })));
      const baseline = await context.newPage();
      await baseline.route(origin + url, route => route.fulfill({ contentType: 'text/html', body: fixture.pages[url].snapshot }));
      await baseline.goto(origin + url);
      await baseline.evaluate(() => document.fonts.ready);
      const normalWidth = await baseline.evaluate(() => document.documentElement.scrollWidth);
      await baseline.close();
      assert.ok(result.bodyWidth <= normalWidth + 2, `${fixture.role} ${width} ${url}: navigation introduced overflow ${result.bodyWidth} vs SSR ${normalWidth}: ${JSON.stringify(offenders)}`);
      console.log(`Existing SSR overflow: ${fixture.role} ${width}px ${url}: ${normalWidth}px (soft navigation ${result.bodyWidth}px).`);
    }
    assert.equal(await page.locator('[data-live-messages]').count(), 1);
    assert.ok(await page.locator('[data-live-messages] [name="csrfmiddlewaretoken"]').inputValue());
  };
  const navigate = async (url, held = false) => {
    if (held) state.held.add(url);
    const immediate = await page.evaluate(url => {
      const target = new URL(url, location.origin);
      const link = [...document.querySelectorAll('a[href]')].find(a => {
        const candidate = new URL(a.href);
        return candidate.pathname + candidate.search === target.pathname + target.search && !a.closest('[hidden]');
      });
      if (link) link.click(); else window.MeridianNavigation.navigate(url);
      return { url: location.pathname + location.search, loading: !!document.querySelector('[data-navigation-loading]') };
    }, url);
    assert.equal(immediate.url, url, `${fixture.role} ${width}: URL first for ${url}`);
    if (held) {
      assert.ok(immediate.loading, `${fixture.role} ${width}: immediate skeleton`);
      await eventually(() => state.releases.has(url), 'held actual content request');
      assert.equal(state.documents.length, 1);
      release(url);
    }
    await arrive(url);
  };
  const booking = async () => {
    assert.equal(await page.locator('#book-appointment [data-booking-form]').count(), 1);
    assert.equal(await page.locator('#book-appointment .slot-picker').count(), 1);
    await page.locator('[data-modal-open="book-appointment"]').first().click();
    await page.waitForFunction(() => document.getElementById('book-appointment')?.open);
    await page.locator('#book-appointment [name="clinician"]').selectOption(fixture.booking.clinician);
    await page.locator('#book-appointment [name="date"]').fill(fixture.booking.day);
    await page.locator('#book-appointment [name="date"]').dispatchEvent('change');
    await eventually(() => state.requests.some(item => item.url.startsWith(fixture.booking.times_url)), 'actual booking slot request');
    assert.equal(await page.locator('#book-appointment .slot-picker').count(), 1);
    await page.locator('#book-appointment [data-dialog-close]').click();
    // This test does not submit a booking; restore the original form before leaving it.
    await page.evaluate(() => document.querySelector('#book-appointment form').reset());
  };
  try {
    state.held.add(fixture.initial);
    await page.goto(origin + fixture.initial);
    await page.waitForFunction(() => Boolean(window.MeridianNavigation));
    await page.evaluate(() => { window.keptHeader = document.querySelector('.site-header'); });
    await eventually(() => state.releases.has(fixture.initial), 'direct document shell hydration');
    assert.equal(await page.locator('[data-navigation-loading]').count(), 1);
    release(fixture.initial); await arrive(fixture.initial);
    if (fixture.role === 'super_admin') {
      await page.waitForFunction(() => document.querySelector('[data-content-section="dashboard-metrics"]')?.hasAttribute('data-deferred-loaded'));
      assert.equal(state.requests.filter(item => item.headers['x-meridian-navigation'] === 'section').length, 1);
      assert.ok((await page.locator('[data-content-section="dashboard-metrics"]').innerText()).includes('Patients'));
    } else {
      for (const [index, url] of fixture.flow.slice(1).entries()) {
        await navigate(url, index === 0);
        if (fixture.booking && url === fixture.flow[1]) await booking();
        if (url.includes('/messages/')) {
          await navigate(fixture.thread_url);
          await page.waitForFunction(() => document.querySelector('#workspace-content').textContent.includes('AUTHORIZED_NAVIGATION_MESSAGE'));
          await page.waitForTimeout(400);
        }
      }
      await navigate(fixture.initial);
      await page.goBack(); await arrive(fixture.flow.at(-1));
      await page.goForward(); await arrive(fixture.initial);
      if (fixture.booking) { await navigate(fixture.flow[1]); await booking(); await navigate(fixture.initial); }
    }
    await page.waitForTimeout(250);
    const sockets = await page.evaluate(() => ({ max: socketMax,
      active: renderedSockets.filter(socket => socket.readyState === 1).map(socket => socket.url) }));
    assert.equal(sockets.max[origin.replace('https:', 'wss:') + '/ws/messages/'], 1, 'one live unread socket');
    for (const count of Object.values(sockets.max)) assert.equal(count, 1, 'socket disposal before remount');
    assert.equal(new Set(sockets.active).size, sockets.active.length);
    for (const request of state.requests.filter(item => item.method === 'POST')) {
      assert.ok(request.url.endsWith('/seen/'), 'no mutation except visible-message acknowledgement');
      assert.ok(request.headers['x-csrftoken'], 'read acknowledgement includes actual CSRF token');
    }
    assert.deepEqual(state.errors, [], `${fixture.role} ${width}: browser errors`);
    assert.deepEqual(state.unexpected, [], `${fixture.role} ${width}: unprepared network requests`);
    assert.equal(state.documents.length, 1, `${fixture.role} ${width}: no hidden full document reload`);
    console.log(`Rendered ${fixture.role} ${width}px passed (${state.requests.length} requests).`);
  } finally {
    state.releases.forEach(resolve => resolve());
    await context.close();
  }
}

(async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  let flows = 0;
  try {
    for (const fixture of datasets) for (const width of [1280, 390]) { await run(browser, fixture, width); flows += 1; }
  } finally { await browser.close(); }
  console.log(JSON.stringify({ flows }));
})().catch(error => { console.error(error); process.exitCode = 1; });
