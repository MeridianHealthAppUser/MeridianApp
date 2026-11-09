(window.MeridianPages?.register || ((_name, initialize) => initialize(null)))('appointment_video', (lifecycle) => {
  const clearTimeout = lifecycle ? lifecycle.clearTimeout : window.clearTimeout.bind(window);
  const listen = lifecycle ? lifecycle.on : (target, ...args) => target?.addEventListener(...args);
  const setTimeout = lifecycle ? lifecycle.timeout : window.setTimeout.bind(window);
  const fetch = lifecycle ? lifecycle.fetch : (...args) => window.fetch(...args);
/* A patient's Open meeting room: My Appointments' table, and the next-consult banner there and on My Treatment
   (templates/portal/includes/meeting_room_button.html). The server authorizes each visible appointment; timers
   refresh at its opening time and close it at its end without reloading the page. Closed, the button stays, greyed
   out with no address; open, it's the blue link into the room, and the banner says the room is open. */
(() => {
  const containers = [...document.querySelectorAll('[data-appointment-video]')];
  const first = containers[0];
  if (!first || !window.fetch || !window.AbortController) return;
  const scope = first.dataset.videoScope;
  // One appointment can be both in the banner and in the table.
  const rows = new Map();
  for (const container of containers) {
    const items = container.matches('[data-video-appointment-id]') ? [container] : container.querySelectorAll('[data-video-appointment-id]');
    for (const row of items) {
      const id = row.dataset.videoAppointmentId;
      if (!rows.has(id)) rows.set(id, []);
      rows.get(id).push(row);
    }
  }
  if (!rows.size || !scope) return;
  const endpoint = new URL(first.dataset.statusUrl, window.location.href);
  if (endpoint.origin !== window.location.origin) return;
  for (const id of rows.keys()) endpoint.searchParams.append('id', id);
  const announcement = document.querySelector('[data-appointment-video-announcement]');
  const everyRow = () => [...rows.values()].flat();
  let serverTime = Date.parse(first.dataset.serverTime);
  let receivedAt = performance.now();
  let timer = null;
  let expiryTimer = null;
  let controller = null;
  let stopped = false;
  let away = false;
  const currentTime = () => Number.isFinite(serverTime) ? serverTime + performance.now() - receivedAt : Date.now();

  // Open (href) or closed (null). Returns whether it has just opened.
  function toggle(row, href) {
    const link = row.querySelector('[data-appointment-video-join]');
    if (!link) return false;
    const newlyAvailable = !link.hasAttribute('href') && !!href;
    if (href) {
      link.setAttribute('href', href);
      link.removeAttribute('aria-disabled');
    } else {
      link.removeAttribute('href');
      link.setAttribute('aria-disabled', 'true');
    }
    link.classList.toggle('console-button--primary', !!href);
    link.classList.toggle('console-button--secondary', !href);
    if (row.hasAttribute('data-video-banner')) {
      row.classList.toggle('is-waiting', !href);
      const eyebrow = row.querySelector('[data-video-eyebrow]');
      if (eyebrow) eyebrow.textContent = href ? eyebrow.dataset.openText : eyebrow.dataset.waitingText;
    }
    return newlyAvailable;
  }

  function expire() {
    const now = currentTime();
    for (const row of everyRow()) {
      const closes = Date.parse(row.dataset.videoClosesAt);
      if (Number.isFinite(closes) && now > closes) toggle(row, null);
    }
  }

  function stop() {
    stopped = true;
    clearTimeout(timer);
    clearTimeout(expiryTimer);
    for (const row of everyRow()) toggle(row, null);
  }

  function scheduleExpiry() {
    clearTimeout(expiryTimer);
    if (stopped || away) return;
    const until = everyRow().map(row => Date.parse(row.dataset.videoClosesAt) - currentTime())
      .filter(value => Number.isFinite(value) && value >= 0);
    if (until.length) expiryTimer = setTimeout(() => { expire(); scheduleExpiry(); }, Math.min(...until, 2147483000) + 1);
  }

  function schedule() {
    clearTimeout(timer);
    if (stopped || away || document.hidden) return;
    const now = currentTime();
    let delay = 30000;
    for (const row of everyRow()) {
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
    return `${item.message} Join window: ${date.format(opens)}–${time.format(closes)}.`;
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
    let opened = false;
    for (const [id, list] of rows) {
      const item = byId.get(id);
      let href = null;
      if (item?.joinUrl && Date.parse(item.opensAt) <= serverTime && serverTime <= Date.parse(item.closesAt)) {
        const url = new URL(item.joinUrl, window.location.href);
        if (url.origin === window.location.origin && url.pathname === `/video/appointments/${id}/` && !url.search && !url.hash) href = url.href;
      }
      for (const row of list) {
        row.dataset.videoOpensAt = item?.opensAt || '';
        row.dataset.videoClosesAt = item?.closesAt || '';
        if (toggle(row, href)) opened = true;
        // The table's (i) beside the date, with the join window too; the banner's line under its title.
        const note = row.querySelector('[data-appointment-video-note]');
        if (note) {
          note.hidden = !!href || !item?.message;
          const bubble = note.querySelector('[role="tooltip"]');
          if (bubble) bubble.textContent = item ? windowText(item) : '';
        }
        const message = row.querySelector('[data-appointment-video-message]');
        if (message) {
          message.hidden = !!href || !item?.message;
          message.textContent = item?.message ? ` · ${item.message}` : '';
        }
      }
    }
    if (opened && !document.hidden && announcement) {
      announcement.textContent = 'Your meeting room is open. Use Open meeting room to join.';
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
