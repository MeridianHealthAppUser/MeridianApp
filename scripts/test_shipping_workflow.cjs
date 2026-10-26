/* Local browser checks for the shipping tab strip. No real shipments are created. */
const assert = require('node:assert/strict');
const { chromium, webkit } = require(process.env.MERIDIAN_PLAYWRIGHT_PATH || 'playwright');

const steps = ['ready', 'lock', 'order', 'courier', 'collection'];
function fixture({ selected = 'ready', malformed = false } = {}) {
  return `<!doctype html><html><head><meta charset="utf-8"><title>Shipping fixture</title></head><body>
  <main><input type="hidden" name="legacy_step" data-shipping-step-field value="${selected}">
  <section id="shipping-workflow" data-shipping-workflow>
    <form method="get" action="/shipping/new/" data-conversation-search>
      <input type="hidden" name="shipping_step" value="${selected}">
    </form>
    <ol aria-label="Shipping steps" data-shipping-tabs>${steps.map(step => `<li><a
      id="shipping-tab-${step}" data-shipping-step="${step}" aria-controls="shipping-panel-${step}"
      aria-selected="${step === selected}" href="/shipping/?week=2026-10-26&amp;shipping_step=${step}#shipping-workflow">${step}</a></li>`).join('')}</ol>
    ${steps.map(step => `<section id="shipping-panel-${step}" data-shipping-panel="${malformed && step === 'collection' ? 'order' : step}"
      ${step === selected ? '' : 'hidden'}><p>${step} guidance</p><form method="post" action="/shipping/action/${step}/">
      <input name="note" aria-label="${step} note"><button type="submit">Confirm ${step}</button>
    </form></section>`).join('')}
  </section></main></body></html>`;
}

