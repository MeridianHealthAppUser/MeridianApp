# Report 2: What the backend still has that the UI doesn't show

Prepared 5 October 2026 for James Westraadt.

**What this covers.** A scan of the current code: the last commit plus all uncommitted edits. It looks for:
- functionality that still exists in the models, logic, services or URLs but has no way in from the UI (or no longer has)
- data that is collected or stored but not displayed anywhere

The scan covered:
- every model and field (65 models)
- every named URL (167 routes), matched against menus, templates, JavaScript and redirects
- every removal in the uncommitted diff
- the patient journey from first enquiry to the signed-in menus

**How it's organised.** By surface (Patient web app, then Practice console), then user type, then menu option, the same as
Report 1. Each finding sits where a user would expect to see it. Each bullet says what is hidden, what still exists behind
it (`file:line`), and where, if anywhere, it can still be seen.

| Label | Meaning |
|---|---|
| **Removed since these edits** | On screen at the last commit (`63d7132`), gone now, but the backend still has it |
| **Kept in backend only** | Stored or supported in the backend, never shown in the product UI (already the case before these edits unless it says "new") |
| **Dormant** | A model or field that no product flow writes. It exists only in the schema, demo data or Django admin |
| "Still visible:" | Where the data can still be seen: Django admin, an export, another role's page, or nowhere |

"Django admin" means **Account menu → Practice administration**, which only Django superusers can open. Ten of the newest
models are not registered there, so for them "Django admin only" doesn't apply (see Appendix C).

**How it was checked.** Three read-only passes over the code: data, pages and logic, and the patient journey. The
important claims were then spot-checked by hand; these are marked **verified**. Anything marked "(unverified)" was read from
the code but not run.

## At a glance

**Removed from the UI by these edits, with the data or logic still there:**
- **Consent status on the History tab.** The "Current consent" card (each consent type, accepted or not, version and
  date) is gone. Staff can now see a patient's consent status only in the JSON export.
- **Who added a clinician to a conversation.** Staff no longer see the "X added Y · date" lines in patient conversations,
  although the data is still loaded. The patient still sees them.
- **Day-off reasons and part-day time off.** The new form is date-only, but the backend still supports both, and older
  entries keep a reason that nobody can see.
- **Clinician access.** Clinicians lost Operations (shipment hold reasons, dispatch details, the patient's note on a
  supply request), Insights (including their own activity statements), Review rules and My access history.
- **Doctor count cards and "awaiting reply".** The doctor Overview and My tasks still compute their count cards on every
  load but hide them. The "Awaiting reply" cues were removed from both message lists; the filters remain.
- **Record Messages tab.** It lost paging and the typed Subject field. A typed subject is now possible only through
  "New patient message".

**Stored, but nobody can see or use it:**
- **Note signature (verified).** "How you sign consult notes" can be edited, but nothing uses it; signed notes show the
  account name.
- **Replaced prescriptions (verified).** A prescription replaced through the new Renew or Change dialogs shows only as
  "Cancelled". The authorisation page still looks for the old audit event, so it never links to the replacement.
- **Summary data.** Height drives the BMI but can't be seen or edited. Summary data stops following the patient's
  profile after the first edit. The questionnaire's combined "Chronic medication and allergies" answer lands only in
  Chronic medication, so Allergies shows "Not recorded".
- **Invoices, payment references and the checkout "Payment method".** Invoices and the payment reference are created
  but never shown. The payment method choice isn't stored at all.
- **Patient details.** Date of birth and record number are never filled by any product flow. No staff screen can edit a
  patient's name, ID, date of birth or phone.
- **Audit detail.** Audit metadata and IP addresses can be seen only in Django admin. Most audit actions have no label,
  and nobody can see another staff member's actions in the product.

**Built in the backend, with no UI at all:**
- **Dormant models.** Email journeys, payouts, invoices, company invites and subscription billing cycles. Only two emails
  exist: password reset and the PathCare form.
- **Onboarding in production (verified, older code).** With `DEBUG` off, automatic screening never runs: every enquiry
  stays "Pending" and online checkout is refused. No console screen can review or convert a lead.
- **Clinical pages off the clinician's menu.** Consultations, Blood tests, Authorisations and Compounding records still
  open by URL. Doctors can also open records of patients not assigned to them (verified).

---

## Patient web app

### Prospective patient / public pages

#### Screening questionnaire
- **Kept in backend only.** Editing the answers ("Change an answer") overwrites the earlier ones. The lead shows only the
  latest set, and the sequence of screening outcomes is kept only in the audit log, which no screen shows. For example,
  someone who answers "Yes" to pregnancy and then changes it to "No" leaves no visible trace.
  Evidence: `care/intake.py:117-126` (`update_or_create`), `care/intake.py:136-137`. Still visible: Django admin only.
- **Kept in backend only.** The full wording of the notices the person accepted is saved with the answers
  (`answers.consent_snapshot`, plus `service_consent` and `telehealth_consent`). Staff see only the consent title and
  version; nobody can read the accepted text later.
  Evidence: `care/intake.py:95`, `care/forms.py:410-411`, `templates/portal/staff_lead_detail.html:38-40`. Still visible:
  Django admin only (raw JSON).
- **Kept in backend only.** Every consent record stores the IP address it was given from. It is never shown.
  Evidence: `ConsentRecord.ip_address` (`care/models.py:158`), written at `care/intake.py:133-134`. Still visible: Django
  admin only.
- **Kept in backend only.** The enquiry is tied to the browser session. There is no enquiry reference, no confirmation email
  and no way back to it from another device. A second visit creates a duplicate lead.
  Evidence: `portal/intake_views.py:51-56`, `care/intake.py:97-106`.
- **Kept in backend only (verified, older code).** In production (`DEBUG` off), automatic screening never runs. Every
  enquiry is saved as *Pending* ("clinical-review-required"), so nobody is cleared and checkout refuses everyone. The
  automatic rules exist only in demo mode.
  Evidence: `care/intake.py:56-58`, `care/checkout.py:40-44`.
- **Kept in backend only.** No marketing source or campaign (UTM) is captured anywhere in the funnel.

#### Questionnaire result
- **Kept in backend only.** The page says "An invoice for the consultation can be requested". In fact an invoice (e.g.
  `IC-000123`, with fee and discount lines) is created at checkout, but neither the patient nor staff can open it.
  Evidence: `templates/portal/questionnaire_result.html:47`, `care/checkout.py:214-223`. Still visible: Django admin only.
- **Kept in backend only.** The price list shows a "Thereafter" monthly rate and "Medication ships every 28 days". The
  thereafter rate (`ongoing_subscription_amount`) is never applied: plans always use the introductory rate, and nothing
  schedules parcels automatically.
  Evidence: `templates/portal/questionnaire_result.html:38,62`, `care/treatment.py:207-211`.
- **Kept in backend only (copy).** The page promises a consult "on Zoom". The app uses its own video room, and the
  appointment's external video-link field is never shown to anyone.
  Evidence: `templates/portal/questionnaire_result.html:32`, `Appointment.video_link` (`care/models.py:378`).

#### Checkout
- **Kept in backend only.** The **Payment method** choice (Card or Instant EFT) is posted and then thrown away. Card fields
  are disabled, and only the test code lets anyone book. The payment stores the reference `test-code:<code>`, which no
  screen shows.
  Evidence: `portal/checkout_forms.py:73-74`, `care/checkout.py:143-262` (never read), `care/checkout.py:224-226`.
  Still visible: nowhere (payment method); Django admin only (reference).
- **Kept in backend only.** No delivery or postal address is collected during onboarding, although the result page
  promises "Medication couriered to your door".
  Evidence: `portal/checkout_forms.py:67-78`, `templates/portal/questionnaire_result.html:41`.
- **Kept in backend only.** Checkout also does three things the patient never sees as such:
  - creates two timeline entries
  - links the consents to the new login
  - assigns the booking doctor as the patient's clinician

  The patient sees these only as care updates under Home → "See everything that's happened".
  Evidence: `care/checkout.py:200-201,231-244`.

#### Sign in and password reset (shared with staff)
- **Removed since these edits (hidden, can come back).** The intro panel ("Care that stays connected…") is commented out.
  Evidence: `templates/accounts/auth_base.html:11-22`, `static/css/auth.css:7-21`.
- Nothing stored here is hidden. A password reset writes `account.password_reset`, which is labelled in My access history.

### Patient (signed in)

#### Home (→ "See everything that's happened", the care updates)
- **Kept in backend only.** The patient never sees the **goal weight** a clinician sets in Summary data. The page even says
  the chart doesn't set a target.
  Evidence: `PatientClinicalSummary.goal_weight_kg` (`care/models.py:1281`), `templates/portal/patient_dashboard.html:56`.
- **Kept in backend only.** The two start weights can differ. The patient's "−X kg since you started" counts from their
  first check-in; staff Summary data counts from the questionnaire weight.
  Evidence: `portal/patient_views.py:88-91`, `care/clinical_summary.py:74`.
- **Kept in backend only.** "Next delivery" can say **On hold**, but never why. The stored hold reason (for example
  "Prescription changed. Dispensing review is required" or a quarantined batch) isn't shown.
  Evidence: `care/prescriptions.py:142-143`, `care/operations.py:86-91,300-312`.
