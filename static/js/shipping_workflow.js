(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('shipping_workflow', (lifecycle) => {
  const steps = ['ready', 'lock', 'order', 'courier', 'collection'];
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

    listen(workflow, 'click', event => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
      const tab = event.target.closest('[data-shipping-step]');
      if (!tabs.includes(tab)) return;
      event.preventDefault();
      activate(tab);
      tab.focus({ preventScroll: true });
    });
    listen(workflow, 'keydown', event => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
      const tab = event.target.closest('[data-shipping-step]');
      const index = tabs.indexOf(tab);
      if (index < 0) return;
      if (event.key === ' ') {
        event.preventDefault();
        activate(tab);
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
