# Native video consultations

Meridian includes its own one-to-one appointment room. The booked doctor and patient use their existing logins; no Zoom account, invitation email or third-party video SDK is required. Camera, microphone and screen sharing use browser WebRTC. Django handles authenticated signalling, not the media stream.

## Joining locally

1. Install updated dependencies: `.venv/bin/python -m pip install -r requirements.txt`.
2. Apply `.venv/bin/python manage.py migrate`, then `.venv/bin/python manage.py runserver`. Daphne now serves HTTP and WebSockets on port 8000. Restart an old development server once if necessary.
3. Sign in as the booked doctor in one browser and the patient in a separate browser/private profile. Use a booked appointment starting within five minutes or currently in progress.
4. Open **Schedule → Appointment details** or **Patient → Appointments** and choose the video join action. Administrative roles do not grant room entry.
5. Opening the room does not activate devices. Choose **Join with camera** or **Join with audio only**, then grant browser permissions. The other participant gets an in-app banner while signed in.
6. Use microphone/camera controls, screen sharing where supported, and **Leave** to finish. Closing the page also stops local media. Leaving does not mark attendance or sign a clinical note.

The room URL is `/video/appointments/<appointment-id>/`. The practice is explicitly displayed and access is checked independently of the practice selected in another tab. Joining does not silently switch that selection. A second tab for the same person replaces the older connection; it cannot occupy a third slot.

Rooms open five minutes before the scheduled start and close at the scheduled end. Cancelled, completed, missed, inactive or mismatched records cannot enter. Access is rechecked during the call. Signing out, changing a password or losing access ends signalling; these checks do not replace locking an unattended device.

## Connection behaviour and history

The second distinct participant initiates. Server-issued room generations reject stale offers and ICE candidates when someone reconnects. The client buffers early ICE, serialises signalling, retries interrupted signalling and gives transient media disconnections a short recovery window. Screen sharing replaces the outgoing video track and restores the intended camera state when it stops.

Invitations are visual in-app banners only. They do not activate devices, autoplay audio, send email or notify a logged-out/locked device. This is not an emergency notification system.

Appointment details show company-scoped **signalling connection times**, not proof that both participants exchanged media or completed a consultation. History tables contain no recordings, transcripts, SDP, ICE payloads or camera/microphone content.

## HTTPS, Redis and TURN

Camera/microphone access requires HTTPS except for trusted local development origins such as localhost. Screen sharing also depends on browser support and an explicit user gesture. Plain HTTP LAN addresses are unsuitable for device testing. See [camera access](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia) and [screen sharing](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getDisplayMedia).

Production settings:

- `VIDEO_ENABLED=true`
- `REDIS_URL` or `VIDEO_REDIS_URL`: private authenticated Redis, shared by every app instance; use TLS where appropriate. Channel delivery and atomic expiring room presence use the same configured backend.
- `VIDEO_TURN_URLS`: comma-separated coturn-compatible relay URLs, e.g. `turn:relay.example.com:3478?transport=udp,turns:relay.example.com:5349?transport=tcp`.
- `VIDEO_TURN_SECRET`: private shared REST-auth secret of at least 32 characters, matching the relay. Never put it in JavaScript, source control or public configuration.
- `VIDEO_TURN_CREDENTIAL_TTL`: temporary credential lifetime, default 3600 seconds, accepted range 60–14400. Choose a lifetime sufficient for appointments; new credentials are obtained on reconnect.
- `VIDEO_ICE_TRANSPORT_POLICY=all`: direct media with relay fallback; `relay` requires the configured relay.
- `VIDEO_STUN_URLS`: STUN endpoints. Local defaults use the two public STUN servers in the supplied blueprint. An empty value uses host candidates only for isolated local tests.

A CSRF-protected, participant-authorised POST issues temporary TURN credentials. Each contains an expiry and random identifier, not a patient name or account ID. The shared secret stays server-side. This follows [coturn's REST credential mechanism](https://github.com/coturn/coturn/blob/master/README.turnserver).

Without TURN, direct/local calls can work, but restrictive NATs or firewalls may prevent connections. Setting a URL does not create a relay. TURN media ports need a separate suitable relay host/service; an HTTP-only App Platform service is insufficient. Test two real devices on different networks before live use.

In-memory signalling and presence support one local process only. With `DEBUG=False` and no Redis, video is unavailable rather than silently using isolated process memory. Redis holds short-lived signalling queues and presence; secure it and review persistence/logging configuration. See the [Channels cross-process backend guidance](https://channels.readthedocs.io/en/stable/topics/channel_layers.html).

## Deployment and boundaries

The optional Chrome regression suite can be run with `MERIDIAN_PLAYWRIGHT_PATH=/path/to/playwright python manage.py test video.test_room_presentation`. It uses synthetic media, including two real browser peer connections, without a live app database. Set `VIDEO_TEST_REDIS_URL` to a disposable Redis test database to include atomic cross-client presence checks in the Django suite. Ordinary tests skip these checks when their optional dependencies are not configured.

The Dockerfile runs Daphne with bounded WebSocket frame/message sizes. A WSGI-only Gunicorn command cannot serve these rooms. The front proxy must pass WebSocket upgrades, preserve the intended host/origin and terminate HTTPS. Only the trusted proxy may supply forwarded headers. See [Channels deployment guidance](https://channels.readthedocs.io/en/stable/deploying.html).

Sockets require exact same-origin validation and current authenticated sessions. Only the appointment's two participants can signal or receive its invitation. Payload types, sizes and rates are bounded; replaced sockets and expired presence cannot act as current participants. WebRTC handles media transport; this is not a separately audited end-to-end-encryption or clinical certification claim.

No external relay, domain, certificate, recording service, email provider or production deployment is provisioned automatically. Test device support and remote-network connectivity, and review privacy, consent and operational policies before real patient use.
