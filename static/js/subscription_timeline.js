(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('subscription_timeline', (lifecycle) => {
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
/* View timeline (James, 26 Nov 2026), on Patient orders > Subscriptions and the record's Treatment tab: loads a
   subscription's weekly timeline (portal.treatment_views.SubscriptionTimelineView, ?fragment=1) into the page's
   [data-timeline-dialog] and opens it at the current week. The link opens the timeline's own page without scripts,
   or when loading fails. */
(() => {
  const dialog = document.querySelector('[data-timeline-dialog]');
  if (!dialog || typeof dialog.showModal !== 'function') return;
  let opener = null;
  listen(document, 'click', async (event) => {
    const link = event.target.closest?.('a[data-timeline-open]');
    if (!link || event.defaultPrevented || event.button || event.metaKey || event.ctrlKey || event.shiftKey) return;
    event.preventDefault();
    opener = link;
    const url = new URL(link.href, window.location.href);
    url.searchParams.set('fragment', '1');
    link.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'text/html' } });
      if (!response.ok) throw new Error(String(response.status));
      dialog.innerHTML = await response.text();  // The app's own escaped template.
    } catch (error) {
      window.location.href = link.href;
      return;
    } finally {
      link.removeAttribute('aria-busy');
    }
    dialog.showModal();
    dialog.querySelector('[data-dialog-scroll]')?.scrollIntoView({ block: 'center' });
  });
  listen(dialog, 'click', (event) => {
    if (event.target === dialog || event.target.closest('[data-dialog-close]')) {
      event.preventDefault();
      dialog.close();
    }
  });
  listen(dialog, 'close', () => {
    dialog.replaceChildren();
    opener?.focus();
  });
})();

});