- **Kept in backend only.** Weight check-ins can't be edited or deleted by anyone. The weight form supports editing, but no
  screen offers it. The weight history says "team-recorded check-ins", but no staff screen records a weight.
  Evidence: `care/forms.py:205-214`, `portal/views.py:590-617` (create only), `care/models.py:830` (`recorded_by`).
- **Kept in backend only.** Each care update knows which record it came from (appointment, payment, message, shipment,
  prescription), but no update links to that record or names it.
  Evidence: `PatientEvent.source_type`/`source_id` (`care/models.py:971-972`). Still visible: Django admin only.
- **Dormant.** The "patient can see this" switch on care updates is always on, because no code writes a staff-only update.
  Evidence: `PatientEvent.is_patient_visible` (`care/models.py:973`).
- **Kept in backend only.** Several things that happen to the patient create no care update:
  - staff accepting or cancelling a supply request
  - a parcel held or cancelled
  - a change of assigned clinician
  - a PathCare form emailed, or the email failing
  - a reply to a data request
  - their clinician's day off clashing with their booking (new in these edits)
  - compounding progress

  Evidence: `care/pharmacy.py:120-174`, `care/operations.py:86-91,300-312`, `care/patient_assignment.py:43-63`,
  `care/pathcare.py:247-273`, `care/privacy.py:89-119`, `care/availability.py:218-263`.

#### My Treatment → Medical information
- **Kept in backend only.** The card is titled "What your doctor has on file", but it shows the patient's own profile and
  questionnaire answers, not the doctor's Summary data. A doctor's correction (allergies, history, safety flags, goal
  weight) never reaches the patient.
  Evidence: `templates/portal/treatment_patient.html:56`, `portal/patient_summary.py:133-159`, `care/clinical_summary.py:87-92`.
- **Kept in backend only.** After booking, the patient can't see their screening safety answers again (pancreatitis,
  thyroid/MEN2, pregnancy, 18 or older, weight-related condition). The questionnaire is locked once they convert, and
  the result page redirects to Home.
  Evidence: `portal/intake_views.py:86-87,146-150,214-215`, `care/models.py:128` (`ScreeningQuestionnaire.answers`).
- **Kept in backend only.** Earlier versions of the medical profile are kept ("previous saved versions are retained"), but
  only clinicians can read them, on the History tab.
  Evidence: `care/patient_care.py:124-128`, `portal/record_views.py:172-175`.

#### My Treatment → treatment card and Treatment and delivery history
- **Changed by these edits.** A prescription made with the new **Change prescription** dialog doesn't carry over the
  doctor's instructions, and the dialog has no instructions field. After a dose change, "Your doctor's instructions"
  disappears for the patient. **Renew** does carry them over.
  Evidence: `care/prescriptions.py:206-210` (no `instructions`) versus `care/prescriptions.py:169`.
- **Kept in backend only (new data).** The doctor's **reason for a change** (e.g. "Side effects – nausea") is stored but not
  shown to the patient; their care update says only "Your doctor recorded a treatment decision".
  Evidence: `TreatmentAuthorization.change_reason` (`care/models.py:224`), `care/prescriptions.py:207,224`.
- **Changed by these edits.** A renewed or changed prescription marks the old one *Cancelled*, so the patient's history
  says "Cancelled · not valid for dispensing" for a simple renewal. The stored link to the replacement (`replaces`) isn't
  shown.
  Evidence: `care/prescriptions.py:125-128`, `templates/portal/treatment_patient.html:160`, `care/models.py:223`.
- **Kept in backend only.** The authorisation's review interval isn't shown; only the plan's "Doctor review due" date is.
  Evidence: `care/models.py:219`, `templates/portal/treatment_patient.html:79`.
- **Kept in backend only.** The patient can't see any invoices, payments or billing cycles. **Next debit date** and
  **cycle number** exist but are never filled or advanced.
  Evidence: `care/models.py:256-259,273-358,1096-1123`, `care/treatment.py:210,242`.
- **Kept in backend only.** Pause and Cancel ask the patient to "tell us before you cancel", but the form has no reason
  field, so staff never learn why.
  Evidence: `templates/portal/treatment_patient.html:86-98`, `care/treatment.py:223-251`.
- **Kept in backend only.** The Deliveries table shows scheduled date, cycle, status and tracking only. The hold reason,
  dispatch and delivery dates, and the parcel's contents (product, dose, quantity) aren't shown.
  Evidence: `templates/portal/treatment_patient.html:194-197`, `care/models.py:1058-1066,1078-1083`.

#### My Treatment → Blood tests
- **Kept in backend only.** The patient's **Blood tests list** (`/patient/blood-tests/`) is in no menu and isn't linked from
  My Treatment. The only ways in are the Home task "Get your blood tests done" (gone once the report is uploaded) and
  breadcrumbs inside the blood-test pages. After that there is no way back to past requests or uploaded reports.
  Evidence: `portal/urls.py:58-60`, `portal/patient_summary.py:197-204`, `templates/portal/patient_lab_detail.html:6`.
- **Kept in backend only (new in these edits).** The filled **PathCare request form** (PDF) is emailed to the patient but
  isn't viewable or downloadable in the portal. The patient isn't told whether the email went out or failed. Staff see
  "Emailed to …", View form and Resend.
  Evidence: `PathCareRequestForm.content` (`care/models.py:785`), staff-only `portal/pathcare_views.py:113-124`.
- **Kept in backend only.** PathCare requests have no due date, so the patient's task never says "please have it done by …".
  Evidence: `care/pathcare.py:209-210`, `portal/patient_summary.py:198`.
- **Kept in backend only.** The doctor's written review of the results is hidden. The patient sees only "Your doctor has
  reviewed this report".
  Evidence: `portal/clinical_views.py:293` (`defer('result_summary')`), `templates/portal/patient_lab_detail.html:21`.

#### My Treatment → Medical profile
- **Kept in backend only.** Saving the profile tells nobody: no update, no task, no message. Summary data stops following the
  profile once anyone edits Summary data, and the Overview doesn't flag a newer profile.
  Evidence: `care/patient_care.py:101-131`, `care/clinical_summary.py:87-92,158-162`.

#### My Appointments
- **Kept in backend only.** The cancellation reason, which whoever cancels must give, is stored only in the timeline entry.
  It isn't on the appointment page for the patient or staff.
  Evidence: `care/appointment_lifecycle.py:96-120`.
- **Kept in backend only (new in these edits).** When a clinician's day off or special hours clash with the patient's
  booking, staff get an "Offer new times" task. The patient's appointment still says **Booked**, with no notice.
  Evidence: `care/availability.py:218-263`.
- **Kept in backend only.** The video connection history (who joined and left, when, and why the connection ended) is shown
  only to staff, one row per session. Per-person rows are Django admin only.
  Evidence: `video/models.py:13-62`, `templates/portal/includes/appointment_video_history.html`.
- **Dormant.** Appointment "outcome notes" exist, but nothing writes or shows them. The external video link is cleaned up
  in code (`portal/patient_views.py:172-173`), but no template shows it.
  Evidence: `Appointment.outcome_notes`, `video_link` (`care/models.py:378-379`).
- **Changed by these edits.** The **Message <clinician>** link follows the narrowed recipient rule. A patient booked with a
  clinician who was never assigned to them can no longer message that clinician, or suggest a new time to them. The
  appointment-proposal logic is unchanged and still supports it.
  Evidence: `portal/patient_views.py:167-188`, `care/messaging.py:75-81`.

#### My Messages
- **Removed since these edits.** The "Waiting for a reply" label, on a conversation where the patient wrote last, is
  replaced by a bold "You:". Nothing is left behind in the backend.
  Evidence: `templates/portal/patient_messages.html:38` (HEAD line 36).
- **Kept in backend only (new data).** The app now records when a staff member read the patient's message, and who. The
  patient sees no read receipt.
  Evidence: `PatientMessage.staff_read_at`, `staff_read_by` (`care/models.py:943-944`), `care/messaging.py:25-41`. Still
  visible: nowhere for `staff_read_by` (not in the Django admin list).

#### My Medications
- **Kept in backend only.** The delivery address has to be typed again for every supply request and isn't saved to the
  patient. The latest one is quietly reused as the **postal address on PathCare forms**, so patients who never made a
  supply request get a blank address.
  Evidence: `portal/operations_forms.py:128-136`, `care/pharmacy.py:97-107`, `care/pathcare.py:87-94`.
- **Kept in backend only.** When staff cancel a submitted request, the patient sees only *Cancelled*. No reason is
  captured, and no update is made; accepting creates no update either.
  Evidence: `care/pharmacy.py:120-174`.
- **Kept in backend only.** The request page shows the shipment's status, planned date and tracking. It doesn't show the
  hold reason or the dispatch and delivery dates.
  Evidence: `templates/portal/pharmacy_order_detail.html:6`.
- **Kept in backend only.** The product card shows quantity, price and description. It doesn't show the product's allowance
  ("4 pens per cycle"), its dose frequency (new in these edits), category or cold-chain flag.
  Evidence: `care/models.py:188-196`, `templates/portal/includes/patient_product_card.html`.

