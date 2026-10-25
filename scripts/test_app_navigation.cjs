/* Delayed synthetic responses verify URL-first navigation and its failure boundaries.
   No live app, user session, or database is used. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium, webkit } = require(process.env.MERIDIAN_PLAYWRIGHT_PATH || 'playwright');

const scope = 'staff:1:2:3:doctor:doctor';
const navigationPaths = ['^/(?:desktop|schedule|patients|messages|missing|forbidden|broken)/$'];
const source = name => fs.readFileSync(`static/js/${name}`, 'utf8');
const label = address => ({ '/desktop/': 'Home', '/schedule/': 'Calendar', '/patients/': 'Patients',
  '/messages/': 'Messages', '/missing/': 'Missing', '/forbidden/': 'Forbidden', '/broken/': 'Broken' })[address] || address;

function workspace(address, text = '') {
  return `<div class="console-shell"><aside class="console-sidebar"><nav class="console-nav">
    ${['/desktop/', '/schedule/', '/patients/', '/messages/', '/missing/', '/forbidden/', '/broken/'].map(url =>
      `<a href="${url}" data-destination="${url}"${url === address ? ' aria-current="page"' : ''}>${label(url)}</a>`).join('')}
    </nav></aside><section class="console-main"><main class="console-content" data-screen="${address}">
    <h1>${label(address)}</h1><p>${text || 'Authorized synthetic page content'}</p>
    <button id="owned-button" type="button">Owned action</button>
    <form id="filter" method="get" action="/patients/"><label>Search<input name="q"></label><button>Search</button></form>
    <form id="save" method="post" action="/save/"><input name="body" value="preserved"><button>Save</button></form>
    <a id="hash" href="#anchor">Local anchor</a><a id="download" href="/report.pdf" download>Download</a>
    <a id="newtab" href="/schedule/" target="_blank">New tab</a><a id="external" href="https://example.test/">External</a>
    <a id="video" href="/video/appointments/6/">Video</a><a id="account" href="/accounts/profile/">Account</a>
    <div style="height:1200px"></div><div id="anchor">Anchor</div></main></section></div>`;
}

async function setup(browser, { shell = false, withWorkspace = false, withNotifications = false, withToasts = false, repeatShell = false,
    initialURL = '/desktop/', configPaths = navigationPaths, expectNavigation = true, rejectCleanup = false, rejectInitialHistory = false, countRegexes = false } = {}) {
  const state = { held: new Set(), releases: new Map(), responses: new Map(), sections: new Map(), assets: new Map(), assetRequests: [], requests: [], documents: [], documentRequests: [] };
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const payload = (url, extra = {}) => ({ version: 1, build: 'test', scope, layout: 'staff',
    canonical_url: url.pathname + url.search, title: `${label(url.pathname)} | Meridian`,
    html: workspace(url.pathname), assets: { styles: [], scripts: [] }, ...extra });
  const document = (isShell = shell) => `<!doctype html><html><head><title>Home | Meridian</title>
    <style>.console-shell{display:flex;gap:20px}.console-nav{display:grid;width:150px}.console-nav a{padding:7px}.console-main{flex:1}.console-content{position:relative}header{height:45px}</style>
    <script src="/static/js/page_lifecycle.js" defer></script>${withWorkspace ? '<script src="/static/js/workspace_polish.js" defer></script>' : ''}<script src="/static/js/app_navigation.js" defer></script>
    ${withNotifications ? '<script src="/static/js/call_notifications.js" defer></script>' : ''}${withToasts ? '<script src="/static/js/toasts.js" defer></script><script src="/static/js/account_menu.js" defer></script>' : ''}
    <script src="/static/js/navigation-fixture.js" defer></script></head><body data-page-navigation>
    <script id="page-navigation-config" type="application/json">${JSON.stringify({ version: 1, build: 'test', scope,
      layout: 'staff', canonical_url: '/desktop/', navigation_paths: configPaths, shell: isShell })}</script>
    <header id="persistent-header" class="site-header">Meridian synthetic workspace
      ${['company', 'patient-company'].map(name => `<form class="company-switcher" method="post" action="/companies/first/activate/">
        <select id="${name}-select" onchange="this.form.action=this.value;this.form.submit()"><option value="/companies/first/activate/">First</option><option value="/companies/second/activate/">Second</option></select>
        <input type="hidden" name="next" value="/desktop/"></form>`).join('')}
      ${withToasts ? '<details data-account-menu><summary>Account menu</summary><a id="profile-link" href="/accounts/profile/">My profile</a><a href="/accounts/password/">Change password</a></details>' : ''}</header>
    ${withToasts ? '<div class="toast-stack"><div data-toast class="toast error" role="alert"><span>Keep this server error</span><button data-toast-close>Dismiss old error</button></div></div>' : ''}
    ${withNotifications ? fs.readFileSync('templates/includes/incoming_call.html', 'utf8') : ''}
    <main id="workspace-content">${workspace('/desktop/', shell ? 'Initial shell' : 'Old private content')}</main></body></html>`;
  await context.route('https://meridian.test/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const fulfill = options => route.fulfill(options).catch(() => {});
    if (state.assets.has(url.pathname)) {
      state.assetRequests.push(url.pathname);
      const key = `asset:${url.pathname}`;
      if (state.held.has(key)) await new Promise(resolve => state.releases.set(key, resolve));
      return fulfill(state.assets.get(url.pathname));
    }
    if (url.pathname === '/static/js/navigation-fixture.js') return fulfill({ contentType: 'application/javascript', body: `
      window.fixtureLoads = (window.fixtureLoads || 0) + 1;
      MeridianPages.register('fixture', lifecycle => {
        window.mounts = (window.mounts || 0) + 1;
        lifecycle.on(document.getElementById('owned-button'), 'click', () => { window.ownedClicks = (window.ownedClicks || 0) + 1; });
        lifecycle.cleanup(() => { window.disposals = (window.disposals || 0) + 1; });
      });` });
    if (url.pathname.startsWith('/static/js/')) return fulfill({ contentType: 'application/javascript', body: source(url.pathname.split('/').at(-1)) });
    if (request.isNavigationRequest()) {
      state.documents.push(url.pathname + url.search);
      state.documentRequests.push({ url: url.pathname + url.search, method: request.method(), body: request.postData() });
      if (state.documents.length === 1) return fulfill({ contentType: 'text/html', body: document() });
      if (repeatShell && url.searchParams.get('__full') !== '1') return fulfill({ contentType: 'text/html', body: document(true) });
      return fulfill({ contentType: 'text/html', body: `<h1>Full navigation ${url.pathname}</h1>` });
    }
    const headers = request.headers();
    const section = headers['x-meridian-navigation'] === 'section' ? headers['x-meridian-section'] : null;
    state.requests.push({ url: url.pathname + url.search, headers, method: request.method(), section });
    const key = section ? `section:${section}` : url.pathname;
    if (state.held.has(key)) await new Promise(resolve => state.releases.set(key, resolve));
    const response = (section ? state.sections.get(section) : state.responses.get(url.pathname)) || {};
    if (response.raw) return fulfill(response.raw);
    return fulfill({ status: response.status || 200, json: payload(url, response.body || {}) });
  });
  await page.addInitScript(() => {
    window.aborts = [];
    const fetch = window.fetch.bind(window);
    window.fetch = (url, options = {}) => {
      options.signal?.addEventListener('abort', () => window.aborts.push(String(url)));
      return fetch(url, options);
    };
  });
  if (countRegexes) await page.addInitScript(paths => {
    window.regexConstructions = 0;
    window.RegExp = new Proxy(window.RegExp, { construct(target, args) { if (paths.includes(args[0])) window.regexConstructions += 1; return Reflect.construct(target, args); } });
  }, navigationPaths);
  if (rejectInitialHistory) await page.addInitScript(() => {
    history.replaceState = () => { throw new DOMException('Initial history quota exceeded', 'SecurityError'); };
  });
  if (rejectCleanup) await page.addInitScript(() => {
    const replace = history.replaceState.bind(history);
    history.replaceState = (state, unused, value) => {
      if (new URL(location.href).searchParams.has('__full') && !new URL(value, location.href).searchParams.has('__full')) {
        throw new DOMException('Cleanup quota exceeded', 'SecurityError');
      }
      return replace(state, unused, value);
    };
  });
  if (withNotifications) await page.addInitScript(() => {
    window.testSockets = [];
    window.WebSocket = class {
      constructor(url) { this.url = url; this.sent = []; this.closed = false; window.testSockets.push(this); queueMicrotask(() => this.onopen?.({})); }
      send(value) { this.sent.push(value); }
      close(code) { this.closed = true; this.onclose?.({ code }); }
    };
  });
  if (shell) state.held.add('/desktop/');
  await page.goto(`https://meridian.test${initialURL}`);
  if (expectNavigation) await page.waitForFunction(() => Boolean(window.MeridianNavigation));
  const release = pathname => { state.held.delete(pathname); state.releases.get(pathname)?.(); state.releases.delete(pathname); };
  const click = pathname => page.locator(`[data-destination="${pathname}"]`).click();
  const arrived = pathname => page.waitForFunction(path => {
    const main = document.querySelector('#workspace-content');
    return main?.querySelector(`[data-screen="${path}"]`) && !main.querySelector('[data-navigation-loading]');
  }, pathname);
  const close = async () => { state.releases.forEach(resolve => resolve()); await context.close(); };
  return { page, state, errors, release, click, arrived, close };
}

async function eventually(check, description) {
  const deadline = Date.now() + 5000;
  while (!check()) {
    if (Date.now() > deadline) throw new Error(`Timed out: ${description}`);
    await new Promise(resolve => setTimeout(resolve, 20));
  }
}

(async () => {
  let cases = 0;
  const engines = [['Chrome', () => chromium.launch({ channel: 'chrome', headless: true })]];
  if (process.env.MERIDIAN_TEST_WEBKIT !== '0') engines.push(['WebKit', () => webkit.launch({ headless: true })]);
  for (const [engine, launch] of engines) {
    let browser;
    try { browser = await launch(); } catch (error) {
      if (engine !== 'WebKit') throw error;
      console.log('WebKit unavailable; Chrome checks still run.');
      continue;
    }
    const run = async (name, test, options) => {
      const fixture = await setup(browser, options);
      try {
        await test(fixture);
        assert.deepEqual(fixture.errors, [], `${engine}: browser errors in ${name}`);
        cases += 1;
      } catch (error) { error.message = `${engine}: ${name}: ${error.message}`; throw error;
      } finally { await fixture.close(); }
    };
    try {
      await run('URL and skeleton appear before response', async ({ page, state, release, arrived }) => {
        state.held.add('/schedule/');
        const immediate = await page.evaluate(() => {
          window.headerReference = document.getElementById('persistent-header');
          document.querySelector('[data-destination="/schedule/"]').click();
          return { path: location.pathname, loading: !!document.querySelector('[data-navigation-loading]'),
            oldBody: document.querySelector('.console-content')?.textContent.includes('Old private content') };
        });
        assert.deepEqual(immediate, { path: '/schedule/', loading: true, oldBody: false });
        await eventually(() => state.releases.has('/schedule/'), 'held Calendar request');
        assert.equal(state.documents.length, 1);
        release('/schedule/'); await arrived('/schedule/');
        assert.equal(await page.title(), 'Calendar | Meridian');
        assert.equal(await page.evaluate(() => headerReference === document.getElementById('persistent-header')), true);
        assert.equal(state.requests.at(-1).headers['x-meridian-navigation'], 'content');
        assert.equal(state.requests.at(-1).headers['x-meridian-navigation-scope'], scope);
      }, { withWorkspace: true });

      await run('latest click wins and obsolete request aborts', async ({ page, state, click, release, arrived }) => {
        state.held.add('/schedule/');
        await click('/schedule/');
        await eventually(() => state.releases.has('/schedule/'), 'first held request');
        await click('/patients/'); await arrived('/patients/');
        release('/schedule/');
        await page.waitForTimeout(100);
        assert.equal(new URL(page.url()).pathname, '/patients/');
        assert.equal(await page.locator('[data-screen="/patients/"]').count(), 1);
        assert.ok((await page.evaluate(() => aborts)).some(url => url.includes('/schedule/')));
      });

      await run('Back and Forward restore URLs without document reloads', async ({ page, state, click, arrived }) => {
        await click('/schedule/'); await arrived('/schedule/');
        await click('/patients/'); await arrived('/patients/');
        await page.goBack(); await arrived('/schedule/');
        assert.equal(new URL(page.url()).pathname, '/schedule/');
        await page.goForward(); await arrived('/patients/');
        assert.equal(new URL(page.url()).pathname, '/patients/');
        assert.equal(state.documents.length, 1);
      });

      await run('404 and 403 clear previous content and retain navigation', async ({ page, state, click }) => {
        for (const [path, status] of [['/missing/', 404], ['/forbidden/', 403]]) {
          state.responses.set(path, { status, body: { error: 'This page is unavailable.' } });
          await click(path);
          await page.waitForSelector('[data-navigation-error]');
          assert.equal(new URL(page.url()).pathname, path);
          assert.equal(await page.locator('.console-content').innerText().then(text => text.includes('Old private content')), false);
          assert.equal(await page.locator('[data-destination="/desktop/"]').count(), 1);
        }
      });

      await run('failed request can retry at the destination URL', async ({ page, state, click, arrived }) => {
        state.responses.set('/broken/', { status: 500, body: { error: 'Try again.' } });
        await click('/broken/'); await page.waitForSelector('[data-navigation-error]');
        state.responses.delete('/broken/');
        await page.locator('[data-navigation-retry]').click(); await arrived('/broken/');
        assert.equal(new URL(page.url()).pathname, '/broken/');
      });

      await run('GET filters update URL while POST and special links stay native', async ({ page, state, arrived }) => {
        await page.locator('#filter input').fill('Example patient');
        await page.locator('#filter button').click(); await arrived('/patients/');
        assert.equal(new URL(page.url()).searchParams.get('q'), 'Example patient');
        const results = await page.evaluate(() => {
          const output = {};
          for (const id of ['hash', 'download', 'newtab', 'external', 'video', 'account']) {
            const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 });
            const observe = incoming => { output[id] = incoming.defaultPrevented; incoming.preventDefault(); };
            window.addEventListener('click', observe, { once: true });
            document.getElementById(id).dispatchEvent(event);
          }
          const form = document.getElementById('save');
          const observe = event => { output.post = event.defaultPrevented; event.preventDefault(); };
          window.addEventListener('submit', observe, { once: true });
          form.dispatchEvent(new SubmitEvent('submit', { bubbles: true, cancelable: true, submitter: form.querySelector('button') }));
          return output;
        });
        assert.ok(Object.values(results).every(prevented => prevented === false), JSON.stringify(results));
        assert.ok(state.requests.every(request => request.method === 'GET'));
      });

      await run('controllers mount once per page and old handlers are removed', async ({ page, state, click, arrived }) => {
        state.responses.set('/schedule/', { body: { assets: { styles: [], scripts: ['/static/js/navigation-fixture.js'] } } });
        for (const path of ['/schedule/', '/patients/', '/schedule/']) {
          await click(path); await arrived(path);
          await page.locator('#owned-button').click();
        }
        assert.equal(await page.evaluate(() => ownedClicks), 3);
        assert.equal(await page.evaluate(() => fixtureLoads), 1);
        assert.equal(await page.evaluate(() => mounts), 4);
        assert.equal(await page.evaluate(() => disposals), 3);
      });

      await run('deferred blocks are bounded and a failed block retries independently', async ({ page, state, click, release, arrived }) => {
        const markers = ['one', 'two', 'three'].map(name =>
          `<section data-deferred-content data-content-section="${name}">Waiting ${name}</section>`).join('');
        state.responses.set('/schedule/', { body: { html: workspace('/schedule/', markers) } });
        for (const name of ['one', 'two', 'three']) {
          state.held.add(`section:${name}`);
          state.sections.set(name, { body: { section: name, html: `<strong>${name} loaded</strong>` } });
        }
        state.sections.set('two', { status: 500, body: { error: 'Section failed' } });
        await click('/schedule/'); await arrived('/schedule/');
        await eventually(() => state.requests.filter(request => request.section).length === 2, 'two section workers');
        assert.equal(state.requests.filter(request => request.section).length, 2);
        assert.equal(await page.locator('[data-content-section="three"]').innerText(), 'Waiting three');
        release('section:one');
        await eventually(() => state.releases.has('section:three'), 'third section after one finishes');
        release('section:two'); release('section:three');
        await page.waitForFunction(() => document.querySelector('[data-content-section="three"]').textContent.includes('three loaded'));
        await page.locator('[data-content-section="two"] button').waitFor();
        state.sections.set('two', { body: { section: 'two', html: '<strong>two loaded</strong>' } });
        await page.locator('[data-content-section="two"] button').click();
        await page.waitForFunction(() => document.querySelector('[data-content-section="two"]').textContent.includes('two loaded'));
        assert.deepEqual(state.requests.filter(request => request.section).map(request => request.section), ['one', 'two', 'three', 'two']);
      });

      await run('leaving aborts deferred blocks and late data cannot replace the destination', async ({ page, state, click, release, arrived }) => {
        state.responses.set('/schedule/', { body: { html: workspace('/schedule/',
          '<section data-deferred-content data-content-section="slow">Waiting</section>') } });
        state.held.add('section:slow');
        state.sections.set('slow', { body: { section: 'slow', html: '<strong>Late private section</strong>' } });
        await click('/schedule/'); await arrived('/schedule/');
        await eventually(() => state.releases.has('section:slow'), 'held deferred block');
        await click('/patients/'); await arrived('/patients/');
        release('section:slow'); await page.waitForTimeout(100);
        assert.equal(new URL(page.url()).pathname, '/patients/');
        assert.equal(await page.locator('#workspace-content').innerText().then(text => text.includes('Late private section')), false);
        assert.ok((await page.evaluate(() => aborts)).some(url => url.includes('/schedule/')));
      });

      await run('server redirect performs canonical full navigation', async ({ page, state, click }) => {
        state.responses.set('/schedule/', { body: { redirect_url: '/accounts/login/?next=/schedule/' } });
        await click('/schedule/');
        await page.waitForURL('https://meridian.test/accounts/login/?next=/schedule/');
        assert.equal(state.documents.at(-1), '/accounts/login/?next=/schedule/');
      });

      await run('registered redirects stay in the shared shell and redirect loops are bounded', async ({ page, state, click, arrived }) => {
        state.responses.set('/schedule/', { body: { redirect_url: '/patients/' } });
        await click('/schedule/'); await arrived('/patients/');
        assert.equal(state.documents.length, 1);
        assert.deepEqual(state.requests.map(request => request.url), ['/schedule/', '/patients/']);
        state.responses.set('/broken/', { body: { redirect_url: '/missing/' } });
        state.responses.set('/missing/', { body: { redirect_url: '/broken/' } });
        await click('/broken/');
        await eventually(() => state.documents.length === 2, 'bounded redirect fallback');
        await page.waitForURL(url => url.searchParams.get('__full') === '1');
        assert.equal(new URL(page.url()).searchParams.get('__full'), '1');
        assert.equal(state.requests.length, 8, 'At most six requests are made for a redirect chain');
      });

      for (const failure of ['non-json', 'scope', 'full-navigation']) {
        await run(`${failure} fallback bypasses a repeated shell instead of reloading forever`, async ({ page, state, click }) => {
          state.responses.set('/schedule/', failure === 'non-json'
            ? { raw: { status: 504, contentType: 'text/html', body: '<h1>Proxy timed out</h1>' } }
            : { status: 409, body: failure === 'scope' ? { scope: 'changed', error: 'Context changed' } : { full_navigation: '/schedule/' } });
          await click('/schedule/');
          await page.waitForURL('https://meridian.test/schedule/?__full=1');
          await page.waitForTimeout(80);
          assert.equal(state.documents.length, 2);
          assert.equal(state.requests.length, 1);
        }, { repeatShell: true });
      }

      await run('sustained scrolling remains below the native WebKit history quota', async ({ page, state, click, arrived }) => {
        const writes = await page.evaluate(async () => {
          const original = history.replaceState.bind(history);
          let writes = 0;
          history.replaceState = (...args) => { writes += 1; return original(...args); };
          for (let frame = 0; frame < 170; frame += 1) {
            window.dispatchEvent(new Event('scroll'));
            await new Promise(resolve => requestAnimationFrame(resolve));
          }
          await new Promise(resolve => setTimeout(resolve, 200));
          return writes;
        });
        assert.ok(writes <= 2, `Scroll settling should save once, observed ${writes}`);
        await click('/schedule/'); await arrived('/schedule/');
        await click('/patients/'); await arrived('/patients/');
        await page.goBack(); await arrived('/schedule/');
        assert.equal(state.documents.length, 1);
      });

      await run('a rejected history write uses a bounded full-page fallback', async ({ page, state, click }) => {
        await page.evaluate(() => { history.replaceState = () => { throw new DOMException('History quota exceeded', 'SecurityError'); }; });
        await click('/schedule/');
        await page.waitForURL('https://meridian.test/schedule/?__full=1');
        assert.equal(state.documents.length, 2);
        assert.equal(state.requests.length, 0);
      });

      await run('an optional scroll-save failure cannot reload an edited page', async ({ page, state, click }) => {
        page.on('dialog', dialog => dialog.dismiss());
        await page.locator('#save input').fill('Do not discard my note');
        await page.evaluate(() => {
          history.replaceState = () => { throw new DOMException('History quota exceeded', 'SecurityError'); };
          window.dispatchEvent(new Event('scroll'));
        });
        await page.waitForTimeout(220);
        await click('/schedule/');
        assert.equal(new URL(page.url()).pathname, '/desktop/');
        assert.equal(await page.locator('#save input').inputValue(), 'Do not discard my note');
        assert.equal(state.documents.length, 1);
      });

      await run('a rejected optional address update preserves the edited form without reloading', async ({ page, state }) => {
        await page.locator('#save input').fill('Keep my unsaved message');
        await page.evaluate(() => {
          history.replaceState = () => { throw new DOMException('History quota exceeded', 'SecurityError'); };
          MeridianNavigation.replaceUrl('/desktop/?search=typed');
        });
        await page.waitForTimeout(100);
        assert.equal(new URL(page.url()).pathname, '/desktop/');
        assert.equal(new URL(page.url()).search, '');
        assert.equal(await page.locator('#save input').inputValue(), 'Keep my unsaved message');
        assert.equal(state.documents.length, 1);
        assert.equal(state.requests.length, 0);
      });

      await run('optional scroll persistence resumes after a fresh authorized navigation', async ({ page, state, click, arrived }) => {
        await page.evaluate(() => {
          const replace = history.replaceState.bind(history);
          history.replaceState = (...args) => { history.replaceState = replace; throw new DOMException('Temporary quota', 'SecurityError'); };
          window.dispatchEvent(new Event('scroll'));
        });
        await page.waitForTimeout(220);
        await click('/schedule/'); await arrived('/schedule/');
        await page.evaluate(() => {
          const replace = history.replaceState.bind(history);
          window.resumedScrollWrites = 0;
          history.replaceState = (...args) => { window.resumedScrollWrites += 1; return replace(...args); };
          window.dispatchEvent(new Event('scroll'));
        });
        await page.waitForTimeout(220);
        assert.equal(await page.evaluate(() => window.resumedScrollWrites), 1);
        assert.equal(state.documents.length, 1);
      });

      await run('complete SSR recovery removes only the recovery flag without fetching again', async ({ page, state, click, arrived }) => {
        assert.equal(page.url(), 'https://meridian.test/desktop/?filter=active#anchor');
        assert.equal(state.documents.length, 1);
        assert.equal(state.requests.length, 0);
        assert.equal(await page.locator('[data-navigation-loading]').count(), 0);
        await click('/schedule/'); await arrived('/schedule/');
        assert.equal(state.documents.length, 1);
      }, { initialURL: '/desktop/?__full=1&filter=active#anchor' });

      await run('a rejected SSR recovery-URL cleanup leaves the full page usable without a loop', async ({ page, state, click, arrived }) => {
        assert.equal(new URL(page.url()).searchParams.get('__full'), '1');
        assert.equal(state.documents.length, 1);
        assert.equal(state.requests.length, 0);
        assert.equal(await page.locator('[data-screen="/desktop/"]').count(), 1);
        await click('/schedule/'); await arrived('/schedule/');
        assert.equal(state.documents.length, 1);
      }, { initialURL: '/desktop/?__full=1', rejectCleanup: true });

      for (const shell of [false, true]) {
        await run(`an initial history rejection does not reload ${shell ? 'a shell' : 'complete SSR'}`, async ({ page, state }) => {
          await page.waitForTimeout(100);
          assert.equal(await page.evaluate(() => typeof MeridianNavigation), 'undefined');
          assert.equal(state.documents.length, 1);
          assert.equal(state.requests.length, 0);
          assert.equal(await page.locator('[data-screen="/desktop/"]').count(), 1);
        }, { shell, rejectInitialHistory: true, expectNavigation: false });
      }

      await run('an initial shell keeps its recovery flag until authorized content arrives', async ({ page, state, release, arrived }) => {
        await eventually(() => state.releases.has('/desktop/'), 'initial shell content with recovery query');
        assert.equal(new URL(page.url()).searchParams.get('__full'), '1');
        state.responses.set('/desktop/', { body: { canonical_url: '/desktop/' } });
        release('/desktop/'); await arrived('/desktop/');
        assert.equal(new URL(page.url()).searchParams.has('__full'), false);
        assert.equal(state.documents.length, 1);
        assert.equal(state.requests.length, 1);
      }, { initialURL: '/desktop/?__full=1', shell: true });

      for (const switcher of ['company', 'patient-company']) {
        await run(`${switcher} switcher submits the current route after soft navigation`, async ({ page, state, click, arrived }) => {
          await click('/patients/'); await arrived('/patients/');
          await page.locator(`#${switcher}-select`).selectOption('/companies/second/activate/');
          await page.waitForURL('https://meridian.test/companies/second/activate/');
          const submissions = state.documentRequests.filter(request => request.method === 'POST');
          assert.equal(submissions.length, 1);
          assert.equal(new URLSearchParams(submissions[0].body).get('next'), '/patients/');
        });
      }

      await run('deferred authentication redirects are processed before section HTML validation', async ({ page, state, click }) => {
        state.responses.set('/schedule/', { body: { html: workspace('/schedule/', '<section data-deferred-content data-content-section="secure">Waiting</section>') } });
        state.sections.set('secure', { body: { scope: undefined, html: undefined, redirect_url: '/accounts/login/?next=/schedule/' } });
        await click('/schedule/');
        await page.waitForURL('https://meridian.test/accounts/login/?next=/schedule/');
        assert.equal(state.documents.at(-1), '/accounts/login/?next=/schedule/');
      });

      await run('existing error toasts survive navigation and new server messages are appended', async ({ page, state, click, arrived }) => {
        state.responses.set('/schedule/', { body: { toast_html: '<div class="toast-stack"><div class="toast success" data-toast><span>New confirmation</span><button data-toast-close>Dismiss new confirmation</button></div></div>' } });
        await click('/schedule/'); await arrived('/schedule/');
        assert.equal(await page.locator('.toast-stack').count(), 1);
        assert.equal(await page.locator('[data-toast]').count(), 2);
        assert.ok((await page.locator('.toast-stack').innerText()).includes('Keep this server error'));
        state.responses.set('/patients/', { body: { toast_html: '' } });
        await click('/patients/'); await arrived('/patients/');
        assert.equal(await page.locator('[data-toast]').count(), 2);
        await page.getByRole('button', { name: 'Dismiss old error', exact: true }).click();
        assert.equal(await page.locator('[data-toast]').count(), 1);
      }, { withToasts: true });

      await run('persistent account controls and toast dismissal work during a held navigation', async ({ page, state, click, release, arrived }) => {
        state.held.add('/schedule/');
        await click('/schedule/');
        await eventually(() => state.releases.has('/schedule/'), 'held content while using account controls');
        await page.locator('[data-account-menu] summary').focus();
        await page.keyboard.press('ArrowDown');
        assert.equal(await page.locator('#profile-link').evaluate(node => document.activeElement === node), true);
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('[data-account-menu]').getAttribute('open'), null);
        await page.getByRole('button', { name: 'Dismiss old error', exact: true }).click();
        assert.equal(await page.locator('[data-toast]').count(), 0);
        release('/schedule/'); await arrived('/schedule/');
      }, { withToasts: true });

      await run('cancelled Back preserves an unmanaged earlier history entry', async ({ page, state, click, arrived }) => {
        await click('/schedule/'); await arrived('/schedule/');
        await page.goBack(); await arrived('/desktop/');
        await page.evaluate(() => history.replaceState(null, '', location.href));
        await page.goForward(); await arrived('/schedule/');
        let allowLeave = false;
        page.on('dialog', async dialog => { await (allowLeave ? dialog.accept() : dialog.dismiss()); });
        await page.locator('#save input').fill('Keep this draft');
        await page.goBack(); await page.waitForURL('https://meridian.test/schedule/');
        assert.equal(await page.locator('#save input').inputValue(), 'Keep this draft');
        allowLeave = true;
        await page.goBack(); await arrived('/desktop/');
        assert.equal(new URL(page.url()).pathname, '/desktop/');
        assert.equal(state.documents.length, 1);
      });

      await run('cancelled Forward to an entry without a position restores the draft on a fresh branch', async ({ page, state, click, arrived }) => {
        await click('/schedule/'); await arrived('/schedule/');
        await page.evaluate(() => history.replaceState({ meridianBranch: history.state.meridianBranch }, '', location.href));
        await page.goBack(); await arrived('/desktop/');
        const branch = await page.evaluate(() => history.state.meridianBranch);
        let allowLeave = false;
        page.on('dialog', dialog => allowLeave ? dialog.accept() : dialog.dismiss());
        await page.locator('#save input').fill('Draft stays on Home');
        await page.goForward(); await page.waitForURL('https://meridian.test/desktop/');
        assert.equal(await page.locator('#save input').inputValue(), 'Draft stays on Home');
        assert.notEqual(await page.evaluate(() => history.state.meridianBranch), branch);
        assert.equal(await page.evaluate(() => history.state.meridianPosition), 0);
        allowLeave = true;
        await page.goBack(); await arrived('/schedule/');
        await page.goBack(); await arrived('/desktop/');
        assert.equal(state.documents.length, 1);
      });

      await run('a cancelled multi-entry unknown jump resets offsets before later known Back traversal', async ({ page, state, click, arrived }) => {
        const initialLength = await page.evaluate(() => history.length);
        for (const path of ['/schedule/', '/patients/', '/messages/']) { await click(path); await arrived(path); }
        await page.evaluate(() => history.go(-3)); await arrived('/desktop/');
        await page.evaluate(() => history.replaceState({ meridianPosition: 0 }, '', location.href));
        await page.evaluate(() => history.go(3)); await arrived('/messages/');
        const branch = await page.evaluate(() => history.state.meridianBranch);
        let allowLeave = false;
        page.on('dialog', dialog => allowLeave ? dialog.accept() : dialog.dismiss());
        await page.locator('#save input').fill('Original message draft');
        await page.evaluate(() => history.go(-3));
        await page.waitForFunction(old => typeof history.state?.meridianBranch === 'string' && history.state.meridianBranch !== old
          && history.state.meridianPosition === 0, branch);
        await page.waitForURL('https://meridian.test/messages/');
        assert.equal(await page.locator('#save input').inputValue(), 'Original message draft');
        assert.notEqual(await page.evaluate(() => history.state.meridianBranch), branch);
        assert.equal(await page.evaluate(() => history.state.meridianPosition), 0);
        assert.equal(await page.evaluate(() => history.length), initialLength + 1, 'Recovery preserves the unknown entry and replaces its old forward branch');
        allowLeave = true;
        await click('/patients/'); await arrived('/patients/');
        assert.equal(await page.evaluate(() => history.state.meridianPosition), 1);
        allowLeave = false;
        await page.locator('#save input').fill('New branch draft');
        await page.goBack(); await page.waitForURL('https://meridian.test/patients/');
        assert.equal(await page.locator('#save input').inputValue(), 'New branch draft');
        allowLeave = true;
        await page.goBack(); await arrived('/messages/');
        await page.goBack(); await arrived('/desktop/');
        assert.equal(state.documents.length, 1);
      });

      await run('native hash entries end the old position branch before multi-step Back cancellation', async ({ page, state, click, arrived }) => {
        const initialBranch = await page.evaluate(() => history.state.meridianBranch);
        await page.locator('#hash').click();
        await page.waitForURL('https://meridian.test/desktop/#anchor');
        await page.waitForFunction(branch => typeof history.state?.meridianBranch === 'string' && history.state.meridianBranch !== branch, initialBranch);
        assert.equal(await page.evaluate(() => history.state.meridianPosition), 0);
        for (const path of ['/schedule/', '/patients/']) { await click(path); await arrived(path); }
        let prompts = 0, allowLeave = false;
        page.on('dialog', dialog => { prompts += 1; return allowLeave ? dialog.accept() : dialog.dismiss(); });
        await page.locator('#save input').fill('Do not lose this note across a hash entry');
        await page.evaluate(() => history.go(-3));
        await eventually(() => prompts === 1, 'one unsaved-changes prompt across the native hash entry');
        await page.waitForURL('https://meridian.test/patients/');
        assert.equal(prompts, 1, 'Unknown branch recovery must not guess offsets across native anchor entries');
        assert.equal(await page.locator('#save input').inputValue(), 'Do not lose this note across a hash entry');
        assert.equal(await page.evaluate(() => history.state.meridianPosition), 0);
        allowLeave = true;
        await page.goBack(); await arrived('/desktop/');
        assert.equal(new URL(page.url()).hash, '');
        assert.equal(state.documents.length, 1);
      });

      await run('an unknown duplicate-address entry reanchors before optional history saves', async ({ page, state, click, arrived }) => {
        for (const path of ['/schedule/', '/desktop/', '/desktop/']) { await click(path); await arrived(path); }
        await page.goBack();
        await page.waitForFunction(() => history.state?.meridianPosition === 2);
        await page.evaluate(() => history.replaceState({ meridianBranch: 'external-entry', meridianPosition: 100 }, '', location.href));
        await page.goForward();
        await page.waitForFunction(() => history.state?.meridianPosition === 3);
        const branch = await page.evaluate(() => history.state.meridianBranch);
        await page.locator('#save input').fill('Draft on the duplicate address');
        await page.goBack();
        await page.waitForFunction(old => history.state?.meridianPosition === 0 && history.state?.meridianBranch !== old
          && history.state?.meridianBranch !== 'external-entry', branch);
        const reanchored = await page.evaluate(() => history.state.meridianBranch);
        await page.evaluate(() => window.dispatchEvent(new Event('scroll')));
        await page.waitForTimeout(220);
        assert.equal(await page.evaluate(() => history.state.meridianBranch), reanchored);
        assert.equal(await page.evaluate(() => history.state.meridianPosition), 0);
        assert.equal(await page.locator('#save input').inputValue(), 'Draft on the duplicate address');
        assert.equal(state.documents.length, 1);
      });

      await run('failed unknown-history recovery gates a no-action POST until the address is restored', async ({ page, state, click, arrived }) => {
        await click('/schedule/'); await arrived('/schedule/');
        await page.goBack(); await arrived('/desktop/');
        await page.evaluate(() => history.replaceState(null, '', location.href));
        await page.goForward(); await arrived('/schedule/');
        page.on('dialog', dialog => dialog.dismiss());
        await page.locator('#save input').fill('Keep the editor and its POST target');
        await page.evaluate(() => {
          document.getElementById('save').removeAttribute('action');
          window.originalPushState = history.pushState.bind(history);
          history.pushState = () => { throw new DOMException('Temporary recovery quota', 'SecurityError'); };
        });
        await page.goBack();
        await page.locator('[data-history-recovery]').waitFor();
        assert.equal(new URL(page.url()).pathname, '/desktop/');
        assert.equal(await page.locator('#workspace-content').evaluate(node => node.inert), true);
        assert.equal(await page.locator('#save input').inputValue(), 'Keep the editor and its POST target');
        await page.evaluate(() => {
          document.getElementById('save').requestSubmit();
          MeridianNavigation.replaceUrl('/desktop/?wrong=1');
          window.dispatchEvent(new Event('scroll'));
        });
        await page.waitForTimeout(220);
        assert.equal(state.documentRequests.filter(request => request.method === 'POST').length, 0);
        assert.equal(await page.evaluate(() => history.state), null, 'The unknown destination must not be stamped while recovery is blocked');
        assert.equal(new URL(page.url()).search, '');
        await page.evaluate(() => { history.pushState = window.originalPushState; });
        await page.getByRole('button', { name: 'Return to editor', exact: true }).click();
        await page.waitForURL('https://meridian.test/schedule/');
        assert.equal(await page.locator('[data-history-recovery]').count(), 0);
        assert.equal(await page.locator('#workspace-content').evaluate(node => node.inert), false);
        assert.equal(await page.locator('#save input').inputValue(), 'Keep the editor and its POST target');
        await click('/patients/');
        assert.equal(new URL(page.url()).pathname, '/schedule/', 'Restoring controllers must not erase dirty-form baselines');
        await page.locator('#save button').click();
        await eventually(() => state.documentRequests.some(request => request.method === 'POST'), 'native editor POST after recovery');
        const submissions = state.documentRequests.filter(request => request.method === 'POST');
        assert.equal(submissions.length, 1);
        assert.equal(submissions[0].url, '/schedule/');
        assert.equal(new URLSearchParams(submissions[0].body).get('body'), 'Keep the editor and its POST target');
      });

      await run('only the immutable initial HTML declaration controls interception', async ({ page, state, click, arrived }) => {
        const eligibility = await page.evaluate(() => {
          const link = document.createElement('a'); link.href = '/blood-tests/5/report/';
          return MeridianNavigation.canHandleLink(link);
        });
        assert.equal(eligibility, false, 'An unregistered attachment URL must stay native even without an author flag');
        state.responses.set('/schedule/', { body: { navigation_paths: ['unanchored'] } });
        await click('/schedule/');
        await arrived('/schedule/');
        assert.equal(await page.evaluate(() => MeridianNavigation.canHandleLink(document.querySelector('[data-destination="/patients/"]'))), true);
        await click('/patients/'); await arrived('/patients/');
        assert.equal(state.documents.length, 1);
      });

      await run('declared route expressions compile once and survive content responses that omit them', async ({ page, state, click, arrived }) => {
        const initial = await page.evaluate(() => window.regexConstructions);
        assert.equal(initial, navigationPaths.length);
        await page.evaluate(() => { for (let i = 0; i < 100; i += 1) MeridianNavigation.canHandleLink(document.querySelector('[data-destination="/patients/"]')); });
        await click('/schedule/'); await arrived('/schedule/');
        await click('/patients/'); await arrived('/patients/');
        assert.equal(await page.evaluate(() => window.regexConstructions), initial);
        assert.equal(state.documents.length, 1);
      }, { countRegexes: true });

      for (const configPaths of [undefined, [], ['unanchored'], ['^[$']]) {
        await run(`malformed initial route declarations fail closed: ${JSON.stringify(configPaths)}`, async ({ page, state, click }) => {
          assert.equal(await page.evaluate(() => typeof MeridianNavigation), 'undefined');
          await click('/schedule/');
          await page.waitForURL('https://meridian.test/schedule/');
          assert.equal(state.documents.length, 2);
          assert.equal(state.requests.length, 0);
        }, { configPaths: configPaths === undefined ? null : configPaths, expectNavigation: false });
      }

      await run('incoming calls remain connected and visible while page requests are pending', async ({ page, state, click, release, arrived }) => {
        await page.waitForFunction(() => testSockets.length === 1);
        state.held.add('/schedule/');
        await click('/schedule/');
        await eventually(() => state.releases.has('/schedule/'), 'held content request for invitation');
        await page.evaluate(() => testSockets[0].onmessage({ data: JSON.stringify({ type: 'incoming_call', appointment_id: 42, caller_name: 'Doctor', practice_name: 'Practice' }) }));
        assert.equal(await page.locator('#incoming-call').isVisible(), true);
        release('/schedule/'); await arrived('/schedule/');
        assert.equal(await page.locator('#incoming-call').isVisible(), true);
        await click('/patients/'); await arrived('/patients/');
        assert.equal(await page.locator('#incoming-call').isVisible(), true);
        assert.equal(await page.evaluate(() => testSockets.length), 1);
        assert.equal(await page.evaluate(() => testSockets[0].closed), false);
        await page.locator('#incoming-call-dismiss').click();
        await page.evaluate(() => testSockets[0].onmessage({ data: JSON.stringify({ type: 'incoming_call', appointment_id: 42, caller_name: 'Repeat', practice_name: 'Practice' }) }));
        assert.equal(await page.locator('#incoming-call').isVisible(), false, 'Deduplication survives page transitions');
      }, { withNotifications: true });

      await run('scope change reloads before showing the response content', async ({ page, state, click }) => {
        state.responses.set('/schedule/', { status: 409, body: { scope: 'staff:9:2:5:doctor:', error: 'Practice changed.' } });
        await click('/schedule/');
        await eventually(() => state.documents.length > 1, 'scope mismatch full navigation');
        assert.ok(state.documents.at(-1).startsWith('/schedule/'));
        assert.equal(await page.locator('[data-screen="/schedule/"]').count(), 0);
      });

      await run('direct shell hydrates with authorized content', async ({ page, state, release, arrived }) => {
        await eventually(() => state.releases.has('/desktop/'), 'initial shell content request');
        assert.equal(new URL(page.url()).pathname, '/desktop/');
        release('/desktop/'); await arrived('/desktop/');
        assert.equal(await page.locator('.console-content').innerText().then(text => text.includes('Initial shell')), false);
        assert.equal(state.documents.length, 1);
      }, { shell: true });

      await run('Back supersedes a pending destination and ignores its late response', async ({ page, state, click, release, arrived }) => {
        state.held.add('/schedule/');
        await click('/schedule/');
        await eventually(() => state.releases.has('/schedule/'), 'pending Calendar before Back');
        await page.goBack(); await arrived('/desktop/');
        release('/schedule/'); await page.waitForTimeout(80);
        assert.equal(new URL(page.url()).pathname, '/desktop/');
        assert.equal(await page.locator('[data-navigation-loading], [data-navigation-error]').count(), 0);
        assert.ok((await page.evaluate(() => aborts)).some(url => url.includes('/schedule/')));
        assert.equal(state.documents.length, 1);
      });

      await run('Back after an error restores authorized content and mounted controls', async ({ page, state, click, arrived }) => {
        state.responses.set('/broken/', { status: 500, body: { error: 'Unavailable' } });
        await click('/broken/'); await page.locator('[data-navigation-error]').waitFor();
        await page.goBack(); await arrived('/desktop/');
        assert.equal(await page.locator('[data-navigation-error]').count(), 0);
        await page.locator('#owned-button').click();
        assert.equal(await page.evaluate(() => ownedClicks), 1);
        assert.equal(state.documents.length, 1);
      });

      await run('cancelled navigation keeps an edited form and its native POST still submits once', async ({ page, state, click }) => {
        const prompts = [];
        page.on('dialog', async dialog => { prompts.push(dialog.message()); await dialog.dismiss(); });
        await page.locator('#save input').fill('Unsent clinical note');
        const requests = state.requests.length;
        const disposals = await page.evaluate(() => window.disposals || 0);
        await click('/schedule/');
        assert.equal(prompts.length, 1);
        assert.equal(new URL(page.url()).pathname, '/desktop/');
        assert.equal(await page.locator('#save input').inputValue(), 'Unsent clinical note');
        assert.equal(await page.locator('[data-navigation-loading]').count(), 0);
        assert.equal(state.requests.length, requests);
        assert.equal(await page.evaluate(() => window.disposals || 0), disposals);
        await page.locator('#save button').click();
        await page.waitForURL('https://meridian.test/save/');
        assert.equal(prompts.length, 1, 'Saving the edited form must not ask to discard it');
        const submissions = state.documentRequests.filter(request => request.method === 'POST');
        assert.equal(submissions.length, 1);
        assert.equal(new URLSearchParams(submissions[0].body).get('body'), 'Unsent clinical note');
      }, { withWorkspace: true });

      await run('cancelled Back keeps the edited page and restores its original history entry', async ({ page, state, click, arrived }) => {
        await click('/schedule/'); await arrived('/schedule/');
        await click('/patients/'); await arrived('/patients/');
        const prompts = [];
        let allowLeave = false;
        page.on('dialog', async dialog => { prompts.push(dialog.message()); await (allowLeave ? dialog.accept() : dialog.dismiss()); });
        await page.locator('#save input').fill('Keep this draft');
        const position = await page.evaluate(() => history.state.meridianPosition);
        const requests = state.requests.length;
        await page.goBack();
        await page.waitForURL('https://meridian.test/patients/');
        assert.equal(prompts.length, 1);
        assert.equal(await page.locator('#save input').inputValue(), 'Keep this draft');
        assert.equal(await page.evaluate(() => history.state.meridianPosition), position);
        assert.equal(state.requests.length, requests);
        allowLeave = true;
        await page.goBack(); await arrived('/schedule/');
        assert.equal(prompts.length, 2);
        await page.goForward(); await arrived('/patients/');
        assert.equal(await page.locator('#save input').inputValue(), 'preserved');
        assert.equal(state.documents.length, 1);
      });

      await run('a newer destination waits for shared pending CSS and preserves server order', async ({ page, state, click, release, arrived }) => {
        const shared = '/static/css/navigation-shared.css', theme = '/static/css/navigation-theme.css';
        state.assets.set(shared, { contentType: 'text/css', body: '#owned-button { color: rgb(0, 128, 0); }' });
        state.assets.set(theme, { contentType: 'text/css', body: '#owned-button { color: rgb(0, 0, 255); }' });
        state.held.add(`asset:${shared}`);
        state.responses.set('/schedule/', { body: { assets: { styles: [shared], scripts: [] } } });
        state.responses.set('/patients/', { body: { assets: { styles: [theme, shared], scripts: [] } } });
        await click('/schedule/');
        await eventually(() => state.releases.has(`asset:${shared}`), 'shared stylesheet request');
        await click('/patients/');
        await eventually(() => state.assetRequests.includes(theme), 'second page theme request');
        await page.waitForTimeout(60);
        assert.equal(await page.locator('[data-navigation-loading]').count(), 1);
        assert.equal(await page.locator('[data-screen="/patients/"]').count(), 0);
        assert.equal(state.assetRequests.filter(path => path === shared).length, 1);
        release(`asset:${shared}`); await arrived('/patients/');
        assert.equal(await page.locator('#owned-button').evaluate(node => getComputedStyle(node).color), 'rgb(0, 128, 0)');
        assert.deepEqual(await page.locator('link[rel="stylesheet"]').evaluateAll(links => links.filter(link => link.media !== 'not all').map(link => new URL(link.href).pathname)), [theme, shared]);
      });

      await run('obsolete styles cannot change the newer destination after arriving late', async ({ page, state, click, release, arrived }) => {
        const oldStyle = '/static/css/navigation-old.css', newStyle = '/static/css/navigation-new.css';
        state.assets.set(oldStyle, { contentType: 'text/css', body: '#owned-button { color: rgb(255, 0, 0); }' });
        state.assets.set(newStyle, { contentType: 'text/css', body: '#owned-button { color: rgb(0, 0, 255); }' });
        state.held.add(`asset:${oldStyle}`);
        state.responses.set('/schedule/', { body: { assets: { styles: [oldStyle], scripts: [] } } });
        state.responses.set('/patients/', { body: { assets: { styles: [newStyle], scripts: [] } } });
        await click('/schedule/');
        await eventually(() => state.releases.has(`asset:${oldStyle}`), 'obsolete stylesheet request');
        await click('/patients/'); await arrived('/patients/');
        release(`asset:${oldStyle}`); await page.waitForTimeout(80);
        assert.equal(new URL(page.url()).pathname, '/patients/');
        assert.equal(await page.locator('#owned-button').evaluate(node => getComputedStyle(node).color), 'rgb(0, 0, 255)');
        assert.equal(await page.locator(`link[href$="${oldStyle}"]`).getAttribute('media'), 'not all');
      });

      await run('stylesheets already in server order are not reattached on later pages', async ({ page, state, click, arrived }) => {
        const styles = ['/static/css/navigation-shared.css', '/static/css/navigation-theme.css'];
        for (const path of styles) state.assets.set(path, { contentType: 'text/css', body: '#owned-button { color: blue; }' });
        for (const path of ['/schedule/', '/patients/']) state.responses.set(path, { body: { assets: { styles, scripts: [] } } });
        await click('/schedule/'); await arrived('/schedule/');
        await page.waitForTimeout(80);
        const requests = state.assetRequests.length;
        await page.evaluate(() => {
          window.stylesheetMoves = 0;
          new MutationObserver(records => { for (const record of records) for (const node of [...record.addedNodes, ...record.removedNodes]) {
            if (node.nodeType === 1 && node.matches('link[rel="stylesheet"]')) window.stylesheetMoves += 1;
          } }).observe(document.head, { childList: true });
        });
        await click('/patients/'); await arrived('/patients/');
        assert.equal(await page.evaluate(() => window.stylesheetMoves), 0);
        assert.equal(state.assetRequests.length, requests);
      });

      for (const kind of ['stylesheet', 'script']) {
        await run(`a stalled ${kind} reaches a retryable error instead of hanging`, async ({ page, state, click, release, arrived }) => {
          const asset = kind === 'stylesheet' ? '/static/css/navigation-slow.css' : '/static/js/navigation-slow.js';
          state.assets.set(asset, { contentType: kind === 'stylesheet' ? 'text/css' : 'application/javascript', body: kind === 'stylesheet' ? '#owned-button { color: blue; }' : 'window.slowScriptArrived = true;' });
          state.held.add(`asset:${asset}`);
          state.responses.set('/schedule/', { body: { assets: { styles: kind === 'stylesheet' ? [asset] : [], scripts: kind === 'script' ? [asset] : [] } } });
          await page.clock.install();
          await click('/schedule/');
          await eventually(() => state.releases.has(`asset:${asset}`), `stalled ${kind} request`);
          await page.clock.fastForward(30001);
          await page.locator('[data-navigation-error]').waitFor();
          assert.equal(new URL(page.url()).pathname, '/schedule/');
          assert.equal(await page.locator('[data-navigation-retry]').count(), 1);
          release(`asset:${asset}`);
          state.responses.delete('/schedule/');
          await page.locator('[data-navigation-retry]').click(); await arrived('/schedule/');
          assert.equal(await page.locator('[data-navigation-error]').count(), 0);
        });
      }

      await run('restored cached pages reauthorize before remounting even with an edited form', async ({ page, state, release, arrived }) => {
        const prompts = [];
        page.on('dialog', async dialog => { prompts.push(dialog.message()); await dialog.dismiss(); });
        await page.locator('#save input').fill('Cached sensitive draft');
        state.held.add('/desktop/');
        state.responses.set('/desktop/', { body: { html: workspace('/desktop/', 'Fresh authorized content') } });
        const mounts = await page.evaluate(() => window.mounts);
        const immediate = await page.evaluate(() => {
          window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }));
          window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
          return { loading: !!document.querySelector('[data-navigation-loading]'),
            oldContent: document.getElementById('workspace-content').textContent.includes('Old private content'),
            oldDraft: !!document.querySelector('#save input') };
        });
        assert.equal(prompts.length, 0);
        assert.deepEqual(immediate, { loading: true, oldContent: false, oldDraft: false });
        await eventually(() => state.releases.has('/desktop/'), 'authorization refresh after cached restoration');
        assert.equal(await page.evaluate(() => window.mounts), mounts);
        assert.equal(state.requests.at(-1).headers['x-meridian-navigation-scope'], scope);
        release('/desktop/'); await arrived('/desktop/');
        assert.equal(await page.evaluate(() => window.mounts), mounts + 1);
        assert.ok((await page.locator('.console-content').innerText()).includes('Fresh authorized content'));
      });

      await run('forms appear only after shared pending scripts register and mount on the destination', async ({ page, state, click, release, arrived }) => {
        const asset = '/static/js/navigation-late-controller.js';
        state.assets.set(asset, { contentType: 'application/javascript', body: `
          window.registeredWhileLoading = !!document.querySelector('[data-navigation-loading]');
          MeridianPages.register('late-controller', lifecycle => {
            window.lateControllerMounts = (window.lateControllerMounts || 0) + 1;
            window.lateControllerPage = document.querySelector('[data-screen]')?.dataset.screen;
            lifecycle.on(document.getElementById('owned-button'), 'click', () => { window.lateOwnedClicks = (window.lateOwnedClicks || 0) + 1; });
          });` });
        state.held.add(`asset:${asset}`);
        for (const path of ['/schedule/', '/patients/']) state.responses.set(path, { body: { assets: { styles: [], scripts: [asset] } } });
        await click('/schedule/');
        await eventually(() => state.releases.has(`asset:${asset}`), 'new controller script request');
        await click('/patients/');
        await eventually(() => state.requests.some(request => request.url === '/patients/'), 'newer destination response');
        await page.waitForTimeout(60);
        assert.equal(await page.locator('[data-navigation-loading]').count(), 1);
        assert.equal(await page.locator('#save').count(), 0, 'Uninitialized forms must not accept edits');
        assert.equal(await page.evaluate(() => window.lateControllerMounts || 0), 0);
        assert.equal(state.assetRequests.filter(path => path === asset).length, 1);
        release(`asset:${asset}`); await arrived('/patients/');
        assert.equal(await page.evaluate(() => window.registeredWhileLoading), true);
        assert.equal(await page.evaluate(() => window.lateControllerMounts), 1);
        assert.equal(await page.evaluate(() => window.lateControllerPage), '/patients/');
        await page.locator('#owned-button').click();
        assert.equal(await page.evaluate(() => window.lateOwnedClicks), 1);
        const prompts = [];
        page.on('dialog', async dialog => { prompts.push(dialog.message()); await dialog.dismiss(); });
        await page.locator('#save input').fill('Draft entered after initialization');
        await click('/schedule/');
        assert.equal(prompts.length, 1);
        assert.equal(new URL(page.url()).pathname, '/patients/');
        assert.equal(await page.locator('#save input').inputValue(), 'Draft entered after initialization');
      });
    } finally { await browser.close(); }
  }
  console.log(JSON.stringify({ navigationCases: cases }));
})().catch(error => { console.error(error); process.exitCode = 1; });
