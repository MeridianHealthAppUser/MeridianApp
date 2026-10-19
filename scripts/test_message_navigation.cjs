/* Realtime messaging must yield to page navigation, then recover after Back or a cancelled load.
   MERIDIAN_NAVIGATION_BASELINE=1 runs the pending-navigation observations against HEAD scripts.
   WebKit runs when installed; set MERIDIAN_TEST_WEBKIT=0 to use Chrome only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { execFileSync } = require('node:child_process');
const { chromium, webkit } = require(process.env.MERIDIAN_PLAYWRIGHT_PATH || 'playwright');
const baseline = process.env.MERIDIAN_NAVIGATION_BASELINE === '1';
const scriptSource = name => baseline
  ? execFileSync('git', ['show', `HEAD:static/js/${name}`], { encoding: 'utf8' })
  : fs.readFileSync(`static/js/${name}`, 'utf8');

async function eventually(check, description) {
  const deadline = Date.now() + 5000;
  while (!check()) {
    if (Date.now() > deadline) throw new Error(`Timed out: ${description}`);
    await new Promise(resolve => setTimeout(resolve, 20));
  }
}

async function setup(browser, hold = '', query = '?thread=7') {
  const state = { hold, background: [], holds: new Map(), unread: 0, snapshotText: 'Initial conversation', destinationStarted: false, pausedRequests: [] };
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();
  await page.exposeBinding('reportDepartingPage', (_, observation) => { state.departingObservation = observation; });
  const html = () => `<!doctype html><html><head>
    <style>.console-nav{display:flex;gap:12px}.console-nav a{display:block;padding:12px}.console-content{position:relative}.record-conversation{height:120px;overflow:auto}</style>
    <script src="/static/conversation_search.js" defer></script><script src="/static/workspace_polish.js" defer></script><script src="/static/live_messages.js" defer></script>
    </head><body>
    <div hidden data-live-messages data-live-scope="staff:1:2" data-status-url="/messages/status/"><input name="csrfmiddlewaretoken" value="test-token"></div>
    <nav class="console-nav"><a id="home" href="/home/">Home</a><a id="hash" href="#composer">Composer</a><a id="newtab" href="/home/" target="_blank">New tab</a><a id="download" href="/download/" download>Download</a><a id="modified" href="/home/">Modified click</a></nav>
    <span class="message-unread-dot" data-live-unread="total" hidden>0</span>
    <main class="console-content"><section data-record-messages>
    <form data-conversation-search action="/messages/"><input name="q" aria-label="Search conversations" value=""></form>
    <aside data-search-region="list">${state.snapshotText}</aside><section class="record-messages__pane">
    <header data-live-region="header">Care conversation</header>
    <ol class="record-conversation" data-live-region="messages" data-live-thread="staff:7" data-live-closed="false"><li data-message-id="1">${state.snapshotText}</li></ol>
    <form data-live-reply id="composer" method="post" action="/reply/"><textarea name="body" required></textarea><button type="submit">Send reply</button></form>
    </section></section></main></body></html>`;
  const json = () => ({ scope: 'staff:1:2', patient: state.unread, team: 0, total: state.unread, changed: false });
  await context.route('https://meridian.test/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const fulfill = options => route.fulfill(options).catch(() => {}); // A cancelled fetch may already be gone.
    if (url.pathname.startsWith('/static/')) return fulfill({ contentType: 'application/javascript', body: scriptSource(url.pathname.split('/').at(-1)) });
    if (url.pathname === '/home/') {
      state.destinationStarted = true;
      await new Promise(resolve => state.holds.set('destination', resolve));
      return fulfill({ contentType: 'text/html', body: '<!doctype html><h1>Home arrived</h1>' });
    }
    if (url.pathname === '/download/') return fulfill({ contentType: 'text/plain', headers: { 'Content-Disposition': 'attachment; filename="example.txt"' }, body: 'Example' });
    const kind = url.pathname === '/messages/status/' ? 'status'
      : url.pathname === '/reply/' ? 'reply'
        : url.searchParams.has('q') ? 'search' : 'snapshot';
    if (!request.isNavigationRequest()) {
      state.background.push(kind);
      if (state.destinationStarted) state.pausedRequests.push(kind);
      if (state.hold === kind) await new Promise(resolve => state.holds.set(kind, resolve));
    }
    const bodyHeaders = state.bodyHold === kind ? { 'X-Test-Hold-Body': kind } : {};
    if (state.bodyHold === kind) state.bodyHold = '';
    if (kind === 'status') return fulfill({ headers: bodyHeaders, json: json() });
    if (kind === 'reply') return fulfill({ headers: { 'X-Test-Redirect': '1' }, contentType: 'text/html', body: html() });
    return fulfill({ headers: bodyHeaders, contentType: 'text/html', body: html() });
  });
  await page.addInitScript(() => {
    window.testSockets = [];
    window.navigationEvents = [];
    window.historyCalls = [];
    window.abortEvents = [];
    window.pendingResponseBodies = {};
    document.addEventListener('workspace:navigating', () => navigationEvents.push('start'));
    document.addEventListener('workspace:navigation-cancelled', () => navigationEvents.push('cancel'));
    const replaceState = history.replaceState.bind(history);
    history.replaceState = (...args) => { historyCalls.push(String(args[2])); return replaceState(...args); };
    const fetch = window.fetch;
    window.fetch = async (...args) => {
      args[1]?.signal?.addEventListener('abort', () => abortEvents.push(String(args[0])));
      const response = await fetch(...args);
      // Model the final response after a successful Django POST/redirect/GET.
      if (response.headers.get('X-Test-Redirect')) Object.defineProperty(response, 'redirected', { value: true });
      const heldBody = response.headers.get('X-Test-Hold-Body');
      if (heldBody) {
        const text = response.text.bind(response);
        response.text = async () => {
          const body = await text();
          // Deliberately let this already-read body survive abort, modeling a queued completion.
          return new Promise(resolve => { pendingResponseBodies[heldBody] = { release: () => resolve(body) }; });
        };
      }
      return response;
    };
    window.WebSocket = class {
      constructor() { this.readyState = 1; testSockets.push(this); queueMicrotask(() => this.onmessage?.({ data: '{"type":"ready"}' })); }
      close(code = 1000) { this.readyState = 3; this.onclose?.({ code }); }
    };
  });
  await page.goto(`https://meridian.test/messages/${query}`);
  const changed = () => page.evaluate(() => testSockets.at(-1).onmessage({ data: '{"type":"messages.changed"}' }));
  const release = kind => { const done = state.holds.get(kind); state.holds.delete(kind); if (state.hold === kind) state.hold = ''; done?.(); };
  const close = async () => { state.holds.forEach(done => done()); state.holds.clear(); await context.close(); };
  return { page, context, state, changed, release, close };
}

async function rawClick(page, selector) {
  const box = await page.locator(selector).boundingBox();
  assert.ok(box, `${selector} must have a visible click target`);
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
}

(async () => {
  let cases = 0;
  const engines = [['Chrome', () => chromium.launch({ channel: 'chrome', headless: true })]];
  if (!baseline && process.env.MERIDIAN_TEST_WEBKIT !== '0') engines.push(['WebKit', () => webkit.launch({ headless: true })]);
  for (const [name, launch] of engines) {
    let browser;
    try { browser = await launch(); } catch (error) {
      if (name !== 'WebKit') throw error;
      console.log('WebKit is not installed; Chrome navigation checks still run.');
      continue;
    }
    try {
      for (const held of ['status', 'snapshot', 'search']) {
        if (process.env.MERIDIAN_NAVIGATION_VERBOSE) console.log(`${name}: held ${held}`);
        const fixture = await setup(browser, held);
        const { page, state, changed, release, close } = fixture;
        try {
          if (held === 'search') {
            await eventually(() => state.background.includes('snapshot'), 'initial snapshot');
            await page.locator('input[name="q"]').fill('dose');
          }
          await eventually(() => state.holds.has(held), `held ${held}`);
          if (process.env.MERIDIAN_NAVIGATION_VERBOSE) console.log(`${name}: ${held} held, clicking Home`);
          const before = await page.locator('[data-search-region="list"]').innerText();
          const historyBefore = await page.evaluate(() => historyCalls.length);
          // Playwright defers page.evaluate while a document navigation is unresolved.
          // Schedule the observation beforehand and return it through an exposed binding.
          await page.evaluate(() => {
            setTimeout(() => {
              testSockets.at(-1).onmessage({ data: '{"type":"messages.changed"}' });
              document.dispatchEvent(new Event('scroll'));
              window.dispatchEvent(new Event('online'));
            }, 300);
            setTimeout(() => reportDepartingPage({ text: document.querySelector('[data-search-region="list"]').innerText,
              history: historyCalls.length, starts: navigationEvents.filter(value => value === 'start').length }), 900);
          });
          await rawClick(page, '#home');
          await eventually(() => state.destinationStarted, 'first click starts destination request');
          if (process.env.MERIDIAN_NAVIGATION_VERBOSE) console.log(`${name}: destination pending`);
          state.snapshotText = 'Late private response'; state.unread = 9;
          release(held);
          await eventually(() => state.departingObservation, 'observation from departing document');
          const result = { engine: name, held, requestsWhileLeaving: state.pausedRequests.slice(),
            oldPageChanged: state.departingObservation.text !== before,
            lateHistoryChanges: state.departingObservation.history - historyBefore };
          if (baseline) console.log(`HEAD baseline observation: ${JSON.stringify(result)}`);
          else {
            assert.deepEqual(result.requestsWhileLeaving, [], `${name}: requests started after leaving ${held}`);
            assert.equal(result.oldPageChanged, false, `${name}: late ${held} replaced the departing page`);
            assert.equal(result.lateHistoryChanges, 0, `${name}: late search changed history`);
            assert.equal(state.departingObservation.starts, 1);
          }
          release('destination');
          if (process.env.MERIDIAN_NAVIGATION_VERBOSE) console.log(`${name}: destination released`);
          await page.waitForURL('https://meridian.test/home/');
          assert.equal(await page.locator('h1').innerText(), 'Home arrived');
          if (!baseline) {
            const backgroundBeforeBack = state.background.length;
            state.destinationStarted = false;
            await page.goBack({ waitUntil: 'domcontentloaded' });
            if (process.env.MERIDIAN_NAVIGATION_VERBOSE) console.log(`${name}: Back restored`);
            await eventually(() => state.background.length > backgroundBeforeBack, 'Back resumes realtime updates');
            await page.waitForFunction(() => document.querySelector('[data-live-unread="total"]').textContent === '9');
          }
          cases += 1;
        } finally { await close(); }
      }
      if (baseline) continue;

      // Losing a pending page load must not leave messages frozen forever.
      {
        const { page, state, changed, close } = await setup(browser);
        try {
          await eventually(() => state.background.includes('snapshot'), 'initial snapshot');
          await page.evaluate(() => window.dispatchEvent(new Event('pagehide')));
          const before = state.background.length;
          await changed(); await page.waitForTimeout(350);
          assert.equal(state.background.length, before, `${name}: pagehide pauses updates`);
          state.unread = 3;
          await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })));
          await page.waitForFunction(() => document.querySelector('[data-live-unread="total"]').textContent === '3');
          await page.evaluate(() => document.dispatchEvent(new CustomEvent('workspace:navigating')));
          const paused = state.background.length;
          await changed(); await page.waitForTimeout(350);
          assert.equal(state.background.length, paused);
          state.unread = 4;
          await page.evaluate(() => document.dispatchEvent(new CustomEvent('workspace:navigation-cancelled')));
          await page.waitForFunction(() => document.querySelector('[data-live-unread="total"]').textContent === '4');
          cases += 1;
        } finally { await close(); }
      }

      // These clicks leave the current document available, so they must not suspend its updates.
      {
        const { page, state, changed, close } = await setup(browser);
        try {
          await eventually(() => state.background.includes('snapshot'), 'initial snapshot');
          for (const [id, modifiers] of [['hash', {}], ['newtab', {}], ['download', {}], ['modified', { ctrlKey: true }], ['modified', { metaKey: true }]]) {
            await page.evaluate(({ id, modifiers }) => {
              // Dispatch through the real window click handler, cancelling only the browser's
              // default action afterwards so the test does not open downloads or extra windows.
              const prevent = event => event.preventDefault();
              window.addEventListener('click', prevent, { once: true });
              document.getElementById(id).dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, button: 0, ...modifiers }));
            }, { id, modifiers });
            assert.deepEqual(await page.evaluate(() => navigationEvents.filter(value => value === 'start')), [], `${name}: ${id} suspended messages`);
          }
          state.unread = 5; await changed();
          await page.waitForFunction(() => document.querySelector('[data-live-unread="total"]').textContent === '5');
          cases += 1;
        } finally { await close(); }
      }

      // Navigation suspension cancels background reads, never an already submitted message.
      for (const nextDraft of ['', 'A newer unsent draft']) {
        const { page, state, release, close } = await setup(browser, 'reply');
        try {
          await eventually(() => state.background.includes('snapshot'), 'initial snapshot');
          await page.locator('textarea').fill('A message already being sent');
          await page.locator('button[type="submit"]').click();
          await eventually(() => state.holds.has('reply'), 'reply reaches server');
          if (nextDraft) await page.locator('textarea').fill(nextDraft);
          await page.evaluate(() => document.dispatchEvent(new CustomEvent('workspace:navigating')));
          assert.equal(await page.evaluate(() => abortEvents.some(url => url.endsWith('/reply/'))), false);
          release('reply');
          await page.waitForFunction(() => !document.querySelector('[data-live-reply]').hasAttribute('aria-busy'));
          assert.equal(await page.locator('textarea').inputValue(), nextDraft, 'A confirmed send clears only its own unchanged draft');
          assert.equal(state.background.filter(kind => kind === 'reply').length, 1);
          await page.evaluate(() => document.dispatchEvent(new CustomEvent('workspace:navigation-cancelled')));
          await page.waitForTimeout(350);
          assert.equal(state.background.filter(kind => kind === 'reply').length, 1, 'Resuming must not resend a message');
          cases += 1;
        } finally { await close(); }
      }

      // A response from the previous page lifecycle cannot overwrite newer data after returning.
      for (const kind of ['status', 'snapshot']) {
        const { page, state, changed, close } = await setup(browser);
        try {
          await eventually(() => state.background.includes('snapshot'), 'initial snapshot');
          await page.waitForTimeout(250);
          await page.evaluate(() => {
            window.appliedOldResponses = [];
            const list = document.querySelector('[data-search-region="list"]');
            const badge = document.querySelector('[data-live-unread="total"]');
            new MutationObserver(() => {
              if (list.textContent.includes('Obsolete snapshot') || badge.textContent === '9') appliedOldResponses.push(true);
            }).observe(document.body, { childList: true, subtree: true, characterData: true });
          });
          state.bodyHold = kind; state.snapshotText = 'Obsolete snapshot'; state.unread = kind === 'status' ? 9 : 0;
          await changed();
          await page.waitForFunction(kind => Boolean(pendingResponseBodies[kind]), kind);
          await page.evaluate(() => document.dispatchEvent(new CustomEvent('workspace:navigating')));
          state.snapshotText = 'Current conversation'; state.unread = 3;
          await page.evaluate(() => document.dispatchEvent(new CustomEvent('workspace:navigation-cancelled')));
          await page.evaluate(kind => pendingResponseBodies[kind].release(), kind);
          await page.waitForFunction(() => document.querySelector('[data-search-region="list"]').textContent === 'Current conversation');
          assert.deepEqual(await page.evaluate(() => appliedOldResponses), [], `${name}: old ${kind} response was applied after resume`);
          assert.equal(await page.locator('[data-live-unread="total"]').textContent(), '3');
          cases += 1;
        } finally { await close(); }
      }

      // Returning to an idle paginated inbox must not turn it into a fresh search on page one.
      {
        const { page, state, close } = await setup(browser, '', '?thread=7&page=2');
        try {
          await eventually(() => state.background.includes('snapshot'), 'initial page-two snapshot');
          await page.evaluate(() => window.dispatchEvent(new Event('pagehide')));
          const before = state.background.length;
          await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })));
          await eventually(() => state.background.length > before, 'page-two refresh after return');
          await page.waitForTimeout(300);
          assert.equal(new URL(page.url()).searchParams.get('page'), '2');
          assert.equal(state.background.includes('search'), false, 'An idle search must not restart after Back');
          cases += 1;
        } finally { await close(); }
      }
    } finally { await browser.close(); }
  }
  console.log(`${cases} message navigation ${baseline ? 'baseline observations completed' : 'browser scenarios passed'}.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