#### Account menu → Account settings
- **Kept in backend only (new data).** The patient's **Title** (chosen in the questionnaire, editable by staff, printed on
  PathCare forms) isn't shown and can't be changed here.
  Evidence: `Patient.title` (`practices/models.py:162`), `templates/portal/patient_account.html:22-29`.
- **Kept in backend only.** **Date of birth** always reads "Not recorded". Nothing captures it, although the SA ID number is
  collected. **Record number** is never assigned, so its row stays hidden. Both can be set only in Django admin.
  Evidence: `templates/portal/patient_account.html:25,28`; only writers are Django admin and demo data (`care/demo.py:165-176`).
- **Kept in backend only.** "Request a change" for name, ID or date of birth opens a message to the clinician. No staff
  screen can edit a patient's name, ID, date of birth, phone, city or record number; only Title and assigned clinician
  can be edited.
  Evidence: `templates/portal/patient_account.html:30-31`, `portal/prescription_views.py:175`.
- **Kept in backend only.** There is nowhere to keep a postal or delivery address; only phone and city can be edited.
  Evidence: `portal/patient_views.py:48-54`.
- **Kept in backend only.** **Sign out other devices** works for any signed-in user, but only the staff My profile has the
  button. For patients it would also write no audit entry.
  Evidence: `accounts/views.py:144-165`, `templates/accounts/profile.html:49`.
- **(unverified)** Patients also have a **My profile** page in the account menu. There they can change the name on their
  login, which doesn't change the name on their patient record.
  Evidence: `templates/includes/account_menu.html:12`, `templates/accounts/profile.html:69-88`.

#### Account menu → Privacy and preferences
- **Kept in backend only.** The marketing preference is saved, but the app sends no marketing, no staff page shows the
  preference, and it creates no marketing consent record.
  Evidence: `care/privacy.py:40-62`, `PatientCommunicationPreference.marketing_enabled` (`care/models.py:1393`). Still
  visible: the patient's page and Django admin.
- **Kept in backend only.** Recorded consents show type and version only. The patient can read the current terms, but not
  the version they accepted, although that text is kept.
  Evidence: `templates/portal/privacy_patient_preferences.html:10`, `care/intake.py:95`.
- **Kept in backend only.** The patient's own views and downloads are never listed, even when the practice stores them.
  Evidence: `portal/privacy_views.py:88-101`, `care/services.py:25-32`.

#### Account menu → My data requests
- **Kept in backend only.** A reply from the practice raises no notification, update or email. The patient can't add to an
  open request, only start a new one. A request for a copy of their information can only be answered with text: staff
  have record exports (Excel and JSON), but nothing hands them to the patient.
  Evidence: `care/privacy.py:89-119`, `care/models.py:1418-1423`.

#### Account menu → Change password
- **Kept in backend only.** Changing the password sends no confirmation email. Only the audit entry "Account password
  changed" is written.
  Evidence: `accounts/views.py:225-237`.

---

## Practice console

### All staff roles (Doctor / Clinician, Practice admin, Super admin)

#### Overview
- **Removed since these edits.** The welcome header lost the role badge and the "View patients" / "View schedule" buttons.
  Nothing is lost: the same links are in the card headers ("View Patients", "View Schedule"). Leftover CSS:
  `.console-role-badge`, `.console-actions`.
  Evidence: `templates/portal/desktop_dashboard.html` (HEAD 32-36 removed), `static/css/console.css:346,380-392`.
- **Removed since these edits.** The "Practice privacy" note now shows only in multi-practice mode, so nobody sees it here.
  Evidence: `templates/portal/desktop_dashboard.html:188-192`.
- **Kept in backend only.** The Recent conversations "Open conversation" links still add a `#conversation-<id>` anchor
  that now exists only in an unused template. The link still lands on the right conversation.
  Evidence: `templates/portal/desktop_dashboard.html:178`, `templates/portal/mobile_dashboard.html:150`.

#### Patient Records & Messages → Patient messages
- **Removed since these edits.** These parts of the old list are gone:
  - the summary cards (Threads in view, Awaiting a reply, Answered)
  - the "Conversations" header with its total
  - each row's patient avatar, the "Awaiting reply / Answered / No messages yet" labels and the per-row "Open record"
    link
  - the page intro and the footnote about private conversations

  The Everything / Awaiting a reply / Answered filters still work.
  Evidence: HEAD `templates/portal/staff_inbox.html:36-50,59,81,88,116,166`.
- **Removed since these edits.** In an open conversation, the "In this conversation: you, … · X added Y · date" line is gone.
  The people and "added by" history are still loaded but not shown; the patient still sees these lines.
  Evidence: `portal/record_messages.py:96` (`participant_rows`), `MessageThreadParticipant.added_by` (`care/models.py:866`).
  Still visible: the patient's side, and the "Add a clinician or close" page lists who is in it.
- **Kept in backend only.** The app no longer uses these values, but still works them out:
  - `thread.record_url` for every row
  - `is_staff_inbox` and `is_doctor`
  - `message_tab_counts`, twice, though the tab badges come from the `message_counts` tag

  Evidence: `portal/inbox.py:158,171-172,201`.
- **Kept in backend only.** "New patient message" can pre-select a patient from `?compose=1&patient=<id>`, but no link in
  the app passes `patient=`.
  Evidence: `portal/inbox.py:167,183-185`.
- **Kept in backend only.** Staff see no read receipt telling them whether the patient has read their reply, although the
  patient's read time is stored. Nor do they see which colleague read the patient's message; that is stored as
  `staff_read_by`, new in these edits.
  Evidence: `PatientMessage.read_at`, `staff_read_by` (`care/models.py:941-944`). Still visible: Django admin (`read_at`
  only).
- **Kept in backend only.** Who started a conversation (`opened_by`) is stored but not shown.
  Evidence: `care/models.py:848`. Still visible: Django admin only.

#### Patient Records & Messages → Team messages
- **Removed since these edits.** These parts of the old layout are gone:
  - the member-count avatar
  - the "Team conversations" header with its count
  - the page intro
  - the "In this conversation" line, now "With … · About … · X added Y"
  - the "About <patient>" link, now "Open <name>'s record"

  Evidence: HEAD `templates/portal/team_inbox.html:40,48,100,102`.
- **Kept in backend only.** Each member's read position is stored, so the app knows which colleagues have read a message,
  but it only drives your own unread badge. Who started the conversation isn't shown.
  Evidence: `TeamThreadMember.last_read_at` (`care/models.py:920`), `TeamThread.opened_by` (`:899`). Still visible: nowhere,
  because the team models aren't in Django admin.

#### Patient Records & Messages → Patient record → Messages tab
- **Removed since these edits.** Paging is gone. The list used to show 20 conversations a page, and a conversation showed
  "Older / Newer messages" in pages of 50. Everything now loads at once.
  Evidence: HEAD `templates/portal/patient_workspace_messages.html:4,8`; `portal/record_messages.py:29-33,91-93`.
- **Removed since these edits.** The "Start a new conversation" form with its own **Subject** field is gone. The record names
  new conversations itself ("Message from Dr <surname>" / "Message from the practice"). A typed subject is still possible
  through Patient messages → New patient message. `record_new_subject` is still computed but no template uses it.
  Evidence: HEAD `patient_workspace_messages.html:17`, `portal/record_messages.py:20-23,62`.
- **Removed since these edits.** The notice "Only conversations you are part of are shown…" is gone.
- **Changed since these edits.** "Manage conversation" is now labelled "Add a clinician or close". **Reopen conversation**
  still exists on that page, but the label no longer hints at it.
  Evidence: `templates/portal/includes/patient_conversation.html:16`, `portal/message_management_views.py:19`.
- **Kept in backend only.** The older ways of posting still work for a request without `return_to`, or with the old value
  `inbox`, but no current template sends one:
  - the reply path
  - the "start a conversation with a typed subject" path
  - `render_workspace_form_error`
  - the appointment-suggestion fallback that renders the unused `patient_detail.html`

  (unverified) In the old reply-error path, the typed text would probably not be shown again.
  Evidence: `portal/views.py:544-585,670-702`, `portal/patient_workspace.py:233-237`, `portal/appointment_views.py:31-33,160-163`.

#### Patient Records & Messages → Patient record → History (clinicians and Super admins)
- **Removed since these edits.** The **"Current consent · <practice>"** card is gone. It listed service, telehealth and
  marketing consent, accepted or not, with version and date. The data is still worked out and appears only in the JSON
  "Export clinical record", plus as individual timeline rows. No other staff screen shows a patient's consent status (lead
  detail shows consents for enquiries only).
  Evidence: HEAD `templates/portal/record_detail.html:66-68`; `portal/record_views.py:341-351,412-415`.
- **Kept in backend only.** History lists a fixed set of audited changes. These changes are stored against the patient but
  never listed:
  - Summary data edits
  - prescription created, renewed or changed (the prescription row itself appears)
  - patient title changes
  - PathCare forms emailed, failed or resent
  - tasks created, updated or completed
  - record tag changes
  - Mark as read or unread
  - conversations closed or reopened, and clinicians added to conversations
  - video calls joined or left

  Evidence: `portal/record_views.py:51-72,189`. Still visible: Django admin only.
- **Kept in backend only.** "Assigned clinician changed" entries don't say from whom to whom. The dated history of which
  clinician looked after the patient is never shown; it is used only to decide who the patient can message.
  Evidence: `ClinicianAssignment` (`care/models.py:878-887`), `care/messaging.py:75-81`. Still visible: nowhere (not in Django
  admin).
