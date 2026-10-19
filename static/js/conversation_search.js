/* Conversation search (Patient messages, Team messages, the record's Messages tab and My Messages):
   the list follows the search as you type. Each part of the page marked data-search-region is
   refreshed from the same part of the result. The result never includes the open conversation, so
   searching never marks anything read. Without scripts, Enter searches. */
(() => {
  const form = document.querySelector('[data-conversation-search]');
  if (!form || !window.fetch || !window.AbortController || !window.DOMParser) return;
  const input = form.querySelector('input[name="q"]');
  const clear = form.querySelector('[data-conversation-search-clear]');
  const openThread = new URL(window.location.href).searchParams.get('thread');
  const live = document.querySelector('[data-live-messages]');
  const scope = live?.dataset.liveScope;
  let timer = null;
  let pending = null;
  let stopped = false;
  let suspended = false;
  let needsSearch = false;

  document.addEventListener('messages:stopped', () => {
    stopped = true;
    window.clearTimeout(timer);
    pending?.abort();
  });

  function accessChanged() {
    document.dispatchEvent(new CustomEvent('messages:access-changed'));
  }

  async function search() {
    if (stopped || suspended) return;
    timer = null;
    needsSearch = true;
    const query = input.value.trim().replace(/\s+/g, ' ');
    const params = new URLSearchParams(new FormData(form));
    if (query) params.set('q', query); else params.delete('q');
    if (pending) pending.abort();
    pending = new AbortController();
    const current = pending;
    document.dispatchEvent(new CustomEvent('messages:searching'));
    let page;
    try {
      const response = await fetch(`${form.dataset.searchUrl || form.action}?${params}`, {
        credentials: 'same-origin', cache: 'no-store', signal: pending.signal,
        headers: scope ? { 'X-Meridian-Scope': scope, 'X-Meridian-Realtime': 'snapshot' } : {},
      });
      if (stopped || suspended || current !== pending) return;
      if (scope && (response.redirected || [401, 403, 409].includes(response.status))) { accessChanged(); return; }
      if (!response.ok) {
        if (!stopped && !suspended && current === pending) {
          needsSearch = false;
          document.dispatchEvent(new CustomEvent('messages:searched'));
        }
        return;
      }
      page = new DOMParser().parseFromString(await response.text(), 'text/html');
    } catch (error) {
      if (!stopped && !suspended && current === pending) {
        needsSearch = false;
        document.dispatchEvent(new CustomEvent('messages:searched'));
      }
      return;  // A newer search replaced this one, or the network failed; the list stays as it was.
    }
    if (stopped || suspended || current !== pending) return;
    // The record's search returns only a fragment; its request scope is checked
    // by the same server mixin. Full-page search responses also carry a marker.
    const returnedScope = page.querySelector('[data-live-messages]')?.dataset.liveScope;
    if (scope && returnedScope && returnedScope !== scope) { accessChanged(); return; }
    document.querySelectorAll('[data-search-region]').forEach((region) => {
      const fresh = page.querySelector(`[data-search-region="${region.dataset.searchRegion}"]`);
      if (fresh) region.innerHTML = fresh.innerHTML;
    });
    // The open conversation stays open and highlighted, and its forms come back to this search.
    if (openThread) {
      document.querySelectorAll('[data-search-region] a[href*="thread="]').forEach((row) => {
        if (new URL(row.href, window.location.href).searchParams.get('thread') !== openThread) return;
        row.classList.add('is-selected');
        row.setAttribute('aria-current', 'true');
      });
    }
    document.querySelectorAll('input[type="hidden"][name="q"]').forEach((field) => { field.value = query; });
    const address = new URL(window.location.href);
    if (query) address.searchParams.set('q', query); else address.searchParams.delete('q');
    address.searchParams.delete('page');
    window.history.replaceState(null, '', address);
    needsSearch = false;
    document.dispatchEvent(new CustomEvent('messages:searched'));
  }

  input.addEventListener('input', () => {
    if (stopped || suspended) return;
    needsSearch = true;
    if (clear) clear.hidden = !input.value;
    window.clearTimeout(timer);
    timer = window.setTimeout(search, 200);
  });
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    window.clearTimeout(timer);
    search();
  });
  if (clear) clear.addEventListener('click', (event) => {
    event.preventDefault();
    input.value = '';
    clear.hidden = true;
    window.clearTimeout(timer);
    search();
    input.focus();
  });

  function suspend() {
    suspended = true;
    window.clearTimeout(timer);
    pending?.abort();
    pending = null;
  }

  function resume() {
    if (stopped || !suspended) return;
    suspended = false;
    // A cancelled search must also release the live refresh's search lock.
    document.dispatchEvent(new CustomEvent('messages:searched'));
    if (needsSearch) search();
  }

  document.addEventListener('workspace:navigating', suspend);
  document.addEventListener('workspace:navigation-cancelled', resume);
  window.addEventListener('pagehide', suspend);
  window.addEventListener('pageshow', resume);
})();
