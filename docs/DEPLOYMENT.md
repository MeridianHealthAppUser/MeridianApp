# Deployment preparation

Deployment files are prepared for the repository `MeridianHealthAppUser/MeridianApp`. A source-code push does not deploy a DigitalOcean application, provision a production database or configure external services.

## GitHub

Review changes before committing and pushing. `.gitignore` excludes local databases, `.env`, virtual environments and collected static assets. Never commit real patient exports, secrets or demo database files. The GitHub Actions workflow runs Django checks, migration-drift checks and tests against PostgreSQL 16, with isolated Redis presence checks.

## Container and DigitalOcean

The Dockerfile installs the pinned dependencies, collects hashed/compressed static assets and runs Daphne as an unprivileged user, serving HTTP and native video WebSockets. Runtime secrets are not baked into the image. `/health/` returns only a database-connectivity status and does not expose configuration or patient information. Only this minimal endpoint is exempt from HTTPS redirection so internal platform health probes work; application pages still require HTTPS in production.

DigitalOcean App Platform supports Dockerfile source builds and a `PRE_DEPLOY` job for migrations. Use the included `.do/app.yaml.example` as a starting point, confirming access to the configured repository. Configure runtime environment variables for **both** the web component and migration job. See the official [Dockerfile reference](https://docs.digitalocean.com/products/app-platform/reference/dockerfile/) and [job configuration](https://docs.digitalocean.com/products/app-platform/how-to/manage-jobs/).

- `DJANGO_DEBUG=false`
- `DJANGO_SECRET_KEY`: a newly generated, private, high-entropy secret stored in the platform's encrypted configuration
- `DATABASE_URL`: your managed PostgreSQL connection string, with TLS required
- `DJANGO_ALLOWED_HOSTS`: exact application/custom domains
- `DJANGO_CSRF_TRUSTED_ORIGINS`: corresponding HTTPS origins
- `DJANGO_TIME_ZONE=Africa/Johannesburg`
- `MULTI_PRACTICE_ENABLED=false` and `SINGLE_PRACTICE_SLUG=meridian-health`: keep the application restricted to Meridian Health (also the defaults). Existing other-practice data is not automatically deleted. See [single-practice operation](SINGLE_PRACTICE.md).
- `REDIS_URL` (or `VIDEO_REDIS_URL`): private shared Redis for video signalling and expiring room presence; video is unavailable in production without it.
- Configure a separate TURN relay for reliable cross-network video. See [native video setup](VIDEO_CONSULTATIONS.md).
- Review `DJANGO_SECURE_HSTS_SECONDS` before enabling a long HSTS lifetime on a real domain.

Do not use the default local SQLite database in an ephemeral production container. Do not run the development server or demo seeder in production. Missing production secret configuration prevents startup. The app trusts the platform's forwarded HTTPS header, so only the trusted proxy should reach the application port.

Recommended release order:

1. Back up the target database and verify that restore procedures work.
2. Build the image and run checks/tests.
3. Run `python manage.py migrate --noinput` as the pre-deploy job.
4. Start the web service and verify health, HTTPS, login, Meridian-only access and static files. Confirm practice switching/management are unavailable.
5. Perform a role-separated smoke test before granting real users access.

HSTS preload is deliberately not enabled automatically. A deployment check may report that the domain is not configured for browser preload; review domain-wide HTTPS policy before opting in.

Payments, outbound email, third-party Zoom integration and courier APIs are not configured. Native WebRTC video is included; production Redis and TURN must be provisioned separately. Configure any future integration separately; never infer payment success from a browser flag. The local review-reminder command can be scheduled only after choosing an authorised operational account and reviewing its scope.

## Meridian administration and first login

The technical administration at `/admin/` uses pinned `django-jazzmin==3.0.5`
with Meridian's clinical theme, locally served assets and a custom practice
overview. The image's existing `collectstatic` step includes the theme, fonts,
icons and charts; no Node build, chart CDN or new infrastructure is needed.
No database schema migrations are introduced by the theme or role switcher.

After deploying this version, open the DigitalOcean **web service Console**.
Create a technical administrator if you do not already have one:

```sh
python manage.py createsuperuser
```

Link that existing active superuser to Meridian (replace the example email):

```sh
python manage.py bootstrap_practice_admin --email 'your-email@example.com'
```

This explicit, audited command creates the configured practice if missing and
adds its Super Admin membership. It never seeds demo records, changes passwords
or reactivates disabled access. Repeating it preserves an existing active role.
Without a practice membership, Django admin login works but the staff portal
cannot open; an already-authenticated superuser clicking Sign in may see 403.

Technical superusers with active Meridian membership can use **Work as** on the
admin overview or in the portal account menu to select **Super admin**,
**Doctor**, or **Practice administrator**. This changes only their own real
membership role, across all browser sessions. They retain their original
identity and Django administration access. It does not impersonate a different
staff member, grant patient identity or bypass authorship, signed-record,
appointment-participant and practice-boundary checks. Doctor actions are
available while the account is in Doctor mode; switching away removes that
account's active Doctor role (including its eligibility for new doctor
assignments). Every change is audited. Ordinary staff cannot use this control.

Dashboard charts show actual scoped aggregates, respecting model permissions.
The 30/90/180-day window applies to weekly registrations and appointments;
active patients/subscriptions and issued invoice totals describe current
state. Empty periods display zero/empty states, never example metrics.

## Validation commands

```sh
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test --noinput
python manage.py collectstatic --noinput
```

Run `python manage.py check --deploy` with the actual production settings, not the default development environment. Optional browser layout tests use `MERIDIAN_PLAYWRIGHT_PATH` pointing to an installed Playwright package and a local Chrome browser; ordinary CI does not require that optional setup.

The source and migrations can be deployed, but technical checks do not establish suitability for live medical use. Complete the clinical, privacy, security and operational prerequisites in the user guide before handling real patient data.