- **Kept in backend only.** There are three exports with similar names:
  - the Overview's "Export record" (Excel workbook)
  - History's "Export clinical record" (JSON), which is the only place consents and medical-profile answers are exported
  - History's "Download Excel" (the timeline only)

  The Excel workbook leaves out authorisation instructions, delivery hold reasons, the Summary data edit history,
  medical-profile answers and team conversations.
  Evidence: `templates/portal/record_detail.html:7,31`, `portal/record_views.py:369,383-420`, `portal/record_workbook.py:86-94,119,152-154`.
- **Kept in backend only (older).** `/patients/<pk>/record/` shows the same History page on its own, but nothing links to it.
  Evidence: `portal/record_urls.py:9`.

#### Patient Records & Messages → Patient record → Treatment (→ authorisation page)
- **Kept in backend only (verified, new in these edits).** A prescription renewed or changed from the new Overview dialogs
  appears on the Treatment tab simply as "Cancelled". Its authorisation page says "not currently valid" instead of "replaced
  by … Open its replacement". The page still looks for the old flow's audit event (`treatment.authorized` with
  `previous_authorization_id`) and ignores the new `replaces` link.
  Evidence: `portal/treatment_views.py:110-115`; new flow logs `prescription.renewed/changed` (`care/prescriptions.py:176,220`)
  and sets `replaces` (`:169,207`).

#### Patient Records & Messages → Patient record → Payments
- **Kept in backend only.** Each payment is linked to its invoice, appointment, subscription and cycle. It also has a
  provider reference ("test-code:…") and a failure reason. The tab and the Excel sheet show only due date, amount, status
  and paid date, and the failure reason is never written.
  Evidence: `care/models.py:1103-1112`, `portal/patient_workspace.py:212-213`, `portal/record_workbook.py:116,149-151`. Still
  visible: Django admin only.

#### Patient Records & Messages → Patient record → Appointments (→ appointment page)
- **Kept in backend only.** For suggested new times, the app stores:
  - who answered and when
  - the appointment a rebooking created
  - a snapshot of the original (status, clinician, length)

  Only the suggested and original times, the people, the note and the status are shown.
  Evidence: `AppointmentProposal` fields (`care/models.py:1484-1493`). Still visible: Django admin only.
- **Kept in backend only.** When a patient cancels, the clinician gets no task or notification. The reason the patient typed
  appears only in the History timeline (clinicians).
  Evidence: `care/appointment_lifecycle.py:96-120`.

#### Patient Records & Messages → Patient record → Blood tests
- **Kept in backend only.** The PathCare form behind a request can be opened only from the Overview log. The Blood tests
  tab and the request page don't link to it. Its sent time, resend count and delivery attempts are stored but never shown.
  Evidence: `PathCareRequestForm.emailed_at`, `send_count`, `email_attempts` (`care/models.py:790-792`). Still visible:
  nowhere (not in Django admin).

#### Calendar & Bookings
- **Removed since these edits.** "Times shown in South Africa time (SAST)" is gone. Month view no longer shows these, and
  `is_selected` is still computed for each day but unused:
  - the selected-day highlight
  - each booking's status
  - "+N more"
  - cancelled bookings

  Evidence: HEAD `templates/portal/staff_schedule.html:13,84,107`, `portal/schedule_calendar.py:57-62,74`.
- **Removed since these edits.** Day-off **reasons** (Leave, Sick leave, Other) and **part-day** time off can no longer be
  entered, and the reason isn't shown. The backend still supports both: `create_time_off(starts_at, ends_at, reason)` and
  `DoctorTimeOff.Reason`. Older entries keep a reason nobody can see; older part-day entries still show their times.
  Evidence: `care/availability.py:108-136`, `care/models.py:475-492`, `portal/availability_forms.py:74-92`. Still visible:
  Django admin only.
- **Kept in backend only.** Removed days off are kept, with who removed them and when, but no list shows past or removed
  days off. Who set a special day (useful when an admin sets one for a doctor) isn't shown either.
  Evidence: `DoctorTimeOff.is_active`, `cancelled_at`, `cancelled_by` (`care/models.py:488-492`), `DoctorSpecialDay.created_by`
  (`:544`). Still visible: Django admin only.

#### Account menu → My profile
- **Kept in backend only (verified, new in these edits).** "How you sign consult notes" (`note_signature`) is stored and
  can be edited (pen on My profile; Users and roles), but **nothing uses it**. Signed consult notes and the clinical log
  show the account's full name.
  Evidence: `accounts/models.py:53`, `accounts/views.py:104`, `accounts/profile.py:22`; signing uses `author=actor`
  (`care/clinical.py:176-186`), the log shows `full_name` (`portal/record_overview.py:37`).
- **Kept in backend only (new data).** Staff Title, contact number and HPCSA number appear only on the person's own My
  profile and in Users and roles. They aren't in any staff list, on PathCare forms, in messages or in Django admin's user
  form.
  Evidence: `accounts/models.py:50-52`, `accounts/admin.py:110-115`.
- **Removed since these edits.** Staff can no longer change their own first and last name; a Super admin does it in Users
  and roles. `save_own_profile` still exists, for patient-only accounts.
  Evidence: `accounts/views.py:48-51,69-71`.
- **Removed since these edits.** "Account access history", "Sign out" and "Back to workspace" were taken off the staff My
  profile page. Sign out remains in the account menu.
- **Kept in backend only.** "Your practice access" is still worked out for staff, but shown only in multi-practice mode.
  Evidence: `accounts/views.py:47`.

#### Account menu → My access history
- **Kept in backend only.** The page lists only your own actions, and most action types have no label. These all appear
  as "Recorded account activity":
  - Summary data edits
  - prescription renewals and changes
  - PathCare forms
  - tasks and title changes
  - days off and special days
  - stock, catalogue and shipments
  - compounding
  - staff and role changes
  - follow-ups
  - read/unread marks

  Nobody can see another staff member's actions in the product.
  Evidence: `portal/privacy_views.py:35-47,67`. Still visible: Django admin only.
- **Kept in backend only.** The detail of every audit event is never shown: its metadata, IP address and target record.
  For example, the previous and new clinician, whether a doctor ticked "I have reviewed this safety flag", a stock
  adjustment reason, or an export's row count.
  Evidence: `AuditEvent.metadata`, `ip_address`, `target_id` (`care/models.py:1213-1217`). Still visible: Django admin only.
- **Kept in backend only.** Views, downloads and exports are stored only when Practice settings → "Store view log on
  database" is on. They are never shown in the app.
  Evidence: `care/services.py:25-44`.

### Doctor / Clinician

#### Menu (now flat: Overview, My tasks, Patient Records & Messages, Calendar & Bookings)
- **Removed from the menu since these edits, still open by URL.** These pages left the clinician's menu but still open
  for clinicians, because they only require clinical access:
  - **Consultations** (`/consultations/`)
  - **Blood tests** (`/blood-tests/`)
  - **Authorisations** (`/authorisations/`)
  - **Compounding records** (`/compounding/`)

  The first two are reachable only by typing the URL. The last two can also be reached from the breadcrumb on an
  authorisation or compounding page, which a doctor reaches from a task.
  Evidence: HEAD `portal/navigation.py:22-25`; `portal/clinical_views.py:38,113,185`, `portal/treatment_views.py:48`,
  `portal/compounding_views.py:39`.
- **Removed since these edits (menu and access).** These pages are now off the menu **and refused** for clinicians:
  - Operations: Subscriptions, Supply requests, Weekly shipping list, Dispatch history, Batches and stock, Catalogue
  - Insights: Metrics and cohorts, Activity statements
  - Settings: Review rules

  Evidence: `portal/views.py:68,75-76`, `portal/operations_views.py:64`, `portal/treatment_views.py:152`,
  `portal/reporting_views.py:75`, `portal/review_views.py:17`.
- **Kept in backend only.** The mobile section pages (`/menu/<section>/`) return "not found" for clinicians, who only have
  the flat bar.
  Evidence: `portal/navigation.py:54-55`, `portal/menu_views.py:26-28`.

#### Overview
- **Removed since these edits (still computed).** The count cards are hidden for clinicians on desktop and mobile. The view
  still runs four count queries (patients, appointments today, open tasks, unread messages) for them on every load.
  Evidence: `templates/portal/desktop_dashboard.html:34`, `templates/portal/mobile_dashboard.html:33`, `portal/views.py:119-134`.

#### My tasks
- **Removed since these edits (still computed).** The Open / Overdue / Completed cards are hidden for clinicians, but
  `metrics` still runs three count queries.
  Evidence: `templates/portal/staff_tasks.html:19-25`, `portal/staff_views.py:78-82`.
- **Removed since these edits.** The "Filter tasks" heading and its description are hidden from view (kept as a
  screen-reader heading) for every role.
  Evidence: `templates/portal/staff_tasks.html:28`.

