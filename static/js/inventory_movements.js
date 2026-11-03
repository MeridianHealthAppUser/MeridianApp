(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('inventory_movements', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target.addEventListener(...args);
  const read = lifecycle ? lifecycle.fetch.bind(lifecycle) : window.fetch.bind(window);
  document.querySelectorAll('[data-inventory-movements]').forEach(section => {
    const content = section.querySelector('[data-movements-content]');
    const label = section.querySelector('[data-movements-toggle-label]');
    let loaded = false;
    let busy = false;
    let current = section.dataset.movementsUrl;
    let attempted = current;
    function message(text) {
      const paragraph = document.createElement('p');
      paragraph.className = 'inventory-movements-placeholder';
      paragraph.textContent = text;
      content.replaceChildren(paragraph);
    }
    async function load(url, focus = false) {
      if (busy) return;
      busy = true;
      attempted = url;
      content.setAttribute('aria-busy', 'true');
      message('Loading stock movements…');
      try {
        const response = await read(url, {credentials:'same-origin', cache:'no-store', headers:{'X-Inventory-Movements':'1'}});
        if (!response.ok || response.redirected) throw new Error('History could not be loaded');
        const html = await response.text();
        if (lifecycle && !lifecycle.isActive) return;
        content.innerHTML = html;
        current = url;
        loaded = true;
        if (focus) content.querySelector('[data-movements-heading]')?.focus({preventScroll:true});
      } catch (error) {
        if (error.name === 'AbortError' || (lifecycle && !lifecycle.isActive)) return;
        message('Couldn’t load stock movements. Try again or open the history page.');
        const controls = document.createElement('div');
        controls.className = 'inventory-movements-error-actions';
        const retry = document.createElement('button');
        retry.type = 'button'; retry.className = 'console-button console-button--secondary'; retry.textContent = 'Try again'; retry.dataset.movementsRetry = '';
        const fallback = document.createElement('a');
        fallback.className = 'console-button console-button--secondary'; fallback.href = section.dataset.movementsUrl; fallback.textContent = 'Open history page';
        controls.append(retry, fallback); content.append(controls);
      } finally {
        busy = false;
        if (!lifecycle || lifecycle.isActive) content.removeAttribute('aria-busy');
      }
    }
    listen(section, 'toggle', () => {
      label.textContent = section.open ? 'Hide history' : 'View history';
      if (section.open && !loaded) load(current);
    });
    listen(content, 'click', event => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
      const page = event.target.closest('a[data-movements-page]');
      const refresh = event.target.closest('[data-movements-refresh], [data-movements-retry]');
      if (page) { event.preventDefault(); load(page.href, true); }
      else if (refresh) { event.preventDefault(); load(refresh.hasAttribute('data-movements-retry') ? attempted : current, true); }
    });
    if (section.open) load(current);
  });
});
