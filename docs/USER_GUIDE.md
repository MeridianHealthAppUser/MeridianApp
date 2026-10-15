# Meridian local application guide

The application has separate URLs for each practice-wide page and one tabbed workspace per selected patient. Overview pages contain summaries and links, not every workflow. The current deployment is **Meridian Health only**: there is no practice selector or practice-management page. Existing Meridian logins and roles are unchanged. See [single-practice operation](SINGLE_PRACTICE.md) for the deployment boundary and retained data model.

## Demo access

Open `/accounts/login/` on the local development server. The existing demo password is `MeridianDemo!2026` unless you have changed it.

| Login | Access |
| --- | --- |
| `sam.marchant@meridianhealth.co.za` | Doctor in Meridian Health |
| `joshua.czech@meridianhealth.co.za` | Super Admin in Meridian Health |
| `lindiwe.mahlangu@meridianhealth.co.za` | Practice Administrator in Meridian Health |
| `nadia.m@example.co.za` | Own patient portal in Meridian Health |
| `ayesha.p@example.co.za` | Own patient portal in Meridian Health |
| `pieter.vdm@example.co.za` | Own patient portal in Meridian Health |
| `zanele.n@example.co.za` | Own patient portal in Meridian Health |

Demo data is for local testing only. The seed command is disabled when `DEBUG=False`; existing passwords are preserved unless the explicit local reset option is used. Do not run the seeder against an existing working database just to obtain newer screens.

## Your profile and account menu

Click your avatar/name at the top right to open **My profile**, **Change password**, your role-appropriate history/preferences and **Sign out**. On mobile the compact avatar opens the same menu. The duplicate staff Account sidebar section and practice selector are absent.

`/accounts/profile/` edits your own shared account name and shows your sign-in email, active practice access and account dates. It cannot change permissions, login email, another user's account or recorded patient/clinical names. Password changes require the current password. Patient **Account** remains a separate care-practice page for contact details, consent and preferences.

## Doctor and care-team pages

Metrics and cohorts uses four compact summary cards, daily activity lines, an appointment-status ring, monthly cohort bars and a recorded weight-change breakdown. Date filters and CSV export remain available for Meridian Health. Expand **View daily counts** or **All measures & definitions** for exact values. “Current snapshot” measures describe now; other measures use the inclusive selected dates. No payment results, missing weights or clinical outcomes are inferred.

| Page | What it does |
| --- | --- |
| `/desktop/`, `/mobile/` | Separate desktop/mobile summary entry points |
| `/tasks/` | Table of patient/general tasks, assignment, due dates, workflow status and arbitrary tags |
| `/patients/` | Searchable Meridian Health patient directory |
| `/patients/<id>/` | One patient workspace; separate patient-specific tabs with a shared header |
| `/patients/<id>/?tab=history` | Role-appropriate history, timeline filters and protected exports; the old `/record/` clinical URL remains an alias |
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

Clinical record access differs by role. Doctors can see their own private notes/drafts through their protected workflows; the shared record excludes private notes and drafts. Super Admins can read shared/signed clinical records, not another doctor's private drafts or patient medical-profile answers. Practice Administrators retain operational access without clinical note bodies. Cross-practice combined records are disabled in the current deployment.

## Working within one patient

Choose a patient in the directory. Their name, details and tab bar remain visible while you work. Tabs use `/patients/<id>/?tab=...` and only render the selected section; they do not take you to an app-wide page with a filter applied.

- **Overview**: a compact summary, with no full forms or conversations. Looking at it does not mark messages read.
- **History**: the permitted event log. Doctors and practice Super Admins see the clinical record; Practice Administrators see only an operational history without clinical note bodies or audit metadata.
- **Appointments**, **Messages**, **Tasks** and **Deliveries**: records and actions for this patient only. The Messages tab opens with no conversation selected and shows each one's unread count. Selecting a conversation shows its own message history, and only opened incoming messages are marked read.
- **Consultations**, **Blood tests**, **Notes** and **Treatment**: shown only to the appropriate clinical roles. Unsigned consultations remain author-only; private notes remain author-only while the author has the Doctor role. Generated clinical tasks must still be completed through their clinical workflow.
- **Weight**: the recorded weight trend and exact check-ins. Chart points are spaced by their actual dates, with first/current/change summaries. The chart covers up to the latest 300 check-ins; the paginated table still provides older entries. There are explicit empty and single-entry states, and longer notes can be expanded.
- **Payments**: existing local records only. No charge can be made here, and a local “paid” status does not verify a real payment.

