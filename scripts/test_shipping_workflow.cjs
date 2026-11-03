/* Local browser checks for stage navigation. Fixtures never change real shipments. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium, webkit } = require(process.env.MERIDIAN_PLAYWRIGHT_PATH || 'playwright');

const steps = ['pending', 'confirmed', 'tracking'];
const actions = { pending: 'Confirm & lock selected', confirmed: 'Book courier', tracking: 'Record collection' };
const stageRows = {
  pending: ['Draft parcel A', 'Held parcel B', 'Ready parcel C'],
  confirmed: ['Locked parcel D', 'Locked parcel E'], tracking: ['Booked parcel F'],
};
function fixture({ selected = 'pending', malformed = false, lifecycle = true } = {}) {
  const rows = selected === 'all' ? Object.values(stageRows).flat() : stageRows[selected];
  return `<!doctype html><html><head><meta charset="utf-8"><title>Shipping fixture</title>
  <link rel="icon" href="data:,">
  ${lifecycle ? '<script defer src="/assets/page_lifecycle.js"></script>' : ''}
  <script defer src="/assets/shipping_workflow.js"></script></head><body>
  <main><input type="hidden" name="legacy_step" data-shipping-step-field value="${selected}">
  <section id="shipping-workflow" data-shipping-workflow>
    <form method="get" action="/shipping/" data-conversation-search>
      <input type="hidden" name="shipping_step" value="${selected}">
    </form>
    <ol aria-label="Shipping steps" data-shipping-tabs>${steps.map(step => `<li><a
      id="shipping-tab-${step}" data-shipping-step="${step}" aria-controls="shipping-panel-${step}"
      aria-selected="${step === selected}" href="/shipping/?week=2026-10-26&amp;shipping_step=${step}#shipping-workflow">${step}</a></li>`).join('')}</ol>
    ${steps.map(step => `<section id="shipping-panel-${step}" data-shipping-panel="${malformed && step === 'tracking' ? 'invalid' : step}"
      ${step === selected ? '' : 'hidden'}><p>${step} guidance</p><form method="post" action="/shipping/action/${step}/">
      <input name="note" aria-label="${step} note"><button type="submit">${actions[step]}</button>
    </form></section>`).join('')}
    <table aria-label="Stage parcels"><tbody>${rows.map(row => `<tr><td>${row}</td></tr>`).join('')}</tbody></table>
  </section></main></body></html>`;
}

async function selected(page, expected) {
  await page.locator('[data-shipping-workflow]').waitFor();
  const current = page.locator('[data-shipping-step][aria-selected="true"]');
  if (expected === 'all') assert.equal(await current.count(), 0);
  else assert.equal(await current.getAttribute('data-shipping-step'), expected);
  assert.deepEqual(await page.locator('[data-shipping-panel]:not([hidden])').evaluateAll(items => items.map(item => item.dataset.shippingPanel)), expected === 'all' ? [] : [expected]);
  assert.deepEqual(await page.getByRole('table', { name: 'Stage parcels' }).locator('td').allTextContents(), expected === 'all' ? Object.values(stageRows).flat() : stageRows[expected]);
}

(async () => {
  let scenarios = 0;
  for (const [name, engine, options] of [['Chrome', chromium, { channel: 'chrome' }], ['WebKit', webkit, {}]]) {
    const browser = await engine.launch({ ...options, headless: true });
    try {
      for (const lifecycle of [true, false]) {
        const errors = [], requests = [];
        const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
        page.on('pageerror', error => errors.push(error.message));
        await page.route('http://meridian.test/**', route => {
          const request = route.request(), url = new URL(request.url());
          if (url.pathname.startsWith('/assets/')) return route.fulfill({
            contentType: 'application/javascript', body: fs.readFileSync(`static/js/${url.pathname.split('/').pop()}`, 'utf8'),
          });
          if (!request.isNavigationRequest()) return route.fulfill({ status: 204, body: '' });
          requests.push({ method: request.method(), url: request.url(), body: request.postData() });
          const candidate = url.searchParams.get('shipping_step') || (url.pathname.startsWith('/shipping/action/') ? url.pathname.split('/').filter(Boolean).at(-1) : 'pending');
          const step = [...steps, 'all'].includes(candidate) ? candidate : 'pending';
          return route.fulfill({ contentType: 'text/html', body: fixture({ selected: step, lifecycle, malformed: url.searchParams.has('malformed') }) });
        });
        await page.goto('http://meridian.test/shipping/new/?week=2026-10-26&shipping_step=confirmed');
        await selected(page, 'confirmed');
        assert.equal(await page.getByRole('tablist').count(), 1);
        assert.equal(await page.getByRole('tab').count(), 3);
        assert.equal(await page.locator('[role="tabpanel"]').count(), 3);
        assert.equal(await page.locator('[role="tablist"] > [role="presentation"]').count(), 3);
        for (const step of steps) {
          assert.equal(await page.locator(`#shipping-panel-${step}`).getAttribute('aria-labelledby'), `shipping-tab-${step}`);
          const count = requests.length;
          await Promise.all([
            page.waitForURL(url => url.pathname === '/shipping/' && url.searchParams.get('shipping_step') === step),
            page.locator(`#shipping-tab-${step}`).click(),
          ]);
          await selected(page, step);
          assert.equal(requests.length, count + 1, 'Stage selection must fetch its server-filtered table');
          assert.equal(requests.at(-1).method, 'GET', 'A stage click must not perform work');
          assert.equal(await page.locator('[data-shipping-step-field]').inputValue(), step);
          assert.equal(await page.locator('input[name="shipping_step"]').inputValue(), step);
        }
        scenarios++;

        await page.evaluate(() => {
          history.replaceState(history.state, '', '/shipping/new/?week=2026-10-26&q=Jayms&status=ready&status=held&product=11&product=12&doctor=21&doctor=22&payment=unpaid&view=medication&page=3&create=1&collect=1&__shell=1');
          document.dispatchEvent(new CustomEvent('messages:searched'));
        });
        const countBeforeLinks = requests.length;
        const hrefs = await page.locator('[data-shipping-step]').evaluateAll(items => items.map(item => item.href));
        hrefs.forEach((href, index) => {
          const url = new URL(href);
          assert.equal(url.pathname, '/shipping/');
          assert.equal(url.searchParams.get('shipping_step'), steps[index]);
          assert.equal(url.searchParams.get('q'), 'Jayms');
          assert.deepEqual(url.searchParams.getAll('status'), ['ready', 'held']);
          assert.deepEqual(url.searchParams.getAll('product'), ['11', '12']);
          assert.deepEqual(url.searchParams.getAll('doctor'), ['21', '22']);
          assert.equal(url.searchParams.get('payment'), 'unpaid');
          assert.equal(url.searchParams.get('view'), 'medication');
          assert.equal(url.searchParams.get('week'), '2026-10-26');
          assert.equal(url.searchParams.get('__full'), '1');
          for (const key of ['page', 'create', 'collect', '__shell']) assert.equal(url.searchParams.get(key), null);
          assert.equal(url.hash, '#shipping-workflow');
        });
        assert.equal(requests.length, countBeforeLinks, 'Updating fallback links must not fetch');
        scenarios++;

        await page.evaluate(() => {
          history.replaceState(history.state, '', '/shipping/new/');
          document.dispatchEvent(new CustomEvent('messages:searched'));
        });
        const validationFallback = new URL(await page.locator('#shipping-tab-confirmed').getAttribute('href'));
        assert.equal(validationFallback.pathname, '/shipping/');
        assert.equal(validationFallback.searchParams.get('week'), '2026-10-26');
        assert.equal(validationFallback.searchParams.get('shipping_step'), 'confirmed');
        scenarios++;

        await page.goto('http://meridian.test/shipping/?week=2026-10-26&shipping_step=pending');
        await page.getByRole('textbox', { name: 'pending note' }).fill('Unsent parcel note');
        const beforeKeys = requests.length;
        await page.locator('#shipping-tab-pending').focus();
        await page.keyboard.press('ArrowLeft');
        assert.equal(await page.evaluate(() => document.activeElement.dataset.shippingStep), 'tracking');
        await selected(page, 'pending');
        assert.equal(await page.getByRole('textbox', { name: 'pending note' }).inputValue(), 'Unsent parcel note');
        assert.equal(requests.length, beforeKeys, 'Arrow keys move focus without navigating or submitting drafts');
        await Promise.all([page.waitForURL(url => url.pathname === '/shipping/' && url.searchParams.get('shipping_step') === 'tracking'), page.keyboard.press('Enter')]);
        await selected(page, 'tracking');
        await page.locator('#shipping-tab-tracking').focus();
        await page.keyboard.press('ArrowRight');
        assert.equal(await page.evaluate(() => document.activeElement.dataset.shippingStep), 'pending');
        await selected(page, 'tracking');
        await Promise.all([page.waitForURL(url => url.pathname === '/shipping/' && url.searchParams.get('shipping_step') === 'pending'), page.keyboard.press('Space')]);
        await selected(page, 'pending');
        await page.locator('#shipping-tab-pending').focus();
        await page.keyboard.press('End');
        assert.equal(await page.evaluate(() => document.activeElement.dataset.shippingStep), 'tracking');
        await page.keyboard.press('Home');
        assert.equal(await page.evaluate(() => document.activeElement.dataset.shippingStep), 'pending');
        assert.equal(await page.locator('[data-shipping-step][tabindex="0"]').count(), 1);
        await page.keyboard.press('Tab');
        assert.equal(await page.evaluate(() => document.activeElement.id), 'shipping-panel-pending');
        scenarios++;

        const nativeClicks = await page.evaluate(() => {
          const workflow = document.querySelector('[data-shipping-workflow]');
          const prevented = [];
          const observe = event => { prevented.push(event.defaultPrevented); event.preventDefault(); };
          workflow.addEventListener('click', observe);
          for (const modifiers of [{}, { ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }]) {
            document.querySelector('#shipping-tab-confirmed').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, ...modifiers }));
          }
          workflow.removeEventListener('click', observe);
          return prevented;
        });
        assert.deepEqual(nativeClicks, [false, false, false, false, false, false]);
        await selected(page, 'pending');
        scenarios++;

        // A cached controller must not install another Space handler or submit two requests.
        await page.addScriptTag({ path: 'static/js/shipping_workflow.js' });
        async function spaceClicks() {
          return page.evaluate(() => {
            const workflow = document.querySelector('[data-shipping-workflow]');
            let count = 0;
            const observe = event => { count++; event.preventDefault(); };
            workflow.addEventListener('click', observe);
            document.querySelector('#shipping-tab-confirmed').dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true }));
            workflow.removeEventListener('click', observe);
            return count;
          });
        }
        assert.equal(await spaceClicks(), 1);
        scenarios++;
        if (lifecycle) {
          await page.evaluate(() => MeridianPages.dispose());
          assert.equal(await page.locator('[data-shipping-workflow]').evaluate(node => !!node.meridianShippingWorkflow), false);
          assert.equal(await spaceClicks(), 0);
          await page.evaluate(() => MeridianPages.mount());
          assert.equal(await spaceClicks(), 1);
          await selected(page, 'pending');
          scenarios++;
        }

        await page.goto('http://meridian.test/shipping/?shipping_step=confirmed');
        await page.getByRole('textbox', { name: 'confirmed note' }).fill('Confirmation stage note');
        await Promise.all([
          page.waitForURL('**/shipping/action/confirmed/'),
          page.getByRole('button', { name: 'Book courier', exact: true }).click(),
        ]);
        const posts = requests.filter(request => request.method === 'POST');
        assert.equal(posts.length, 1);
        assert.equal(new URL(posts[0].url).pathname, '/shipping/action/confirmed/');
        assert.match(posts[0].body, /note=Confirmation\+stage\+note/);
        scenarios++;

        await page.goto('http://meridian.test/shipping/?shipping_step=all');
        await selected(page, 'all');
        assert.equal(await page.locator('[data-shipping-workflow]').evaluate(node => !!node.meridianShippingWorkflow), false);
        await page.locator('#shipping-tab-confirmed').click();
        await page.waitForURL(url => url.pathname === '/shipping/' && url.searchParams.get('shipping_step') === 'confirmed');
        await selected(page, 'confirmed');
        scenarios++;

        await page.goto('http://meridian.test/shipping/?malformed=1');
        assert.equal(await page.locator('[role="tablist"]').count(), 0, 'Incomplete markup must retain native fallback');
        assert.equal(await page.locator('[data-shipping-workflow]').evaluate(node => !!node.meridianShippingWorkflow), false);
        assert.equal(await page.locator('#shipping-tab-confirmed').getAttribute('href'), '/shipping/?week=2026-10-26&shipping_step=confirmed#shipping-workflow');
        scenarios++;
        assert.deepEqual(errors, []);
        await page.close();
      }

      const noScript = await browser.newPage({ javaScriptEnabled: false });
      await noScript.route('http://meridian.test/**', route => {
        const step = new URL(route.request().url()).searchParams.get('shipping_step') || 'pending';
        return route.fulfill({ contentType: 'text/html', body: fixture({ selected: step }) });
      });
      await noScript.goto('http://meridian.test/shipping/?week=2026-10-26');
      await noScript.locator('#shipping-tab-confirmed').click();
      await noScript.waitForURL(url => url.pathname === '/shipping/' && url.searchParams.get('shipping_step') === 'confirmed');
      await selected(noScript, 'confirmed');
      assert.equal(new URL(noScript.url()).searchParams.get('week'), '2026-10-26');
      await noScript.close();
      scenarios++;
      console.log(`${name}: server-filtered stage navigation, keyboard, filters, lifecycle, POST separation and native fallbacks passed.`);
    } finally { await browser.close(); }
  }
  console.log(`${scenarios} shipping workflow scenarios passed.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
