# Page navigation

Authenticated navigation updates the address and destination loading state immediately, then loads authorized Django-rendered content. Django, PostgreSQL and the existing Daphne service remain responsible for the application. This change requires no new infrastructure, dependency or database migration.

## Page loading

- The server explicitly declares 88 HTML destinations for the shared navigation controller, including detail/editor pages, Back/Forward, filters, errors and retry. The initial document supplies only its layout's paths (19 patient or 69 staff); responses do not repeat them. External links, downloads, account changes, video rooms and form POSTs retain native browser navigation. Unknown routes are not fetched speculatively, and the server rejects content/section requests to native destinations before invoking their views. An inventory test requires all 116 current portal GET views to choose HTML or native/API transport explicitly.
- The 41 registered main destinations in `portal/page_navigation.py` return a lightweight shell on a browser document request. Their existing authentication, practice and applicable object/role checks run before the shell. Message reads, audit events, flash messages and one-time booking confirmations are not consumed by shell rendering.
- Detail/editor routes without a registered lightweight access resolver keep their complete server-rendered response on a direct visit or hard refresh. They still participate in internal content navigation through their original authorized view.
- Super Admin dashboard metrics are a separate deferred section: appointment/task content arrives first, then the counts load. The client permits at most two section requests at once. This is the first registered deferred section; other pages currently return their main content together.
- `?__full=1` requests a complete server-rendered page. Shells always show an “Open full page” link, including when JavaScript is enabled but a required script or browser capability is unavailable. Automatic fallbacks bypass shell hydration to prevent repeated reloads during persistent content/proxy failures.
- Once an authorized complete SSR document initializes successfully, its optional history update removes `__full=1` without another request. If scripts or history updates are unavailable, the complete document remains usable with the flag retained.

## Server contract and access

`GET` with `X-Meridian-Navigation: content` invokes an eligible original view and returns JSON containing `version`, `canonical_url`, `scope`, `layout`, `build`, `title`, `html`, `assets` and outside-content message/notification markup. `html` contains the rendered `main#workspace-content` contents. Styles and scripts are collected from the whole rendered document and restricted to the same origin. Only the initial document includes `navigation_paths`; the client compiles those patterns once and retains them until a build change causes a full reload. The patterns declare transport eligibility only; original view permissions still authorize every content response.

The client sends `X-Meridian-Navigation-Scope`. Staff scope includes the active practice, account, membership, role and clinician type; patient scope includes the practice, owned patient record and account. Scope mismatch returns 409 before page-specific data is built. These markers detect stale tabs; they never grant permission. Ordinary tenant, assignment, clinical, conversation-membership and CSRF checks still apply.

Same-origin server redirects return an explicit `redirect_url`. Eligible page redirects use the content transport with a bounded redirect count; login and native destinations use normal navigation. Deferred sections recognize login redirects before validating section markup. Authorization and missing-page errors retain their HTTP status. Shells and content/section responses are private and `no-store`, with the relevant request headers included in `Vary`.

A section request uses `X-Meridian-Navigation: section` and `X-Meridian-Section: dashboard-metrics` at the dashboard URL, with the same scope header. The section has its own Super Admin gate and returns scoped HTML. Arbitrary section names are rejected.

The collected static manifest identifies a production build. Without a manifest, a cached SHA-256 fingerprint covers application Python, HTML templates, recognized static file types, the Dockerfile and requirements. A replacement process recomputes this fingerprint; hidden/cache files, Git metadata, environment files, local databases and patient uploads are excluded. A changed build causes a full navigation instead of mixing old JavaScript initializers with new HTML.

## Controller lifecycle

Page scripts register through `MeridianPages.register(name, initialize)`. The initializer receives a scope for listeners, timers, animation frames, cleanup and background fetches. Navigation disposes those resources before replacing content, then mounts the registered controllers against the new document content. GET cancellation includes response-body loading; stale responses cannot replace a later destination. Submitted mutations are not automatically aborted or retried by navigation.

Controllers for retained shell elements register with `{persistent: true}`. Incoming-call notifications, the account menu and toasts remain active during soft navigation; `dispose({all: true})` ends them on a real page exit. Existing call banners and unclosed error toasts remain available while the destination loads. Genuine network disconnects still have no server-side invitation replay. Message-page controllers continue to dispose and reload their current database state.

Scope cancellation methods (`clearTimeout`, `clearInterval`, `cancelAnimationFrame`) remove cancelled handles from tracking immediately. Scroll history writes are debounced, and an optional scroll-save failure cannot reload an edited page. An explicit navigation whose history write is rejected uses the full-page fallback after the ordinary unsaved-form check. Practice-switcher destinations track the current URL, and stylesheets already in the required order remain in place.

Optional address updates from search/dialog controllers never trigger a full reload when rejected, preserving unsaved drafts. A failed scroll save is retried after the next successful navigation. Managed history entries include a branch marker so offset-based restoration cannot trust positions across native hash entries or unknown history branches. If a cancelled traversal has no trustworthy position, a new entry restores the editor URL and retained draft without overwriting the unknown destination or guessing a direction. This necessarily truncates that entry's Forward branch. If the browser also rejects restoration, the retained editor is temporarily disabled with an explicit retry control; forms cannot submit to the mismatched address. Retrying does not reset the original dirty-form baseline.

## Deployment and rollback

`PAGE_NAVIGATION_ENABLED` defaults to `true`. Deploy the application normally, including `collectstatic`. No additional App Platform component, Valkey database or scheduled job is needed.

To roll back this enhancement, set `PAGE_NAVIGATION_ENABLED=false` on the web service and redeploy/restart with that setting. New page responses use the complete SSR path; reload existing tabs to discard the already loaded navigation controller. The ordinary forms and access checks remain available.

## Verification

Run against disposable test databases and synthetic browser fixtures:

```sh
python manage.py test portal.test_page_navigation practices.test_request_context --noinput
MERIDIAN_PLAYWRIGHT_PATH=<installed-playwright-package> node scripts/test_app_navigation.cjs
MERIDIAN_PLAYWRIGHT_PATH=<installed-playwright-package> node scripts/test_page_lifecycle.cjs
MERIDIAN_PLAYWRIGHT_PATH=<installed-playwright-package> node scripts/test_live_messages.cjs
MERIDIAN_NAVIGATION_BROWSER=1 MERIDIAN_PLAYWRIGHT_PATH=<installed-playwright-package> \
python manage.py test portal.test_page_navigation --noinput
```

The rendered browser checks require Chrome and Playwright's matching WebKit installation. Checks cover immediate URL/loading changes, late responses, sustained scrolling, bounded fallbacks, Back/Forward, errors, scoped access, persistent shell controls, lifecycle cleanup, deferred sections, one-time state, ordinary SSR fallback and build changes. Browser behavior under artificial latency is tested separately from production server latency; this change alone does not establish that PostgreSQL or Daphne caused a particular delay.

Navigation validation includes 110 shared browser scenarios across Chrome and WebKit, six lifecycle scenarios, and six journeys using actual Django-rendered responses and assets for patient, clinician and Super Admin accounts on desktop and mobile. Twelve additional rendered-shell checks verify native fallback with unavailable scripts/capabilities, disabled JavaScript and persistent proxy errors. Automated access results match ordinary rendering in 164 comparisons across all 41 shell routes and four account roles; registry inventory checks classify all 116 portal GET views, including 88 HTML destinations. These checks use disposable data and do not replace a smoke test after deployment. Fresh full regression results are recorded in [VERIFICATION.md](VERIFICATION.md).
