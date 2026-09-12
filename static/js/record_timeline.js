(() => {
  'use strict';
  const card = document.querySelector('[data-record-timeline]');
  if (!card) return;
  const scroll = card.querySelector('[data-timeline-scroll]');
  const list = card.querySelector('[data-timeline-entries]');
  const loader = card.querySelector('[data-timeline-loader]');
  const status = card.querySelector('[data-timeline-status]');
  const button = card.querySelector('[data-timeline-load]');
  const reload = card.querySelector('[data-timeline-reload]');
  const sentinel = card.querySelector('[data-timeline-sentinel]');
  if (!scroll || !list || !loader || !status || !button || !reload || !sentinel) return;

  let next = card.dataset.nextUrl || '';
  let loading = false;
  let stopped = false;
  let automatic = true;
  let controller;
  let observer;
  const completed = new Set();
  const keys = new Set([...list.querySelectorAll('[data-entry-key]')].map(row => row.dataset.entryKey));
  const sameOriginURL = value => {
    if (typeof value !== 'string' || !value) return '';
    const url = new URL(value, window.location.href);
    if (url.origin !== window.location.origin || !['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
      throw new Error('unsafe-url');
    }
    return url.href;
  };
  const terminal = message => {
    const restoreFocus = document.activeElement === button;
    stopped = true;
    automatic = false;
    observer?.disconnect();
    status.textContent = message;
    loader.dataset.state = 'error';
    button.hidden = true;
    reload.hidden = false;
    if (restoreFocus) reload.focus({ preventScroll: true });
  };
  try { next = sameOriginURL(next); } catch (_) {
    terminal('The history link is no longer valid. Reload this record to continue.');
  }
  button.hidden = !next || stopped;

  const load = async (fromButton = false) => {
    if (loading || stopped || !next) return;
    if (completed.has(next)) {
      terminal('The history has changed. Reload this record to continue.');
      return;
    }
    const requested = next;
    loading = true;
    controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 20000);
    scroll.setAttribute('aria-busy', 'true');
    button.disabled = true;
    button.textContent = 'Loading…';
    status.textContent = 'Loading older entries…';
    loader.dataset.state = 'loading';
    try {
      const response = await fetch(requested, {
        credentials: 'same-origin', cache: 'no-store', redirect: 'manual',
        headers: { Accept: 'application/json' }, signal: controller.signal,
      });
      if ([400, 401, 403, 404, 409, 410].includes(response.status) || response.redirected || response.type === 'opaqueredirect') {
        terminal('Your session, access or record scope has changed. Reload this record to continue.');
        return;
      }
      if (!response.ok) throw new Error('request-failed');
      if (!response.headers.get('content-type')?.toLowerCase().includes('application/json')) {
        terminal('Your session, access or record scope has changed. Reload this record to continue.');
        return;
      }
      const payload = await response.json();
      if (typeof payload.html !== 'string' || !(payload.next_url === null || typeof payload.next_url === 'string')) {
        throw new Error('invalid-response');
      }
      const following = sameOriginURL(payload.next_url || '');
      if (following && (following === requested || completed.has(following))) throw new Error('repeated-cursor');
      const fragment = document.createElement('template');
      fragment.innerHTML = payload.html;
      const rows = [...fragment.content.children];
      if (rows.some(row => row.tagName !== 'LI' || (!row.dataset.entryKey && !row.hasAttribute('data-timeline-empty'))) ||
          fragment.content.querySelector('script, style, iframe, object, embed, link, meta') ||
          [...fragment.content.querySelectorAll('*')].some(node => [...node.attributes].some(attribute => /^on/i.test(attribute.name)))) {
        throw new Error('invalid-response');
      }
      let added = 0;
      let firstAdded;
      for (const row of rows) {
        const key = row.dataset.entryKey;
        if (key && !keys.has(key)) {
          list.querySelector('[data-timeline-empty]')?.remove();
          keys.add(key);
          list.append(row);
          firstAdded ||= row;
          added++;
        }
      }
      completed.add(requested);
      next = following;
      status.textContent = next ? (added ? `${added} older ${added === 1 ? 'entry' : 'entries'} loaded. Scroll for more.` : 'No additional visible entries in this batch. Load older entries to continue.') : 'End of matching history.';
      loader.dataset.state = next ? 'ready' : 'complete';
      button.hidden = !next;
      automatic = added > 0;
      if (fromButton) {
        const target = firstAdded || status;
        target.tabIndex = -1;
        target.focus({ preventScroll: true });
        scroll.scrollTop += target.getBoundingClientRect().top - scroll.getBoundingClientRect().top - 16;
      }
      if (!next) observer?.disconnect();
    } catch (error) {
      if (error.name === 'AbortError' && stopped) return;
      // A failed request never advances the cursor. Only an explicit retry does.
      automatic = false;
      status.textContent = 'Older entries could not be loaded. Your current entries are unchanged.';
      loader.dataset.state = 'error';
      button.textContent = 'Try again';
    } finally {
      window.clearTimeout(timeout);
      loading = false;
      scroll.removeAttribute('aria-busy');
      button.disabled = false;
      if (loader.dataset.state !== 'error') button.textContent = 'Load older entries';
      // Re-observe after append: short pages may not have moved the sentinel
      // outside the root, so there would otherwise be no new intersection event.
      if (next && automatic && !stopped && observer) {
        observer.unobserve(sentinel);
        observer.observe(sentinel);
      }
    }
  };
  button.addEventListener('click', () => { automatic = true; load(true); });
  if ('IntersectionObserver' in window && next && !stopped) {
    observer = new IntersectionObserver(entries => {
      if (automatic && entries.some(entry => entry.isIntersecting)) load();
    }, { root: scroll, rootMargin: '0px 0px 100px 0px', threshold: 0 });
    observer.observe(sentinel);
  }
  window.addEventListener('pagehide', () => {
    stopped = true;
    controller?.abort();
    observer?.disconnect();
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted) terminal('Reload this record to confirm your current access and history.');
  });
})();
