/* Page controllers must survive repeated soft visits without duplicate listeners,
   stale responses, or sockets operating on a later page. Run with Playwright. */
const assert = require('node:assert/strict');
const { chromium } = require(process.env.MERIDIAN_PLAYWRIGHT_PATH || 'playwright');

(async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const errors = [];
  let cases = 0;
  async function setup(html) {
    const page = await browser.newPage();
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
    await page.route('https://meridian.test/**', route => route.fulfill({ contentType: 'text/html', body: `<!doctype html><body>${html}</body>` }));
    await page.goto('https://meridian.test/messages/?thread=7');
    await page.addScriptTag({ path: 'static/js/page_lifecycle.js' });
    return page;
  }
  const script = (page, name) => page.addScriptTag({ path: `static/js/${name}.js` });
  const remount = page => page.evaluate(() => { MeridianPages.dispose(); MeridianPages.mount(); });
  try {
    // Returning to a page must not stack global handlers or duplicate slot-picker DOM.
    {
      const page = await setup(`<form data-booking-form data-times-url="/times/">
        <select name="clinician"><option value="1">Clinician</option></select>
        <input name="date" value="2026-10-24">
        <div class="console-form__field"><label>Time</label><select name="time"><option value="09:00">09:00</option><option value="09:15">09:15</option></select></div>
        <button type="submit">Book</button></form>
        <div data-time-range><select class="quarter-hour-time" id="range-start"><option value="08:00">08:00</option><option value="07:45">07:45</option></select><select class="quarter-hour-time" id="range-end"><option value="08:00">08:00</option><option value="08:15">08:15</option></select></div>
        <button data-modal-open="example">Open dialog</button><dialog class="app-dialog" id="example"><button data-dialog-close>Close</button></dialog>`);
      await page.evaluate(() => {
        window.requests = 0; window.opens = 0;
        document.querySelector('dialog').addEventListener('dialog:open', () => window.opens++);
        window.fetch = async () => { window.requests++; return new Response(JSON.stringify({ times: ['09:00', '09:15'], slots: [{ time: '09:00', label: 'Available', state: 'free' }] }), { headers: { 'Content-Type': 'application/json' } }); };
      });
      await script(page, 'dialogs');
      await script(page, 'appointment_booking');
      await script(page, 'time_ranges');
      for (let i = 0; i < 3; i++) { await remount(page); await page.waitForTimeout(20); }
      assert.equal(await page.locator('.slot-picker').count(), 1);
      await page.locator('#range-start').selectOption('07:45');
      assert.equal(await page.locator('#range-end option[value="08:00"]').count(), 1);
      const before = await page.evaluate(() => window.requests);
      await page.locator('[name="date"]').fill('2026-10-25');
      await page.locator('[name="date"]').dispatchEvent('change');
      await page.waitForTimeout(30);
      assert.equal(await page.evaluate(() => window.requests), before + 1);
      await page.locator('[data-modal-open]').click();
      assert.equal(await page.evaluate(() => window.opens), 1);
      await page.locator('[data-dialog-close]').click();
      await page.evaluate(() => MeridianPages.dispose());
      assert.equal(await page.locator('.slot-picker').count(), 0);
      assert.equal(await page.locator('[name="time"] option').count(), 2);
      await page.close(); cases++;
    }

    // A body that finishes after leaving search cannot update the next page or its URL.
    {
      const page = await setup('<form data-conversation-search action="/messages/"><input name="q"></form><aside data-search-region="list">Initial list</aside>');
      await page.evaluate(() => {
        window.fetch = async () => {
          window.started = true;
          const response = new Response('unused');
          response.text = () => new Promise(resolve => { window.releaseBody = () => resolve('<aside data-search-region="list">Stale private search</aside>'); });
          return response;
        };
      });
      await script(page, 'conversation_search');
      await page.locator('input').fill('old search');
      await page.waitForFunction(() => typeof window.releaseBody === 'function');
      await page.evaluate(() => {
        MeridianPages.dispose();
        document.body.innerHTML = '<h1>Another page</h1><aside data-search-region="list">Current page list</aside>';
        history.pushState({ page: 'current' }, '', '/appointments/?page=2');
        MeridianPages.mount();
        window.releaseBody();
      });
      await page.waitForTimeout(50);
      assert.equal(await page.locator('[data-search-region]').textContent(), 'Current page list');
      assert.ok(page.url().endsWith('/appointments/?page=2'));
      await page.close(); cases++;
    }

    // Global unread updates restart once per page; old sockets and a submitted reply
    // finishing later must neither stop nor overwrite the newly mounted page.
    {
      const config = '<div data-live-messages data-live-scope="staff:1:2" data-status-url="/status/" hidden><input name="csrfmiddlewaretoken" value="token"></div><span data-live-unread="total">0</span>';
      const page = await setup(`${config}<section data-record-messages><ol data-live-region="messages" data-live-thread="7"><li data-message-id="1">Original message</li></ol><form data-live-reply action="/reply/" method="post"><textarea name="body">My reply</textarea><button type="submit">Send</button></form></section>`);
      await page.evaluate(() => {
        window.sockets = []; window.posts = 0; window.unread = 2;
        window.WebSocket = class {
          constructor() { this.readyState = 1; window.sockets.push(this); }
          close(code = 1000) { this.readyState = 3; this.onclose?.({ code }); }
        };
        window.fetch = async (url, options = {}) => {
          if (options.method === 'POST') {
            window.posts++;
            return new Promise(resolve => { window.finishReply = () => resolve(new Response('Expired response', { status: 403 })); });
          }
          if (String(url).includes('/status/')) return new Response(JSON.stringify({ scope: 'staff:1:2', patient: window.unread, team: 0, total: window.unread }), { headers: { 'Content-Type': 'application/json' } });
          return new Response(document.documentElement.outerHTML, { headers: { 'Content-Type': 'text/html' } });
        };
      });
      await script(page, 'live_messages');
      await page.waitForFunction(() => document.querySelector('[data-live-unread]').textContent === '2');
      await page.locator('button').click();
      await page.waitForFunction(() => typeof window.finishReply === 'function');
      await page.evaluate(config => {
        window.oldSocket = window.sockets.at(-1);
        MeridianPages.dispose();
        document.body.innerHTML = config + '<h1>Appointments</h1>';
        history.pushState({}, '', '/appointments/');
        window.unread = 12;
        MeridianPages.mount();
        window.finishReply();
        window.oldSocket.onclose({ code: 4001 });
        window.oldSocket.onmessage({ data: '{"type":"messages.changed"}' });
      }, config);
      await page.waitForFunction(() => document.querySelector('[data-live-unread]').textContent === '9+');
      assert.equal(await page.locator('h1').textContent(), 'Appointments');
      assert.equal(await page.locator('[data-live-status]').count(), 0);
      assert.equal(await page.evaluate(() => window.posts), 1);
      for (let i = 0; i < 3; i++) await remount(page);
      assert.equal(await page.evaluate(() => window.sockets.filter(socket => socket.readyState === 1).length), 1);
      await page.close(); cases++;
    }

    // Invitations use one socket even when their retained header is remounted.
    {
      const page = await setup('<aside id="incoming-call" hidden><p id="incoming-call-title"></p><p id="incoming-call-practice"></p><a id="incoming-call-join"></a><button id="incoming-call-dismiss">Dismiss</button></aside>');
      await page.evaluate(() => {
        window.sockets = [];
        window.WebSocket = class {
          constructor() { this.readyState = 1; window.sockets.push(this); }
          close(code = 1000) { this.readyState = 3; this.onclose?.({ code }); }
        };
      });
      await script(page, 'call_notifications');
      for (let i = 0; i < 3; i++) await remount(page);
      assert.equal(await page.evaluate(() => window.sockets.filter(socket => socket.readyState === 1).length), 1);
      await page.evaluate(() => {
        const data = JSON.stringify({ type: 'incoming_call', appointment_id: 7, caller_name: 'Clinician', practice_name: 'Practice' });
        window.sockets[0].onmessage({ data });
      });
      assert.equal(await page.locator('#incoming-call').isHidden(), true);
      await page.evaluate(() => window.sockets.at(-1).onmessage({ data: JSON.stringify({ type: 'incoming_call', appointment_id: 7, caller_name: 'Clinician', practice_name: 'Practice' }) }));
      assert.equal(await page.locator('#incoming-call').isVisible(), true);
      await page.locator('#incoming-call-dismiss').click();
      assert.equal(await page.locator('#incoming-call').isHidden(), true);
      await page.close(); cases++;
    }

    assert.deepEqual(errors, []);
    console.log(`Passed ${cases} page lifecycle browser scenarios.`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
