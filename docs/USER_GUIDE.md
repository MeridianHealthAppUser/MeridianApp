# Meridian local application guide

The application has separate URLs for each workspace. Overview pages contain summaries and links, not every workflow. Use the practice selector before working with a record; changing practice clears old record IDs and filters. A shared login can have different roles in different practices.

## Demo access

Open `/accounts/login/` on the local development server. The existing demo password is `MeridianDemo!2026` unless you have changed it.

| Login | Access |
| --- | --- |
| `sam.marchant@meridianhealth.co.za` | Doctor in Meridian Health and Orion Men's Health |
| `joshua.czech@meridianhealth.co.za` | Practice Super Admin in both practices |
| `lindiwe.mahlangu@meridianhealth.co.za` | Practice Administrator in Meridian Health |
| `nadia.m@example.co.za` | Own patient portal in both practices |

Demo data is for local testing only. The seed command is disabled when `DEBUG=False`; existing passwords are preserved unless the explicit local reset option is used. Do not run the seeder against an existing working database just to obtain newer screens.

## Your profile and account menu

Click your avatar/name at the top right to open **My profile**, **Change password**, your role-appropriate history/preferences and **Sign out**. On mobile the compact avatar opens the same menu. The practice selector remains alongside it; the duplicate staff Account sidebar section has been removed.

`/accounts/profile/` edits your own shared account name and shows your sign-in email, active practice access and account dates. It cannot change permissions, login email, another user's account or recorded patient/clinical names. Password changes require the current password. Patient **Account** remains a separate care-practice page for contact details, consent and preferences.

## Doctor and care-team pages

| Page | What it does |
| --- | --- |
| `/desktop/`, `/mobile/` | Separate desktop/mobile summary entry points |
| `/tasks/` | Table of patient/general tasks, assignment, due dates, workflow status and arbitrary tags |
| `/patients/` | Searchable directory; current practice or permitted combined view |
| `/patients/<id>/record/` | Clinical timeline, filters, authorisation, consent, weights and protected JSON export |
| `/consultations/` | Author-owned drafts and explicit final signing; signed notes cannot be edited |
| `/blood-tests/` | Request tests, receive a private PDF and record the requesting doctor's review |
| `/authorisations/` | Explicit doctor treatment decisions, pause/revoke and reviewed renewals |
| `/compounding/` | Draft, review and manual-submission tracking; printable summaries are not prescriptions |
| `/schedule/` | Table/calendar, available times, working pattern, time off and affected bookings |
| `/schedule/book/` | Staff booking for an existing patient, with doctor and patient clash checks |
| `/messages/` | Patient conversations, replies and mutually accepted appointment-change suggestions |

From an appointment's **Details** page, the booked doctor can record completed/no-show attendance after its end time. Administrators can cancel a booking; patients can cancel their own future booking with a reason. Completing attendance does not sign a clinical note. Rebooking retains the original appointment and requires a separate accepted proposal.

Native **video consultations** are available from appointment details for the booked doctor and patient, from five minutes before the appointment until its end. Camera access requires an explicit Join action. Mic/camera controls, screen sharing, reconnect handling and in-app invitations are included; connection history is not automatic attendance. Read the [video guide](VIDEO_CONSULTATIONS.md) for local testing and required Redis/TURN deployment configuration.

In Messages, **Route / manage** can assign a reply task to a doctor, close or reopen a conversation. Closing retains its history and is blocked while an appointment suggestion is pending. Administrative task notes are not sent as a patient reply.

Clinical record access differs by role. Doctors can see their own private notes/drafts through their protected workflows; the combined record excludes private notes and drafts. Super Admins can read shared/signed clinical records, not another doctor's private drafts or patient medical-profile answers. Practice Administrators retain operational access without clinical note bodies. Combined records match the same login identity only, require a stated care purpose and include only permitted active practices.

## Local treatment and supply

No payment, email, external prescribing, Zoom or courier integration is connected.

