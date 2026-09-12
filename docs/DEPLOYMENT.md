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
- `REDIS_URL` (or `VIDEO_REDIS_URL`): private shared Redis for video signalling and expiring room presence; video is unavailable in production without it.
- Configure a separate TURN relay for reliable cross-network video. See [native video setup](VIDEO_CONSULTATIONS.md).
- Review `DJANGO_SECURE_HSTS_SECONDS` before enabling a long HSTS lifetime on a real domain.

Do not use the default local SQLite database in an ephemeral production container. Do not run the development server or demo seeder in production. Missing production secret configuration prevents startup. The app trusts the platform's forwarded HTTPS header, so only the trusted proxy should reach the application port.

Recommended release order:

1. Back up the target database and verify that restore procedures work.
2. Build the image and run checks/tests.
3. Run `python manage.py migrate --noinput` as the pre-deploy job.
4. Start the web service and verify health, HTTPS, login, practice switching and static files.
5. Perform a role-separated smoke test before granting real users access.

HSTS preload is deliberately not enabled automatically. A deployment check may report that the domain is not configured for browser preload; review domain-wide HTTPS policy before opting in.

Payments, outbound email, third-party Zoom integration and courier APIs are not configured. Native WebRTC video is included; production Redis and TURN must be provisioned separately. Configure any future integration separately; never infer payment success from a browser flag. The local review-reminder command can be scheduled only after choosing an authorised operational account and reviewing its scope.

## Validation commands

```sh
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test --noinput
python manage.py collectstatic --noinput
```

Run `python manage.py check --deploy` with the actual production settings, not the default development environment. Optional browser layout tests use `MERIDIAN_PLAYWRIGHT_PATH` pointing to an installed Playwright package and a local Chrome browser; ordinary CI does not require that optional setup.

The source and migrations can be deployed, but technical checks do not establish suitability for live medical use. Complete the clinical, privacy, security and operational prerequisites in the user guide before handling real patient data.
