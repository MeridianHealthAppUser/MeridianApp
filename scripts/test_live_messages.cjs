/* Browser-level checks for realtime delivery, read visibility, navigation and safe reply behaviour.
   Run with MERIDIAN_PLAYWRIGHT_PATH pointing to an installed playwright package. */
const assert = require('node:assert/strict');
const { chromium } = require(process.env.MERIDIAN_PLAYWRIGHT_PATH || 'playwright');

(async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const errors = [];
  let cases = 0;
  async function setup({ inbox = true, older = false, withSearch = false, viewport = { width: 1280, height: 800 } } = {}) {
    const state = { scope: 'staff:1:2', ids: Array.from({ length: 20 }, (_, i) => i + 1), unread: 0, reads: [], posts: [], searches: [], snapshots: 0, sendMode: 'success', closed: false };
    const context = await browser.newContext({ viewport });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    const html = () => `<!doctype html><html><head><style>
      .record-conversation {height:200px;overflow:auto;margin:0;padding:0;list-style:none;} li {height:50px;}
      .console-nav{display:block}.staff-mobile-nav{display:none}@media(max-width:700px){.console-nav{display:none}.staff-mobile-nav{display:block;position:fixed;bottom:0;left:0}}
      a{position:relative;display:inline-flex;width:100px;padding:10px;}textarea{width:250px;height:60px}
      </style></head><body>
      <div hidden data-live-messages data-live-scope="${state.scope}" data-status-url="/messages/status/"><input name="csrfmiddlewaretoken" value="test-token"></div>
      <nav class="console-nav"><a href="/inbox/">Messages<span class="message-unread-dot" data-live-unread="total" hidden></span><span class="sr-only" data-live-unread-label="total"></span></a></nav>
      <nav class="staff-mobile-nav"><a href="/inbox/">Messages<span class="message-unread-dot" data-live-unread="total" hidden></span><span class="sr-only" data-live-unread-label="total"></span></a></nav>
      <main id="workspace-content">${inbox ? `<section data-record-messages>${withSearch ? '<form data-conversation-search action="/inbox/"><input name="q" aria-label="Search conversations"></form>' : ''}<aside data-search-region="list">Thread list ${state.ids.length}</aside><section class="record-messages__pane">
      <header data-live-region="header">Care conversation</header>
      <ol class="record-conversation" data-record-conversation data-live-region="messages" data-live-thread="staff-message-seen:7" data-live-read-url="/seen/7/" data-live-closed="${state.closed}" ${older ? 'data-live-history="older"' : ''}>${state.ids.map(id => `<li data-message-id="${id}">Message ${id}</li>`).join('')}</ol>
      <form data-live-reply data-record-reply method="post" action="/reply/7/"><textarea name="body" required></textarea><button type="submit">Send reply</button></form></section></section>` : '<h1>Dashboard</h1>'}</main>
      </body></html>`;
    await context.route('https://meridian.test/**', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname === '/messages/status/') return route.fulfill({ json: { scope: state.scope, patient: state.unread, team: 0, total: state.unread } });
      if (withSearch && url.pathname === '/inbox/' && url.searchParams.has('q')) {
        state.searches.push(request.headers());
        if (request.headers()['x-meridian-scope'] !== state.scope) return route.fulfill({ status: 409, json: { scope: state.scope } });
        if (state.holdSearch) await new Promise(resolve => { state.releaseSearch = resolve; });
        return route.fulfill({ contentType: 'text/html', body: html().replace(`Thread list ${state.ids.length}`, 'Late private search result') }).catch(() => {});
      }
      if (url.pathname === '/seen/7/') {
        const ids = JSON.parse(request.postData()).message_ids;
        state.reads.push(ids);
        const changed = state.unread && ids.includes(String(state.ids.at(-1)));
        if (changed) state.unread = 0;
        return route.fulfill({ json: { scope: state.scope, patient: state.unread, team: 0, total: state.unread, changed: !!changed } });
      }
      if (url.pathname === '/reply/7/') {
        state.posts.push(request.postData());
        if (state.sendMode === 'network') return route.abort('failed');
        await new Promise(resolve => setTimeout(resolve, 300));
        if (state.sendMode === 'invalid') return route.fulfill({ contentType: 'text/html', body: html().replace('<textarea', '<p class="record-error">Please shorten your message.</p><textarea') });
        state.ids.push(state.ids.at(-1) + 1);
        // Playwright interception cannot follow a redirect to this synthetic hostname. Return
        // the final HTML and model Response.redirected below; Django tests cover actual PRG.
        return route.fulfill({ headers: { 'X-Test-Redirect': '1' }, contentType: 'text/html', body: html() });
      }
      if (request.headers()['x-meridian-realtime']) state.snapshots += 1;
      return route.fulfill({ contentType: 'text/html', body: html() });
    });
    await page.goto(`https://meridian.test/${inbox ? 'inbox/?thread=7' : 'dashboard/'}${older ? '&message_page=2' : ''}`);
    await page.addStyleTag({ path: 'static/css/live_messages.css' });
    await page.evaluate(() => {
      window.sockets = [];
      const fetch = window.fetch;
      window.fetch = async (...args) => {
        const response = await fetch(...args);
        if (response.headers.get('X-Test-Redirect')) Object.defineProperty(response, 'redirected', { value: true });
        return response;
      };
      window.simulatedHidden = false;
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => window.simulatedHidden });
      window.WebSocket = class {
        constructor(url) { this.url = url; this.readyState = 1; window.sockets.push(this); queueMicrotask(() => this.onmessage?.({ data: '{"type":"ready"}' })); }
        close(code = 1000) { this.readyState = 3; this.onclose?.({ code }); }
      };
    });
    await page.addScriptTag({ path: 'static/js/conversations.js' });
    if (withSearch) await page.addScriptTag({ path: 'static/js/conversation_search.js' });
    await page.addScriptTag({ path: 'static/js/live_messages.js' });
    await page.waitForTimeout(650);
    const changed = () => page.evaluate(() => window.sockets.at(-1).onmessage({ data: '{"type":"messages.changed"}' }));
    const hide = value => page.evaluate(value => { window.simulatedHidden = value; document.dispatchEvent(new Event('visibilitychange')); }, value);
    return { page, context, state, changed, hide };
  }
  try {
    // Messages are fetched on every page, but only inbox pages receive an HTML snapshot.
    for (const width of [1280, 390]) {
      const { page, context, state, changed } = await setup({ inbox: false, viewport: { width, height: 800 } });
      assert.equal(state.snapshots, 0);
      const nav = width > 700 ? '.console-nav' : '.staff-mobile-nav';
      for (const count of [2, 12, 100, 0]) {
        state.unread = count; await changed();
        const expected = count > 99 ? '99+' : String(count);
        await page.waitForFunction(({ selector, count, expected }) => {
          const badge = document.querySelector(selector);
          return count ? !badge.hidden && badge.textContent === expected : badge.hidden;
        }, { selector: `${nav} [data-live-unread]`, count, expected });
        const badge = page.locator(`${nav} .message-unread-dot`);
        assert.equal(await badge.isVisible(), count > 0);
        // Both responsive copies receive the count, including whichever is currently hidden by layout.
        assert.deepEqual(await page.locator('[data-live-unread="total"]').allTextContents(), [expected, expected]);
        assert.equal(await page.locator(`${nav} [data-live-unread-label]`).textContent(), count ? `, ${count} unread messages` : '');
        if (count) {
          assert.equal(await badge.evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(217, 45, 32)');
          assert.equal(await badge.evaluate(el => getComputedStyle(el).color), 'rgb(255, 255, 255)');
          assert.ok(await badge.evaluate(el => el.scrollWidth <= el.clientWidth));
        }
      }
      assert.equal(state.snapshots, 0);
      await context.close(); cases += 1;
    }
    // The patient's single navigation becomes a bottom bar on mobile; the same live target works in both layouts.
    for (const width of [1280, 390]) {
      const { page, context, state, changed } = await setup({ inbox: false, viewport: { width, height: 800 } });
      await page.locator('.staff-mobile-nav').evaluate(el => el.remove());
      await page.locator('.console-nav').evaluate(el => {
        el.className = 'patient-navigation';
        el.innerHTML = '<div class="patient-navigation__links"><a href="/patient/messages/">My Messages<span class="message-unread-dot" data-live-unread="patient" hidden></span><span class="sr-only" data-live-unread-label="patient"></span></a></div>';
      });
      await page.addStyleTag({ path: 'static/css/patient_pages.css' });
      for (const count of [2, 12, 100, 0]) {
        state.unread = count; await changed();
        const expected = count > 99 ? '99+' : String(count);
        await page.waitForFunction(({ count, expected }) => {
          const badge = document.querySelector('[data-live-unread="patient"]');
          return count ? !badge.hidden && badge.textContent === expected : badge.hidden;
        }, { count, expected });
        const badge = page.locator('.patient-navigation .message-unread-dot');
        assert.equal(await badge.isVisible(), count > 0);
        assert.equal(await badge.textContent(), expected);
        assert.equal(await page.locator('[data-live-unread-label="patient"]').textContent(), count ? `, ${count} unread messages` : '');
        if (count === 12 && process.env.MERIDIAN_MESSAGE_SCREENSHOTS) {
          await page.screenshot({ path: `${process.env.MERIDIAN_MESSAGE_SCREENSHOTS}/meridian-message-badge-${width}.png` });
        }
        if (count) {
          assert.equal(await badge.evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(217, 45, 32)');
          assert.equal(await badge.evaluate(el => getComputedStyle(el).color), 'rgb(255, 255, 255)');
          assert.ok(await badge.evaluate(el => el.scrollWidth <= el.clientWidth));
        }
      }
      assert.equal(await page.locator('.patient-navigation').evaluate(el => getComputedStyle(el).position), width > 832 ? 'sticky' : 'fixed');
      await context.close(); cases += 1;
    }

    // Hidden tabs don't fetch conversations or acknowledge newly delivered messages.
    {
      const { page, context, state, changed, hide } = await setup();
      await page.locator('textarea').fill('An unfinished question');
      await page.locator('textarea').evaluate(el => { el.focus(); el.setSelectionRange(3, 8); });
      await page.locator('ol').evaluate(el => { el.scrollTop = 0; });
      const baseline = state.snapshots;
      await hide(true); state.ids.push(21); state.unread = 1; await changed();
      await page.waitForTimeout(450);
      assert.equal(state.snapshots, baseline);
      assert.ok(!state.reads.flat().includes('21'));
      assert.equal(await page.locator('.console-nav [data-live-unread]').isVisible(), true);
      await hide(false);
      await page.waitForSelector('[data-message-id="21"]');
      assert.equal(await page.locator('textarea').inputValue(), 'An unfinished question');
      assert.deepEqual(await page.locator('textarea').evaluate(el => [el.selectionStart, el.selectionEnd]), [3, 8]);
      assert.equal(await page.locator('ol').evaluate(el => el.scrollTop), 0);
      assert.ok(!state.reads.flat().includes('21'));
      await page.locator('ol').evaluate(el => { el.scrollTop = el.scrollHeight; el.dispatchEvent(new Event('scroll')); });
      await page.waitForFunction(() => document.querySelector('.console-nav [data-live-unread]').hidden);
      assert.ok(state.reads.flat().includes('21'));
      await context.close(); cases += 1;
    }

    // Follow the newest message only when the reader was already at the bottom.
    {
      const { page, context, state, changed } = await setup();
      state.ids.push(21); await changed(); await page.waitForSelector('[data-message-id="21"]');
      assert.ok(await page.locator('ol').evaluate(el => el.scrollHeight - el.clientHeight - el.scrollTop < 2));
      await context.close(); cases += 1;
    }

    // Sending uses one POST, doesn't reload, and preserves text typed while a request is in flight.
    {
      const { page, context, state } = await setup();
      await page.locator('textarea').fill('First reply');
      await page.locator('form[data-live-reply]').evaluate(form => { form.requestSubmit(); form.requestSubmit(); });
      await page.locator('textarea').fill('Next thought');
      await page.waitForSelector('[data-message-id="21"]');
      assert.equal(state.posts.length, 1);
      assert.equal(await page.locator('textarea').inputValue(), 'Next thought');
      assert.equal(await page.evaluate(() => window.sockets.length), 1);
      await page.waitForFunction(() => !document.querySelector('[type="submit"]').disabled);
      await page.locator('button[type="submit"]').click();
      await page.waitForSelector('[data-message-id="22"]');
      await page.waitForFunction(() => !document.querySelector('[data-live-reply]').hasAttribute('aria-busy'));
      assert.equal(await page.locator('textarea').inputValue(), '', JSON.stringify({ posts: state.posts, error: await page.locator('[data-live-send-error]').textContent() }));
      assert.equal(state.posts.length, 2);
      await context.close(); cases += 1;
    }

    for (const mode of ['network', 'invalid']) {
      const { page, context, state } = await setup();
      state.sendMode = mode;
      await page.locator('textarea').fill('Keep this draft');
      await page.locator('button[type="submit"]').click();
      await page.waitForSelector('[data-live-send-error]:not([hidden])');
      assert.equal(await page.locator('textarea').inputValue(), 'Keep this draft');
      await page.waitForTimeout(700);
      assert.equal(state.posts.length, 1);
      assert.match(await page.locator('[data-live-send-error]').innerText(), mode === 'network' ? /could not confirm/ : /shorten/);
      await context.close(); cases += 1;
    }

    // Older history never shifts underneath the reader as new messages change offset pagination.
    {
      const { page, context, state, changed } = await setup({ older: true });
      state.ids = [10, 11, 12, 13]; state.unread = 1; await changed();
      await page.waitForSelector('[data-live-history-notice]');
      assert.equal(await page.locator('[data-message-id="1"]').count(), 1);
      assert.match(await page.locator('[data-live-history-notice] a').getAttribute('href'), /thread=7/);
      assert.doesNotMatch(await page.locator('[data-live-history-notice] a').getAttribute('href'), /message_page/);
      await context.close(); cases += 1;
    }

    // A conversation closed elsewhere stops replies without destroying a draft; reopening restores the control.
    {
      const { page, context, state, changed } = await setup();
      await page.locator('textarea').fill('Unsent draft');
      state.closed = true; await changed();
      await page.waitForFunction(() => document.querySelector('[type="submit"]').disabled);
      assert.equal(await page.locator('textarea').inputValue(), 'Unsent draft');
      state.closed = false; await changed();
      await page.waitForFunction(() => !document.querySelector('[type="submit"]').disabled);
      assert.equal(await page.locator('textarea').inputValue(), 'Unsent draft');
      await context.close(); cases += 1;
    }

    // Reconnection catches up from persistent data, and terminal scope/auth changes clear old content.
    {
      const { page, context, state } = await setup();
      await page.evaluate(() => window.sockets[0].close(4503));
      state.ids.push(21);
      await page.waitForSelector('[data-message-id="21"]');
      assert.equal(await page.evaluate(() => window.sockets.length), 2);
      await page.locator('textarea').fill('Preserve on logout');
      await page.evaluate(() => window.sockets.at(-1).close(4001));
      assert.equal(await page.locator('[data-message-id]').count(), 0);
      assert.equal(await page.locator('textarea').inputValue(), 'Preserve on logout');
      assert.equal(await page.locator('button[type="submit"]').isDisabled(), true);
      await page.waitForTimeout(1500);
      assert.equal(await page.evaluate(() => window.sockets.length), 2);
      await context.close(); cases += 1;
    }
    {
      const { page, context, state, changed } = await setup();
      await page.locator('textarea').fill('Old practice draft');
      state.scope = 'staff:99:2'; await changed();
      await page.waitForSelector('[data-live-status]');
      assert.equal(await page.locator('[data-message-id]').count(), 0);
      assert.equal(await page.locator('textarea').inputValue(), 'Old practice draft');
      await context.close(); cases += 1;
    }
    // Searches use the same practice scope and cannot repopulate content after logout.
    {
      const { page, context, state } = await setup({ withSearch: true });
      state.holdSearch = true;
      await page.locator('textarea').fill('Keep this private draft');
      // Even if a response cannot be cancelled, the stopped-state check must reject it.
      await page.evaluate(() => {
        const nativeFetch = window.fetch;
        window.fetch = (url, options) => nativeFetch(url, { ...options, signal: undefined });
      });
      const started = page.waitForRequest(request => new URL(request.url()).searchParams.get('q') === 'pending');
      await page.getByLabel('Search conversations').fill('pending');
      await started;
      for (let attempts = 0; !state.releaseSearch && attempts < 20; attempts++) await new Promise(resolve => setTimeout(resolve, 10));
      assert.ok(state.releaseSearch);
      assert.equal(state.searches[0]['x-meridian-scope'], 'staff:1:2');
      assert.equal(state.searches[0]['x-meridian-realtime'], 'snapshot');
      await page.evaluate(() => window.sockets.at(-1).close(4001));
      const completed = page.waitForResponse(response => new URL(response.url()).searchParams.get('q') === 'pending');
      state.releaseSearch();
      await completed;
      await page.waitForTimeout(350);
      assert.equal(await page.locator('[data-search-region]').textContent(), '');
      assert.equal(await page.locator('[data-message-id]').count(), 0);
      assert.equal(await page.locator('textarea').inputValue(), 'Keep this private draft');
      await page.getByLabel('Search conversations').fill('after logout');
      await page.waitForTimeout(350);
      assert.equal(state.searches.length, 1);
      await context.close(); cases += 1;
    }
    {
      const { page, context, state } = await setup({ withSearch: true });
      await page.locator('textarea').fill('Original practice draft');
      state.scope = 'staff:99:2';
      await page.getByLabel('Search conversations').fill('new practice');
      await page.waitForFunction(() => !document.querySelector('[data-message-id]'));
      assert.equal(await page.locator('[data-search-region]').textContent(), '');
      assert.equal(await page.locator('textarea').inputValue(), 'Original practice draft');
      assert.equal(state.searches[0]['x-meridian-scope'], 'staff:1:2');
      await context.close(); cases += 1;
    }
    // The shared search helper still works on pages without realtime configuration.
    {
      const context = await browser.newContext();
      const page = await context.newPage();
      await page.route('https://search.test/**', route => {
        if (new URL(route.request().url()).searchParams.has('q')) {
          assert.equal(route.request().headers()['x-meridian-scope'], undefined);
          return route.fulfill({ contentType: 'text/html', body: '<section data-search-region="list">Ordinary search result</section>' });
        }
        return route.fulfill({ contentType: 'text/html', body: '<form data-conversation-search action="/search/"><input name="q" aria-label="Search"></form><section data-search-region="list">Initial list</section>' });
      });
      await page.goto('https://search.test/search/');
      await page.addScriptTag({ path: 'static/js/conversation_search.js' });
      await page.getByLabel('Search', { exact: true }).fill('query');
      await page.waitForFunction(() => document.querySelector('[data-search-region]').textContent === 'Ordinary search result');
      await context.close(); cases += 1;
    }
    assert.deepEqual(errors, []);
    console.log(`${cases} realtime messaging browser scenarios passed (desktop/mobile numeric badges, unread visibility, drafts/scroll, sends, history, reconnect and session isolation).`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