Lists are paginated within the selected patient. A tab change never changes the patient. Booking, reply, note and task errors retain the draft in the relevant section. Global navigation such as **Patients**, **Schedule** or **Messages** remains the intentional way to leave the patient's workspace for a practice-wide page.

### History loading and downloads

The clinical history initially shows 20 events. Scroll its panel or use **Load older entries** to append older entries; normal pagination remains available when JavaScript is unavailable. Activity/date filters are retained and history is limited to Meridian Health. Times are displayed in South Africa time (SAST).

**Download Excel** exports the same filtered history snapshot, including older entries not yet loaded on screen. The workbook includes its scope and filters, excludes private notes, unsigned consultations, attachment bytes and arbitrary audit metadata, and writes cells as text. An export above 10,000 entries asks you to narrow the filters; it is not silently cut short. **Export clinical record** remains a separate protected JSON clinical-record download.

History links expire and recheck practice/role access. If the practice or your access changes in another tab, reload the record before loading more events or downloading. Neither loading more history nor downloading it changes a clinical record.

## Local treatment and supply

No payment, email, external prescribing, Zoom or courier integration is connected.

1. A Super Admin publishes a product in `/catalogue/`. For a main medicine the product is the pen the dispensary orders and sends (Semaglutide 0.25 mg). Products with clinical or supply history retain their identity, strength and safety settings.
   - **Doses** are what doctors prescribe: the weekly mg, the pen it comes in, how many pens go out how often, and the price and how often it is billed (Tirzepatide 7.5 mg weekly, 1 × 10 mg pen every 5 weeks). Several doses can share a pen. Supply is entered as the practice dispenses it, never worked out from the mg in a pen. A dose without a price can't be prescribed. Once prescribed or in a starting plan, its pen, amount and supply are fixed; add a new dose for those changes. A step-up or only-if-necessary tier is shown when a doctor chooses it.
   - **Starting plans** are steps of one dose for some weeks, then a last dose until the prescription ends (Semaglutide 0.25 mg weekly for 4 weeks, 0.5 mg weekly for 4 weeks, then 1 mg weekly). The questionnaire result shows each medicine's first plan with its prices.
2. A doctor prescribes from the patient record: Initiate (a starting plan), Renew or Change, for 4 to 24 weeks. A care plan costs what the dose in effect costs. A doctor can also record an explicit authorisation for any product in `/authorisations/`. The application does not choose medication or calculate a dose.
3. Saving a prescription drafts its parcels on the Weekly shipping list (`/shipping/`): on the practice's shipping day (Practice settings, Tuesday by default), the first on or after the prescription starts, then one every dispatch interval with the dose's pens. A renewal carries on the same rhythm. A change cancels the old prescription's drafts and holds any parcel already marked ready for review. Each parcel goes out within its own supply period: the dispensary can send a dose's pens once in each period, counted from when the patient started that dose.
4. Ad-hoc parcels: **Create shipment** on the Weekly shipping list opens a dialog for the patient, product, pens and date. Patients can also request supplies from `/patient/pharmacy/`; an administrator accepts a request in `/supply-requests/`, which adds a draft parcel. Submitted requests count against the supply allowance.
5. Each week, on `/shipping/`: change any draft that needs it (another day, other pens) from its page, then **Mark drafts ready**. Each draft is checked against its prescription, care plan and supply allowance; any that can't go stay drafts with the reason beside them.
6. **Check and lock week** confirms the ready parcels, checking each again. **Pens to order** lists the week's pens by product, with a CSV for the supplier order. Pens aren't kept in stock: just enough is ordered for the week's parcels.
7. After actual dispatch, record a tracking reference, and optionally each line's batch and expiry, and confirm dispatch. A fixed snapshot retains products, dose, quantity, batch note and address. Record delivery separately. `/dispatch-history/` preserves that history, and the manifest CSV includes the batch notes for tracing a recall.

