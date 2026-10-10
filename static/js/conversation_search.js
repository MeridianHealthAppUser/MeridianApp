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
  let timer = null;
  let pending = null;

  async function search() {
    const query = input.value.trim().replace(/\s+/g, ' ');
    const params = new URLSearchParams(new FormData(form));
    if (query) params.set('q', query); else params.delete('q');
    if (pending) pending.abort();
    pending = new AbortController();
    let page;
    try {
      const response = await fetch(`${form.dataset.searchUrl || form.action}?${params}`, { credentials: 'same-origin', signal: pending.signal });
      if (!response.ok) return;
      page = new DOMParser().parseFromString(await response.text(), 'text/html');
    } catch (error) {
      return;  // A newer search replaced this one, or the network failed; the list stays as it was.
    }
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
  }

  input.addEventListener('input', () => {
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
})();
