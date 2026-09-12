/* In-app invitations only: no camera access, outbound email or autoplay audio. */
(() => {
  'use strict';
  const banner = document.getElementById('incoming-call');
  if (!banner || !window.WebSocket) return;
  const title = document.getElementById('incoming-call-title');
  const practice = document.getElementById('incoming-call-practice');
  const join = document.getElementById('incoming-call-join');
  const dismiss = document.getElementById('incoming-call-dismiss');
  let socket = null, retry = null, expiry = null, attempt = 0, suspended = false, terminal = false, activeId = null;
  const seen = new Map();

  function hide() {
    banner.hidden = true;
    activeId = null;
    clearTimeout(expiry);
  }
  function show(message) {
    const rawId = message.appointment_id ?? message.booking_id;
    const id = String(rawId ?? '');
    if (!/^[1-9]\d{0,17}$/.test(id) || typeof message.caller_name !== 'string'
        || typeof message.practice_name !== 'string') return;
    const now = Date.now();
    for (const [key, until] of seen) if (until <= now) seen.delete(key);
    if (seen.has(id)) return;
    seen.set(id, now + 30000);
    if (seen.size > 100) seen.delete(seen.keys().next().value);
    title.textContent = `${message.caller_name.slice(0,160)} is waiting to join you`;
    practice.textContent = message.practice_name.slice(0,160);
    // Never follow a URL supplied over the socket, even from our own server.
    join.href = `/video/appointments/${id}/`;
    activeId = id;
    banner.hidden = false;
    clearTimeout(expiry);
    expiry = setTimeout(hide, 30000);
  }
  function connect() {
    clearTimeout(retry);
    if (suspended || terminal || !navigator.onLine || socket) return;
    const candidate = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/notifications/`);
    socket = candidate;
    candidate.onopen = () => { attempt = 0; };
    candidate.onmessage = event => {
      if (socket !== candidate || typeof event.data !== 'string' || event.data.length > 8192) return;
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (!message || typeof message !== 'object') return;
      if (message.type === 'incoming_call') show(message);
      if (message.type === 'call_cancelled' && String(message.appointment_id) === activeId) hide();
      if (message.type === 'ping') candidate.send(JSON.stringify({type:'pong'}));
    };
    candidate.onclose = event => {
      if (socket !== candidate) return;
      socket = null;
      hide();
      if ([4001,4003,4403,4503].includes(event.code)) terminal = true;
      if (suspended || terminal || !navigator.onLine) return;
      const delay = Math.min(30000, 1000 * 2 ** Math.min(attempt++,5)) + Math.random()*500;
      retry = setTimeout(connect,delay);
    };
    candidate.onerror = () => { /* onclose handles backoff; never interrupt app work. */ };
  }
  dismiss.addEventListener('click',hide);
  window.addEventListener('pagehide',() => {
    suspended = true;
    clearTimeout(retry); hide();
    const previous = socket; socket = null; previous?.close(1000);
  });
  window.addEventListener('pageshow',() => { suspended = false; connect(); });
  window.addEventListener('online',connect);
  connect();
})();