Invalid authorisations hold unshipped parcels, never rewrite dispatched history. Shared allowance groups combine quantities but do not imply authorisation for other products or doses.

## Administration and insight

- `/leads/`: questionnaire enquiries, screening outcome, versioned consent and follow-up notes. Follow-ups can assign an administrator, set the next contact date, record booking interest or close an enquiry. They cannot create an account or change clinical screening.
- `/dropouts/`: paused/cancelled care plans and administrative follow-up history. Following up does not restart treatment.
- `/subscriptions/`: local care-plan records. Patients explicitly enrol, pause/resume or cancel their own plan on My Treatment (`/patient/treatment/`); no payment is collected.
- `/data-requests/`: administrators respond to patient access/correction/deletion-review requests. Responses are retained and visible to the patient. A deletion request does not erase records automatically.
- `/settings/users/`: Super Admin staff access management for Meridian Health. Linking an existing sign-in does not silently rename it or reset its password. The last active practice Super Admin cannot be removed.
- `/settings/practices/`: disabled in single-practice mode, together with creation and editing endpoints. A practice Super Admin is not Django's site-wide superuser.
- `/settings/policies/`: publish a new immutable policy version; effective dates control which document is shown publicly. Existing acceptances remain linked to their original version.
- `/review-rules/`: planning settings and an explicit local reminder check. It creates deduplicated doctor reminders and can hold invalid unshipped supply. A reminder is not a completed review and never extends an authorisation or orders tests.
- `/metrics/`: Meridian Health operational counts, creation cohorts and descriptive paired-weight data. Unrecorded cost/payment inputs are not guessed.
- `/activity-statements/`: doctor-owned statements and Super Admin preparation/approval using explicitly entered rates. Completed appointments and saved messages are counted. Approved snapshots are immutable. No money is transferred or marked paid.
- `/account/access-history/`: your own scoped, sanitised access/activity history.

The local review command is available for a configured scheduler or manual use:

```sh
python manage.py refresh_review_tasks --company meridian-health --actor-email joshua.czech@meridianhealth.co.za --within-days 30
```

It acts on the selected practice's live records. No scheduler is enabled automatically. Configure a real authorised account for any non-demo environment.

## Patient pages

The patient sidebar has five destinations: Home, My Treatment, My Appointments, My Messages (with the unread count) and My Medications. Account settings is in the account menu. Weight history and Updates open from Home; the medical profile, blood tests and the care plan are on or linked from My Treatment; booking is on My Appointments; the basket and request history open from My Medications.

Home shows the authorisation and next consult, what is on its way, and the recorded weight chart. Its only form logs a weight check-in, and it never marks a conversation read. My Treatment lists tasks raised from the record, such as booking a follow-up before an authorisation lapses; each one disappears once the record changes.

- Book an available review/follow-up slot; download a private `.ics` calendar entry or suggest a new time in Messages.
- Save a stage-2 medical profile with retained revisions. Self-reported answers are not automatic clinical assessments.
- Record weight and view actual history/chart data.
- Upload a PDF against an owned blood-test request. Reports are private downloads, not public media links; clinician review notes are not shown to the patient.
- Review orders and delivery updates from their own practice record only.
- Update local contact details, change the shared-login password, change marketing preferences independently of consent, and submit privacy/data requests from Account.

Submitting the anonymous `/questionnaire/` creates a **lead only**. A cleared demo lead can open the checkout, choose an initial consultation time and create a login. Card and EFT payments are not connected and no card details are collected. Only the checkout test code (`CHECKOUT_TEST_CODE`, `devtest` locally) brings the fee to R0 and converts the lead into a booked, signed-in patient with a paid invoice. The practice needs a consultation fee in **Practice settings** and at least one doctor with open times.

## Before real patient use

This is a locally working application, not clinical/regulatory approval. Review screening criteria, consent text, prescribing/dispensing governance, record retention, security controls, backup/restore, file scanning and operational procedures before live deployment. PDF upload validation checks basic format and size; it is not malware scanning. Public policy pages show saved practice documents and do not supply legal advice or invented approved notices.

Repository and deployment configuration do not mean the application has been deployed. Payment processing, outbound email and automatic paid lead-to-account conversion remain unavailable by design.
