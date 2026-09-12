/* Native one-to-one WebRTC. Signaling carries no media and is generation-scoped. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const stopStream = stream => stream?.getTracks().forEach(track => track.stop());
  const terminalCodes = new Set([4001, 4002, 4003, 4004, 4005, 4008, 4403, 4503, 1009]);
  const MAX_ICE = 128, MAX_QUEUE = 160;

  class AppointmentRoom {
    constructor(config) {
      this.config = config;
      this.room = $('video-room');
      this.socket = null;
      this.pc = null;
      this.localStream = null;
      this.shareStream = null;
      this.outboundVideo = null;
      this.videoSender = null;
      this.remoteStream = null;
      this.epoch = null;
      this.initiatedEpoch = null;
      this.socketGeneration = 0;
      this.mediaGeneration = 0;
      this.pendingIce = [];
      this.signalQueue = Promise.resolve();
      this.queueDepth = 0;
      this.joined = false;
      this.joining = false;
      this.terminal = false;
      this.micMuted = false;
      this.cameraOff = false;
      this.screenBusy = false;
      this.cameraBusy = false;
      this.sharing = false;
      this.remoteMedia = { audio: true, video: true, screen: false };
      this.reconnectAttempt = 0;
      this.connectedAt = null;
      this.connectedMilliseconds = 0;
      this.iceConfig = null;
      this.dragged = false;
      this.abortIce = null;
      this.bind();
      this.initialize();
    }

    initialize() {
      $('peer-name').textContent = this.config.peerName;
      $('remote-name').textContent = this.config.peerName;
      $('peer-initial').textContent = Array.from(this.config.peerName || 'M')[0].toUpperCase();
      $('company-name').textContent = this.config.companyName;
      try {
        const format = new Intl.DateTimeFormat('en-ZA', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Africa/Johannesburg' });
        $('appointment-time').textContent = `${format.format(new Date(this.config.startsAt))} · South Africa time`;
      } catch { $('appointment-time').textContent = ''; }
      $('toggle-share').hidden = typeof navigator.mediaDevices?.getDisplayMedia !== 'function';
      if (!window.isSecureContext) {
        this.fail('A secure connection is required', 'Open this appointment using HTTPS. Camera and microphone access are not available on an insecure connection.', false);
      } else if (!navigator.mediaDevices?.getUserMedia || !window.RTCPeerConnection || !window.WebSocket) {
        this.fail('This browser cannot join video calls', 'Please use a browser with camera, microphone and WebRTC support, then open your appointment again.', false);
      }
    }

    bind() {
      $('join-camera').addEventListener('click', () => this.join(false));
      $('join-audio').addEventListener('click', () => this.join(true));
      $('fallback-audio').addEventListener('click', () => this.join(true));
      $('retry-media').addEventListener('click', () => this.terminal ? location.reload() : this.join(false));
      $('toggle-mic').addEventListener('click', () => this.toggleMic());
      $('toggle-camera').addEventListener('click', () => this.toggleCamera());
      $('toggle-share').addEventListener('click', () => this.toggleShare());
      $('leave-call').addEventListener('click', () => { this.cleanup(); location.assign(this.config.returnUrl); });
      $('play-remote').addEventListener('click', () => this.playRemote());
      window.addEventListener('pagehide', () => this.cleanup());
      window.addEventListener('beforeunload', () => this.cleanup());
      window.addEventListener('online', () => {
        if (this.joined && !this.terminal && !this.socket) { clearTimeout(this.reconnectTimer); this.connectSignaling(); }
      });
      document.addEventListener('pointermove', () => this.showChrome());
      document.addEventListener('pointerdown', () => this.showChrome());
      document.addEventListener('focusin', () => this.showChrome());
      document.addEventListener('focusout', () => this.showChrome());
      window.addEventListener('resize', () => { if (this.dragged) this.placePreview(); });
      this.bindPreviewDrag();
    }

    setState(state, title, detail = '') {
      this.room.dataset.state = state;
      $('connection-badge').dataset.state = state;
      $('connection-label').textContent = ({ preflight: 'Not joined', joining: 'Preparing', waiting: 'Waiting', connecting: 'Connecting', connected: 'Connected', reconnecting: 'Reconnecting', error: 'Unable to join', ended: 'Ended' })[state] || state;
      $('room-announcement').textContent = title;
      if (state === 'connected') {
        $('room-overlay').hidden = true;
        $('remote-caption').hidden = false;
        this.resumeClock();
      } else {
        this.pauseClock();
        $('room-overlay').hidden = false;
        if (!['preflight', 'error', 'ended'].includes(state)) {
          $('preflight-panel').hidden = true;
          $('error-panel').hidden = true;
          $('waiting-panel').hidden = false;
          $('waiting-heading').textContent = title;
          $('waiting-detail').textContent = detail;
          $('connection-spinner').hidden = state === 'waiting';
        }
      }
      this.showChrome();
    }

    notice(message) {
      $('room-notice').textContent = message;
      $('room-notice').hidden = false;
      clearTimeout(this.noticeTimer);
      this.noticeTimer = setTimeout(() => { $('room-notice').hidden = true; }, 6500);
      this.showChrome();
    }

    async join(audioOnly) {
      if (this.joining || this.joined || this.terminal) return;
      this.joining = true;
      const generation = ++this.mediaGeneration;
      this.setState('joining', 'Check your browser permissions', 'Allow microphone access' + (audioOnly ? '.' : ' and camera access to continue.'));
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: audioOnly ? false : { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'user' } });
        if (generation !== this.mediaGeneration || this.terminal) { stopStream(stream); return; }
        this.localStream = stream;
        this.cameraOff = audioOnly;
        this.micMuted = false;
        this.outboundVideo = stream.getVideoTracks()[0] || null;
        this.updateLocalPreview();
        this.watchLocalTracks(stream);
        this.setState('joining', 'Preparing your appointment room', 'Your preview is visible only to you until the other participant connects.');
        this.iceConfig = await this.fetchIce();
        if (generation !== this.mediaGeneration || this.terminal) return;
        this.joined = true;
        this.joining = false;
        $('room-controls').hidden = false;
        $('relay-notice').hidden = this.iceConfig.relayConfigured !== false;
        this.updateControls();
        this.connectSignaling();
      } catch (error) {
        if (generation !== this.mediaGeneration || this.terminal) return;
        this.joining = false;
        this.stopMedia();
        if (error.videoAccessFailure) {
          this.fail('The appointment room is unavailable', error.message, false);
          return;
        }
        const denied = ['NotAllowedError', 'PermissionDeniedError', 'SecurityError'].includes(error.name);
        const unavailable = ['NotFoundError', 'DevicesNotFoundError', 'NotReadableError'].includes(error.name);
        this.showMediaError(denied ? 'Permission was not granted' : unavailable ? 'A camera or microphone is unavailable' : 'We couldn’t prepare your call',
          denied ? 'Allow access in your browser’s site settings, then try again. You can explicitly choose audio only if your camera is unavailable.' : 'Check that your device is connected and not being used by another application. You can try again or choose audio only.');
      }
    }

    async fetchIce() {
      this.abortIce?.abort();
      this.abortIce = new AbortController();
      const response = await fetch(this.config.iceConfigUrl, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'X-CSRFToken': $('video-csrf').querySelector('[name="csrfmiddlewaretoken"]').value, 'Accept': 'application/json' },
        signal: this.abortIce.signal,
      });
      if (!response.ok || response.redirected || !response.headers.get('Content-Type')?.includes('application/json')) {
        const error = new Error(response.status === 403 ? 'Your appointment access has changed or its joining window has closed. Return to appointments to check the booking.' : 'Connection settings could not be loaded. Return to your appointment and try again.');
        error.videoAccessFailure = true;
        throw error;
      }
      try {
        const config = await response.json();
        if (!Array.isArray(config.iceServers)) throw new Error('Invalid connection configuration');
        return config;
      } catch {
        const error = new Error('Connection settings could not be loaded. Return to your appointment and try again.');
        error.videoAccessFailure = true;
        throw error;
      }
    }

    showMediaError(title, detail) {
      this.setState('error', title);
      $('waiting-panel').hidden = true;
      $('preflight-panel').hidden = true;
      $('error-panel').hidden = false;
      $('error-heading').textContent = title;
      $('error-detail').textContent = detail;
      $('retry-media').hidden = false;
      $('retry-media').textContent = 'Try camera again';
      $('fallback-audio').hidden = false;
    }

    fail(title, detail, retry = true) {
      this.cleanup();
      this.setState('error', title);
      $('preflight-panel').hidden = true;
      $('waiting-panel').hidden = true;
      $('error-panel').hidden = false;
      $('error-heading').textContent = title;
      $('error-detail').textContent = detail;
      $('retry-media').textContent = 'Reload appointment';
      $('retry-media').hidden = !retry;
      $('fallback-audio').hidden = true;
    }

    async connectSignaling() {
      if (!this.joined || this.terminal || this.socket) return;
      const generation = ++this.socketGeneration;
      this.setState(this.reconnectAttempt ? 'reconnecting' : 'connecting', this.reconnectAttempt ? 'Reconnecting your call' : 'Opening your appointment', 'Your microphone and camera controls remain available.');
      try {
        if (this.iceConfig.expiresAt && Date.parse(this.iceConfig.expiresAt) < Date.now() + 60000) {
          this.iceConfig = await this.fetchIce();
          if (this.terminal || generation !== this.socketGeneration) return;
        }
        const url = new URL(this.config.signalPath, location.href);
        if (url.origin !== location.origin) throw new Error('Invalid signaling origin');
        url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const socket = new WebSocket(url.href);
        this.socket = socket;
        this.signalQueue = Promise.resolve();
        this.queueDepth = 0;
        socket.onopen = () => {
          if (this.socket !== socket || this.terminal) return;
          this.lastPong = Date.now();
          clearInterval(this.heartbeatTimer);
          this.heartbeatTimer = setInterval(() => {
            if (Date.now() - this.lastPong > 45000) this.reconnectPair();
            else this.send('ping');
          }, 15000);
        };
        socket.onmessage = event => {
          if (this.socket !== socket || this.terminal || typeof event.data !== 'string' || event.data.length > 73728) return;
          let message;
          try { message = JSON.parse(event.data); } catch { return; }
          if (++this.queueDepth > MAX_QUEUE) { this.fail('Too much signaling activity', 'The call was stopped safely. Reopen the appointment to try again.'); return; }
          this.signalQueue = this.signalQueue.then(async () => {
            if (this.socket === socket && !this.terminal && generation === this.socketGeneration) await this.handleSignal(message);
          }).catch(() => {
            if (this.socket === socket && !this.terminal) this.reconnectPair();
          }).finally(() => { if (generation === this.socketGeneration) this.queueDepth = Math.max(0, this.queueDepth - 1); });
        };
        socket.onclose = event => {
          if (this.socket !== socket || this.terminal) return;
          this.socket = null;
          clearInterval(this.heartbeatTimer);
          this.resetPeer();
          if (terminalCodes.has(event.code)) {
            const detail = event.code === 4005 ? 'This appointment was opened in another tab. Continue there, or close that tab and reopen the appointment.' : event.code === 4503 ? 'The video service is temporarily unavailable. Return to appointments and try again later.' : 'Your appointment access has ended, the room is full, or this session is no longer authorised. Return to appointments to check your booking.';
            this.fail('This room is no longer available', detail, false);
          } else this.scheduleReconnect();
        };
        socket.onerror = () => { /* onclose handles retry without leaking transport details. */ };
      } catch (error) {
        if (this.terminal || generation !== this.socketGeneration) return;
        if (error.videoAccessFailure) this.fail('The appointment room is unavailable', error.message, false);
        else this.scheduleReconnect();
      }
    }

    send(type, payload) {
      if (!this.socket || this.socket.readyState !== WebSocket.OPEN || this.terminal) return false;
      const message = { type };
      if (type !== 'ping') { message.room_epoch = this.epoch; message.payload = payload; }
      this.socket.send(JSON.stringify(message));
      return true;
    }

    async handleSignal(message) {
      if (!message || typeof message !== 'object') return;
      if (message.type === 'pong') { this.lastPong = Date.now(); return; }
      if (message.type === 'room_status') {
        if (String(message.my_user_id) !== String(this.config.myUserId)) { this.fail('This session has changed', 'Return to appointments and sign in again.', false); return; }
        this.adoptEpoch(message.room_epoch);
        if (message.peer_count === 2) {
          if (this.pc?.connectionState === 'connected') { this.sendMediaState(); return; }
          this.setState('connecting', 'Connecting with your participant', 'Establishing your audio and video connection.');
          if (message.should_initiate && this.initiatedEpoch !== this.epoch) {
            this.initiatedEpoch = this.epoch;
            const pc = this.createPeer();
            const epoch = this.epoch;
            const offer = await pc.createOffer();
            if (!this.currentPeer(pc, epoch)) return;
            await pc.setLocalDescription(offer);
            if (this.currentPeer(pc, epoch)) this.send('offer', { type: pc.localDescription.type, sdp: pc.localDescription.sdp });
          }
        } else this.setState('waiting', `Waiting for ${this.config.peerName}`, 'You are in the appointment room. Your participant can join from their appointment.');
        this.sendMediaState();
        return;
      }
      if (String(message.from_user_id) === String(this.config.myUserId)) return;
      if (message.type === 'peer_joined') {
        this.adoptEpoch(message.room_epoch);
        if (this.pc?.connectionState === 'connected') return;
        this.setState('connecting', `${this.config.peerName} is joining`, 'Establishing your audio and video connection.');
        this.sendMediaState();
        return;
      }
      if (message.type === 'peer_left') {
        this.adoptEpoch(message.room_epoch);
        this.resetPeer();
        this.setState('waiting', `${this.config.peerName} has left`, 'You can wait here for them to rejoin, or leave the appointment.');
        return;
      }
      if (!this.epoch || message.room_epoch !== this.epoch) return;
      if (message.type === 'media_state') {
        if (message.payload && ['audio', 'video', 'screen'].every(key => typeof message.payload[key] === 'boolean')) {
          this.remoteMedia = message.payload;
          this.updateRemoteMedia();
        }
        return;
      }
      if (message.type === 'offer') {
        if (this.pc && this.pc.signalingState !== 'stable') return;
        const pc = this.createPeer(true, true), epoch = this.epoch;
        this.setState('connecting', 'Connecting your call', 'Establishing your audio and video connection.');
        await pc.setRemoteDescription(message.payload);
        if (!this.currentPeer(pc, epoch)) return;
        await this.attachAnswerTracks(pc, epoch);
        if (!this.currentPeer(pc, epoch)) return;
        await this.flushIce(pc, epoch);
        const answer = await pc.createAnswer();
        if (!this.currentPeer(pc, epoch)) return;
        await pc.setLocalDescription(answer);
        if (this.currentPeer(pc, epoch)) this.send('answer', { type: pc.localDescription.type, sdp: pc.localDescription.sdp });
      } else if (message.type === 'answer') {
        const pc = this.pc, epoch = this.epoch;
        if (!pc || pc.signalingState !== 'have-local-offer') return;
        await pc.setRemoteDescription(message.payload);
        if (this.currentPeer(pc, epoch)) await this.flushIce(pc, epoch);
      } else if (message.type === 'ice-candidate') {
        const pc = this.pc;
        if (pc?.remoteDescription?.type) {
          try { await pc.addIceCandidate(message.payload); } catch { /* A single obsolete candidate need not end a viable connection. */ }
        } else if (this.pendingIce.length < MAX_ICE) this.pendingIce.push(message.payload);
        else this.reconnectPair();
      }
    }

    adoptEpoch(epoch) {
      if (typeof epoch !== 'string' || !epoch || epoch.length > 200) throw new Error('Missing room generation');
      if (epoch !== this.epoch) {
        this.resetPeer();
        this.epoch = epoch;
        this.initiatedEpoch = null;
      }
    }

    currentPeer(pc, epoch) { return !this.terminal && this.pc === pc && this.epoch === epoch; }

    createPeer(keepCandidates = false, answering = false) {
      const candidates = keepCandidates ? this.pendingIce : [];
      this.resetPeer();
      this.pendingIce = candidates;
      const pc = new RTCPeerConnection({ iceServers: this.iceConfig.iceServers, iceTransportPolicy: this.iceConfig.iceTransportPolicy === 'relay' ? 'relay' : 'all' });
      const epoch = this.epoch;
      this.pc = pc;
      this.remoteStream = new MediaStream();
      // The answerer must reuse the transceivers created from the offer.
      // Pre-creating one with addTransceiver can leave it unassociated with an
      // offered m-line: ICE connects, but its outgoing video is never sent.
      if (!answering) {
        this.localStream.getAudioTracks().forEach(track => pc.addTrack(track, this.localStream));
        this.videoSender = pc.addTransceiver(this.outboundVideo || 'video', { direction: 'sendrecv', streams: this.outboundVideo ? [new MediaStream([this.outboundVideo])] : [] }).sender;
      }
      pc.onicecandidate = event => {
        if (this.currentPeer(pc, epoch) && event.candidate) this.send('ice-candidate', event.candidate.toJSON());
      };
      pc.ontrack = event => {
        if (!this.currentPeer(pc, epoch)) return;
        if (!this.remoteStream.getTracks().some(track => track.id === event.track.id)) this.remoteStream.addTrack(event.track);
        $('remote-video').srcObject = this.remoteStream;
        event.track.onunmute = () => { if (this.currentPeer(pc, epoch)) this.updateRemoteMedia(); };
        event.track.onended = () => { if (this.currentPeer(pc, epoch)) this.updateRemoteMedia(); };
        this.updateRemoteMedia();
        this.playRemote();
      };
      pc.onconnectionstatechange = () => {
        if (!this.currentPeer(pc, epoch)) return;
        if (pc.connectionState === 'connected') {
          clearTimeout(this.disconnectTimer);
          this.reconnectAttempt = 0;
          this.setState('connected', `Connected with ${this.config.peerName}`);
          this.sendMediaState();
        } else if (pc.connectionState === 'disconnected') {
          this.setState('reconnecting', 'Connection interrupted', 'We are giving your connection a moment to recover.');
          clearTimeout(this.disconnectTimer);
          this.disconnectTimer = setTimeout(() => { if (this.currentPeer(pc, epoch) && pc.connectionState === 'disconnected') this.reconnectPair(); }, 8000);
        } else if (['failed', 'closed'].includes(pc.connectionState)) this.reconnectPair();
      };
      return pc;
    }

    async attachAnswerTracks(pc, epoch) {
      const transceivers = pc.getTransceivers();
      const audio = transceivers.find(item => item.receiver.track.kind === 'audio');
      const video = transceivers.find(item => item.receiver.track.kind === 'video');
      if (!audio || !video) throw new Error('The offer does not include the appointment media channels');
      const microphone = this.localStream.getAudioTracks().find(track => track.readyState !== 'ended') || null;
      audio.direction = 'sendrecv';
      await audio.sender.replaceTrack(microphone);
      if (!this.currentPeer(pc, epoch)) return;
      video.direction = 'sendrecv';
      this.videoSender = video.sender;
      await video.sender.replaceTrack(this.outboundVideo);
    }

    async flushIce(pc, epoch) {
      const candidates = this.pendingIce.splice(0);
      for (const candidate of candidates) {
        if (!this.currentPeer(pc, epoch)) return;
        try { await pc.addIceCandidate(candidate); } catch { /* Ignore an unusable individual candidate. */ }
      }
    }

    resetPeer() {
      clearTimeout(this.disconnectTimer);
      const old = this.pc;
      this.pc = null;
      this.videoSender = null;
      if (old) { old.ontrack = old.onicecandidate = old.onconnectionstatechange = null; old.close(); }
      this.pendingIce = [];
      this.remoteStream = null;
      $('remote-video').srcObject = null;
      $('remote-caption').hidden = true;
      $('play-remote').hidden = true;
      this.room.classList.remove('has-remote-video', 'remote-sharing');
      this.remoteMedia = { audio: true, video: true, screen: false };
      this.pauseClock();
    }

    reconnectPair() {
      if (this.terminal || !this.joined) return;
      this.disconnectSocket();
      this.resetPeer();
      this.scheduleReconnect();
    }

    scheduleReconnect() {
      if (this.terminal || !this.joined) return;
      clearTimeout(this.reconnectTimer);
      if (++this.reconnectAttempt > 10) { this.fail('The connection could not be restored', 'Your camera and microphone have been stopped. Check your internet connection, then reload the appointment.'); return; }
      this.setState('reconnecting', 'Reconnecting your call', navigator.onLine === false ? 'You appear to be offline. We will try again when your connection returns.' : 'Please keep this page open. You can still mute, turn off your camera or leave.');
      const delay = Math.min(12000, 500 * 2 ** (this.reconnectAttempt - 1)) * (.8 + Math.random() * .4);
      this.reconnectTimer = setTimeout(() => this.connectSignaling(), delay);
    }

    disconnectSocket() {
      this.socketGeneration++;
      clearInterval(this.heartbeatTimer);
      const old = this.socket;
      this.socket = null;
      if (old) { old.onclose = old.onmessage = old.onopen = old.onerror = null; old.close(); }
      this.epoch = null;
      this.initiatedEpoch = null;
    }

    async playRemote() {
      try { await $('remote-video').play(); $('play-remote').hidden = true; }
      catch { if (this.remoteStream && !this.terminal) $('play-remote').hidden = false; }
    }

    updateRemoteMedia() {
      const video = this.remoteStream?.getVideoTracks().some(track => track.readyState !== 'ended');
      this.room.classList.toggle('has-remote-video', Boolean(video && (this.remoteMedia.video || this.remoteMedia.screen)));
      this.room.classList.toggle('remote-sharing', this.remoteMedia.screen);
      $('remote-media-state').textContent = [this.remoteMedia.screen ? 'Sharing screen' : !this.remoteMedia.video ? 'Camera off' : '', !this.remoteMedia.audio ? 'Microphone muted' : ''].filter(Boolean).join(' · ');
    }

    sendMediaState() { this.send('media_state', { audio: !this.micMuted, video: !this.cameraOff, screen: this.sharing }); }

    async toggleMic() {
      if (!this.joined || this.terminal || this.micBusy) return;
      this.micBusy = true;
      const generation = this.mediaGeneration;
      try {
        let track = this.localStream.getAudioTracks().find(item => item.readyState !== 'ended');
        if (!track) {
          const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
          if (this.terminal || generation !== this.mediaGeneration) { stopStream(stream); return; }
          track = stream.getAudioTracks()[0];
          this.localStream.getAudioTracks().forEach(item => this.localStream.removeTrack(item));
          this.localStream.addTrack(track);
          this.watchLocalTracks(stream);
          this.micMuted = false;
          const sender = this.pc?.getSenders().find(item => item.track?.kind === 'audio');
          if (sender) await sender.replaceTrack(track);
        } else this.micMuted = !this.micMuted;
        track.enabled = !this.micMuted;
        this.updateControls();
        this.sendMediaState();
      } catch { if (!this.terminal) this.notice('Microphone access is unavailable. Check your device and browser permissions.'); }
      finally { this.micBusy = false; }
    }

    async toggleCamera() {
      if (!this.joined || this.terminal || this.cameraBusy) return;
      this.cameraBusy = true;
      $('toggle-camera').disabled = true;
      const generation = this.mediaGeneration;
      try {
        let track = this.localStream.getVideoTracks().find(item => item.readyState !== 'ended');
        if (!track) {
          const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user' }, audio: false });
          if (this.terminal || generation !== this.mediaGeneration) { stopStream(stream); return; }
          track = stream.getVideoTracks()[0];
          this.localStream.getVideoTracks().forEach(item => this.localStream.removeTrack(item));
          this.localStream.addTrack(track);
          this.watchLocalTracks(stream);
          this.cameraOff = false;
        } else this.cameraOff = !this.cameraOff;
        track.enabled = !this.cameraOff;
        if (!this.sharing) { this.outboundVideo = track; await this.replaceVideo(track); }
        this.updateLocalPreview();
        this.updateControls();
        this.sendMediaState();
      } catch { if (!this.terminal) this.notice('Camera access is unavailable. Your audio can continue; check browser permissions to enable the camera.'); }
      finally { this.cameraBusy = false; $('toggle-camera').disabled = false; }
    }

    async replaceVideo(track) {
      const sender = this.videoSender, pc = this.pc;
      if (!sender) return;
      try { await sender.replaceTrack(track); }
      catch { if (this.pc === pc && !this.terminal) this.reconnectPair(); }
    }

    async toggleShare() {
      if (!this.joined || this.terminal || this.screenBusy) return;
      if (this.sharing) { await this.stopSharing(); return; }
      this.screenBusy = true;
      $('toggle-share').disabled = true;
      const generation = this.mediaGeneration;
      try {
        const stream = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: { ideal: 15, max: 30 } }, audio: false });
        if (this.terminal || generation !== this.mediaGeneration) { stopStream(stream); return; }
        const track = stream.getVideoTracks()[0];
        if (!track) { stopStream(stream); return; }
        this.shareStream = stream;
        this.sharing = true;
        this.outboundVideo = track;
        track.onended = () => { if (this.shareStream === stream && !this.terminal) this.stopSharing(); };
        await this.replaceVideo(track);
        if (this.terminal) return;
        this.updateLocalPreview();
        this.updateControls();
        this.sendMediaState();
        this.notice('You are sharing your screen. Avoid showing information you do not intend to share.');
      } catch (error) {
        if (!this.terminal && !['NotAllowedError', 'AbortError'].includes(error.name)) this.notice('Screen sharing is not available in this browser or device. Your call can continue.');
      } finally { this.screenBusy = false; $('toggle-share').disabled = false; }
    }

    async stopSharing() {
      const stream = this.shareStream;
      if (!stream) return;
      this.shareStream = null;
      this.sharing = false;
      stream.getTracks().forEach(track => { track.onended = null; track.stop(); });
      this.outboundVideo = this.localStream?.getVideoTracks().find(track => track.readyState !== 'ended') || null;
      if (this.outboundVideo) this.outboundVideo.enabled = !this.cameraOff;
      await this.replaceVideo(this.outboundVideo);
      if (this.terminal) return;
      this.updateLocalPreview();
      this.updateControls();
      this.sendMediaState();
    }

    updateLocalPreview() {
      const preview = $('local-preview');
      preview.hidden = !this.localStream;
      const visible = this.sharing || (!this.cameraOff && Boolean(this.outboundVideo));
      preview.classList.toggle('is-sharing', this.sharing);
      preview.classList.toggle('is-camera-off', !visible);
      $('local-camera-off').hidden = visible;
      $('local-video').srcObject = this.shareStream || this.localStream;
      $('local-video').play().catch(() => {});
      $('preview-label').textContent = this.sharing ? 'Your screen' : 'You';
      $('local-video').setAttribute('aria-label', this.sharing ? 'Your screen preview' : 'Your camera preview');
    }

    updateControls() {
      $('toggle-mic').setAttribute('aria-pressed', String(this.micMuted));
      $('toggle-mic').setAttribute('aria-label', this.micMuted ? 'Unmute microphone' : 'Mute microphone');
      $('mic-label').textContent = this.micMuted ? 'Unmute' : 'Mute';
      $('toggle-camera').setAttribute('aria-pressed', String(this.cameraOff));
      $('toggle-camera').setAttribute('aria-label', this.cameraOff ? 'Turn camera on' : 'Turn camera off');
      $('camera-label').textContent = this.cameraOff ? 'Camera on' : 'Camera off';
      $('toggle-share').setAttribute('aria-pressed', String(this.sharing));
      $('toggle-share').setAttribute('aria-label', this.sharing ? 'Stop sharing screen' : 'Share your screen');
      $('share-label').textContent = this.sharing ? 'Stop sharing' : 'Share screen';
      this.showChrome();
    }

    watchLocalTracks(stream) {
      stream.getTracks().forEach(track => {
        track.onended = () => {
          if (this.terminal) return;
          if (track.kind === 'video') this.cameraOff = true;
          else this.micMuted = true;
          this.updateLocalPreview();
          this.updateControls();
          this.sendMediaState();
          this.notice(`${track.kind === 'video' ? 'Camera' : 'Microphone'} access stopped. Check your device or browser permissions.`);
        };
      });
    }

    resumeClock() {
      if (this.connectedAt !== null) return;
      this.connectedAt = performance.now();
      $('call-timer').hidden = false;
      this.paintClock();
      clearInterval(this.clockTimer);
      this.clockTimer = setInterval(() => this.paintClock(), 1000);
    }

    pauseClock() {
      if (this.connectedAt !== null) this.connectedMilliseconds += performance.now() - this.connectedAt;
      this.connectedAt = null;
      clearInterval(this.clockTimer);
      this.paintClock();
    }

    paintClock() {
      const seconds = Math.floor((this.connectedMilliseconds + (this.connectedAt !== null ? performance.now() - this.connectedAt : 0)) / 1000);
      $('call-timer').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
    }

    showChrome() {
      this.room.classList.remove('chrome-hidden');
      clearTimeout(this.fadeTimer);
      if (this.room.dataset.state !== 'connected' || matchMedia('(prefers-reduced-motion: reduce)').matches || matchMedia('(pointer: coarse)').matches) return;
      this.fadeTimer = setTimeout(() => {
        if (this.room.dataset.state !== 'connected' || document.activeElement?.closest('.room-chrome,.local-preview') || !$('room-notice').hidden || !$('play-remote').hidden) return;
        this.room.classList.add('chrome-hidden');
      }, 4000);
    }

    bindPreviewDrag() {
      const handle = $('move-preview');
      let drag = null;
      handle.addEventListener('pointerdown', event => {
        if (event.button !== 0) return;
        event.preventDefault();
        const box = $('local-preview').getBoundingClientRect();
        drag = { dx: event.clientX - box.left, dy: event.clientY - box.top, pointer: event.pointerId };
        this.dragged = true;
        handle.setPointerCapture(event.pointerId);
      });
      handle.addEventListener('pointermove', event => { if (drag && drag.pointer === event.pointerId) this.placePreview(event.clientX - drag.dx, event.clientY - drag.dy); });
      const finish = () => { drag = null; };
      handle.addEventListener('pointerup', finish);
      handle.addEventListener('pointercancel', finish);
      handle.addEventListener('lostpointercapture', finish);
      handle.addEventListener('keydown', event => {
        if (event.key === 'Home') { event.preventDefault(); $('local-preview').removeAttribute('style'); this.dragged = false; return; }
        const deltas = { ArrowLeft: [-16, 0], ArrowRight: [16, 0], ArrowUp: [0, -16], ArrowDown: [0, 16] };
        if (!deltas[event.key]) return;
        event.preventDefault();
        const box = $('local-preview').getBoundingClientRect();
        this.dragged = true;
        this.placePreview(box.left + deltas[event.key][0], box.top + deltas[event.key][1]);
      });
    }

    placePreview(x, y) {
      const preview = $('local-preview'), box = preview.getBoundingClientRect();
      const maxX = Math.max(8, innerWidth - box.width - 8);
      const maxY = Math.max(80, innerHeight - box.height - 155);
      preview.style.position = 'fixed';
      preview.style.right = 'auto';
      preview.style.left = `${Math.max(8, Math.min(maxX, x ?? box.left))}px`;
      preview.style.top = `${Math.max(80, Math.min(maxY, y ?? box.top))}px`;
    }

    stopMedia() {
      [this.localStream, this.shareStream].forEach(stream => stream?.getTracks().forEach(track => { track.onended = null; track.stop(); }));
      this.localStream = this.shareStream = this.outboundVideo = null;
      this.sharing = false;
      $('local-video').srcObject = null;
      $('local-preview').hidden = true;
    }

    cleanup() {
      this.terminal = true;
      this.joined = this.joining = false;
      this.mediaGeneration++;
      this.abortIce?.abort();
      clearTimeout(this.reconnectTimer);
      clearTimeout(this.fadeTimer);
      clearTimeout(this.noticeTimer);
      this.disconnectSocket();
      this.resetPeer();
      this.stopMedia();
      $('room-controls').hidden = true;
      $('room-notice').hidden = true;
    }
  }

  const configNode = $('video-room-config');
  if (!configNode || !$('video-room')) return;
  try { new AppointmentRoom(JSON.parse(configNode.textContent)); }
  catch { $('room-status-detail').textContent = 'This appointment could not be prepared. Return to appointments and try again.'; $('join-actions').hidden = true; }
})();
