(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('consumption', (lifecycle) => {
  const dialog = document.getElementById('consumption-detail');
  if (!dialog || typeof dialog.showModal !== 'function') return;
  const content = dialog.querySelector('[data-consumption-content]');
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target.addEventListener(...args);
  let controller, trigger;
  const close = () => { controller?.abort(); dialog.close(); trigger?.focus(); };
  async function load(url, opener) {
    controller?.abort(); controller = new AbortController();
    const pending = controller;
    if (opener) trigger = opener;
    content.replaceChildren();
    const loading = document.createElement('div'); loading.className = 'consumption-dialog-loading';
    const message = document.createElement('span'); message.textContent = 'Loading dispatch details…';
    const dismiss = document.createElement('button'); dismiss.type = 'button'; dismiss.className = 'console-button console-button--secondary'; dismiss.textContent = 'Close'; dismiss.dataset.consumptionClose = '';
    loading.append(message, dismiss); content.append(loading);
    if (!dialog.open) dialog.showModal();
    dialog.setAttribute('aria-busy', 'true');
    try {
      const target = new URL(url, location.href); target.searchParams.set('fragment', '1');
      const response = await fetch(target, {signal:pending.signal, credentials:'same-origin', headers:{'X-Requested-With':'XMLHttpRequest'}});
      if (!response.ok || response.headers.get('X-Consumption-Fragment') !== '1') throw new Error('Unavailable');
      const html = await response.text();
      if (pending !== controller || !dialog.open) return;
      const documentFragment = new DOMParser().parseFromString(html, 'text/html').querySelector('[data-consumption-fragment]');
      if (!documentFragment) throw new Error('Unavailable');
      content.replaceChildren(documentFragment); dialog.scrollTop = 0;
      content.querySelector('[data-consumption-close]')?.focus();
    } catch (error) {
      if (error.name === 'AbortError' || pending !== controller) return;
      message.textContent = 'Could not load these dispatches. ';
      const retry = document.createElement('a'); retry.href = url; retry.textContent = 'Open details';
      message.append(retry);
    } finally { if (pending === controller) dialog.removeAttribute('aria-busy'); }
  }
  listen(document, 'click', event => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    const target = event.target.closest('[data-consumption-detail]');
    if (target) { event.preventDefault(); load(target.href, target); return; }
    if (!dialog.contains(event.target)) return;
    if (event.target.closest('[data-consumption-close]')) { event.preventDefault(); close(); return; }
    const page = event.target.closest('.ops-pagination a');
    if (page) { event.preventDefault(); load(page.href); }
  });
  listen(dialog, 'click', event => { if (event.target === dialog) close(); });
  listen(dialog, 'cancel', event => { event.preventDefault(); close(); });
  lifecycle?.cleanup(() => { controller?.abort(); if (dialog.open) dialog.close(); });
});
