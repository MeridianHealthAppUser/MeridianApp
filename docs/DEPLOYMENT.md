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
- `CHECKOUT_TEST_CODE`: a discount code that settles the initial consultation at R0 and runs the full patient conversion without a payment provider. It defaults to `devtest` only when `DJANGO_DEBUG=true` and is empty (off) otherwise. Leave it unset on any site real patients can reach.
- `MULTI_PRACTICE_ENABLED=false` and `SINGLE_PRACTICE_SLUG=meridian-health`: keep the application restricted to Meridian Health (also the defaults). Existing other-practice data is not automatically deleted. See [single-practice operation](SINGLE_PRACTICE.md).
- `REDIS_URL` (or `VIDEO_REDIS_URL`): private shared Redis for video signalling and expiring room presence; video is unavailable in production without it.
- `CELERY_BROKER_URL` (optional): the broker for background tasks, such as emailing PathCare blood-test forms. It can point at the same Redis as `REDIS_URL`, but `REDIS_URL` alone does not turn it on. When you set it, also run a **worker** component from the same image with `celery -A config worker --loglevel=info` and the same environment as the web component; failed emails are then retried. Without it, tasks run inside the web request and a failed email is marked failed straight away, to be resent from the clinical log.
- `DJANGO_EMAIL_HOST` and the other `DJANGO_EMAIL_*` settings: the transactional email (Brevo) used for password reset links, PathCare forms, welcome emails, and message and appointment notifications. Until a host is set, production sends no email: Forgot password points people to support, no notifications go out, and PathCare forms are kept in the clinical log marked not emailed (nothing is printed to the server log), ready to resend once email is set up.
- `DJANGO_SITE_URL`: the app's public address, such as `https://app.meridianhealth.co.za`, for the links and logo in emails sent outside a page request (notifications and reminders). Set it to `${APP_URL}` on DigitalOcean. Without it, the first `DJANGO_ALLOWED_HOSTS` entry is used over HTTPS.
- `DJANGO_SUPPORT_EMAIL` (optional): the address every email's "Please do not reply" line points to. Default `support@meridianhealth.co.za`.
- `SPACES_BUCKET`, `SPACES_REGION`, `SPACES_ACCESS_KEY_ID` and `SPACES_SECRET_ACCESS_KEY` (all four or none) and optional `SPACES_PREFIX`: the private DigitalOcean Space for patient files (PathCare forms and uploaded lab reports). Without them, production keeps those files in the database. Set them on web, worker and the migrate job. See [Patient files on Spaces](#patient-files-on-spaces).
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

Payments, third-party Zoom integration and courier APIs are not configured. Transactional email (password reset links, PathCare forms and notifications; see [Emails and appointment reminders](#emails-and-appointment-reminders)) uses the `DJANGO_EMAIL_*` settings. Native WebRTC video is included; production Redis and TURN must be provisioned separately. Configure any future integration separately; never infer payment success from a browser flag. The local review-reminder command can be scheduled only after choosing an authorised operational account and reviewing its scope.

## Patient files on Spaces

PathCare blood-test forms and patients' uploaded lab reports are private patient files. Once the four `SPACES_*` settings are set, they are stored in a private DigitalOcean Space. Until then, production keeps them in the database. They are never kept on the container's own disk, which is wiped on every deploy. The app never hands out a link to the Space: every view or download goes through the app's own access checks, and the app fetches the file with its own key. Each file's SHA-256 is checked when it is read.

Deploying this version needs no Space. The migration copies existing forms and reports into patient files in the database and never contacts Spaces.

DigitalOcean has no South African region. Choose the nearest one, ideally the region your app and database are in: `fra1` (Frankfurt), `ams3` (Amsterdam) or `lon1` (London). Patient information is then stored outside South Africa, so confirm that your POPIA cross-border assessment and privacy notice cover it.

1. **Create the Space.** In DigitalOcean, open **Spaces Object Storage** and choose **Create a Spaces Bucket**. Pick the region, give it a name such as `meridian-health-patient-files`, leave the **CDN off** and set file listing to **Restricted**. Never make the Space or any file in it public.
2. **Create a key for that Space only.** Under **Spaces Object Storage → Access Keys**, choose **Create Access Key** and then **Limited access**. Select only this Space with **Read/Write/Delete** permission. Copy the access key ID and the secret now, because the secret is shown only once.
3. **Set the environment variables.** In the app's **Settings**, add these to the web component, the worker (if you run one) and the `migrate` job, with **Encrypt** ticked:
   - `SPACES_BUCKET`: the Space's name
   - `SPACES_REGION`: its region, for example `fra1`
   - `SPACES_ACCESS_KEY_ID` and `SPACES_SECRET_ACCESS_KEY`: the key from step 2
   - Optionally, `SPACES_PREFIX`: the folder in the Space for these files (default `meridian-health`). Changing it later affects new files only.
4. **Deploy.** Saving the settings redeploys the app. From then on, each new file goes to the Space as soon as it is saved. If the Space can't be reached at that moment, the file stays in the database and the error is logged; the next step picks it up.
5. **Move the existing files once.** Open the web component's **Console** and run `python manage.py move_patient_files_to_spaces --dry-run` to see how many files will move. Then run `python manage.py move_patient_files_to_spaces`. Each file is uploaded and checked before it is removed from the database. The command is safe to run again, for example if it reports any failures. It refuses to run until Spaces is configured.
6. **Check.** In the app, open a blood-test report and a PathCare form ("View form").
7. **Plan backups.** Once the files are moved, database backups no longer contain these PDFs. Consider turning on versioning for the Space (DigitalOcean supports it through the S3-compatible API), so that a deleted or overwritten file can be recovered. Also consider a scheduled copy to a second Space or another location, and include the Space in your restore tests.

Local development keeps patient files under `media/patient-files/`. Git ignores that folder and the app never serves it. The test suite keeps them in memory and never reaches a real Space.

## Realtime messages

Deploy the updated application code to enable live patient and team conversations and unread navigation dots on
desktop and mobile. PostgreSQL continues to store messages and read positions. The existing shared `REDIS_URL`
provides the Channels transport; if `VIDEO_REDIS_URL` is also set, that override currently backs the shared channel
layer for both messages and video. App-level environment variables can supply this connection to the web service.
No additional database, Celery worker or TURN relay is needed for realtime text messages.

Keep the web component running Daphne with `config.asgi:application`, as in the Dockerfile. The same service serves
the authenticated, same-origin `/ws/messages/` WebSocket. Its events contain no message text or patient identifiers:
they trigger an authorized HTTP request for the viewer's current message view and unread status. Hidden tabs do not
mark newly arriving messages as read until the messages are actually shown.

Ordinary form POSTs remain available when JavaScript cannot enhance the page. If the live connection drops, the
client reconnects and reconciles visible content at 30-second intervals. Verify using two separate accounts: send a
patient or team message, check it appears without reloading in an open conversation, and check the unread dot on the
recipient's desktop and mobile navigation. Reading it should clear that recipient's dot without clearing another
participant's unread state. Deployment does not require a messaging schema migration; migration `0036` in this
release is for the separate 15-minute email reminder delivery records. Existing components are reused, although
higher traffic and connection counts can increase resource requirements.

## Emails and appointment reminders

With the `DJANGO_EMAIL_*` settings in place, the app emails:

| Email | To | When |
| --- | --- | --- |
| Welcome | The patient | They create their account at the questionnaire checkout |
| New message | Everyone else in the conversation (patient, staff in it, team members) | A message is sent. Once per conversation until they've opened it since; the message itself is never in the email |
| Appointment confirmed | The patient, and the clinician unless they made the booking | An appointment is booked, or moved to an agreed new time |
| Appointment cancelled | The patient (with the practice's reason, when the practice cancelled), and the clinician unless they cancelled it | A booking is cancelled |
| Appointment reminder | The patient and the clinician | 24 hours before it starts |
| Appointment starting soon | The patient and the clinician | When the appointment enters its final 15 minutes, independently of the booking confirmation or day-before reminder |
| PathCare form | The patient | A doctor requests blood tests, or resends the form |

Patients are emailed only when they have an active login, and staff only while active in the practice. Each email links into the app. `templates/emails/` holds their look; `care/notifications.py` decides who gets what.

Reminders need a **scheduled job**, because nothing else runs on a timer. The existing Valkey connection does not
schedule work. This command sends the starting-soon emails itself; it needs no Celery worker or additional database:

1. Add a **job** component to the app on DigitalOcean, or add the `appointment-reminders` job from `.do/app.yaml.example` to the app spec.
2. Build it from the same repository and Dockerfile as the web component, with run command `python manage.py send_appointment_reminders`.
3. Under **Job Trigger**, choose **On a schedule**, cron `*/15 * * * *` (every 15 minutes, the shortest App Platform allows), time zone `Africa/Johannesburg`. This lines up with the app's quarter-hour appointment slots. Container startup and email delivery take additional time, so an email arrives approximately 15 minutes before the appointment, not at an exact guaranteed second. An appointment imported with an off-quarter-hour start can receive it later within the final 15 minutes. A booking created after the scheduled run may have only its immediate confirmation before the appointment starts.
4. Give it the same environment as the web component: `DATABASE_URL`, `DJANGO_SECRET_KEY`, `DJANGO_SITE_URL`, the `DJANGO_EMAIL_*` settings, `MULTI_PRACTICE_ENABLED`, `SINGLE_PRACTICE_SLUG` and `DJANGO_DEBUG=false`, plus `CELERY_BROKER_URL` if you run a worker.

The **day-before reminder** retains its existing behavior: an appointment booked less than 24 hours ahead gets a
confirmation instead of a separate day-before email. The **starting-soon reminder** is separate and still goes to both
the patient and booked clinician, including a clinician who made the booking. Both emails link to the appointment's
detail page, where the eligible participant can join the video call during the permitted joining window.

Starting-soon deliveries are recorded in PostgreSQL for each recipient and booked start only after the email server
accepts the email. Overlapping or repeated job runs skip successful recipients. If one recipient's send fails, only
that recipient is retried, after 60 seconds and then another 120 seconds within the same job invocation. An invocation
with failures can therefore run for about three additional minutes, plus the email attempts; successful runs do not
wait. Retries reload the booking and stop when it has started, been cancelled or moved out of the due window. They
also use the currently booked clinician. A changed start permits a fresh reminder; rebooking through the app creates
a new appointment. If a clinician changes after the patient was reminded for that start, the new clinician receives
their own reminder and the patient is not emailed twice. A process or database failure after the mail server accepts
an email but before its success record commits can still cause a duplicate on retry; SMTP does not offer an atomic
transaction with the application database.

This feature needs the normal pre-deploy `python manage.py migrate --noinput` step for the successful-delivery table.
You can run `python manage.py send_appointment_reminders` by hand from the web component's **Console**, but it sends
real emails for currently due appointments. Without email configured it sends nothing and leaves reminders eligible.
Do not set `CELERY_BROKER_URL` without also running the Celery worker: ordinary notifications and the day-before
reminder use that queue when configured.

DigitalOcean scheduled jobs are charged only while running, at the selected component size's rate. The minimum
shared size is currently $5/month if running continuously; a short scheduled job is prorated for its runtime. Confirm
the component estimate in DigitalOcean before adding it. For tighter timing than the platform's 15-minute schedule,
an always-running scheduler would be a separate component and additional cost. See the official
[scheduled-job setup](https://docs.digitalocean.com/products/app-platform/how-to/manage-jobs/) and
[App Platform pricing](https://docs.digitalocean.com/products/app-platform/details/pricing/).

Locally, every email is printed in the terminal running `runserver` instead of being sent. Run `python manage.py send_appointment_reminders` to see due day-before and starting-soon reminders. `seed_demo` sends nothing.

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

For subsequent accounts, use **Users → Add user** in `/admin/`. Enter the person's
name, email and password, then choose **Doctor**, **Practice administrator**,
**Super admin**, or **Patient**. Saving creates the login and its active Meridian
membership or linked patient record together. Staff enter the practice workspace;
patients enter their care portal. The configured practice must already be active.
In multi-practice mode, also choose the practice. Creating access requires the
corresponding membership or patient add permission, in addition to user permissions.

The separate **Django administration (advanced)** options are only shown to
technical superusers. Choosing the practice **Super admin** role does not enable
those technical privileges. Existing accounts still use their membership or
patient record to manage practice access; adding a user does not repair older,
unlinked accounts.

Technical superusers with active Meridian membership can use **Work as** on the
admin overview or in the portal account menu to select **Super admin**,
**Doctor**, or **Practice administrator**. This changes only their own real
membership role, across all browser sessions. They retain their original
identity and Django administration access. It does not impersonate a different
staff member, grant patient identity or bypass authorship, signed-record,
appointment-participant and practice-boundary checks. Doctor actions are
available while the account is in Doctor mode; switching away removes that
account's active Doctor role (including its eligibility for new doctor
assignments). Leaving Doctor mode is blocked while existing clinical commitments
still require that account to remain an active doctor; the workspace explains
the conflict and leaves access unchanged. Technical administration remains
available throughout. Every successful change is audited. Ordinary staff
cannot use this control.

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
