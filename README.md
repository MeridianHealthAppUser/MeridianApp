# Meridian Health

Django care application for **Meridian Health**, with separate staff and patient pages, role-scoped records, clinical workflows, scheduling, messaging, pharmacy operations and reporting. The default deployment is single-practice; practice switching, creation, management and combined reporting are disabled.

The in-app workflows are implemented for local testing. Payment processing, outbound email and other live external integrations are intentionally excluded. Questionnaire submissions remain leads; the dummy checkout does not create a login. Clinical and production governance review is still required before real patient use.

Start with the [current application guide](docs/USER_GUIDE.md), [implementation checklist](docs/IMPLEMENTATION_PLAN.md) and [deployment guide](docs/DEPLOYMENT.md). The detailed checkpoint notes below explain individual workflows.

Native one-to-one video consultations are included: [video setup and usage](docs/VIDEO_CONSULTATIONS.md). Run the updated ASGI/Daphne server for video, with Redis and a configured TURN relay for production.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_demo
.venv/bin/python manage.py runserver
```

Open `/` for the public landing page. Sign in at `/accounts/login/`, then use `/desktop/` or `/mobile/` for staff and `/patient/` for your own care portal. Accounts with both staff and patient records can use the header to move between their workspace and care portal.

The local demo creates Meridian Health only. Demo accounts share password `MeridianDemo!2026`:

| Account | Email | Practice access |
| --- | --- | --- |
| Joshua, Super Admin | joshua.czech@meridianhealth.co.za | Meridian Health |
| Sam, Doctor | sam.marchant@meridianhealth.co.za | Meridian Health |
| Lindiwe, Practice Administrator | lindiwe.mahlangu@meridianhealth.co.za | Meridian Health |
| Nadia, Patient | nadia.m@example.co.za | Own Meridian Health patient record |

`seed_demo` is a development-only demonstration command. Existing passwords are preserved unless you explicitly pass `--reset-passwords`; example records can be refreshed, so do not rerun it against a working database just to obtain new screens. It refuses to run with `DEBUG=False`. Use a separate account created with `manage.py createsuperuser` for Django's site-wide technical administration.

## Separate staff pages checkpoint

**Current deployment:** `MULTI_PRACTICE_ENABLED=false` and `SINGLE_PRACTICE_SLUG=meridian-health` are the defaults, including on PostgreSQL/DigitalOcean. No migration, password reset or reseed is needed. Other-practice records cannot be reached through old sessions, direct links, report filters, intake forms or video rooms. Existing other-practice data is not automatically deleted or merged. The historical multi-practice workflows described below are retained in code only and require explicitly enabling the environment flag. See [single-practice operation](docs/SINGLE_PRACTICE.md).

The overview is still a summary. Primary staff navigation opens separate Django views and templates:

- `/tasks/`: a task table showing tasks assigned to or created by the doctor, or all practice tasks for administrators; filter by workflow status, patient/general type, patient, or free-form tag. Complete a task without leaving the list.
- `/tasks/new/` and `/tasks/<id>/`: create and edit patient-linked or general practice tasks, assign practice staff, record a note, set a due time, and manage reusable labels.
- `/patients/`: the active practice's patient directory, with name/ID/email/record search and doctor filtering.
- `/patients/<id>/`: one patient workspace with separate Overview, History, Appointments, Consultations, Blood tests, Notes, Weight, Messages, Tasks, Payments, Treatment and Deliveries tabs, subject to the current role. Each tab renders only its own content and keeps the same patient header.
- `/patients/<id>/?tab=history`: the selected patient's filtered history. Doctors and practice Super Admins receive the protected clinical timeline and exports; Practice Administrators receive a limited operational history without clinical bodies. The old `/patients/<id>/record/` remains a clinical History alias, not a second patient-detail layout.
- `/schedule/`: switch between a daily appointment table and a month calendar. The date and doctor selection carry across views. Select a calendar day to see its appointment table and available times. Doctors see and manage their own working hours/time off, while administrators can inspect practice doctors. **Offer a new time** opens the correct conversation with the appointment selected.
- `/messages/`: the dedicated secure conversation inbox.
- `/consultations/`: doctor-owned draft notes and practice-visible signed consultations; available to doctors and practice Super Admins (signed records only for Super Admins).
- `/blood-tests/`: laboratory requests, protected PDF reports and requesting-doctor reviews; available to doctors and practice Super Admins (read-only).
- `/leads/`: enquiries for the selected practice, available only to Practice Administrators and practice Super Admins. Open a lead for its contact details, screening answers and consent history. Switching practice clears the previous list filters.

Desktop and mobile use the same section routes and permissions. Practice switching stays on the selected section but clears its previous practice's filters; switching from a patient record returns to the overview. Primary navigation is not implemented with dashboard fragment links. In-page links to a specific form or conversation remain appropriate within a record.

### Unified patient workspace

Open a patient from `/patients/`, then use the tabs beneath their name. For example, `/patients/<id>/?tab=appointments` contains only that patient's appointment table and booking form; `?tab=messages` contains only their conversations. Tabs never redirect to an app-wide list with a patient filter. The global Schedule, Messages, Consultations and other navigation destinations remain separate practice-wide pages. Overview contains summaries only and never marks conversations read. A submitted form returns to its patient section; invalid forms keep the submitted values there.

Clinical History starts with 20 events and loads older entries inside its scrollable panel, with explicit **Load older entries** and pagination fallbacks. Dates are shown in South Africa time. Activity, date and permitted cross-practice filters apply to both the displayed timeline and **Download Excel**. The `.xlsx` download includes the selected history snapshot, not just the currently loaded rows; exports over 10,000 entries require narrower filters rather than silently truncating. Export cells are text, and private notes, unsigned consultations, attachment bytes and arbitrary audit metadata are excluded. The existing protected JSON clinical-record export remains separate. Expiring history links recheck the active practice and permissions; reload after changing practice or access.

The Weight tab shows an actual date-spaced line chart, first/latest/change summaries and exact recorded values in a compact paginated table. Empty and single-check-in states are supported. Charts use at most the latest 300 entries; earlier entries remain available in the table. The Payments tab displays existing local records only: a saved status is not confirmation that money was collected, and no charge action is available.

The top-right account menu opens `/accounts/profile/` for your own shared name and read-only access details. The patient portal's `/patient/account/` remains the separate practice-specific contact/preferences page. Neither screen lets you grant roles or change someone else's identity.

### Doctor working hours and time off checkpoint

As Sam, open `/schedule/` and save the seven-day **Working pattern**. Each day supports one continuous working interval in South Africa time; unchecked days are non-working days. Working hours belong to the selected practice. They take effect immediately for new bookings and agreed appointment changes, without moving, cancelling or deleting existing bookings. Keep `DJANGO_TIME_ZONE=Africa/Johannesburg` (the default) so form input and displayed times match the SAST availability rules.

Until a doctor saves a pattern in a practice, existing manual availability and booking workflows remain usable with clash/time-off checks. No default hours are assumed. After a pattern is saved, an entire appointment must fit inside that practice's working interval. **Available time** computes up to 100 future 30-minute windows, starting every 15 minutes, subtracting actual occupied appointments and active time off across the doctor's practices. Previewing times does not reserve them or create database slots. Recorded manual slots of at least 30 minutes are used only where no pattern is configured.

**Time off** supports partial or multi-day periods. It is recorded against the selected practice but blocks the doctor across all practices. Only the origin practice sees its reason; other practices receive a generic unavailable result. Cancelling time off is a soft cancellation with an audit record, not deletion. Identical repeated submissions do not add another active block. Only an active doctor can edit their own hours or time off; Practice Administrators and practice Super Admins have read-only access to their practice doctors' availability.

**Appointments needing attention** lists existing future bookings that no longer fit the hours or overlap time off. The list remains practice-scoped, is paginated, and does not change an appointment's status. Review the record and use the existing mutually accepted appointment-suggestion workflow to arrange a new time. Proposal acceptance re-checks availability, so a suggestion made before leave was added cannot bypass the new block. Both booking writes and availability updates acquire the same clinician lock on PostgreSQL.

Forms carry expiring, signed doctor/practice context in addition to CSRF protection, so switching practices in another tab cannot redirect a stale form into the new practice. Invalid forms retain their values. The additive migration `care.0005_doctor_availability` creates the hours and time-off tables without resetting patients, bookings or other records.

Test this batch on a future appointment day: save working hours, add time off across an existing appointment, confirm that its original time/status remains and it is flagged, then cancel the time off. Check both Calendar and Table views. Time off recorded in a practice that is later deactivated remains blocking until its end: the origin practice and doctor membership must be reactivated to cancel it through the portal. This deliberately does not silently discard recorded unavailability.

### Consultation notes and blood-test review checkpoint

As Sam, open **Patients → Nadia → Consultations → New consultation note**. Choose an optional appointment, enter the actual consultation time and save a draft. A **Sign consultation note** task appears under My tasks. Open it, check the final wording, tick the signing confirmation and sign. The signed record is locked, the signing task is completed, and a snapshot appears in the patient's staff clinical record. Only the author can access an unsigned draft or sign it. Other practice doctors and practice Super Admins can read signed records; Practice Administrators and patient accounts cannot open these clinical-note pages. Signing never changes a booking, prescription, payment or treatment decision.

From the same patient record choose **Request blood tests**, enter the tests you have decided to request and optionally a due date. This makes an in-app request only: no laboratory order, email or external request form is sent. As Nadia, open **Tests** (`/patient/blood-tests/`) and upload one PDF report, up to 5 MB. The requesting doctor can also upload on the patient's behalf. Uploading creates a **Review laboratory results** task for the requesting doctor. As Sam, open that task, read the report and record an internal review note. Completing the review closes the task. Nadia sees the reviewed status, not the internal review wording; communicate any clinical advice separately through the established care workflow.

Each section has its own route, template and practice-scoped, paginated list. Filters and old record IDs are cleared on practice switching. Generated signing/review tasks open the relevant clinical page and cannot be reassigned, tagged or completed through the generic task editor. Ordinary tasks and existing note tags remain available. Signed notes and submitted reports cannot be overwritten. Use a separate follow-up note or new request for a correction; a structured addendum/replacement workflow is not part of this checkpoint.

Reports are stored as protected database bytes, not public media files. Downloads require current practice access or ownership of the patient's record, are served as attachments with no-store/nosniff headers, and are audited. PDF checks only validate file size, extension and basic framing: they are **not malware scanning**. Private storage hardening, virus scanning/quarantine, retention/backups, appropriate access policy review and clinical governance remain prerequisites before real patient use. No automated result interpretation is performed.

Signed, expiring user/practice/patient/record contexts protect all clinical writes alongside CSRF. Draft revisions reject stale edits; repeated create/upload/sign/review submissions cannot duplicate workflow history or overwrite originals. The additive migration `care.0006_clinical_workflows` preserves existing encounters, requests and other data without backfilling signed snapshots or tasks. Django technical admin cannot bypass the new workflow by editing/deleting workflow records, linked signed snapshots, generated tasks or encounter-linked appointments.

## Separate patient pages checkpoint

Sign in as Nadia and open `/patient/`. The compact overview contains care-plan, latest-weight and unread-message summaries, upcoming appointments, recent check-ins and care updates. Panels fit their content instead of forcing large empty areas, and there is no duplicate “Your care at” practice strip above the greeting. Each navigation item opens its own page on desktop and mobile:

- `/patient/blood-tests/`: own practice's requests, PDF upload/download and review status, with 20 requests per page. Internal clinician review notes are not displayed.
- `/patient/appointments/`: upcoming, past/cancelled or all appointments, paginated in groups of 20. **Suggest a new time** opens Messages with that appointment selected. Eligible booked participants can open the native appointment-bound video room during its join window.
- `/patient/messages/`: secure inbox with open/closed filters, 20 conversations per list page and 50 messages per history page. Only incoming messages actually opened are marked read. Replies, new conversations and appointment-change actions return to the selected conversation. The overview never marks messages read.
- `/patient/progress/`: weight check-in form, first/latest/change summaries and paginated weight history. Invalid entries keep their submitted values beside the errors.
- `/patient/account/`: edit the phone number and city for the selected practice only; identity, sign-in email, assigned doctor and recorded consents are read-only. Updates audit the changed field names, not their values.

Switching practice preserves the patient section but clears the previous practice's thread IDs and filters. Every page requires the signed-in person's own active patient record, independently of any staff role on the same login. Internal tasks, tags and clinical notes are not exposed through these pages. No schema migration or demo reset is needed for this navigation update.

Contact updates, weight check-ins and new-conversation forms carry an expiring signed patient/practice context. Switching practices in another tab before submitting an old form returns an error instead of saving into the newly selected practice. Submitted drafts remain visible for review. Existing conversation actions also require the thread to belong to the selected patient record.

### General tasks and free-form tags

Leave the patient blank to create a general practice task. Doctors can manage tasks they created or are assigned; practice administrators and Super Admins can manage all tasks in their active practice. Assignees must be active staff in that same practice. An existing task's creator is preserved when its assignee changes.

Tags are arbitrary reusable labels, not workflow states: create labels such as `Important` or `Urgent`, apply several, and uncheck them to remove them from a record. The same practice's labels can be applied to tasks and clinical notes; note authors can use **Edit tags** without changing the clinical note body. Tags are internal, remain separate between practices, and are not shown in the patient portal. Open/In progress/Completed/Cancelled remain separate task workflow states.

Before restarting an existing checkout after a schema update, run `.venv/bin/python manage.py migrate`. Migration `care.0003_general_tasks_and_record_tags` adds nullable patient links, task creators, and company-scoped tag tables without resetting existing records. The development-server warning is normal locally; production uses the configured ASGI/Daphne process, not the development server.

## Questionnaire and lead checkpoint

Open `/questionnaire/` without logging in. Select the practice, enter contact and ID/passport details, height and weight, answer the five screening questions, and optionally enter medication/allergy notes. The displayed service/privacy and telehealth notices are accepted with one checkbox. This does not opt anyone into marketing.

Submission creates a company-scoped `Lead`, its `ScreeningQuestionnaire`, and two versioned `ConsentRecord` records. It does **not** create or link a `User`, `Patient`, staff membership, appointment, subscription or payment, even when the submitted email belongs to an existing account. The anonymous browser session owns the result and edit screens; lead IDs in a query string grant no access. Forms use signed, expiring session-bound tokens and a unique nullable lead submission key (`care.0004_lead_submission_key`) to protect against accidental duplicate submission. Existing/imported leads are preserved.

The result is at `/questionnaire/result/`. **Change an answer** revises the same enquiry and reassesses it; its practice cannot be moved through that form. Start a fresh questionnaire for another practice. Existing accepted consent records are retained, and the submitted answers include a snapshot of the displayed notices. A notice changed while the form is open requires a fresh acceptance.

With `DEBUG=True`, the questionnaire mirrors the supplied revision 2.6 prototype's screening checks for demonstration, using unrounded BMI for threshold comparisons. A passing result opens `/questionnaire/checkout/`, a **dummy pricing/payment preview only**. There are no card fields, charges, account-creation actions, booking reservations, confirmation emails or Zoom links. Referred submissions remain leads too. In production (`DEBUG=False`), all submissions remain pending clinical review and dummy checkout is unavailable. Approved practice notices are required there; draft fallback wording is local-demo only. The prototype checks and consent wording require clinical/legal review before live onboarding; general product information is available from [SAHPRA's professional-information repository](https://pi-pil-repository.sahpra.org.za/wp-content/uploads/2025/08/Final-Wegovy-PI.pdf), but does not validate this app's screening pathway.

Test this checkpoint in two browser sessions: submit an enquiry, then sign in as Lindiwe or Joshua and open **Leads** in that practice. Confirm it does not appear in Patients. Sam has no Leads navigation or endpoint access. Real payment integration is deliberately deferred: conversion must eventually follow a verified payment-provider event, never a browser-supplied `paid` flag, and reuse an existing login only after identity verification.

## Patient workflow checkpoint

As Sam, open Nadia from the patient list. Use **Notes** to add a clinical note, **Appointments** to book a future appointment, **Tasks** to create or open a task, and **Messages** to send a message. Each action stays within Nadia's patient context. As Nadia, open **Messages** to read or reply to a conversation, and **Progress** to record a weight check-in. Change practice to see the separate record.

Invalid forms retain submitted values and show errors alongside the field. Appointment times are checked across a clinician’s practices to prevent overlaps. A completed task keeps its original completion time if submitted again. A cancelled task cannot be completed.

Doctors can add clinical notes. Doctors and practice Super Admins can read shared clinical notes; a private note is only shown to its author while that person has the Doctor role in the practice. The combined clinical timeline excludes private notes. Practice Administrators manage appointments, tasks and conversations without viewing clinical note bodies. Django technical superusers remain separate privileged accounts.

The staff unread badge is a shared team inbox count of patient-origin messages. Viewing a patient conversation marks those displayed messages as read for the team. Messages and clinical changes are recorded with their practice and audit metadata.

### Appointment suggestions in messages

Staff can click **Messages** or **Inbox** from either dashboard to open `/messages/`. Select a patient's thread on the left to read and reply on the right, or use **Open record** for their clinical record. The inbox is filtered to the selected practice. Only messages in the selected conversation are marked read; **Awaiting a reply** is based on the latest sender, not whether a message has been read. Sending a reply or appointment suggestion keeps you in the inbox.

In the conversation, open **Suggest a different appointment time**, select the appointment, enter a new time (SAST), and optionally include a message. Either the booked doctor or the patient can suggest a time; only the other person can accept or decline. The sender can withdraw a suggestion. A newer suggestion replaces the pending one, keeping its history.

The current appointment stays unchanged until acceptance. Availability is checked across the doctor's practices when suggesting and again when accepting; suggestions do not reserve slots. Rebooking a cancelled or missed visit creates a new appointment after agreement and preserves the original record. Completed visits cannot be moved. Practice Administrators and practice Super Admins can read the conversation but cannot agree on either participant's behalf.

To try it: sign in as Sam, open Nadia's record and its secure conversation, and send a time suggestion. In a separate browser session, sign in as Nadia and accept it in Messages. Check the new time in both appointment lists. This is an in-app workflow; it does not send email or change external calendars.

To verify the application: `.venv/bin/python manage.py test`. In-app stock, dispatch and delivery records are implemented; payment collection, external courier APIs and outbound email remain disconnected by design.

## Tenancy and identity model

`User` is the single email/password identity. `Company` is a practice and the tenancy boundary. A `CompanyMembership` links a user to a company with exactly one active role: Doctor, Practice Administrator, or Super Admin. One user can therefore switch between any number of practices without a separate login.

The membership-level **Super Admin** role is deliberately different from Django's `is_superuser`: the former is a role within one practice, whereas `is_superuser` is reserved for site-wide technical administration.

`CompanyScopedModel` is the abstract base required for future practice data: it supplies its protected `company` foreign key, timestamps, and a `.for_company(company)` queryset. `Patient` is the initial example. It is a company-local patient record which can optionally link to the user identity, allowing the same person to hold an independently isolated patient record at several practices.

Every staff view resolves `get_active_company(request)` and filters scoped data with `.for_company(active_company)`. The desktop and mobile dashboards use `StaffCompanyRequiredMixin`; the practice selector only accepts active memberships. Patient views use a separate patient-practice session selection and require the record to be owned by the signed-in user. Switching practice from a record returns to the overview.

## Deployment on DigitalOcean App Platform

The Dockerfile, PostgreSQL/Redis-backed GitHub Actions checks and `.do/app.yaml.example` target `MeridianHealthAppUser/MeridianApp`. Pushing the source does not provision or deploy DigitalOcean. Follow the [deployment guide](docs/DEPLOYMENT.md) to configure encrypted runtime settings, a managed PostgreSQL database, a pre-deploy migration job and the video infrastructure.

Never commit the values from the production environment.
