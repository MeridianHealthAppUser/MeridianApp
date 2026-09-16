# Meridian Health only

The default runtime configuration is:

```sh
MULTI_PRACTICE_ENABLED=false
SINGLE_PRACTICE_SLUG=meridian-health
```

These defaults apply when neither variable is set. Environment changes require restarting every web/ASGI process. On DigitalOcean, use the same configuration for all components. The database remains PostgreSQL in production; this change does not require a schema migration.

## What changes

- Staff and patient practice switchers are absent. Old switch requests are forbidden.
- Practice list, creation and editing pages are disabled, including Company management in Django's technical admin. Users and roles for Meridian remain manageable.
- Patient directories, history, exports, reports, account profiles and access histories are limited to Meridian. Forged combined-report requests are rejected.
- Questionnaires and public policy pages use Meridian automatically. Old enquiry links for another practice are inaccessible.
- Video access rechecks the enabled practice, including existing WebSocket connections.
- An existing session pointing at another practice resolves to Meridian only if its user already has the appropriate Meridian access. No membership is granted automatically.
- Other-practice-only accounts cannot sign in through the application login. Shared users retain their existing Meridian roles and passwords.
- Missing or inactive `meridian-health` fails closed: the application does not select some other practice.
- The development demo command creates only Meridian and does not recreate/reactivate Orion in this mode. Do not rerun demo seeding against a working database.

## Data and reversibility

The database models and foreign keys remain intact. Disabling access does not delete clinical records, merge patient identities, cancel existing bookings or rewrite shared accounts. Internal conflict checks continue to consider existing bookings to avoid double-booking a shared clinician or patient.

Removing another practice's stored data is a separate, explicitly scoped operation. Back up the database first, confirm the exact practice and linked records, and preserve shared user identities. Do not delete a shared user to remove one practice. Protected clinical relationships must not be bypassed casually.

For a deliberately restored multi-practice deployment, set `MULTI_PRACTICE_ENABLED=true` and restart all application processes. Existing active memberships then work again; archived or deleted practices are not recreated by the feature flag. This option is deployment configuration, not an in-app setting.

## Verification

```sh
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test --noinput
```

The original multi-practice fixture classes explicitly enable that configuration so retained behavior stays covered. Dedicated `test_single_practice*` suites override it to the default single-practice configuration, covering normal access, forged requests and restoration. No test needs to use the working database.
