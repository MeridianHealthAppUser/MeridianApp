(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('shipping_workflow', (lifecycle) => {
  const steps = ['pending', 'confirmed', 'tracking'];
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target.addEventListener(...args);

  document.querySelectorAll('[data-shipping-workflow]').forEach(workflow => {
    if (workflow.meridianShippingWorkflow) return;
    const tablist = workflow.querySelector('[data-shipping-tabs]');
    const tabs = [...workflow.querySelectorAll('[data-shipping-step]')];
    const panels = [...workflow.querySelectorAll('[data-shipping-panel]')];
    // Keep the server's normal links and selected panel usable if the markup is incomplete.
    if (!tablist || tabs.length !== steps.length || panels.length !== steps.length
        || tabs.filter(tab => tab.getAttribute('aria-selected') === 'true').length !== 1
        || !steps.every(step => tabs.filter(tab => tab.dataset.shippingStep === step).length === 1
          && panels.filter(panel => panel.dataset.shippingPanel === step).length === 1)) return;
    const pairs = tabs.map(tab => ({ tab, panel: panels.find(panel => panel.dataset.shippingPanel === tab.dataset.shippingStep) }));
    if (!pairs.every(({ tab, panel }) => tab.tagName === 'A' && tab.hasAttribute('href')
        && tablist.contains(tab) && tab.id && panel.id && tab.id !== panel.id
        && document.getElementById(tab.id) === tab && document.getElementById(panel.id) === panel
        && tab.getAttribute('aria-controls') === panel.id)
        || new Set(pairs.flatMap(({ tab, panel }) => [tab.id, panel.id])).size !== steps.length * 2) return;

    workflow.meridianShippingWorkflow = true;
    const fallbackAddresses = new Map(tabs.map(tab => [tab, new URL(tab.href)]));
    tablist.setAttribute('role', 'tablist');
    [...tablist.children].forEach(item => {
      if (item.tagName === 'LI') item.setAttribute('role', 'presentation');
    });
    pairs.forEach(({ tab, panel }) => {
      tab.setAttribute('role', 'tab');
      panel.setAttribute('role', 'tabpanel');
      panel.setAttribute('aria-labelledby', tab.id);
      panel.tabIndex = 0;
    });

    function syncLinks() {
      const address = new URL(window.location.href);
      address.searchParams.delete('note');
      address.searchParams.delete('page');
      address.searchParams.delete('create');
      address.searchParams.delete('collect');
      address.searchParams.delete('__shell');
      address.searchParams.set('__full', '1');
      address.hash = 'shipping-workflow';
      tabs.forEach(tab => {
        // Invalid POSTs may render this page at /shipping/new/. Its fallback
        // links must keep the server's canonical GET route and selected week.
        const fallback = fallbackAddresses.get(tab);
        const destination = new URL(address);
        destination.pathname = fallback.pathname;
        if (!destination.searchParams.has('week') && fallback.searchParams.has('week')) {
          destination.searchParams.set('week', fallback.searchParams.get('week'));
        }
        destination.searchParams.set('shipping_step', tab.dataset.shippingStep);
        tab.href = destination.href;
      });
      const all = workflow.querySelector('.shipping-all-stages');
      if (all) {
        const destination = new URL(address);
        destination.pathname = fallbackAddresses.get(tabs[0]).pathname;
        if (!destination.searchParams.has('week')) destination.searchParams.set('week', fallbackAddresses.get(tabs[0]).searchParams.get('week'));
        destination.searchParams.set('shipping_step', 'all');
        all.href = destination.href;
      }
    }

    function activate(selected) {
      pairs.forEach(({ tab, panel }) => {
        const active = tab === selected;
        tab.setAttribute('aria-selected', String(active));
        tab.tabIndex = active ? 0 : -1;
        panel.hidden = !active;
      });
      const fields = new Set([
        ...workflow.querySelectorAll('input[name="shipping_step"]'),
        ...document.querySelectorAll('[data-shipping-step-field]'),
      ]);
      fields.forEach(field => {
        field.value = selected.dataset.shippingStep;
      });
      syncLinks();
    }
    activate(tabs.find(tab => tab.getAttribute('aria-selected') === 'true'));

    listen(workflow, 'keydown', event => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
      const tab = event.target.closest('[data-shipping-step]');
      const index = tabs.indexOf(tab);
      if (index < 0) return;
      if (event.key === ' ') {
        event.preventDefault();
        tab.click();
        return;
      }
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = tabs.length - 1;
      else return;
      event.preventDefault();
      tabs.forEach((item, position) => { item.tabIndex = position === next ? 0 : -1; });
      tabs[next].focus();
    });
    listen(document, 'messages:searched', syncLinks);
    lifecycle?.cleanup(() => { delete workflow.meridianShippingWorkflow; });
  });
});

(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('shipping_selection', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target.addEventListener(...args);
  function refresh() {
    const boxes = [...document.querySelectorAll('[data-select-shipment]')].filter(box => !box.disabled);
    const controls = [...document.querySelectorAll('[data-select-all-shipments]')];
    const button = document.querySelector('#shipping-bulk-confirm button');
    const count = new Set(boxes.filter(box => box.checked).map(box => box.value)).size;
    if (button) { button.disabled = !count; button.textContent = count ? `Confirm & lock ${count} selected` : 'Confirm & lock selected'; }
    controls.forEach(control => {
      control.disabled = boxes.length === 0;
      control.checked = boxes.length > 0 && boxes.every(box => box.checked);
      control.indeterminate = count > 0 && !control.checked;
    });
  }
  listen(document, 'change', event => {
    const box = event.target;
    const boxes = [...document.querySelectorAll('[data-select-shipment]')].filter(other => !other.disabled);
    if (box.matches('[data-select-shipment]')) {
      boxes.filter(other => other.value === box.value).forEach(other => { other.checked = box.checked; });
    } else if (box.matches('[data-select-all-shipments]')) {
      boxes.forEach(other => { other.checked = box.checked; });
    } else return;
    refresh();
  });
  listen(document, 'messages:searched', refresh);
  refresh();
});