#### Patient Records & Messages → All patients
- **Changed since these edits (verified).** A clinician's list, and their Overview Patients card, show only patients
  assigned to them, and the Clinician filter is gone. Patients they used to look after or cover for no longer appear.
  However, the record page doesn't check assignment, so any patient's record still opens from Team messages, tasks,
  appointment pages or a typed URL. From there the clinician can also start a patient conversation, although "New patient
  message" lists only their own patients.
  Evidence: `portal/record_views.py:109-113`, `portal/views.py:119-122`, `portal/patient_workspace.py:261` (no assignment
  check), `portal/inbox.py:67-73`.

#### Patient Records & Messages → Patient record → Overview (Summary data and prescription)
- **Kept in backend only (new data).** The patient's **height** is stored in Summary data and drives the BMI, but it is
  never shown and can't be edited, so a wrong height silently gives a wrong BMI. The **start weight** is shown but can't be
  corrected; only the goal weight is editable.
  Evidence: `PatientClinicalSummary.height_cm`, `start_weight_kg` (`care/models.py:1279-1280`), `care/clinical_summary.py:22-31`,
  `portal/record_overview.py:110`. Still visible: nowhere for height (the model isn't in Django admin).
- **Kept in backend only (new behaviour).** Summary data follows the questionnaire and the patient's medical profile only
  until the first edit. After that the saved copy is frozen, so later profile changes (medication, allergies, history) no
  longer reach the Overview. The current answers are only in the History sidebar.
  Evidence: `care/clinical_summary.py:87-92,153-162`.
- **Kept in backend only (new behaviour).** The questionnaire asks one combined question, "Chronic medication and
  allergies". Summary data puts the whole answer under **Chronic medication** and shows **Allergies: Not recorded** unless
  the patient later fills in allergies on their profile. A safety-relevant allergy can look absent.
  Evidence: `care/forms.py:363-370`, `care/clinical_summary.py:78-79`.
- **Kept in backend only.** Clinicians never see the questionnaire itself, because Leads is admin-only. That includes the
  18-or-older answer, the weight-related-condition answer, BMI at enquiry, and the screening outcome and reasons.
  Evidence: `portal/lead_views.py:31-32`, `care/clinical_summary.py:72-83`.
- **Kept in backend only.** Medical-profile answers beyond the four that seed Summary data appear only in the History
  sidebar and timeline: family history, smoking, alcohol, activity, nutrition, goals and questions.
  Evidence: `portal/patient_care_forms.py` (`PROFILE_FIELDS`), `care/clinical_summary.py:75-79`.
- **Kept in backend only.** The patient's **ID or passport number** isn't on the record header, Overview or quick facts. It
  appears only in the All patients list and the PathCare preview. **Date of birth** is always "Not recorded".
  Evidence: `templates/portal/patient_workspace.html:14-19`, `templates/portal/patient_list.html:75`.
- **Kept in backend only (new data).** The doctor's tick "I have reviewed this and still want to prescribe" is stored only in
  audit metadata, not on the prescription or in its log line.
  Evidence: `care/prescriptions.py:107-113,176-178,220-223`. Still visible: Django admin only.
- **Kept in backend only (new data).** Each renewal or change records which prescription it replaced (`replaces`), but no
  screen shows the link (see Treatment above).
  Evidence: `care/models.py:223`, `care/prescriptions.py:169,207`.
- **Kept in backend only.** `prescription_days_left` is computed for the Overview but never shown. The Summary data edit
  history shows the latest 50 edits only; older ones are counted but can't be read, and the history isn't in either export.
  Evidence: `portal/record_overview.py:118-129,148`; `PatientSummaryEdit` isn't in Django admin.

#### Patient Records & Messages → Patient record → Deliveries
- **Removed since these edits.** Clinicians no longer get "Open delivery", and every Operations page now refuses them. They
  can no longer see a shipment's hold reason, items and batches at dispatch, delivery address, or prepared, locked and
  delivered times. Nor can they see the patient's own note on a supply request. Only scheduled date, cycle, status and
  tracking remain in the tab, and dispatched batches appear in the Overview "Dispensed" log.
  Evidence: `templates/portal/patient_workspace.html:76`, `portal/operations_views.py:64`, `care/models.py:1059-1066,1321-1322`.

#### Calendar & Bookings
- **Removed since these edits.** Clinicians lost the "View schedule" form (date picker and "Whose diary") and the
  "Available time" card. They move with the arrows, Today, the Month grid and "Next appointment day". A `?date=` typed in
  the URL still works, and the open-slot calculation is skipped for them.
  Evidence: `templates/portal/staff_schedule.html:37-62,198-215`, `portal/staff_views.py:140-144,186-191`.
- **Kept in backend only (new in these edits).** A clinician's own day off or special day that clashes with bookings
  creates a high-priority "Offer new times" task for the practice. The clinician doesn't see that task in My tasks.
  Evidence: `care/availability.py:218-265`, `care/task_services.py:19-20`.

#### Insights → Activity statements
- **Removed since these edits.** Clinicians could open their own activity statements at the last commit (counts, rates,
  reference amount, approval). Insights is now admin-only, and statements are for Super admins only. The "clinicians see
  only their own statements" branch still exists but is unreachable for them. "Your month to date" on My profile shows
  counts only.
  Evidence: `portal/reporting_views.py:75,113-117`, `DoctorActivityStatement` (`care/models.py:1434-1446`).

#### Account menu → My access history
- **Removed since these edits.** Hidden from the clinician's account menu, and the page refuses clinicians. They can no
  longer see their own record of changes.
  Evidence: `templates/includes/account_menu.html:18`, `portal/privacy_views.py:241-243`. Still visible: Django admin only.

### Practice admin

#### Menu (Care, Operations, Administration, Insights)
- **Removed since these edits.** The separate Care → "Messages" item is gone. Patient messages and Team messages are now
  tabs of Patient Records & Messages, which carries the unread badge. Nothing is lost.
  Evidence: `portal/navigation.py:28-31`, `templates/portal/includes/records_header.html:8-12`.
- **Kept in backend only (edge case, unverified whether such accounts exist).** A practice admin who also has a clinician
  type used to see Settings → Review rules and Insights → Activity statements. Both are now Super-admin-only in the menu,
  but both pages still open for that person by URL.
  Evidence: `portal/navigation.py:85-93`, `portal/review_views.py:16-17`, `portal/reporting_views.py:113-117`.

#### Patient Records & Messages → Patient record (administrative view)
- **Kept in backend only.** The admin's History tab lists only a fixed set of operational events. These are stored against
  the patient but not listed:
  - title edits
  - tasks
  - conversations closed or reopened, and clinicians added to them
  - shipments prepared, held or cancelled
  - care plans paused or resumed

  Evidence: `portal/patient_workspace.py:42-52,222-228`. Still visible: Django admin only.
- **Kept in backend only.** The Deliveries tab doesn't load the hold reason; staff must open each delivery to see why it is
  held.
  Evidence: `portal/patient_workspace.py:218`.
- **Kept in backend only.** The Payments tab doesn't show invoices, invoice lines, the provider reference or the failure
  reason (see All staff → Payments).
- **Kept in backend only.** A practice admin can't export the record. "Export record" (Excel) is for clinical staff, and the
  workbook download refuses admins.
  Evidence: `portal/record_workbook.py:171`.

#### Patient Records & Messages → All patients
- **Kept in backend only.** The **Cycle** and **Next debit** columns are never filled, because plan cycles don't advance and
  next debit is always blank.
  Evidence: `templates/portal/patient_list.html:79-80`, `care/treatment.py:210,242`.

#### Operations → Weekly shipping list / Dispatch history (→ shipment page)
- **Kept in backend only.** The weekly shipping list has no delivery-address column. A supply request's address appears only
  on its own page, and is copied into the shipment only at dispatch. Subscription parcels have **no address at all**,
  because none is ever collected.
  Evidence: `templates/portal/operations_shipping.html:6`, `care/operations.py:371-372`.
- **Kept in backend only.** The shipment page doesn't show who prepared it, or which authorisation and care plan it was made
  under.
  Evidence: `Shipment.prepared_by`, `authorization`, `subscription` (`care/models.py:1054,1062-1063`). Still visible: Django
  admin only.

#### Operations → Batches and stock
- **Kept in backend only.** A stock movement is linked to the shipment it was allocated to, but the batch page's movement
  list doesn't show which shipment.
  Evidence: `StockMovement.shipment` (`care/models.py:1022`). Still visible: Django admin only.

#### Operations → Subscriptions
- **Dormant.** There is a per-cycle billing record (cycle number, start, due date, amount, status), but no product flow
  creates one (only demo data does) and no page shows one.
  Evidence: `SubscriptionCycle` (`care/models.py:273-307`), only writer `care/demo.py:227`.

#### Operations → Catalogue
- **Kept in backend only (new data).** The product's new "How often a dose is taken" is editable in the product form but not
  shown on the read-only product view or to patients.
  Evidence: `portal/operations_forms.py:12-16`, `care/models.py:188-196`.

#### Administration → Leads
- **Kept in backend only.** Admins can't review a questionnaire or change a lead's screening outcome. The questionnaire's
  review status, reviewer and clinical notes are never set (saving the questionnaire wipes them). In production every lead
  stays *Pending*, and pending leads aren't counted in any summary card.
  Evidence: `ScreeningQuestionnaire.status`, `reviewed_by`, `clinical_notes` (`care/models.py:127-131`), `care/intake.py:124-125`,
  `portal/lead_views.py:43-48`. Still visible: Django admin (editable there, but then invisible in the app).
- **Kept in backend only.** Admins can't turn a lead into a patient, send a booking link, or link a lead to an existing
  patient. Patients are created only at the test-code checkout or in Django admin. Follow-up can set the stage to
  *Interested in booking*, but that changes nothing else.
  Evidence: `care/followups.py:47-53`, `care/checkout.py:123-140`.
- **Kept in backend only.** A converted lead says "Converted enquiry" but doesn't link to the patient record, and the record
  doesn't link back. The notice still says "Payment processing and account activation are not available in this
  checkpoint".
  Evidence: `Lead.converted_patient` (`care/models.py:104`), `templates/portal/staff_lead_detail.html:14`.
- **Kept in backend only.** The lead page doesn't show any of these: earlier answers after an edit, the sequence of
  screening outcomes, the full consent text accepted, or the IP address recorded with each consent.
  Evidence: `care/intake.py:95,117-137`, `care/models.py:158`.

#### Administration → Dropouts
- **Kept in backend only.** The list has no phone, email or link to the patient record, which staff need to follow up. The
  date a plan was paused isn't shown, and the patient's reason for stopping is never captured. Each follow-up note stores
  the lead stage before and after, but only "after" is shown.
  Evidence: `templates/portal/followup_dropouts.html:5`, `care/treatment.py:223-251`,
  `AdministrativeFollowUp.stage_before` (`care/models.py:1373`).

#### Administration → Data requests
- **Kept in backend only.** The request page has no link to the patient's record, and a reply can't carry an attachment such
  as the exported record. A patient's marketing preference isn't visible to any staff member, so the practice can't check
  it before contacting someone.
  Evidence: `templates/portal/privacy_staff_request_detail.html:4-5`, `care/models.py:1393,1418-1423`.

### Super admin

Everything listed for All staff and Practice admin applies. A Super admin with a clinician type also gets the Doctor
items about the record Overview and History.

#### Patient Records & Messages → Patient record (a Super admin who isn't also a clinician)
- **Kept in backend only.** They can edit Summary data, which is seeded from the patient's medical profile, but they can't
  see the profile itself. The History sidebar and the profile versions are shown only to clinicians.
  Evidence: `portal/record_views.py:154,172-175,349-350`, `care/clinical_summary.py:123-127`.

#### Care → Compounding records
- **Kept in backend only.** The cancellation time, the reviewer and the snapshot date are stored but not shown. The status
  reads "Cancelled before submission" with no date.
  Evidence: `CompoundingRecord.cancelled_at`, `reviewed_by`, `snapshot.recorded_on` (`care/compounding_models.py:21,25,29`).
  Still visible: Django admin only.

#### Insights → Activity statements
- **Kept in backend only.** Each statement stores the exact appointments and messages it counted (`source_ids`), but neither
  the page nor the CSV export shows them, so a count can't be checked against the records behind it.
  Evidence: `care/models.py:1442`, `care/reporting.py:151-157`, `portal/reporting_views.py:171-202`. Still visible: Django
  admin only.

#### Settings → Practice settings
- **Kept in backend only.** Practice settings can change only "Store view log on database". These settings are shown on
  public and patient pages but can be changed only in Django admin:
  - the consultation fee
  - the two subscription amounts
  - the delivery interval
  - the support email

  The default review interval is editable in Review rules.
  Evidence: `portal/practice_settings_views.py:62-66`, `care/models.py:36-47`, `care/admin.py:93-95`.
- **Dormant.** "Payment grace days" and "Time zone name" are stored per practice and used nowhere.
  Evidence: `PracticeSettings.payment_grace_days`, `timezone_name` (`care/models.py:44,46`).
- **Kept in backend only (new in these edits).** The "Event log settings" list of what is always stored leaves out several
  event types these edits added: Summary data edits, patient title changes, PathCare forms emailed, failed or resent, and
  Mark as read/unread.
  Evidence: `portal/practice_settings_views.py:21-59`.
- **Changed (backend).** Two view or export events, `activity_statement.viewed` and `shipping.manifest_exported`, are stored
  even when "Store view log on database" is off. They are missing from the list of view and export actions, so they appear
  as "Recorded account activity".
  Evidence: `portal/reporting_views.py:199`, `portal/operations_views.py:430`, `care/services.py:27-32`.

#### Account menu → Practice administration (Django admin; superusers only)
- **Kept in backend only.** These records have no screen in the practice console and can only be managed in Django admin:
  - invoices and invoice lines
  - subscription cycles
  - payout runs and doctor payout lines
  - email journeys and email deliveries
  - company invites

  Leads, questionnaires, weight entries, message threads, patient events, payments and record tags can also be edited
  directly there.
  Evidence: `care/admin.py:104-115,140-158,272-295,321-366`.
- **Nowhere at all.** Ten models are **not registered** in Django admin, so their data can't be seen outside the product
  screens described above:
  - PathCare request forms
  - patient medical profiles and their versions
  - Summary data and its edit history
  - conversation participants
  - clinician assignment history
  - team conversations, members and messages

  Evidence: `care/admin.py` (registers every other care model).
- **Kept in backend only.** There are three management commands: `refresh_review_tasks` (also in the UI as Review rules →
  run check), `bootstrap_practice_admin` and `seed_demo`.

---

## Behind the scenes (no UI at all)

- **Emails.** Only two exist: the password-reset email and the PathCare form email. These don't exist:
  - booking confirmation
  - appointment reminder
  - new-message alert
  - enquiry acknowledgement
  - delivery, payment or password-changed email
  - SMS
- **Email journeys (dormant).** `EmailJourney` and `EmailDelivery` have triggers for lead created, consult booked, payment
  failed, review due and shipment dispatched, but no code sends them. PathCare emails create no delivery rows.
  Evidence: `care/models.py:1174-1207`; used only in Django admin and demo data.
- **Celery (new in these edits).** There is one task: the PathCare email, which retries 3 times 2 minutes apart and is then
  marked *Failed*. Without a broker it runs straight away in the web process. There is no scheduler (Celery beat) and no
  scheduled job.
  Evidence: `care/tasks.py`, `config/celery.py`, `config/settings.py:199-207`.
- **Blood-test emails marked "Sent" when nothing was sent (verified).** With no email host configured, Django writes emails to
  the server log. The PathCare send "succeeds" and the form is marked Emailed. Password reset checks for this; the
  PathCare request doesn't.
  Evidence: `care/pathcare.py:261-263`, `config/settings.py:194`.
- **Review reminders.** These run only when triggered: from Review rules → run check, or the `refresh_review_tasks` command.
  The 30-day reminder tasks and parcel holds aren't automatic. Review rules (interval, "blood tests required") are
  planning records that no patient or authorisation uses.
  Evidence: `care/review_automation.py:35-84`, `care/review_rules.py:32-42`.
- **Dormant models.** No product flow writes or shows these; only demo data or Django admin does:
  - `PayoutRun` and `DoctorPayoutLine`, in practice replaced by activity statements
  - `EmailJourney` and `EmailDelivery`
  - `CompanyInvite`, as there is no invite flow
  - `SubscriptionCycle`
  - `AvailabilitySlot`, which booking code reads and updates but never creates

  Evidence: `care/models.py:273,396,1126,1144,1174,1192,1224`; only writer `care/demo.py`.
- **Dormant fields.** Nothing writes these:
  - `Appointment.outcome_notes`
  - `Payment.failure_reason`
  - `ScreeningQuestionnaire.status`, `reviewed_by` and `clinical_notes`, which are always reset
  - questionnaire stage 2
  - lead stage *Consultation* and screening outcome *Not eligible*
  - next debit date and the thereafter rate
  - payment grace days and time zone name
  - patient date of birth and record number
- **Single practice.** `MULTI_PRACTICE_ENABLED = false` hides or refuses everything practice-related:
  - the practice switcher
  - Settings → Practices
  - the practice privacy note
  - the sidebar practice name
  - "Your practice access" on the staff profile

  Evidence: `practices/tenancy.py:41-43`, `portal/views.py:203-260`, `practices/management_views.py:163-166`.
- **Superuser "Work as" role switcher.** It is in the account menu, for technical superusers in single-practice mode only.
  Evidence: `practices/role_switching.py:67-82`.
- **Staff who leave.** Data can become unreadable when a staff member leaves:
  - Patient conversations whose only staff participants are deactivated can't be read by anyone at the practice; only
    conversations with no one in them are re-adopted.
  - The same applies to that person's private notes and unsigned consultation drafts.
  - Team conversations can be read by nobody at all, because the team models aren't in Django admin.

  Evidence: `care/messaging.py:18-22,119-122`.
- **Deactivated patients vanish.** No product flow sets `Patient.is_active = False`. If a superuser does it in Django admin,
  every list, record and export filters the patient out.
  Evidence: `portal/patient_workspace.py:79`, `portal/record_views.py:302`.

## Open questions

### Removed by these edits: please confirm each is intended
1. **Consent status.** It is no longer visible to staff on the record (JSON export only). Bring the card back, or show
   consent elsewhere?
2. **"Who added whom" in patient conversations.** Hidden from staff, still shown to the patient. Restore it for staff?
3. **Day-off reasons and part-day time off.** The UI can no longer create or show them, but the backend still supports
   them. Remove them from the backend, or bring back the reason?
4. **Clinician access.** Clinicians now have only their own patients in lists, no Operations pages (so no hold reasons or
   supply-request notes), no own activity statements and no My access history. Please confirm each, and decide whether
   the "own statements" logic should go.
5. **"Awaiting reply" cues.** Removed from both the patient's and the staff message lists; the filters remain. OK?
6. **Record Messages tab.** It no longer pages, and has no Subject field. OK for long histories?

### Stored but not shown, or not used
7. **Note signature (verified).** Should "How you sign consult notes" appear on signed notes, the clinical log and the
   Excel export?
8. **Prescription replacement (verified).** Point the authorisation page at `TreatmentAuthorization.replaces`, so
   renewals and changes link to their replacement instead of reading "not currently valid"?
9. **Change prescription drops the doctor's instructions.** Carry them over, or add an instructions field to the dialog?
10. **Summary data.**
    - Make height visible and editable, and start weight correctable?
    - Should Summary data keep following the patient's profile after the first edit, or flag a newer profile?
    - Split "Chronic medication and allergies" into two questions, or seed the answer into both fields?
11. **The patient's view of their care.**
    - Should patients see the doctor-confirmed Summary values and goal weight, or should the "What your doctor has on
      file" wording change?
    - Should patients see their PathCare form?
    - Should patients see their Title in Account settings?
    - Should patients see why a parcel is on hold?
    - Should the Blood tests list get a menu entry?
12. **Invoices and payment details.** Show invoices and payment references to staff, the patient, or both? Store the
    checkout payment method?

### Not built yet in the product
13. **Production onboarding (verified, older code).** Screening never runs and checkout refuses everyone with `DEBUG` off,
    and no screen converts a lead. Is a staff "review lead / convert / send booking link" step planned, or is Django admin
    the intended route?
14. **Patient demographics.** Date of birth and record number are never filled, and no staff screen can edit a patient's
    name, ID, date of birth or phone. Derive date of birth from the SA ID? Add staff editing?
15. **Address.** No address is captured at onboarding or kept on the patient. Supply-request addresses are reused on PathCare
    forms. Add a postal or delivery address to Account settings? Should subscription parcels require one?
16. **Notifications.** There are no emails for bookings, messages, data-request replies, deliveries or a password change.
    The email-journey models are ready but unwired. Which should come first?
17. **Access by link (verified).** Doctors can open any patient's record by link, and can still open the four clinical list
    pages by URL. Should records check assignment? Should the list pages be refused?
18. **Hand-over.** Is a hand-over step needed when a staff member's access is removed, so their conversations, private notes,
    drafts and team threads aren't orphaned?
19. **Three exports with similar names**: Excel "Export record", JSON "Export clinical record" and "Download Excel" for
    History. Keep all three?
20. **Practice settings.** Add the consultation fee, subscription amounts, delivery interval and support email to Practice
    settings, so they don't need Django admin?
21. **Dead code.** Delete the legacy POST paths and unused values listed in Appendix B, once no outside integration relies
    on them?

---

## Appendix A: Routes with no ordinary link

Every named route (167) was matched against menus, templates, forms, JavaScript and redirects. These are the ones reached only in unusual ways; every other route is linked normally.

| URL name | Path | View | How reachable |
|---|---|---|---|
| `staff-patient-record` | `/patients/<pk>/record/` | `ClinicalRecordView` | Not linked anywhere; type the URL. History tab shows the same view via `?tab=history` (pre-existing) |
| `staff-patient-record-export` | `/patients/<pk>/record/export/` | `ClinicalRecordExportView` (JSON) | Only from History tab "Export clinical record" (`record_detail.html:7`) |
| `patient-labs` | `/patient/blood-tests/` | `PatientLabListView` | Only the breadcrumb on a patient blood-test request page (pre-existing) |
| `patient-subscription` | `/patient/subscription/` | `PatientSubscriptionView` | Legacy address; GET redirects to My Treatment (pre-existing) |
| `clinical-consultations` | `/consultations/` | `ConsultationListView` | Doctors: URL only. Super admin: Care menu |
| `clinical-labs` | `/blood-tests/` | `LabListView` | Doctors: URL only. Super admin: Care menu |
| `treatment-authorisations` | `/authorisations/` | `AuthorizationListView` | Doctors: breadcrumb on authorisation pages, or URL. Super admin: menu |
| `compounding-list` | `/compounding/` | `CompoundingListView` | Doctors: breadcrumb on compounding pages, or URL. Super admin: menu |
| `treatment-review-rules` (+ create/detail/default/run) | `/review-rules/…` | `ReviewRule*View`, `ReviewRunView` | Super admin menu. Practice admin with clinician type: URL only (edge case) |
| `activity-statements` (+ create/detail/export) | `/activity-statements/…` | `Statement*View` | Super admin menu. Practice admin with clinician type: URL only (edge case). Doctors: blocked |
| `staff-menu-section` | `/menu/<section>/` | `StaffMenuSectionView` | Mobile bottom bar for admins only; not found for doctors |
| `activate-company`, `activate-patient-company` | `/companies/<slug>/activate/`, `/patient/companies/<slug>/activate/` | `Activate*CompanyView` | Multi-practice only; permission denied here |
| `management-practices` / `-create` / `-edit` | `/settings/practices/…` | `ManagementPractice*View` | Multi-practice only; permission denied here |
| `practice-role` | `/accounts/practice-role/` | `PracticeRoleView` | Account menu "Work as", technical superusers only |
| `health` | `/health/` | `healthcheck` | DigitalOcean health check only |
| `ice-config` | `/video/appointments/<id>/ice/` | `video.views.ice_config` | Fetched by the video room's JavaScript only |
| `room` | `/video/appointments/<id>/` | `video.views.room` | Built as a plain string in `video/access.py:50-51` (no `reverse`); shown as "Join" links |
| `staff-patient-record-history` | `/patients/<pk>/record/history/` | `ClinicalRecordHistoryView` | History tab's scroll-to-load JavaScript only |
| `record-conversation-search` | `/patients/<pk>/messages/search/` | `RecordConversationSearchView` | Search-as-you-type JavaScript on the record's Messages tab only |
| `password-change-done`, `password-reset-done`, `password-reset-complete` | `/accounts/password/…` | Django auth views | Redirect targets after the forms only |
| `password-reset-confirm` | `/accounts/password/reset/<uid>/<token>/` | `AccountPasswordResetConfirmView` | Emailed link only |
| `availability-*` (6 routes) | `/schedule/…` | `availability_views` | Form actions built in the view (`portal/availability_views.py:80,91,138-142`); all used |
| `clinical-lab-result-download`, `patient-lab-result-download` | `…/report/` | `*LabResultDownloadView` | URLs built in `portal/clinical_views.py`; used |

Every other named route is linked from a menu, template, form or redirect.

## Appendix B: Code and styles left behind

Candidates for clean-up. Nothing here affects users today.

**Views and templates**
- `PatientDetailView` (`portal/views.py:263-288`) has no route. It was unrouted before this work, in commit `011efde`.
- `templates/portal/patient_detail.html` is only rendered by the unreachable appointment-suggestion error fallback (`portal/appointment_views.py:160-163`). It still posts to `staff-thread-create` and `staff-message-create` without `return_to`. Its CSS `.thread-message*` is in `static/css/console.css:783-794`.
- `_patient_record_context` (`portal/views.py:365-421`, including `'thread_form': PatientThreadForm(...)` at :412) and `_conversation_threads` (`portal/views.py:291-320`) serve only those two paths.
- `portal/task_views.py:18` imports `_patient_record_context` but never uses it.
- The legacy POST paths described under the record's Messages tab:
  - `return_to` `'inbox'` alias (`portal/views.py:557`, `portal/appointment_views.py:31-33`)
  - the no-`return_to` branches of `StaffMessageCreateView` and `StaffThreadCreateView` (`portal/views.py:575-576,585,696,700-701`)
  - `render_workspace_form_error` for `message_form`, `thread_form` and `proposal_form` (`portal/patient_workspace.py:233-237`)
  - `legacy_thread_form` handling (`portal/record_messages.py:44-49`)
- `staff_inbox_redirect()` (`portal/inbox.py:37-38`) has a misleading name: it now goes to the record, not the inbox.
- `messages_tabs.html` include was deleted, and no references remain. **(OK)**

**Values computed but never shown**
- Doctor Overview `metrics` (`portal/views.py:119-134`) and doctor My tasks `metrics` (`portal/staff_views.py:78-82`).
- `thread.record_url` (`portal/inbox.py:158`, `portal/record_messages.py:38`).
- `record_new_subject` (`portal/record_messages.py:62`).
- `participant_rows` on the staff side (`portal/record_messages.py:96`).
- `is_staff_inbox`, `is_doctor` in Patient messages (`portal/inbox.py:171-172`).
- `message_tab_counts` in the page data (`portal/inbox.py:201`, `portal/team_views.py:120,132`); the badges use the `message_counts` tag.
- `prescription_days_left` (`portal/record_overview.py:148`).
- `is_selected` per calendar day (`portal/schedule_calendar.py:74`).
- `membership_page` for staff in single-practice mode (`accounts/views.py:47`).
- (pre-existing) `legacy_appointment_context` on the record Overview (`portal/patient_workspace.py:162`); only the Appointments tab form uses it.

**Unused parameters and options**
- `?patient=` pre-select for "New patient message" (`portal/inbox.py:167,183-185`).
- `selected_id=` keyword is never passed (`portal/inbox.py:130`, `portal/record_messages.py:26`).
- `messages_title(membership)` ignores its argument (`portal/navigation.py:23-25`).

**Logic nothing reaches**
- The clinician branch of `StatementView.statements()` (`portal/reporting_views.py:113-117`).
- Part-day time off and reason (`care/availability.py:108-126`, `care/models.py:478-487`).

**CSS rules no longer used**
- `.console-actions` (`static/css/console.css:346,1396-1397`)
- `.console-role-badge` (`static/css/console.css:380-392`, `static/css/workspace_polish.css:31`)
- `.staff-calendar*` old month grid (`static/css/console.css:1080-1255`)
- `.staff-schedule-grid` (`static/css/console.css:1052-1054,1223,1247`) and `.staff-schedule-grid.schedule-workspace-grid` (`static/css/schedule_availability.css:2,160,166`)
- `.schedule-panel-subheading`, `.schedule-time-off-dates`, `.schedule-time-off-empty` (`static/css/schedule_availability.css:39-47`)
- `.patient-workspace-message-grid` (`static/css/patient_workspace.css:51,55`)
- `.inbox-page-header` (`static/css/workspace_polish.css:12`)
- The removed inbox classes (`.inbox-metrics`, `.inbox-thread*`, `.inbox-conversation*`, `.inbox-message*`, `.messages-tabs`, `.message-stack`, `.patient-inbox-pill--waiting`) were cleaned out of the CSS. **(OK)**

**Static files not loaded by any template** (pre-existing)
- `static/css/desktop.css`, `static/css/mobile.css`.
- All other CSS and JS files, including the new `conversations.js`, `conversation_search.js`, `record_overview.js` and `toasts.js`, are loaded somewhere. Every template under `templates/` is rendered or included by something; the only exception, `admin/login.html`, is a Django admin override.

## Appendix C: Model field coverage

Every model, with the fields that no product screen, form, script or export shows.

"Not shown" means not displayed or editable in any product template, form, JS or export. Plumbing is listed only where it
helps. "Visible elsewhere" says where the data can still be seen.

| Model (file:line) | Fields not shown anywhere in the product UI | Visible elsewhere |
|---|---|---|
| User (`accounts/models.py:34`) | `note_signature` (shown on My profile but never applied); `title`, `contact_number`, `hpcsa_number` (My profile and Users and roles only) | Not in the Django admin user form |
| Company (`practices/models.py:15`) | none | n/a |
| CompanyMembership (`practices/models.py:55`) | none | n/a |
| Patient (`practices/models.py:144`) | `title` hidden from the patient; `date_of_birth`, `medical_record_number` shown but never filled; `is_active` | Django admin |
| PracticeSettings (`care/models.py:36`) | `payment_grace_days`, `timezone_name` (unused); fee, subscription amounts, delivery interval and support email not editable in the app | Django admin |
| ConsentDocument (`:57`) | `content_hash` | Django admin |
| Lead (`:78`) | `converted_patient` (no link); `submission_key` | Django admin |
| ScreeningQuestionnaire (`:119`) | `status`, `reviewed_by`, `clinical_notes` (dormant); `answers.consent_snapshot`, `answers.service_consent`, `answers.telehealth_consent`; whole answers hidden from clinicians and from the patient after conversion | Lead page (admins); Django admin |
| ConsentRecord (`:143`) | `ip_address`; `user`; staff "Current consent" card removed | History timeline and JSON export; Django admin |
| MedicationProduct (`:172`) | none | n/a |
| TreatmentAuthorization (`:206`) | `replaces`; `change_reason` (only inside `log_text`) | Django admin |
| PatientSubscription (`:246`) | none | n/a |
| SubscriptionCycle (`:273`) | all (dormant) | Django admin |
| Invoice (`:310`) | all | Django admin |
| InvoiceLine (`:344`) | all | Django admin |
| Appointment (`:360`) | `video_link`, `outcome_notes` | Django admin |
| AvailabilitySlot (`:396`) | all (legacy) | Django admin |
| DoctorWorkingPattern (`:422`) | none | n/a |
| DoctorTimeOff (`:475`) | `reason` (Removed), `cancelled_at`, `cancelled_by`, `is_active` history | Django admin |
| DoctorSpecialDay (`:534`) | `created_by` | Django admin |
| ClinicalEncounter (`:569`) | `revision`, `signing_task`, `signed_note`, `submission_key` (plumbing) | Django admin |
| RecordTag (`:623`) | `created_by` | Django admin |
| TaskTagAssignment / ClinicalNoteTagAssignment (`:643`, `:658`) | who added a tag and when (`created_at`) | Django admin |
| ClinicalTask (`:673`) | none | n/a |
| ClinicalNote (`:715`) | none (private notes by design) | n/a |
| LabRequest (`:733`) | `review_task` (plumbing); `result_summary` only on the History sheet in Excel, not on the Blood tests sheet | Lab page, History, both exports |
| PathCareRequestForm (`:772`) | `emailed_at`, `send_count`, `email_attempts`, `size`, `sha256`; PDF not available to the patient | Nowhere (not in Django admin) |
| LabResult (`:805`) | `sha256` | Django admin |
| WeightEntry (`:829`) | none | n/a |
| MessageThread (`:840`) | `opened_by` | Django admin |
| MessageThreadParticipant (`:861`) | `added_by` and `created_at` for staff (Removed) | Patient side only; not in Django admin |
| ClinicianAssignment (`:878`) | all (assignment history) | Nowhere (not in Django admin) |
| TeamThread (`:890`) | `opened_by` | Nowhere |
| TeamThreadMember (`:914`) | `last_read_at` of others | Nowhere |
| TeamMessage (`:927`) | none | n/a |
| PatientMessage (`:936`) | `read_at` for staff (patient read receipts), `staff_read_by`, `staff_read_at` for the patient | Django admin (`read_at` only) |
| PatientEvent (`:955`) | `source_type`, `source_id`, `is_patient_visible` | Django admin |
| MedicationBatch (`:980`) | none | n/a |
| StockMovement (`:1010`) | `shipment`, `idempotency_key` | Django admin |
| Shipment (`:1045`) | `prepared_by`, `authorization`, `subscription`; all detail now hidden from clinicians (Removed) | Shipment page (admins); Django admin |
| ShipmentItem (`:1078`) | none | n/a |
| Payment (`:1096`) | `subscription`, `subscription_cycle`, `appointment`, `invoice`, `provider_reference`, `failure_reason` | Django admin |
| PayoutRun (`:1126`) | all (dormant) | Django admin |
| DoctorPayoutLine (`:1144`) | all (dormant) | Django admin |
| ReviewRule (`:1162`) | none | n/a |
| EmailJourney (`:1174`) | all (dormant) | Django admin |
| EmailDelivery (`:1192`) | all (dormant) | Django admin |
| AuditEvent (`:1210`) | `metadata`, `ip_address`, `target_id`; most action types; anyone else's actions; view and export events | Django admin |
| CompanyInvite (`:1224`) | all (dormant) | Django admin |
| PatientMedicalProfile (`:1241`) | family history, smoking, alcohol, activity, nutrition and goals not on the Overview; whole profile hidden from non-clinician Super Admins | History tab (clinicians), JSON export |
| PatientMedicalProfileRevision (`:1250`) | answers not in Excel | History tab, JSON export |
| PatientClinicalSummary (`:1266`) | `height_cm` (not shown or editable); `start_weight_kg` not editable | Nowhere for height |
| PatientSummaryEdit (`:1297`) | edits beyond the latest 50; not in exports | Nowhere (not in Django admin) |
| PharmacyOrder (`:1310`) | `revision` (plumbing); `note` and `delivery_address` now hidden from clinicians (Removed) | Ops pages (admins), patient page |
| PharmacyOrderItem (`:1337`) | none | n/a |
| AdministrativeFollowUp (`:1359`) | `stage_before` | Django admin |
| PatientCommunicationPreference (`:1391`) | `marketing_enabled` hidden from all staff | Patient page; Django admin |
| PatientDataRequest / Reply (`:1396`, `:1418`) | none | n/a |
| DoctorActivityStatement (`:1434`) | `source_ids`; whole statement now hidden from clinicians (Removed) | Super Admin pages; Django admin |
| AppointmentProposal (`:1460`) | `responded_by`, `responded_at`, `resulting_appointment`, `original_status`, `original_clinician`, `original_duration_minutes`, `proposer_role` | Django admin |
| CompoundingRecord (`care/compounding_models.py:10`) | `cancelled_at`, `reviewed_by`, `snapshot.recorded_on`, `revision` | Django admin |
| AuthorizationReviewReminder (`care/compounding_models.py:54`) | the reminder row itself (its task is shown) | Django admin |
| CallSession (`video/models.py:13`) | `last_seen_at`, `lease_expires_at`, `presence_revision` (plumbing); hidden from patients | Staff appointment page; Django admin |
| CallParticipant (`video/models.py:54`) | all: `user`, `joined_at`, `last_seen_at`, `ended_at`, `end_reason` | Django admin only |