async function selected(page, expected) {
  assert.equal(await page.locator('[data-shipping-step][aria-selected="true"]').getAttribute('data-shipping-step'), expected);
  assert.deepEqual(await page.locator('[data-shipping-panel]:not([hidden])').evaluateAll(items => items.map(item => item.dataset.shippingPanel)), [expected]);
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
          requests.push({ method: route.request().method(), url: route.request().url(), body: route.request().postData() });
          return route.fulfill({ contentType: 'text/html', body: fixture({ selected: 'lock' }) });
        });
        await page.goto('http://meridian.test/shipping/new/?week=2026-10-26');
        if (lifecycle) await page.addScriptTag({ path: 'static/js/page_lifecycle.js' });
        await page.addScriptTag({ path: 'static/js/shipping_workflow.js' });
        await selected(page, 'lock');
        assert.equal(await page.getByRole('tablist').count(), 1);
        assert.equal(await page.getByRole('tab').count(), 5);
        assert.equal(await page.locator('[role="tabpanel"]').count(), 5);
        assert.equal(await page.locator('[role="tablist"] > [role="presentation"]').count(), 5);
        for (const step of steps) {
          assert.equal(await page.locator(`#shipping-panel-${step}`).getAttribute('aria-labelledby'), `shipping-tab-${step}`);
          await page.locator(`#shipping-tab-${step}`).click();
          await selected(page, step);
          assert.equal(await page.evaluate(() => document.activeElement.dataset.shippingStep), step);
          assert.equal(await page.locator('[data-shipping-step-field]').inputValue(), step);
          assert.equal(await page.locator('input[name="shipping_step"]').inputValue(), step);
        }
        scenarios++;

        await page.evaluate(() => {
          history.replaceState(history.state, '', '/shipping/new/?week=2026-10-26&q=Jayms&status=ready&create=1&collect=1&__shell=1');
          document.dispatchEvent(new CustomEvent('messages:searched'));
        });
        const hrefs = await page.locator('[data-shipping-step]').evaluateAll(items => items.map(item => item.href));
        hrefs.forEach((href, index) => {
          const url = new URL(href);
          assert.equal(url.searchParams.get('shipping_step'), steps[index]);
          assert.equal(url.pathname, '/shipping/');
          assert.equal(url.searchParams.get('q'), 'Jayms');
          assert.equal(url.searchParams.get('status'), 'ready');
          assert.equal(url.searchParams.get('week'), '2026-10-26');
          assert.equal(url.searchParams.get('__full'), '1');
          assert.equal(url.searchParams.get('create'), null);
          assert.equal(url.searchParams.get('collect'), null);
          assert.equal(url.searchParams.get('__shell'), null);
          assert.equal(url.hash, '#shipping-workflow');
        });
        assert.equal(requests.length, 1);
        scenarios++;

        await page.evaluate(() => {
          history.replaceState(history.state, '', '/shipping/new/');
          document.dispatchEvent(new CustomEvent('messages:searched'));
        });
        const validationFallback = new URL(await page.locator('#shipping-tab-lock').getAttribute('href'));
        assert.equal(validationFallback.pathname, '/shipping/');
        assert.equal(validationFallback.searchParams.get('week'), '2026-10-26');
        assert.equal(validationFallback.searchParams.get('shipping_step'), 'lock');
        assert.equal(requests.length, 1);
        scenarios++;

        await page.locator('#shipping-tab-ready').click();
        await page.getByRole('textbox', { name: 'ready note' }).fill('Unsent parcel note');
        for (const step of steps.slice(1)) await page.locator(`#shipping-tab-${step}`).click();
        await page.locator('#shipping-tab-ready').click();
        assert.equal(await page.getByRole('textbox', { name: 'ready note' }).inputValue(), 'Unsent parcel note');
        assert.equal(requests.length, 1, 'Selecting tabs must not fetch, navigate, or submit a form');
        assert.equal(new URL(page.url()).searchParams.get('shipping_step'), null);
        scenarios++;

        await page.locator('#shipping-tab-ready').focus();
        await page.keyboard.press('ArrowLeft');
        assert.equal(await page.evaluate(() => document.activeElement.dataset.shippingStep), 'collection');
        await selected(page, 'ready');
        await page.keyboard.press('Enter');
        await selected(page, 'collection');
        await page.keyboard.press('ArrowRight');
        assert.equal(await page.evaluate(() => document.activeElement.dataset.shippingStep), 'ready');
        await selected(page, 'collection');
        await page.keyboard.press('Space');
        await selected(page, 'ready');
        await page.keyboard.press('End');
        assert.equal(await page.evaluate(() => document.activeElement.dataset.shippingStep), 'collection');
        await page.keyboard.press('Home');
        assert.equal(await page.evaluate(() => document.activeElement.dataset.shippingStep), 'ready');
        assert.equal(await page.locator('[data-shipping-step][tabindex="0"]').count(), 1);
        await page.keyboard.press('Tab');
        assert.equal(await page.evaluate(() => document.activeElement.id), 'shipping-panel-ready');
        assert.equal(requests.length, 1);
        scenarios++;

        const nativeClicks = await page.evaluate(() => {
          const workflow = document.querySelector('[data-shipping-workflow]');
          const prevented = [];
          const observe = event => { prevented.push(event.defaultPrevented); event.preventDefault(); };
          workflow.addEventListener('click', observe);
          for (const modifiers of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }]) {
            document.querySelector('#shipping-tab-order').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, ...modifiers }));
          }
          workflow.removeEventListener('click', observe);
          return prevented;
        });
        assert.deepEqual(nativeClicks, [false, false, false, false, false]);
        await selected(page, 'ready');
        scenarios++;

        if (lifecycle) {
          await page.evaluate(() => MeridianPages.dispose());
          const inactive = await page.evaluate(() => {
            const workflow = document.querySelector('[data-shipping-workflow]');
            let prevented;
            workflow.addEventListener('click', event => { prevented = event.defaultPrevented; event.preventDefault(); }, { once: true });
            document.querySelector('#shipping-tab-order').click();
            return { prevented, mounted: !!workflow.meridianShippingWorkflow };
          });
          assert.deepEqual(inactive, { prevented: false, mounted: false });
          await selected(page, 'ready');
          await page.evaluate(() => MeridianPages.mount());
          await selected(page, 'ready');
          await page.locator('#shipping-tab-order').click();
          await selected(page, 'order');
          assert.equal(await page.getByRole('textbox', { name: 'ready note', includeHidden: true }).inputValue(), 'Unsent parcel note');
          scenarios++;
        }
        // Re-evaluating a cached controller must not install another set of handlers.
        await page.addScriptTag({ path: 'static/js/shipping_workflow.js' });
        const updates = await page.evaluate(() => {
          const workflow = document.querySelector('[data-shipping-workflow]');
          const observer = new MutationObserver(() => {});
          observer.observe(workflow, { attributes: true, subtree: true, attributeFilter: ['aria-selected'] });
          document.querySelector('#shipping-tab-courier').click();
          const count = observer.takeRecords().length;
          observer.disconnect();
          return count;
        });
        assert.equal(updates, 5);
        await selected(page, 'courier');
        assert.equal(requests.length, 1);
        assert.deepEqual(errors, []);
        scenarios++;

        await page.getByRole('textbox', { name: 'courier note' }).fill('Courier collection note');
        await Promise.all([
          page.waitForRequest(request => request.method() === 'POST'),
          page.getByRole('button', { name: 'Confirm courier', exact: true }).click(),
        ]);
        assert.equal(requests.filter(request => request.method === 'POST').length, 1);
        assert.equal(new URL(requests.find(request => request.method === 'POST').url).pathname, '/shipping/action/courier/');
        assert.match(requests.find(request => request.method === 'POST').body, /note=Courier\+collection\+note/);
        scenarios++;
        await page.close();
      }

      const incomplete = await browser.newPage();
      await incomplete.setContent(fixture({ malformed: true }));
      await incomplete.addScriptTag({ path: 'static/js/shipping_workflow.js' });
      assert.equal(await incomplete.locator('[role="tablist"]').count(), 0, 'Incomplete markup must retain native fallback');
      assert.equal(await incomplete.locator('[data-shipping-workflow]').evaluate(node => !!node.meridianShippingWorkflow), false);
      await incomplete.close();
      scenarios++;

      const noScript = await browser.newPage({ javaScriptEnabled: false });
      await noScript.route('http://meridian.test/**', route => {
        const step = new URL(route.request().url()).searchParams.get('shipping_step') || 'ready';
        return route.fulfill({ contentType: 'text/html', body: fixture({ selected: step }) });
      });
      await noScript.goto('http://meridian.test/shipping/new/?week=2026-10-26');
      await noScript.locator('#shipping-tab-order').click();
      await noScript.waitForURL('**shipping_step=order#shipping-workflow');
      await selected(noScript, 'order');
      assert.equal(new URL(noScript.url()).searchParams.get('week'), '2026-10-26');
      await noScript.close();
      scenarios++;
      console.log(`${name}: workflow panels, keyboard, drafts, native links, cleanup and fallback checks passed.`);
    } finally { await browser.close(); }
  }
  console.log(`${scenarios} shipping workflow scenarios passed.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
