# Local verification — 12 September 2026

## Core application checks (before native video)

- Final PostgreSQL 16 regression run: **797 tests passed, no skips**, with optional Chrome tests enabled. This includes two real simultaneous-booking tests using separate database connections: competing bookings cannot double-book one doctor or one patient across practices.
- SQLite regression run immediately before the final presentation assertion: **796 tests, 791 passed and 5 expected skips** (three optional browser tests and two PostgreSQL-only locking tests). The final presentation changes also passed their 73-test focused run.
- Django system checks: no issues. Migration drift check: no changes detected.
- Existing local schema is migrated through `care.0010_compounding_and_review_reminders`. Migrations are additive; no demo reset was performed.

The full PostgreSQL run used:

```sh
DATABASE_URL=<isolated-postgresql-test-database> \
MERIDIAN_PLAYWRIGHT_PATH=<installed-playwright-package> \
python manage.py test --noinput --failfast
```

Do not point verification or demo-seeding workflows at a production database. Tests create and drop a separate test database and require appropriately scoped local credentials.

## Browser verification

- A complete cloned-data supply journey passed: product setup → explicit doctor authorisation → patient basket/request → stock receipt → administrator acceptance → allocation → weekly lock → manual dispatch → delivery → patient update. Inventory changed once; switching practices denied access to the previous practice's order.
- 51 route/viewport checks passed alongside that journey.
- The polished interface passed a separate 60-layout check at 1440px, 390px and 320px: no document overflow, duplicate element IDs or JavaScript errors. Current sidebar destinations remained visible; mobile primary actions were at least 44px high.
- Eight final overview checks at 1440px, 768px, 390px and 320px verified the neutral greeting, “Appointments today” label and mobile-only full-width final metric card.
- Overview cards measured 96px high on desktop. Example appointment and empty-task summary panels measured about 127px and 119px, with no forced matching height.
- Desktop and mobile screenshots were visually inspected. Landing/questionnaire/public policy pages do not receive authenticated-workspace style overrides.

Browser workflows used disposable database copies. Existing local accounts, passwords, patients and operational records were not reseeded or replaced. Temporary test servers were stopped; the user's development server on port 8000 was left running.

## Deployment preparation

- Docker image built successfully with hashed/compressed static assets.
- An isolated, unprivileged container migrated an empty disposable database and served the health endpoint, login page and hashed CSS. Application HTTP requests redirect to HTTPS; the minimal internal health probe remains available without a proxy header.
- Container startup was checked without Gunicorn control-socket errors.
- Production Django checks reported only `security.W021`: browser HSTS preload is deliberately not enabled automatically. Review domain-wide HTTPS policy before opting in.
- GitHub Actions configuration and a DigitalOcean App Platform example are included. Neither workflow was executed remotely, and no repository push, production database or external deployment was performed.

## Native video verification

- Real Chrome doctor and patient sessions connected through Daphne and exchanged audio and video packets **in both directions**. The check was repeated with coturn and `iceTransportPolicy=relay`; both selected candidates were confirmed as `relay`, not a direct fallback.
- Both journeys verified explicit device permission, incoming in-app invitations, microphone/camera controls, screen-track replacement and camera restoration, signalling reconnect without new capture, duplicate-tab replacement and track cleanup, leaving and connection history. Desktop, 390px and 320px layouts had no document overflow and controls were at least 44px high.
- The browser regression suite contains 18 scenarios, including two native `RTCPeerConnection` instances and an assertion against the one-way answerer transceiver regression found during end-to-end testing. Synthetic media is used; no patient media was recorded.
- Two separate Daphne workers using PostgreSQL and Redis passed real cross-worker offer delivery, no-echo delivery and duplicate-tab replacement. Unauthorized users and cross-origin requests were rejected. Connection history remained separate from clinical attendance, and contains no SDP or media.
- The additive `video.0001_initial` migration was applied after a local database backup. Browser workflows used a disposable clone; existing source appointments, users and passwords were preserved.

The relay check used an isolated **local** coturn container. It verifies TURN credentials, relay-only configuration and real media transport, but does not replace testing HTTPS devices on separate mobile/home networks against the eventual production relay. See [VIDEO_CONSULTATIONS.md](VIDEO_CONSULTATIONS.md).

## Limits

Passing tests is not a clinical, regulatory or penetration-test certification. Payment collection, outbound email, live prescribing, third-party Zoom integration and courier APIs remain excluded. Native appointment video is provided instead. The questionnaire remains lead-only; dummy checkout cannot create a patient login. File uploads have basic PDF checks, not malware scanning. Review the live-use prerequisites in [USER_GUIDE.md](USER_GUIDE.md) and [DEPLOYMENT.md](DEPLOYMENT.md) before handling real patient data.
