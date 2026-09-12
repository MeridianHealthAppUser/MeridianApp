/* Deterministic tests: no live server, database, real camera or network.
   MERIDIAN_PLAYWRIGHT_PATH=/path/to/playwright PYTHON=python3 node scripts/test_video_room.cjs
   PLAYWRIGHT_MODULE remains a supported legacy alias. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require(process.env.MERIDIAN_PLAYWRIGHT_PATH || process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const render = [
  "import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')",
  "import django; django.setup()",
  "from django.template.loader import render_to_string",
  "from django.test import RequestFactory",
  "config = {'appointmentId': 17, 'myUserId': 1, 'peerName': 'Example Patient', 'companyName': 'Meridian Health', 'signalPath': '/ws/video/appointments/17/', 'iceConfigUrl': '/video/appointments/17/ice/', 'returnUrl': '/appointments/', 'startsAt': '2026-09-12T12:00:00+02:00', 'endsAt': '2026-09-12T12:30:00+02:00'}",
  "print(render_to_string('video/room.html', {'video_config': config, 'csrf_token': 'a' * 64}))",
].join('\n');
const html = execFileSync(process.env.PYTHON || 'python3', ['-c', render], { cwd: root, encoding: 'utf8' });

function installFakes({ nativeRTC = false } = {}) {
  const test = window.videoTest = { sockets: [], peers: [], tracks: [], mediaCalls: [], displayCalls: 0, events: [], nextFailure: null };
  const media = kind => {
    let track;
    if (kind === 'video') {
      const canvas = document.createElement('canvas'); canvas.width = 320; canvas.height = 180;
      canvas.getContext('2d').fillRect(0, 0, 320, 180);
      track = canvas.captureStream(1).getVideoTracks()[0];
      if (nativeRTC) setInterval(() => { const ctx = canvas.getContext('2d'); ctx.fillStyle = Date.now() % 2 ? '#176c66' : '#275c76'; ctx.fillRect(0, 0, 320, 180); }, 100);
    } else {
      const context = new AudioContext();
      track = context.createMediaStreamDestination().stream.getAudioTracks()[0];
    }
    test.tracks.push(track); return track;
  };
  const createStream = constraints => new MediaStream([...(constraints.audio ? [media('audio')] : []), ...(constraints.video ? [media('video')] : [])]);
  Object.defineProperty(navigator, 'mediaDevices', { value: {
    getUserMedia: async constraints => {
      test.mediaCalls.push(constraints); test.events.push('media');
      if (test.nextFailure) { const name = test.nextFailure; test.nextFailure = null; throw new DOMException('Test failure', name); }
      if (test.deferMedia) return new Promise(resolve => { test.resolveMedia = () => resolve(createStream(constraints)); });
      return createStream(constraints);
    },
    getDisplayMedia: async () => { test.displayCalls++; return new MediaStream([media('video')]); },
  }});
  class Socket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; test.sockets.push(this); test.events.push('socket'); queueMicrotask(() => { this.readyState = 1; this.onopen?.({}); }); }
    send(text) { const message = JSON.parse(text); this.sent.push(message); if (nativeRTC && window.signalToPeer) window.signalToPeer(message).catch(() => {}); }
    receive(message) { this.onmessage?.({ data: JSON.stringify(message) }); }
    close(code = 1000) { this.readyState = 3; queueMicrotask(() => this.onclose?.({ code })); }
  }
  class Peer {
    constructor(config) { this.config = config; this.connectionState = 'new'; this.signalingState = 'stable'; this.senders = []; this.transceivers = []; this.candidates = []; this.offerCount = 0; this.closed = false; test.peers.push(this); }
    addTrack(track) { const sender = { track, replaceTrack: async next => { sender.track = next; } }; this.senders.push(sender); return sender; }
    addTransceiver(track) { const transceiver = { sender: this.addTrack(typeof track === 'string' ? null : track), receiver: { track: { kind: typeof track === 'string' ? track : track.kind } } }; this.transceivers.push(transceiver); return transceiver; }
    getSenders() { return this.senders; }
    getTransceivers() { return this.transceivers; }
    async createOffer() { this.offerCount++; return { type: 'offer', sdp: 'test-offer' }; }
    async createAnswer() { return { type: 'answer', sdp: 'test-answer' }; }
    async setLocalDescription(description) { this.localDescription = description; this.signalingState = description.type === 'offer' ? 'have-local-offer' : 'stable'; }
    async setRemoteDescription(description) { this.remoteDescription = description; this.signalingState = description.type === 'offer' ? 'have-remote-offer' : 'stable'; if (description.type === 'offer' && !this.senders.length) { this.addTransceiver('audio'); this.addTransceiver('video'); } }
    async addIceCandidate(candidate) { if (!this.remoteDescription) throw new Error('ICE applied before description'); this.candidates.push(candidate); }
    close() { this.closed = true; this.connectionState = 'closed'; this.onconnectionstatechange?.(); }
    setState(state) { this.connectionState = state; this.onconnectionstatechange?.(); }
  }
  window.WebSocket = Socket;
  if (nativeRTC) {
    const NativePeer = window.RTCPeerConnection;
    window.RTCPeerConnection = class extends NativePeer {
      constructor(config) { super(config); test.peers.push(this); }
    };
  } else window.RTCPeerConnection = Peer;
}

async function open(browser, { width = 1440, height = 900, reducedMotion = 'reduce', touch = false, iceHtml = false, nativeRTC = false, userId = 1 } = {}) {
  const context = await browser.newContext({ viewport: { width, height }, reducedMotion, hasTouch: touch });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(installFakes, { nativeRTC });
  await page.route('https://video.test/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/room/') return route.fulfill({ contentType: 'text/html', body: html.replace('"myUserId": 1', '"myUserId": ' + userId) });
    if (url.pathname.startsWith('/static/')) return route.fulfill({ contentType: url.pathname.endsWith('.js') ? 'application/javascript' : 'text/css', body: fs.readFileSync(path.join(root, url.pathname.slice(1))) });
    if (url.pathname.endsWith('/ice/')) {
      assert.equal(route.request().method(), 'POST'); assert.ok(route.request().headers()['x-csrftoken']);
      await page.evaluate(() => videoTest.events.push('ice'));
      if (iceHtml) return route.fulfill({ contentType: 'text/html', body: '<h1>Sign in</h1>' });
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ iceServers: [], expiresAt: '2099-01-01T00:00:00Z', relayConfigured: false, iceTransportPolicy: nativeRTC ? 'all' : 'relay' }) });
    }
    return route.fulfill({ contentType: 'text/html', body: '<h1>Appointments</h1>' });
  });
  await page.goto('https://video.test/room/');
  return { page, context, errors };
}
const emit = (page, message) => page.evaluate(message => videoTest.sockets.at(-1).receive(message), message);
async function join(page, audio = false) {
  await page.locator(audio ? '#join-audio' : '#join-camera').click();
  await page.waitForFunction(() => videoTest.sockets.length === 1);
}
const state = (page, value) => page.waitForFunction(value => document.querySelector('#video-room').dataset.state === value, value);
async function checkLayout(page) {
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), page.viewportSize().width);
  const duplicates = await page.evaluate(() => { const ids = [...document.querySelectorAll('[id]')].map(el => el.id); return ids.filter((id, i) => ids.indexOf(id) !== i); });
  assert.deepEqual(duplicates, []);
}
async function screenshot(page, name) {
  if (process.env.VIDEO_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.VIDEO_SCREENSHOT_DIR, name + '.png') });
}

(async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  let checks = 0;
  try {
    for (const width of [1440, 390, 320]) {
      const { page, context, errors } = await open(browser, { width });
      await checkLayout(page); await screenshot(page, 'preflight-' + width);
      assert.equal(await page.evaluate(() => videoTest.mediaCalls.length + videoTest.sockets.length), 0);
      await join(page);
      assert.deepEqual(await page.evaluate(() => videoTest.events), ['media', 'ice', 'socket']);
      await emit(page, { type: 'room_status', peer_count: 1, should_initiate: false, my_user_id: 1, room_epoch: 'one' });
      await state(page, 'waiting'); await checkLayout(page); await screenshot(page, 'waiting-' + width);
      assert.equal(await page.evaluate(() => videoTest.peers.length), 0);
      await page.locator('#toggle-camera').click();
      await page.locator('#toggle-share').click();
      await page.waitForFunction(() => document.querySelector('#local-preview').classList.contains('is-sharing'));
      const screenId = await page.evaluate(() => videoTest.tracks.at(-1).id);
      await emit(page, { type: 'peer_joined', from_user_id: 2, room_epoch: 'two' });
      await emit(page, { type: 'ice-candidate', from_user_id: 2, room_epoch: 'old', payload: { candidate: 'stale' } });
      await emit(page, { type: 'ice-candidate', from_user_id: 2, room_epoch: 'two', payload: { candidate: 'early' } });
      await emit(page, { type: 'offer', from_user_id: 2, room_epoch: 'two', payload: { type: 'offer', sdp: 'remote-offer' } });
      await page.waitForFunction(() => videoTest.sockets[0].sent.some(message => message.type === 'answer'));
      assert.equal(await page.evaluate(() => videoTest.peers[0].senders[1].track.id), screenId);
      assert.equal(await page.evaluate(() => videoTest.peers[0].config.iceTransportPolicy), 'relay');
      assert.deepEqual(await page.evaluate(() => videoTest.peers[0].candidates.map(c => c.candidate)), ['early']);
      assert.equal(await page.locator('#connection-label').innerText(), 'Connecting');
      await page.evaluate(() => videoTest.peers[0].setState('connected'));
      await state(page, 'connected'); await screenshot(page, 'connected-' + width);
      assert.equal(await page.locator('#connection-label').innerText(), 'Connected');
      await emit(page, { type: 'room_status', peer_count: 2, should_initiate: false, my_user_id: 1, room_epoch: 'two' });
      await page.waitForTimeout(20);
      assert.equal(await page.locator('#connection-label').innerText(), 'Connected', 'same-generation status does not downgrade a live connection');
      await page.evaluate(() => videoTest.tracks.at(-1).onended());
      await page.waitForFunction(() => !document.querySelector('#local-preview').classList.contains('is-sharing'));
      assert.equal(await page.evaluate(() => videoTest.peers[0].senders[1].track.enabled), false, 'camera remains off after browser stops sharing');
      assert.equal(await page.evaluate(() => videoTest.tracks.at(-1).readyState), 'ended');
      await page.locator('#move-preview').focus();
      await page.keyboard.press('ArrowLeft'); await page.keyboard.press('ArrowDown');
      const box = await page.locator('#local-preview').boundingBox();
      assert.ok(box.x >= 0 && box.y >= 0 && box.x + box.width <= width);
      await page.clock.install();
      await page.evaluate(() => videoTest.peers[0].setState('disconnected'));
      await page.clock.fastForward(7000);
      assert.equal(await page.evaluate(() => videoTest.peers[0].closed), false);
      await page.evaluate(() => videoTest.peers[0].setState('connected'));
      await page.clock.fastForward(2000);
      assert.equal(await page.evaluate(() => videoTest.peers[0].closed), false);
      await page.evaluate(() => dispatchEvent(new Event('pagehide')));
      assert.ok(await page.evaluate(() => videoTest.tracks.every(track => track.readyState === 'ended')));
      assert.deepEqual(errors, []);
      await context.close(); checks++;
    }

    {
      const { page, context, errors } = await open(browser);
      await page.evaluate(() => { videoTest.nextFailure = 'NotAllowedError'; });
      await page.locator('#join-camera').click(); await state(page, 'error');
      assert.equal(await page.evaluate(() => videoTest.sockets.length), 0);
      await page.locator('#fallback-audio').click();
      await page.waitForFunction(() => videoTest.sockets.length === 1);
      await emit(page, { type: 'room_status', peer_count: 2, should_initiate: true, my_user_id: 1, room_epoch: 'second' });
      await page.waitForFunction(() => videoTest.sockets[0].sent.some(item => item.type === 'offer'));
      await emit(page, { type: 'room_status', peer_count: 2, should_initiate: true, my_user_id: 1, room_epoch: 'second' });
      await page.waitForTimeout(30);
      assert.equal(await page.evaluate(() => videoTest.peers.length), 1);
      assert.equal(await page.evaluate(() => videoTest.peers[0].offerCount), 1);
      assert.equal(await page.evaluate(() => videoTest.peers[0].senders[1].track), null);
      await page.evaluate(() => videoTest.sockets[0].close(4005)); await state(page, 'error');
      assert.ok(await page.evaluate(() => videoTest.tracks.every(track => track.readyState === 'ended')));
      await page.clock.install(); await page.clock.fastForward(30000);
      assert.equal(await page.evaluate(() => videoTest.sockets.length), 1);
      assert.deepEqual(errors, []); await context.close(); checks++;
    }
    {
      const { page, context, errors } = await open(browser);
      await page.evaluate(() => { videoTest.deferMedia = true; });
      await page.locator('#join-camera').click();
      await page.waitForFunction(() => !!videoTest.resolveMedia);
      await page.evaluate(() => { dispatchEvent(new Event('pagehide')); videoTest.resolveMedia(); });
      await page.waitForTimeout(30);
      assert.ok(await page.evaluate(() => videoTest.tracks.every(track => track.readyState === 'ended')));
      assert.equal(await page.evaluate(() => videoTest.sockets.length), 0);
      assert.deepEqual(errors, []); await context.close(); checks++;
    }
    {
      const { page, context, errors } = await open(browser, { iceHtml: true });
      await page.locator('#join-camera').click(); await state(page, 'error');
      assert.equal(await page.locator('#error-heading').innerText(), 'The appointment room is unavailable');
      assert.ok(await page.evaluate(() => videoTest.tracks.every(track => track.readyState === 'ended')));
      assert.equal(await page.evaluate(() => videoTest.sockets.length), 0);
      assert.deepEqual(errors, []); await context.close(); checks++;
    }
    {
      const { page, context, errors } = await open(browser);
      await join(page);
      await emit(page, { type: 'room_status', peer_count: 2, should_initiate: true, my_user_id: 1, room_epoch: 'before' });
      await page.waitForFunction(() => videoTest.peers.length === 1);
      await page.evaluate(() => {
        videoTest.peers[0].setState('connected');
        videoTest.oldStateCallback = videoTest.peers[0].onconnectionstatechange;
        videoTest.oldIceCallback = videoTest.peers[0].onicecandidate;
      });
      await page.clock.install();
      await page.evaluate(() => videoTest.sockets[0].close(1006));
      await state(page, 'reconnecting');
      await page.clock.fastForward(1000);
      await page.waitForFunction(() => videoTest.sockets.length === 2);
      await emit(page, { type: 'room_status', peer_count: 2, should_initiate: true, my_user_id: 1, room_epoch: 'after' });
      await page.waitForFunction(() => videoTest.peers.length === 2);
      await page.evaluate(() => {
        videoTest.peers[0].connectionState = 'failed'; videoTest.oldStateCallback();
        videoTest.oldIceCallback({ candidate: { toJSON: () => ({ candidate: 'old-pc' }) } });
      });
      assert.equal(await page.evaluate(() => videoTest.peers[1].closed), false);
      assert.equal(await page.evaluate(() => videoTest.sockets[1].sent.some(item => item.type === 'ice-candidate')), false);
      assert.equal(await page.evaluate(() => videoTest.sockets[1].sent.filter(item => item.type === 'offer').length), 1);
      await page.evaluate(() => dispatchEvent(new Event('pagehide')));
      assert.ok(await page.evaluate(() => videoTest.tracks.every(track => track.readyState === 'ended')));
      assert.deepEqual(errors, []); await context.close(); checks++;
    }
    for (const code of [4001, 4003, 4004, 4008, 4403, 4503, 1009]) {
      const { page, context, errors } = await open(browser);
      await join(page); await page.clock.install();
      await page.evaluate(code => videoTest.sockets[0].close(code), code);
      await state(page, 'error'); await page.clock.fastForward(30000);
      assert.equal(await page.evaluate(() => videoTest.sockets.length), 1);
      assert.ok(await page.evaluate(() => videoTest.tracks.every(track => track.readyState === 'ended')));
      assert.deepEqual(errors, []); await context.close(); checks++;
    }
    for (const options of [{ reducedMotion: 'no-preference' }, { reducedMotion: 'reduce' }, { reducedMotion: 'no-preference', touch: true }]) {
      const { page, context, errors } = await open(browser, options);
      await join(page); await page.clock.install();
      await emit(page, { type: 'room_status', peer_count: 2, should_initiate: true, my_user_id: 1, room_epoch: 'fade' });
      await page.waitForFunction(() => videoTest.peers.length === 1);
      await page.evaluate(() => { document.activeElement.blur(); videoTest.peers[0].setState('connected'); });
      await page.clock.fastForward(5000);
      assert.equal(await page.locator('#video-room').evaluate(el => el.classList.contains('chrome-hidden')), options.reducedMotion === 'no-preference' && !options.touch);
      await page.mouse.move(100, 100);
      await page.locator('#move-preview').focus(); await page.clock.fastForward(5000);
      assert.equal(await page.locator('#video-room').evaluate(el => el.classList.contains('chrome-hidden')), false);
      await page.evaluate(() => dispatchEvent(new Event('pagehide')));
      assert.deepEqual(errors, []); await context.close(); checks++;
    }
    // Real browser RTCPeerConnections: preserve this regression test. A fake
    // sender cannot detect a video transceiver left without an offered mid.
    {
      const first = await open(browser, { nativeRTC: true, userId: 1 });
      const second = await open(browser, { nativeRTC: true, userId: 2 });
      const relay = (target, from) => async message => {
        if (['offer', 'answer', 'ice-candidate', 'media_state'].includes(message.type)) await emit(target, { ...message, from_user_id: from });
      };
      await first.page.exposeFunction('signalToPeer', relay(second.page, 1));
      await second.page.exposeFunction('signalToPeer', relay(first.page, 2));
      await join(first.page); await join(second.page);
      await emit(first.page, { type: 'room_status', peer_count: 1, should_initiate: false, my_user_id: 1, room_epoch: 'first' });
      await emit(first.page, { type: 'peer_joined', from_user_id: 2, room_epoch: 'native' });
      await emit(second.page, { type: 'room_status', peer_count: 2, should_initiate: true, my_user_id: 2, room_epoch: 'native' });
      for (const { page } of [first, second]) {
        await page.waitForFunction(() => videoTest.peers[0]?.connectionState === 'connected' && document.querySelector('#remote-video').videoWidth > 0, null, { timeout: 15000 });
        const video = await page.evaluate(() => videoTest.peers[0].getTransceivers().filter(item => item.receiver.track.kind === 'video').map(item => ({ mid: item.mid, direction: item.currentDirection, sends: item.sender.track?.kind })));
        assert.equal(video.length, 1);
        assert.notEqual(video[0].mid, null);
        assert.equal(video[0].direction, 'sendrecv');
        assert.equal(video[0].sends, 'video');
        assert.equal(await page.locator('#connection-label').innerText(), 'Connected');
      }
      await screenshot(first.page, 'native-answerer');
      await screenshot(second.page, 'native-offerer');
      await first.page.locator('#toggle-camera').click();
      await second.page.waitForFunction(() => !document.querySelector('#video-room').classList.contains('has-remote-video'));
      await first.page.locator('#toggle-share').click();
      await second.page.waitForFunction(() => document.querySelector('#video-room').classList.contains('remote-sharing') && document.querySelector('#video-room').classList.contains('has-remote-video'));
      assert.equal(await first.page.evaluate(() => videoTest.peers[0].getTransceivers().find(item => item.receiver.track.kind === 'video').sender.track.id === videoTest.tracks.at(-1).id), true);
      await first.page.evaluate(() => videoTest.tracks.at(-1).onended());
      await second.page.waitForFunction(() => !document.querySelector('#video-room').classList.contains('has-remote-video'));
      assert.equal(await first.page.evaluate(() => videoTest.peers[0].getTransceivers().find(item => item.receiver.track.kind === 'video').sender.track.enabled), false);
      await first.page.locator('#toggle-camera').click();
      await second.page.waitForFunction(() => document.querySelector('#video-room').classList.contains('has-remote-video'));
      for (const { page, context, errors } of [first, second]) {
        await page.evaluate(() => dispatchEvent(new Event('pagehide')));
        assert.ok(await page.evaluate(() => videoTest.tracks.every(track => track.readyState === 'ended')));
        assert.deepEqual(errors, []); await context.close();
      }
      checks++;
    }
    console.log('PASS: ' + checks + ' video-room scenarios including 3 responsive widths and native two-browser WebRTC; synthetic media only, no app server, database or external network.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
