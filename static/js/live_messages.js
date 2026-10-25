(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('live_messages', (lifecycle) => {
  const clearTimeout = lifecycle ? lifecycle.clearTimeout : window.clearTimeout.bind(window);
  const clearInterval = lifecycle ? lifecycle.clearInterval : window.clearInterval.bind(window);
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
  const setTimeout = lifecycle ? lifecycle.timeout : window.setTimeout.bind(window);
  const setInterval = lifecycle ? lifecycle.interval : window.setInterval.bind(window);
  const fetch = lifecycle ? lifecycle.fetch : (...args) => window.fetch(...args);
/* WebSockets carry invalidations only. Authorised HTML/JSON comes from Django, and PostgreSQL
   supplies missed messages after a disconnect. Keep the composer and its unsent text in place. */
(() => {
  const config = document.querySelector('[data-live-messages]');
  if (!config || !window.fetch || !window.DOMParser) return;
  const scope = config.dataset.liveScope;
  const headers = { 'X-Meridian-Realtime': 'snapshot', 'X-Meridian-Scope': scope };
  let stopped = false, suspended = false, socket, reconnectTimer, refreshTimer, readTimer, heartbeatTimer;
  let retry = 1000, refreshing = false, refreshAgain = false, sending = false, reading = false;
  let revision = 0, searching = false, generation = 0;
  let lastSocketMessage = Date.now();
  const acknowledged = new Set();
  const pendingGets = new Set();
  const messageList = () => document.querySelector('[data-live-region="messages"]');
  const hasInbox = !!document.querySelector('[data-record-messages], .patient-inbox');
  const token = () => config.querySelector('[name="csrfmiddlewaretoken"]')?.value;

  function notice(text, reload = false) {
    if (lifecycle && !lifecycle.isActive) return;
    let box = document.querySelector('[data-live-status]');
    if (!box) {
      box = document.createElement('p');
      box.dataset.liveStatus = '';
      box.className = 'live-message-status';
      box.setAttribute('role', 'status');
      const content = document.querySelector('[data-record-messages], .patient-inbox, #workspace-content');
      (content || document.body).prepend(box);
    }
    box.replaceChildren(document.createTextNode(text));
    if (reload) {
      const link = document.createElement('a');
      link.href = window.location.href;
      link.textContent = ' Reload this page';
      box.append(link);
    }
    box.hidden = !text;
  }

  function updateCounts(data) {
    if (lifecycle && !lifecycle.isActive) return;
    for (const name of ['patient', 'team', 'total']) {
      const count = Math.max(0, Number(data[name]) || 0);
      document.querySelectorAll(`[data-live-unread="${name}"]`).forEach(el => {
        el.hidden = count === 0;
        const limit = el.classList.contains('message-unread-dot') ? 99 : 9;
        el.textContent = count > limit ? `${limit}+` : String(count);
      });
      document.querySelectorAll(`[data-live-unread-label="${name}"]`).forEach(el => {
        el.textContent = count ? `, ${count} unread message${count === 1 ? '' : 's'}` : '';
      });
    }
  }

  function stop() {
    if (stopped) return;
    stopped = true;
    document.dispatchEvent(new CustomEvent('messages:stopped'));
    clearTimeout(reconnectTimer); clearTimeout(refreshTimer); clearTimeout(readTimer);
    clearInterval(heartbeatTimer); clearInterval(pollTimer);
    socket?.close();
    // A tab can outlive a logout, practice switch or removed membership. Keep only the user's draft.
    document.querySelectorAll('[data-live-region], [data-search-region]').forEach(el => el.replaceChildren());
    document.querySelectorAll('.record-messages__pane, .patient-conversation').forEach(pane => {
      const form = pane.querySelector('form textarea')?.form;
      pane.replaceChildren(...(form ? [form] : []));
      if (form) {
        listen(form, 'submit', event => event.preventDefault());
        form.querySelectorAll('button, select').forEach(el => { el.disabled = true; });
      }
    });
    document.querySelectorAll('[data-live-reply] button').forEach(el => { el.disabled = true; });
    updateCounts({ patient: 0, team: 0, total: 0 });
    notice('Your session or conversation access changed. Copy any unsent draft before reloading.', true);
  }

  function validScope(other) {
    if (lifecycle && !lifecycle.isActive) return false;
    if (!other || other !== scope) { stop(); return false; }
    return !stopped;
  }

  async function request(url, options = {}) {
    if (stopped || suspended) throw new Error('Page is inactive');
    const controller = new AbortController();
    const version = generation;
    const background = !options.method || options.method.toUpperCase() === 'GET';
    if (background) pendingGets.add(controller);
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', signal: controller.signal, ...options,
        headers: { ...headers, ...options.headers } });
      if (lifecycle && !lifecycle.isActive) throw new Error('Page is inactive');
      if ([401, 403, 409].includes(response.status)) { stop(); throw new Error('Access changed'); }
      // Keep the cancellation and timeout active until the whole body has arrived.
      const body = await response.text();
      if (background && (suspended || version !== generation)) throw new Error('Page is inactive');
      return { response, body };
    } finally { clearTimeout(timeout); pendingGets.delete(controller); }
  }

  async function status() {
    const { response, body } = await request(config.dataset.statusUrl);
    if (!response.ok) throw new Error('Unable to refresh messages');
    if (!(response.headers.get('content-type') || '').includes('application/json')) { stop(); return; }
    const data = JSON.parse(body);
    if (validScope(data.scope)) updateCounts(data);
  }

  function parsePage(html) {
    const page = new DOMParser().parseFromString(html, 'text/html');
    if (!validScope(page.querySelector('[data-live-messages]')?.dataset.liveScope)) return null;
    return page;
  }

  function preserveScroll(region, change, follow = false) {
    const top = region.scrollTop;
    const bottom = region.scrollHeight - region.clientHeight - top < 70;
    const anchor = [...region.querySelectorAll('[data-message-id]')].find(el => el.offsetTop + el.offsetHeight > region.offsetTop + top);
    const anchorId = anchor?.dataset.messageId;
    const offset = anchor?.getBoundingClientRect().top;
    change();
    if (follow && bottom) region.scrollTop = region.scrollHeight;
    else {
      region.scrollTop = top;
      const replacement = anchorId && region.querySelector(`[data-message-id="${anchorId}"]`);
      if (replacement) region.scrollTop += replacement.getBoundingClientRect().top - offset;
    }
  }

  function historyNotice() {
    const list = messageList();
    if (!list || list.dataset.liveHistory !== 'older') return;
    let box = document.querySelector('[data-live-history-notice]');
    if (!box) {
      box = document.createElement('p'); box.className = 'live-message-status'; box.dataset.liveHistoryNotice = '';
      const link = document.createElement('a');
      const url = new URL(window.location.href); url.searchParams.delete('message_page');
      link.href = url.href; link.textContent = 'View latest messages'; box.append(link); list.before(box);
    }
  }

  function applyPage(page) {
    if (!page || stopped || suspended) return;
    const current = messageList(), fresh = page.querySelector('[data-live-region="messages"]');
    if (current && (!fresh || current.dataset.liveThread !== fresh.dataset.liveThread)) { stop(); return; }
    const older = current?.dataset.liveHistory === 'older';
    document.querySelectorAll('[data-search-region]').forEach(region => {
      const next = page.querySelector(`[data-search-region="${region.dataset.searchRegion}"]`);
      if (next) preserveScroll(region, () => { region.innerHTML = next.innerHTML; });
    });
    document.querySelectorAll('[data-live-region]').forEach(region => {
      const name = region.dataset.liveRegion;
      const next = page.querySelector(`[data-live-region="${name}"]`);
      if (!next || (older && ['messages', 'history'].includes(name))) return;
      if (region.innerHTML !== next.innerHTML) preserveScroll(region, () => { region.innerHTML = next.innerHTML; }, name === 'messages');
      region.hidden = next.hidden;
    });
    if (current && fresh) {
      const wasClosed = current.dataset.liveClosed === 'true';
      current.dataset.liveClosed = fresh.dataset.liveClosed;
      if (fresh.dataset.liveReadUrl) current.dataset.liveReadUrl = fresh.dataset.liveReadUrl;
      else delete current.dataset.liveReadUrl;
      current.setAttribute('aria-label', fresh.getAttribute('aria-label') || 'Messages');
      const form = document.querySelector('[data-live-reply]');
      if (form) {
        const closed = fresh.dataset.liveClosed === 'true';
        form.querySelectorAll('button[type="submit"]').forEach(el => { el.disabled = closed || sending; });
        if (closed) notice('This conversation is closed. Your unsent draft is still here.');
        else notice('');
      }
      else if (wasClosed && fresh.dataset.liveClosed !== 'true') notice('This conversation is open again. Reload this page to reply.', true);
    }
    if (older) historyNotice();
    document.dispatchEvent(new CustomEvent('messages:updated'));
    scheduleRead();
  }

  async function refresh() {
    if (stopped || suspended) return;
    if (refreshing || sending) { refreshAgain = true; return; }
    refreshing = true;
    refreshAgain = false;
    const address = window.location.href;
    const version = revision;
    const cycle = generation;
    try {
      await status();
      if (!stopped && !suspended && cycle === generation && hasInbox && !document.hidden) {
        const { response, body } = await request(address);
        if (response.status === 404) { stop(); return; }
        if (!response.ok) throw new Error('Unable to load messages');
        const page = parsePage(body);
        // A search can finish while the request is in flight. Do not put its old list back.
        if (cycle === generation && address === window.location.href && version === revision && !sending && !searching) applyPage(page);
        else refreshAgain = true;
      }
    } catch (error) {
      // Transient disconnects are retried by the socket and periodic catch-up.
    } finally {
      refreshing = false;
      if (refreshAgain && !stopped && !suspended && !sending && !searching) scheduleRefresh();
    }
  }

  function scheduleRefresh() {
    if (stopped || suspended) return;
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 150);
  }

  function visibleMessages(list) {
    const bounds = list.getBoundingClientRect();
    const top = Math.max(0, bounds.top), bottom = Math.min(window.innerHeight, bounds.bottom);
    if (!list.getClientRects().length || bottom <= top) return [];
    return [...list.querySelectorAll('[data-message-id]')].filter(row => {
      const rect = row.getBoundingClientRect();
      return rect.bottom > top && rect.top < bottom && !acknowledged.has(`${list.dataset.liveThread}:${row.dataset.messageId}`);
    }).map(row => row.dataset.messageId).filter(id => /^\d+$/.test(id)).slice(0, 500);
  }

  async function acknowledge() {
    if (stopped || suspended || document.hidden || reading) return;
    const list = messageList();
    if (!list?.dataset.liveReadUrl) return;
    const ids = visibleMessages(list);
    if (!ids.length) return;
    reading = true;
    const cycle = generation;
    try {
      const { response, body } = await request(list.dataset.liveReadUrl, { method: 'POST', headers: {
        'Content-Type': 'application/json', 'X-CSRFToken': token(),
      }, body: JSON.stringify({ message_ids: ids }) });
      if (response.status === 404) { stop(); return; }
      if (!response.ok) return;
      const data = JSON.parse(body);
      if (!suspended && cycle === generation && validScope(data.scope)) {
        ids.forEach(id => acknowledged.add(`${list.dataset.liveThread}:${id}`));
        updateCounts(data);
        if (data.changed) scheduleRefresh();
      }
    } catch (error) { /* Retry only read acknowledgements, never a send. */ }
    finally { reading = false; }
  }

  function scheduleRead() {
    if (stopped || suspended) return;
    clearTimeout(readTimer);
    readTimer = setTimeout(acknowledge, 250);
  }

  function sendError(form, text) {
    if (lifecycle && !lifecycle.isActive) return;
    let error = form.querySelector('[data-live-send-error]');
    if (!error) { error = document.createElement('p'); error.dataset.liveSendError = ''; error.className = 'live-message-error'; error.setAttribute('role', 'alert'); form.prepend(error); }
    error.textContent = text;
    error.hidden = !text;
  }

  listen(document, 'submit', async event => {
    const form = event.target.closest('[data-live-reply]');
    if (!form) return;
    event.preventDefault();
    if (sending || stopped || suspended || messageList()?.dataset.liveClosed === 'true') return;
    const textarea = form.querySelector('textarea[name="body"]');
    if (!textarea?.value.trim() || !form.reportValidity()) return;
    const draft = textarea.value;
    revision += 1;
    const fields = new FormData(form);
    const cycle = generation;
    sending = true; sendError(form, '');
    form.setAttribute('aria-busy', 'true');
    form.querySelectorAll('button[type="submit"]').forEach(el => { el.disabled = true; });
    try {
      const { response, body } = await request(form.action, { method: 'POST', body: fields });
      if (!response.ok && response.status !== 400 && response.status !== 422) throw new Error('Send failed');
      const page = parsePage(body);
      if (!page) return;
      if (!response.redirected) {
        const errors = [...page.querySelectorAll('[data-live-reply] .errorlist, [data-live-reply] .record-error')].map(el => el.textContent.trim());
        sendError(form, errors.join(' ') || 'Your message was not sent. Please check it and try again.');
        return;
      }
      if (textarea.value === draft) { textarea.value = ''; textarea.dispatchEvent(new Event('input', { bubbles: true })); }
      // Preserve an older history page when replying; fetch that page, not the redirect's latest slice.
      if (messageList()?.dataset.liveHistory === 'older') historyNotice();
      else if (cycle === generation) applyPage(page);
    } catch (error) {
      if (!stopped && !suspended) sendError(form, 'We could not confirm whether this message was sent. Your draft is saved here. Check the conversation before sending again.');
    } finally {
      sending = false;
      if (lifecycle && !lifecycle.isActive) return;
      form.removeAttribute('aria-busy');
      form.querySelectorAll('button[type="submit"]').forEach(el => { el.disabled = stopped || messageList()?.dataset.liveClosed === 'true'; });
      scheduleRefresh();
    }
  });

  function connect() {
    if (stopped || suspended || !window.WebSocket) return;
    clearTimeout(reconnectTimer);
    const url = new URL('/ws/messages/', window.location.href);
    url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    lastSocketMessage = Date.now();
    let connection;
    try { connection = new WebSocket(url); socket = connection; }
    catch (error) { reconnect(); return; }
    connection.onmessage = event => {
      if (connection !== socket) return;
      lastSocketMessage = Date.now();
      let message; try { message = JSON.parse(event.data); } catch (error) { return; }
      if (message.type === 'ready') { retry = 1000; scheduleRefresh(); }
      if (message.type === 'messages.changed') scheduleRefresh();
    };
    connection.onclose = event => {
      if (connection !== socket) return;
      if (event.code === 4001) { stop(); return; }
      reconnect();
    };
    connection.onerror = () => connection.close();
  }

  function reconnect() {
    if (stopped || suspended) return;
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, retry + Math.random() * 300);
    retry = Math.min(retry * 2, 30000);
  }

  function suspend() {
    suspended = true;
    generation += 1;
    clearTimeout(reconnectTimer); clearTimeout(refreshTimer); clearTimeout(readTimer);
    pendingGets.forEach(controller => controller.abort());
    socket?.close();
  }

  lifecycle?.cleanup(() => {
    stopped = true;
    suspend();
    // A delayed close/message from the old socket must never stop the new page.
    socket = null;
    clearInterval(heartbeatTimer); clearInterval(pollTimer);
  });

  function resume() {
    if (stopped) return;
    suspended = false;
    if (!socket || socket.readyState > 1) connect();
    scheduleRefresh(); scheduleRead();
  }

  listen(document, 'visibilitychange', () => {
    if (!document.hidden) { scheduleRefresh(); scheduleRead(); }
  });
  listen(document, 'messages:access-changed', stop);
  listen(document, 'messages:searching', () => { searching = true; revision += 1; });
  listen(document, 'messages:searched', () => { searching = false; scheduleRefresh(); });
  listen(document, 'scroll', scheduleRead, true);
  listen(window, 'resize', scheduleRead);
  listen(window, 'online', () => { scheduleRefresh(); if (!socket || socket.readyState > 1) connect(); });
  listen(document, 'workspace:navigating', suspend);
  listen(document, 'workspace:navigation-cancelled', resume);
  listen(window, 'pageshow', resume);
  listen(window, 'pagehide', suspend);
  const pollTimer = setInterval(() => { scheduleRefresh(); scheduleRead(); }, 30000);
  heartbeatTimer = setInterval(() => {
    if (!stopped && !suspended && socket && socket.readyState < 2 && Date.now() - lastSocketMessage > 65000) socket.close();
  }, 10000);
  // Initial read acknowledgements wait until the browser has laid out the visible conversation.
  const initial = messageList();
  if (initial && initial.dataset.liveHistory !== 'older') initial.scrollTop = initial.scrollHeight;
  connect(); scheduleRefresh(); scheduleRead();
})();

});
