(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('appointment_video', (lifecycle) => {
  const clearTimeout = lifecycle ? lifecycle.clearTimeout : window.clearTimeout.bind(window);
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
  const setTimeout = lifecycle ? lifecycle.timeout : window.setTimeout.bind(window);
  const fetch = lifecycle ? lifecycle.fetch : (...args) => window.fetch(...args);
/* My Appointments: the server authorizes each visible Join action; timers refresh
   at its opening time and remove it at its end without reloading the page. */
(() => {
  const table = document.querySelector('[data-appointment-video]');
  if (!table || !window.fetch || !window.AbortController) return;
  const scope = table.dataset.videoScope;
  const rows = new Map([...table.querySelectorAll('[data-video-appointment-id]')]
    .map(row => [row.dataset.videoAppointmentId, row]));
  if (!rows.size || !scope) return;
  const endpoint = new URL(table.dataset.statusUrl, window.location.href);
  if (endpoint.origin !== window.location.origin) return;
  for (const id of rows.keys()) endpoint.searchParams.append('id', id);
  const announcement = document.querySelector('[data-appointment-video-announcement]');
  let serverTime = Date.parse(table.dataset.serverTime);
  let receivedAt = performance.now();
  let timer = null;
  let expiryTimer = null;
  let controller = null;
  let stopped = false;
  let away = false;
  const currentTime = () => Number.isFinite(serverTime) ? serverTime + performance.now() - receivedAt : Date.now();

  function toggle(row, href) {
    const holder = row.querySelector('[data-appointment-video-action]');
    const link = row.querySelector('[data-appointment-video-join]');
    const view = row.querySelector('[data-appointment-video-view]');
    if (!holder || !link) return;
    const newlyAvailable = holder.hidden && !!href;
    holder.hidden = !href;
    if (href) link.setAttribute('href', href); else link.removeAttribute('href');
    view?.classList.toggle('console-button--primary', !href);
    view?.classList.toggle('console-button--secondary', !!href);
    if (newlyAvailable && !document.hidden && announcement) {
      announcement.textContent = 'Your video appointment is ready to join. Use Join video call in the Actions column.';
    }
  }

  function expire() {
    const now = currentTime();
    for (const row of rows.values()) {
      const closes = Date.parse(row.dataset.videoClosesAt);
      if (Number.isFinite(closes) && now > closes) toggle(row, null);
    }
  }

  function stop() {
    stopped = true;
    clearTimeout(timer);
    clearTimeout(expiryTimer);
    for (const row of rows.values()) toggle(row, null);
  }

  function scheduleExpiry() {
    clearTimeout(expiryTimer);
    if (stopped || away) return;
    const until = [...rows.values()].map(row => Date.parse(row.dataset.videoClosesAt) - currentTime())
      .filter(value => Number.isFinite(value) && value >= 0);
    if (until.length) expiryTimer = setTimeout(() => { expire(); scheduleExpiry(); }, Math.min(...until, 2147483000) + 1);
  }

  function schedule() {
    clearTimeout(timer);
    if (stopped || away || document.hidden) return;
    const now = currentTime();
    let delay = 30000;
    for (const row of rows.values()) {
      for (const value of [row.dataset.videoOpensAt, row.dataset.videoClosesAt]) {
        const until = Date.parse(value) - now;
        if (Number.isFinite(until) && until >= 0) delay = Math.min(delay, until + 50);
      }
    }
    timer = setTimeout(refresh, Math.max(50, delay));
  }

  function windowText(item) {
    if (!item.message) return '';
    const opens = new Date(item.opensAt), closes = new Date(item.closesAt);
    if (!item.opensAt || !item.closesAt || !Number.isFinite(opens.valueOf()) || !Number.isFinite(closes.valueOf())) return item.message;
    const date = new Intl.DateTimeFormat('en-GB', { timeZone: 'Africa/Johannesburg', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false });
    const time = new Intl.DateTimeFormat('en-GB', { timeZone: 'Africa/Johannesburg', hour: '2-digit', minute: '2-digit', hour12: false });
    return `${item.message} Join window: ${date.format(opens)}–${time.format(closes)} SAST.`;
  }

  function apply(data) {
    if (data.scope !== scope || !Array.isArray(data.appointments) || !Number.isFinite(Date.parse(data.serverTime))) {
      stop();
      return;
    }
    // A delayed response must not move the estimated server clock backwards or
    // restore a link whose window already expired while that request was waiting.
    serverTime = Math.max(Date.parse(data.serverTime), Number.isFinite(serverTime) ? currentTime() : 0);
    receivedAt = performance.now();
    const byId = new Map(data.appointments.map(item => [String(item.id), item]));
    for (const [id, row] of rows) {
      const item = byId.get(id);
      row.dataset.videoOpensAt = item?.opensAt || '';
      row.dataset.videoClosesAt = item?.closesAt || '';
      let href = null;
      if (item?.joinUrl && Date.parse(item.opensAt) <= serverTime && serverTime <= Date.parse(item.closesAt)) {
        const url = new URL(item.joinUrl, window.location.href);
        if (url.origin === window.location.origin && url.pathname === `/video/appointments/${id}/` && !url.search && !url.hash) href = url.href;
      }
      toggle(row, href);
      const note = row.querySelector('[data-appointment-video-note]');
      if (note) {
        note.hidden = !!href || !item?.message;
        const bubble = note.querySelector('[role="tooltip"]');
        if (bubble) bubble.textContent = item ? windowText(item) : '';
      }
    }
    scheduleExpiry();
  }

  async function refresh() {
    expire();
    scheduleExpiry();
    if (stopped || away || document.hidden || controller) return;
    clearTimeout(timer);
    controller = new AbortController();
    const timeout = setTimeout(() => controller?.abort(), 10000);
    try {
      const response = await fetch(endpoint.href, {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: { Accept: 'application/json', 'X-Meridian-Scope': scope },
      });
      if (response.redirected || [401, 403, 404, 409].includes(response.status)) { stop(); return; }
      if (!response.ok) return;
      if (!response.headers.get('Content-Type')?.includes('application/json')) { stop(); return; }
      apply(await response.json());
    } catch {
      // A temporary network failure keeps the last authorized state until its expiry.
    } finally {
      clearTimeout(timeout);
      controller = null;
      expire();
      schedule();
    }
  }

  listen(document, 'visibilitychange', () => {
    if (document.hidden) { clearTimeout(timer); controller?.abort(); }
    else refresh();
  });
  listen(window, 'focus', refresh);
  listen(window, 'online', refresh);
  listen(window, 'pagehide', () => { away = true; clearTimeout(timer); clearTimeout(expiryTimer); controller?.abort(); });
  listen(window, 'pageshow', () => { away = false; refresh(); });
  lifecycle?.cleanup(() => { stopped = true; away = true; controller?.abort(); });
  refresh();
})();

});
