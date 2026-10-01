# Local verification — 12 September 2026

## Final regression and package checks

- **PostgreSQL 16: all 990 tests passed, no skips** (97.3 seconds), with Redis atomic-presence checks, Chrome presentation checks, patient editor layouts and timeline interactions enabled.
- **SQLite: 990 tests, 975 passed and 15 expected skips** (31.5 seconds). The skips are 12 optional browser checks, one isolated-Redis check and two PostgreSQL-only concurrent-booking tests; all of these ran in the PostgreSQL verification above.
- Django system checks and dependency checks passed. No migration drift was detected; the additive schema includes `care.0010_compounding_and_review_reminders` and `video.0001_initial`.
- The final Docker application image built successfully, collecting 162 static assets. Its unprivileged Daphne container migrated an empty disposable database and returned 200 for health, proxied HTTPS login and hashed CSS; ordinary HTTP login redirected to HTTPS. Production checks reported only the intentional HSTS-preload warning described below.
- Existing local data and passwords were preserved. Verification databases, servers and containers were separate from the user's development server on port 8000.

The final PostgreSQL run used:

```sh
DATABASE_URL=<isolated-postgresql-test-database> \
VIDEO_TEST_REDIS_URL=<isolated-redis-test-database> \
MERIDIAN_PLAYWRIGHT_PATH=<installed-playwright-package> \
MERIDIAN_TIMELINE_BROWSER=1 \
MERIDIAN_WORKSPACE_FORMS_BROWSER=1 \
python manage.py test --noinput --failfast
```

## Patient web app restructure — 30 September 2026

- **SQLite: 1,152 tests, all passing apart from 15 expected skips** (the optional browser, isolated-Redis and PostgreSQL-only checks). The run includes the new `portal.test_patient_web_app` checks for the unread count, task rules, medication groups, answering a suggested time from My Appointments and logging a weight from Home.
- Home, My Treatment, My Appointments (with and without open times), My Messages (conversation and new message), My Medications (all three groups), the basket, Account settings and Updates were rendered from the local demo patient and inspected at 1280px and 390px. The extra demo data for these renders was rolled back afterwards.
- The optional Playwright checks (`scripts/test_patient_dashboard.cjs`, `scripts/operations_layout_smoke.cjs`) were updated for the new Home layout and versioned stylesheet links. They did not run here, because Playwright is not installed locally.
- No migrations were added and the practice console is unchanged.
- **Brand theme:** 1,154 tests pass apart from the same 15 skips. New checks confirm that the theme loads last on patient pages (and on a patient's own profile and password pages), never on staff pages, and that its two font files exist. A production `collectstatic` into a scratch folder rewrote the theme's font URLs to hashed file names. All patient pages, including blood tests, appointment details, weight history, medical profile and privacy, were re-inspected at 1280px, and the main sections at 390px.

## Patient workspace and final UI verification

- The canonical patient workspace passed 32 role-permitted tab clicks across Doctor, Practice Administrator and Super Admin accounts. Every tab retained the selected patient and was checked at 1440px, 390px and 320px, with no document overflow or JavaScript errors. Mobile patient-section targets are at least 44px high.
- Dedicated consultation, blood-test and appointment detail/forms retain the same patient header and current tab; invalid values remain in context. Task, treatment, delivery and nested compounding actions preserve patient context when opened from a patient tab, including successful POST redirects. Their global entry points retain the practice-wide shell. The editor checks include 39 layouts, plus 18 additional real-clone shared-action layouts. Clinical access is not granted by selecting a tab or supplying a presentation marker.
- Eighteen adversarial workspace tests cover tenant/role boundaries, malformed imported relations, oversized identifiers, private/unsigned clinical content, selected-message receipts and retention of invalid off-page note-tag edits.
- History starts with 20 events and loads older entries inside the bounded timeline panel. Browser checks cover scroll-triggered loading, stable event ordering, duplicate suppression, manual fallback, retry/end states and expired or changed access. The signed cursor and spreadsheet export recheck current permissions and the selected snapshot/filter scope.
- Real `.xlsx` files were independently opened using openpyxl 3.1.5, in both normal and read-only modes, without warnings. Empty sheets, Unicode, long text split across continuation rows, SAST timestamps and leading-zero identifiers round-tripped correctly. Formula-like text remained string cells, never executable formulas. The independent reader was installed only in a temporary external directory and is not a runtime dependency.
- Weight history uses real date-spaced values with exact paginated measurements; empty, single-point and constant-weight states were checked. Reporting charts retain exact aggregate tables and distinguish current snapshots from the selected reporting period. The reporting verification included 56 checks with six desktop/mobile chart layouts.
- The compact patient overview passed 60 focused patient-page/navigation/account tests, including six rendered empty/populated overview layouts. An independent live-clone browser review visited all 12 patient sections at 1440px, 768px, 390px and 320px (48 layouts): no overflow or JavaScript errors. The duplicate practice strip is removed, overview styling stays isolated, and mobile navigation leaves the final content accessible.
- The top-right profile menu and own-profile page were checked across all four roles on desktop and mobile. Profile edits remain limited to the signed-in user's shared name; password, access history and POST sign-out retain their existing protected routes.

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
- GitHub Actions configuration and a DigitalOcean App Platform example are included. This report records local verification; remote CI is a separate result. A source-code push does not provision a production database, TURN service or external application deployment.

## Native video verification

- Real Chrome doctor and patient sessions connected through Daphne and exchanged audio and video packets **in both directions**. The check was repeated with coturn and `iceTransportPolicy=relay`; both selected candidates were confirmed as `relay`, not a direct fallback.
- Both journeys verified explicit device permission, incoming in-app invitations, microphone/camera controls, screen-track replacement and camera restoration, signalling reconnect without new capture, duplicate-tab replacement and track cleanup, leaving and connection history. Desktop, 390px and 320px layouts had no document overflow and controls were at least 44px high.
- The browser regression suite contains 18 scenarios, including two native `RTCPeerConnection` instances and an assertion against the one-way answerer transceiver regression found during end-to-end testing. Synthetic media is used; no patient media was recorded.
- Two separate Daphne workers using PostgreSQL and Redis passed real cross-worker offer delivery, no-echo delivery and duplicate-tab replacement. Unauthorized users and cross-origin requests were rejected. Connection history remained separate from clinical attendance, and contains no SDP or media.
- The additive `video.0001_initial` migration was applied after a local database backup. Browser workflows used a disposable clone; existing source appointments, users and passwords were preserved.

The relay check used an isolated **local** coturn container. It verifies TURN credentials, relay-only configuration and real media transport, but does not replace testing HTTPS devices on separate mobile/home networks against the eventual production relay. See [VIDEO_CONSULTATIONS.md](VIDEO_CONSULTATIONS.md).

## Limits

Passing tests is not a clinical, regulatory or penetration-test certification. Payment collection, outbound email, live prescribing, third-party Zoom integration and courier APIs remain excluded. Native appointment video is provided instead. The questionnaire remains lead-only; the checkout creates a patient login only with the checkout test code. File uploads have basic PDF checks, not malware scanning. Review the live-use prerequisites in [USER_GUIDE.md](USER_GUIDE.md) and [DEPLOYMENT.md](DEPLOYMENT.md) before handling real patient data.
