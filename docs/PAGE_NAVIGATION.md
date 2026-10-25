# Page navigation

Authenticated navigation updates the address and destination loading state immediately, then loads authorized Django-rendered content. Django, PostgreSQL and the existing Daphne service remain responsible for the application. This change requires no new infrastructure, dependency or database migration.

## Page loading

- Internal HTML destinations use one shared navigation controller, including Back/Forward, filters, errors and retry. External links, downloads, account changes, video rooms and form POSTs retain native browser navigation.
- The 40 registered main destinations in `portal/page_navigation.py` return a lightweight shell on a browser document request. Their existing authentication, practice and applicable object/role checks run before the shell. Message reads, audit events, flash messages and one-time booking confirmations are not consumed by shell rendering.
- Detail/editor routes without a registered lightweight access resolver keep their complete server-rendered response on a direct visit or hard refresh. They still participate in internal content navigation through their original authorized view.
- Super Admin dashboard metrics are a separate deferred section: appointment/task content arrives first, then the counts load. The client permits at most two section requests at once. This is the first registered deferred section; other pages currently return their main content together.
- A page that runs another site's script, such as Peach's card form, always loads as a complete page: the client only adds same-origin `/static/` scripts.
- `?__full=1` requests a complete server-rendered page. Shells include a no-JavaScript link to this fallback; load errors also provide an “Open full page” link.

## Server contract and access

`GET` with `X-Meridian-Navigation: content` invokes the original view and returns JSON containing `version`, `canonical_url`, `scope`, `layout`, `build`, `title`, `html`, `assets`, navigation metadata and outside-content message/notification markup. `html` contains the rendered `main#workspace-content` contents. Styles and scripts are collected from the whole rendered document and restricted to the same origin.

The client sends `X-Meridian-Navigation-Scope`. Staff scope includes the active practice, account, membership, role and clinician type; patient scope includes the practice, owned patient record and account. Scope mismatch returns 409 before page-specific data is built. These markers detect stale tabs; they never grant permission. Ordinary tenant, assignment, clinical, conversation-membership and CSRF checks still apply.

Same-origin server redirects return an explicit `redirect_url`; the client performs a normal navigation. Authorization and missing-page errors retain their HTTP status. Shells and content/section responses are private and `no-store`, with the relevant request headers included in `Vary`.

A section request uses `X-Meridian-Navigation: section` and `X-Meridian-Section: dashboard-metrics` at the dashboard URL, with the same scope header. The section has its own Super Admin gate and returns scoped HTML. Arbitrary section names are rejected.

The collected static manifest identifies a production build. Without a manifest, a cached SHA-256 fingerprint covers application Python, templates, static files, the Dockerfile and requirements. A replacement process recomputes this fingerprint; Git metadata, environment files, local databases and patient uploads are excluded. A changed build causes a full navigation instead of mixing old JavaScript initializers with new HTML.

## Controller lifecycle

Page scripts register through `MeridianPages.register(name, initialize)`. The initializer receives a scope for listeners, timers, animation frames, cleanup and background fetches. Navigation disposes those resources before replacing content, then mounts the registered controllers against the new document content. GET cancellation includes response-body loading; stale responses cannot replace a later destination. Submitted mutations are not automatically aborted or retried by navigation.

## Deployment and rollback

`PAGE_NAVIGATION_ENABLED` defaults to `true`. Deploy the application normally, including `collectstatic`. No additional App Platform component, Valkey database or scheduled job is needed.

To roll back this enhancement, set `PAGE_NAVIGATION_ENABLED=false` on the web service and redeploy/restart with that setting. New page responses use the complete SSR path; reload existing tabs to discard the already loaded navigation controller. The ordinary forms and access checks remain available.

## Verification

Run against disposable test databases and synthetic browser fixtures:

```sh
python manage.py test portal.test_page_navigation practices.test_request_context --noinput
MERIDIAN_PLAYWRIGHT_PATH=<installed-playwright-package> node scripts/test_app_navigation.cjs
MERIDIAN_PLAYWRIGHT_PATH=<installed-playwright-package> node scripts/test_page_lifecycle.cjs
MERIDIAN_NAVIGATION_BROWSER=1 MERIDIAN_PLAYWRIGHT_PATH=<installed-playwright-package> \
python manage.py test portal.test_page_navigation --noinput
```

Checks cover immediate URL/loading changes, late responses, Back/Forward, errors, scoped access, lifecycle cleanup, deferred sections, one-time state, ordinary SSR fallback and build changes. Browser behavior under artificial latency is tested separately from production server latency; this change alone does not establish that PostgreSQL or Daphne caused a particular delay.

The final navigation run passed 19 tests, including six journeys using actual Django-rendered responses and assets for patient, clinician and Super Admin accounts on desktop and mobile. The shared client suite passed 44 scenarios across Chrome and WebKit, alongside four lifecycle checks and 16 existing messaging scenarios. Access results matched ordinary rendering in 164 comparisons across the 41 shell routes and four account roles. The full backend regression run passed 1,486 tests with 17 expected optional skips; the final navigation/browser run was executed separately. Production static collection and Django system checks passed. These checks use disposable data and do not replace a smoke test after deployment.