1. A Super Admin publishes a product in `/catalogue/`. Products with clinical/stock history retain their identity, strength and safety settings.
2. A doctor records an explicit authorisation for that patient and product. The application does not choose medication or calculate a dose.
3. The patient opens `/patient/pharmacy/`, updates their basket and submits a supply request with delivery details. A basket does not reserve physical stock; submitted requests count against the supply allowance.
4. An administrator reviews `/supply-requests/` and accepts a request for a planned date. This creates a draft shipment only.
5. Receive stock in `/stock/`. Batch identity, expiry, cold-chain confirmation and quantity changes have a ledger. Quarantine, release, adjustment and write-off require explicit actions; write-off is terminal.
6. Prepare the shipment from `/shipping/`. Eligible batches are allocated by expiry order. Authorisation, allowance, treatment horizon, cold chain and stock sufficiency are checked atomically.
7. Check and lock the prepared weekly list. Download the manifest if needed. Locking does not send anything.
8. After actual dispatch, record a tracking reference and confirm dispatch. A fixed snapshot retains products, dose, quantity, batch, expiry and address. Record delivery separately. `/dispatch-history/` preserves that history.

Preparation reserves inventory; recording dispatch does not deduct it twice. Cancelled, unshipped allocations are released. Invalid authorisations hold unshipped supply, never rewrite dispatched history. Shared allowance groups combine quantities but do not imply authorisation for other products or doses.

## Administration and insight

- `/leads/`: questionnaire enquiries, screening outcome, versioned consent and follow-up notes. Follow-ups can assign an administrator, set the next contact date, record booking interest or close an enquiry. They cannot create an account or change clinical screening.
- `/dropouts/`: paused/cancelled care plans and administrative follow-up history. Following up does not restart treatment.
- `/subscriptions/`: local care-plan records. Patients explicitly enrol, pause/resume or cancel their own plan in `/patient/subscription/`; no payment is collected.
- `/data-requests/`: administrators respond to patient access/correction/deletion-review requests. Responses are retained and visible to the patient. A deletion request does not erase records automatically.
- `/settings/users/`: Super Admin staff access management, including authorised multi-practice memberships. Linking an existing sign-in does not silently rename it or reset its password. The last active practice Super Admin cannot be removed.
- `/settings/practices/`: Super Admin practice setup. A practice Super Admin is not Django's site-wide superuser.
- `/settings/policies/`: publish a new immutable policy version; effective dates control which document is shown publicly. Existing acceptances remain linked to their original version.
- `/review-rules/`: planning settings and an explicit local reminder check. It creates deduplicated doctor reminders and can hold invalid unshipped supply. A reminder is not a completed review and never extends an authorisation or orders tests.
- `/metrics/`: actual operational counts, creation cohorts and descriptive paired-weight data; current practice or permitted combined scope. Unrecorded cost/payment inputs are not guessed.
- `/activity-statements/`: doctor-owned statements and Super Admin preparation/approval using explicitly entered rates. Completed appointments and saved messages are counted. Approved snapshots are immutable. No money is transferred or marked paid.
- `/account/access-history/`: your own scoped, sanitised access/activity history.

The local review command is available for a configured scheduler or manual use:

```sh
python manage.py refresh_review_tasks --company meridian-health --actor-email joshua.czech@meridianhealth.co.za --within-days 30
```

It acts on the selected practice's live records. No scheduler is enabled automatically. Configure a real authorised account for any non-demo environment.

## Patient pages

Patients have separate Overview, Appointments, Messages, Progress, Tests, Treatment, Subscription, Medical profile, Updates, Pharmacy, Orders and Account destinations.

- Book an available review/follow-up slot; download a private `.ics` calendar entry or suggest a new time in Messages.
- Save a stage-2 medical profile with retained revisions. Self-reported answers are not automatic clinical assessments.
- Record weight and view actual history/chart data.
- Upload a PDF against an owned blood-test request. Reports are private downloads, not public media links; clinician review notes are not shown to the patient.
- Review orders and delivery updates from their own practice record only.
- Update local contact details, change the shared-login password, change marketing preferences independently of consent, and submit privacy/data requests from Account.

Submitting the anonymous `/questionnaire/` creates a **lead only**. The checkout is a local dummy preview and never creates a user or collects card details. Paid conversion remains intentionally unavailable because payment functionality was excluded.

## Before real patient use

This is a locally working application, not clinical/regulatory approval. Review screening criteria, consent text, prescribing/dispensing governance, record retention, security controls, backup/restore, file scanning and operational procedures before live deployment. PDF upload validation checks basic format and size; it is not malware scanning. Public policy pages show saved practice documents and do not supply legal advice or invented approved notices.
