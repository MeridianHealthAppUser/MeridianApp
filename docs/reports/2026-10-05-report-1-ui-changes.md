# Report 1: What the uncommitted edits change in the UI

Prepared 5 October 2026 for James Westraadt.

**What this compares.** The last commit (`63d7132 Fix spinner`) against everything uncommitted in the working tree:
112 modified files (+3,670 / −1,332 lines) and 54 new files or folders. That includes this session's work and the earlier
uncommitted Rev 3.10 / 3.11 work: special days, prescriptions, Summary data, PathCare blood forms, password reset,
Celery and staff details.

**How it's organised.** By surface (Patient web app, then Practice console), then user type, then menu option. Each
bullet starts with a label, says in plain words what a user sees or can do, and gives the code evidence (`file:line`).

| Label | Meaning |
|---|---|
| **Added** | New screen, button, field or behaviour |
| **Changed** | Existing thing that looks or works differently |
| **Removed** | Something that was on screen and no longer is |
| **Kept** | Called out because a neighbouring change might suggest it was removed |
| (indirect) | The page's template didn't change, but what it shows did, because of backend changes |

**How it was checked.** Six read-only passes over the diff and the code, then spot checks of the most important claims
(marked **verified** in "Open questions"). The suite passes: 1293 tests. Items marked "(unverified)" were read from
code but not run.

## At a glance

- **Clinicians have a new flat menu**: Overview, My tasks, Patient Records & Messages, Calendar & Bookings. Operations,
  Insights, Review rules and My access history are hidden and now refused for them. Consultations, Blood tests,
  Authorisations and Compounding records are hidden but still open by direct link.
- **Patient Records & Messages** replaces the separate Patients and Messages items. It has three tabs: All patients,
  Patient messages and Team messages. The record's Messages tab and both message tabs share one layout:
  - a conversation list and a "Choose a conversation" pane
  - read/unread envelope buttons
  - search as you type
  - "New patient message"
- **Clinicians and Super admins get a new clinical record Overview**:
  - a clinical/dispensed log
  - editable Summary data with safety flags
  - a weight chart with expected range and projection
  - Current prescription with Renew and Change dialogs
  - Request blood tests (PathCare form preview and email)
  - an Excel "Export record"
- **Calendar & Bookings was rebuilt**:
  - Day and Month views, with booking chips in Month view
  - a Working Pattern & Availability tab with the new Special days and date-only Days off
  - clash warnings with Undo, plus automatic "Offer new times" tasks for the practice
- **My profile for staff** is now mostly read-only:
  - Title is shown above First name
  - clinicians have pen edits for note signature and PathCare name and code
  - there is a Security card with "Sign out others"
  - the Super admin maintains staff details in Users and roles, including Title (default Dr) for new staff
- **Overview and My tasks for doctors** lost their count cards. The Overview header is simplified and its card buttons
  now read "View Schedule" and "View Patients".
- **Patients and prospective patients:**
  - a new sign-in page with a back arrow, "Forgot password" and "Get started" for the questionnaire
  - a full password-reset flow by email
  - a Title question in the questionnaire
  - search and a bold "You:" in My Messages
  - new messages can go only to the assigned or previously assigned clinicians
  - toasts instead of message strips, on every page

---

## Patient web app

### Patient (signed in)

#### Home
- **Changed** (indirect, no template change) When a clinician renews, changes or creates a prescription from the patient record, Home updates to match: the "Authorised until …" pill, "Your medicine" and "Authorisation expires" show the new prescription.
  Evidence: `care/prescriptions.py:154-181`, `care/prescriptions.py:184-225`; displayed by the unchanged `templates/portal/patient_dashboard.html:20-36`.
- **Added** (indirect) New entries can appear under "See everything that's happened" (care updates): "Treatment authorisation renewed" or "Treatment authorised" ("Your doctor recorded a treatment decision. Open your treatment page to see its details."), and "Laboratory request created" when a clinician sends a PathCare blood-test form.
  Evidence: `care/prescriptions.py:179`, `care/prescriptions.py:224`, `care/clinical.py:216` (called from the new `care/pathcare.py:198-222`).

#### My Treatment (→ treatment page, Blood tests)
- **Changed** (indirect) After a renewal or change, the "Treatment authorisations" history shows the old prescription as "Cancelled · not valid for dispensing" and the new one as "Currently authorised". A changed prescription's dose reads like "0.25 mg weekly" (strength plus how often).
  Evidence: `care/prescriptions.py:125-150` (supersede), `care/prescriptions.py:48-51` (`dose_label`), `care/models.py` (new `MedicationProduct.dose_frequency`, `TreatmentAuthorization.replaces/change_reason/log_text`); displayed by the unchanged `templates/portal/treatment_patient.html:155-161`. The doctor's reason for a change is not shown to the patient.
- **Changed** (indirect) When the medicine or dose is changed, deliveries not yet sent switch to "Held" in the patient's Deliveries table until dispensing is reviewed. A renewal at the same dose leaves deliveries alone.
  Evidence: `care/prescriptions.py:125-150`; shown in `templates/portal/treatment_patient.html:197`. The hold reason itself ("Prescription changed. Dispensing review is required before dispatch.") is only shown to staff.
- **Added** (indirect) A PathCare blood-test request from a clinician appears in My Treatment → Blood tests as a new request (named after the tests requested, status Requested), and the patient can upload the report there as before.
  Evidence: `care/pathcare.py:208-209` (calls existing `create_lab_request`), `portal/clinical_views.py:286-310` (unchanged patient list). The filled PathCare PDF itself is **not** viewable in the app; it is only emailed (see "Emails" below).

#### My Appointments
- **Changed** On an upcoming or cancelled appointment with no existing conversation, the "Message <clinician>" button now uses the appointment's clinician only if that clinician is (or was) assigned to the patient. Otherwise it names and goes to the patient's assigned clinician. If the patient has no clinician to message, the "Contact care team" link shows instead.
  Evidence: `portal/patient_views.py:167-188`; `templates/portal/patient_appointments.html:135`; tests in `portal/test_private_conversations.py` ("Message Cara Current").
- **Changed** (indirect) Times offered when a patient books a review or follow-up now follow the clinician's new "special days" (different hours on one date) and whole days off set in the console.
  Evidence: `care/availability.py:353-410` (`open_slots_for_day` reads `DoctorSpecialDay`), used by `care/patient_care.py:74`.

#### My Messages
- **Added** A search box ("Search your messages", magnifier icon, × to clear) at the right of the toolbar. The list filters as you type (Enter works without JavaScript). It matches the subject, message text, who sent a message, and the names of clinicians in the conversation, and every word typed must match.
  Evidence: `templates/portal/patient_messages.html:25`, `templates/portal/includes/conversation_search.html` (new), `static/js/conversation_search.js` (new), `static/css/conversation_search.css:19` (right-aligned), `portal/patient_views.py:239-242`, `portal/inbox.py:94-114`. Search is capped at 100 characters and 8 words. The patient's own name and record number also match, so searching your own name returns every conversation.
- **Added** While searching, the count reads "N conversation(s) matching "…"", and an empty result says "No conversations match "…"." Searching keeps the open conversation open and highlighted, never marks anything read, and carries the search into conversation links and Previous/Next paging.
  Evidence: `templates/portal/patient_messages.html:22`, `:34`, `:43`, `:47-50`; `static/js/conversation_search.js:31-45`; test `portal/test_patient_web_app.py` (`test_my_messages_search_…`).
- **Changed** A conversation's preview now starts with a bold "**You:**" when your message was the last one. Your own name no longer appears in the "latest from" line above it.
  Evidence: `templates/portal/patient_messages.html:38`, `portal/patient_views.py:249-252`.
- **Removed** The amber "Waiting for a reply" tag on conversations where you sent the last message.
  Evidence: `templates/portal/patient_messages.html:38` (old line removed), `static/css/patient_messages.css` (`.patient-inbox-pill--waiting` deleted).
- **Changed** Unread conversations now show a red number badge at the right of the subject (1–9, then "9+") instead of a small red dot. It matches the badge on My Messages in the menu.
  Evidence: `templates/portal/patient_messages.html:35`, `static/css/patient_messages.css:18-20`; test `portal/test_private_conversations.py` (`test_each_conversation_shows_its_unread_count_as_a_badge`).
- **Changed** In "Start a new message", the To list now holds only the assigned clinician (first, marked "(your clinician)") and clinicians assigned to the patient before. A clinician the patient is merely booked with is no longer offered. A patient never assigned a clinician sees "You can message a clinician once <practice> assigns one to you."
  Evidence: `care/messaging.py:75-81` (`recipient_choices`), `care/forms.py:236-249`; `templates/portal/patient_messages.html` (unchanged "has_recipients" message); tests in `portal/test_private_conversations.py`.

#### My Medications
- No changes found in the catalogue, basket or orders pages.
  Evidence: no diff in pharmacy views/templates. Only the shared toasts (below) apply.

#### Account menu (→ Account settings, Privacy and preferences, My data requests, Change password, Sign out)
- **Changed** (not visible) Nothing changes in the patient's account menu items or the Account settings page. The patient's new **Title** is not shown or editable anywhere in the patient app.
  Evidence: `templates/includes/account_menu.html:14-17` (patient links unchanged), `templates/portal/patient_account.html` (no diff). Title is set in the questionnaire (below) or by staff on the record.
- **Kept in backend only** New activity labels ("Account password reset", "Signed out of other devices", "Staff details updated") were added. A patient's password reset is logged without a patient link, so it does **not** appear in "Your recent account activity" on Privacy and preferences.
  Evidence: `portal/privacy_views.py:45-47`, `portal/privacy_views.py:100-101` (filters by patient), `accounts/views.py:300-309`.

#### Emails a patient receives
- **Added** An email "Your blood test request from <practice>" with the filled PathCare request form attached as a PDF. It says "<Dr Surname> has requested blood tests for you … Take the form, printed or on your phone, to any PathCare collection point." Staff can resend it.
  Evidence: `care/pathcare.py:247-265`, `care/tasks.py` (new, retries 3 times), `care/pathcare.py:225-239` (resend). It goes to the patient's sign-in email, or else the questionnaire (lead) email. The form prints the patient's title, name, ID/passport, and the delivery address from their latest supply request (`care/pathcare.py:87-122`).
- **Added** A password reset email: "Reset your Meridian Health password", with a one-hour, single-use link (see Forgot password below).
  Evidence: `templates/accounts/password_reset_email.txt`, `templates/accounts/password_reset_subject.txt` (new).

### Prospective patient / public pages

#### Sign in (shared with staff)
- **Changed** The page has no header bar. The Meridian logo sits centred above a single sign-in card and links to the landing page.
  Evidence: `templates/accounts/auth_base.html:7`, `:24-28` (new), `static/css/auth.css:2-6` (new), `templates/base.html:23` and `:65` (new `site_header` block).
- **Removed** (hidden, reversible) The green intro panel ("Care that stays connected." with the "Secure access…" line).
  Evidence: `templates/accounts/auth_base.html:11-22` (commented out), `static/css/auth.css:7-21` (commented out).
- **Added** A round back-arrow button ("Back to home") beside the "Secure sign in" label, going to the landing page.
  Evidence: `templates/accounts/login.html:6`, `static/css/base.css:37-39`, `static/css/auth.css:56-57`.
- **Added** A full-width secondary "Forgot password" button under "Sign in".
  Evidence: `templates/accounts/login.html:33`, `static/css/auth.css:48`.
- **Added** At the bottom of the card, a "Not a patient yet? Start with the questionnaire below." line in small text, with a full-width secondary "Get started" button below it that opens the screening questionnaire. Staff see it too.
  Evidence: `templates/accounts/login.html:40-45`, `static/css/auth.css:59-63`.
- **Changed** The email field placeholder now reads "Enter your email" instead of "you@example.com".
  Evidence: `accounts/forms.py:28`.

#### Forgot password / password reset (new, shared with staff)
- **Added** A "Forgot password" page with an email field ("Email address", "Enter your email"), an "Email me a reset link" button and a "Back to sign in" button. Whether or not the email has an account, the reply is the same.
  Evidence: `templates/accounts/password_reset.html` (new), `accounts/views.py:261-284`, `accounts/forms.py:91-98`, `accounts/urls.py:22`.
- **Added** If email isn't set up on the server, the page instead says "Password reset by email isn't available yet. Your practice administrator or Meridian support can help…" and offers only "Back to sign in".
  Evidence: `templates/accounts/password_reset.html` (else branch), `config/settings.py:195` (`PASSWORD_RESET_EMAIL_ENABLED`).
- **Added** A "Check your email" page after requesting: the link works once for one hour, check spam, plus "Back to sign in".
  Evidence: `templates/accounts/password_reset_done.html` (new), `accounts/urls.py:23`.
- **Added** A "Choose a new password" page from the emailed link ("This signs you out everywhere else you're signed in."), with Django's two new-password fields, the password rules under the first, and a "Save new password" button. An expired, used or tampered link shows "This link has expired" with "Request a new link" and "Back to sign in".
  Evidence: `templates/accounts/password_reset_confirm.html` (new), `accounts/views.py:292-309`, `config/settings.py:197` (1 hour).
- **Added** A "Password updated" page ("Your new password is saved. Sign in with it to continue.") with a "Sign in" button.
  Evidence: `templates/accounts/password_reset_complete.html` (new), `accounts/urls.py:25`.
- **Added** (behind the scenes) After 5 requests for one email, or 20 from one network address, within an hour, no more emails are sent, but the page still says "Check your email".
  Evidence: `accounts/views.py:245-258`, `:281-284`.

#### Screening questionnaire
- **Added** A round back-arrow button ("Back to home") beside "Step 1 of 3 · Screening · Stage 1 of 2", going to the landing page.
  Evidence: `templates/portal/questionnaire.html:10`, `static/css/questionnaire.css:5-6`.
- **Added** An optional **Title** dropdown in "Who you are" ("Choose a title", Mr, Mrs, Ms, Miss, Dr, Prof). It sits on its own row after First name / Last name, half width (full width on phones). The field order is now: First name + Last name, Title, Email address + Mobile, ID/Passport number.
  Evidence: `templates/portal/questionnaire.html:34-38`, `care/forms.py:340-341`, `static/css/questionnaire.css:26`, `:125`.
- **Changed** (indirect) The chosen title is saved with the answers and pre-filled if the person comes back to the questionnaire. It is copied to their patient record when they book, then printed on PathCare forms and shown to staff (lead detail, patient record).
  Evidence: `care/forms.py:401`, `portal/intake_views.py:91` (pre-fill), `care/checkout.py:129-132`, `practices/models.py` (new `Patient.title`), `portal/lead_views.py:81` and `:92-93` (staff).

#### Questionnaire result and checkout
- No visible change; only the stylesheet version string changed.
  Evidence: `templates/portal/questionnaire_result.html:5`, `templates/portal/questionnaire_checkout.html:5`.
- **Changed** (indirect) Consultation times offered at checkout follow clinicians' new special days and whole days off.
  Evidence: `care/checkout.py:94` and `:183` call `open_slots_for_day` (`care/availability.py:353-410`).

#### Landing page
- No changes. The sign-in, password reset and questionnaire back buttons now point to it.
  Evidence: `templates/portal/landing.html` (no diff).

### All users (app-wide)

#### Confirmation and error messages (every page)
- **Changed** Messages after an action (e.g. "Your message has been sent to …", "Your preferences have been saved") are now toasts fixed at the **top right**, above the header, instead of a box at the top of the page content. Each toast has a × close button. Confirmations fade out after 3 seconds, and hovering or focusing one pauses the fade. Errors stay until closed. Patient pages keep the patient colours.
  Evidence: `templates/base.html:66-72`, `static/css/base.css:40-50`, `static/js/toasts.js` (new), `static/css/patient_theme.css:104-105` (unchanged colours). With reduced motion there is no animation, but confirmations still disappear after 3 seconds.

---

## Practice console

Who can do what on a patient record (used throughout):
- **Clinical access** = any clinician (Doctor or Dietitian) **or** a Super admin. Evidence: `practices/models.py:130`.
- **Prescriber** = a clinician whose type is Doctor, whatever their role, so a Super admin who is a doctor counts and a
  dietitian does not. Only prescribers can renew, change or create prescriptions and request blood tests. Evidence: `practices/models.py:121`, `portal/record_overview.py:141-180`.
- **Practice admin** has no clinical access, because the database does not allow a practice admin to be a clinician. Evidence: `practices/models.py:88-91`.

### All staff roles (Doctor / Clinician, Practice admin, Super admin)

#### App-wide: messages after you do something (toasts)
- **Changed** Confirmation and error messages (for example "Day off removed…", "Your details have been updated.") no longer appear as a strip at the top of the page. They now pop up as small cards in the top-right corner, above the header. Confirmations fade out after 3 seconds (the fade pauses while you hover over one), errors stay until you close them, and every card has a × button.
  Evidence: `templates/base.html:66-72`, `static/css/base.css:40-50`, `static/js/toasts.js` (new). This also applies to the patient web app, because every page uses `base.html`.

#### Menu (desktop sidebar and mobile bottom bar)
- **Added** The menu entry for Patient Records & Messages now shows a red count of unread messages: unread patient messages plus unread team messages. Above 9 it shows "9+". On the mobile bar, for admins, the count sits on the Care section and on Care's options page.
  Evidence: `templates/portal/includes/nav_badge.html` (new), `portal/navigation.py:28-46,96-110`, `portal/templatetags/workspace_ui.py:9-42`, `portal/menu_views.py:22-24`, `templates/portal/staff_menu_section.html:6`, `templates/portal/includes/staff_mobile_navigation.html:8`, `static/css/console.css:195-202,1213-1216`.
- **Changed** On desktop the sidebar is wider: 17rem instead of 15rem. This lets "Patient Records & Messages" and its count fit on one line.
  Evidence: `static/css/workspace_polish.css:64-66`.
- **Changed** Two menu items were renamed for every role: "Patients" is now **Patient Records & Messages**, and "Schedule and availability" is now **Calendar & Bookings**. The separate **Messages** item was removed; messages are now tabs inside Patient Records & Messages.
  Evidence: `portal/navigation.py:20-31,61-68`. Role-specific menu changes are listed under each role below.

#### Overview (dashboard)
- **Removed** The welcome header no longer shows the role tag (for example "Clinician") or the "View patients" and "View schedule" buttons.
  Evidence: `templates/portal/desktop_dashboard.html:27-32` (the deleted `console-actions` block).
- **Changed** Two card buttons were renamed: on Upcoming appointments, "Full schedule" is now **View Schedule**; on Patients, "Patient directory" is now **View Patients**.
  Evidence: `templates/portal/desktop_dashboard.html:58,122`.
- **Changed** Upcoming appointments and Open tasks now take exactly half the width each and are the same height. Before, appointments was wider and each card was only as tall as its content. Below them come the Patients table and then Recent conversations, the same order as before. On narrow screens the two cards stack.
  Evidence: `static/css/workspace_polish.css:51-53` (before: `console.css:455-459`, 1.3fr / 0.7fr, aligned to the top).
- **Changed** The "Practice privacy" note at the bottom now shows only when the app runs more than one practice. With single-practice mode on, nobody sees it.
  Evidence: `templates/portal/desktop_dashboard.html:188-192`.

#### Patient Records & Messages (menu entry and page header)
- **Changed** The separate "Patients" and "Messages" menu items are now one item, **Patient Records & Messages**, which opens the All patients tab. It stays highlighted on any of its tabs and on an open patient record. Evidence: `portal/navigation.py:20-31`, `portal/navigation.py:62-67`.
- **Removed** The old "Patients | Team" message tabs, which had teal count pills. Evidence: `templates/portal/includes/messages_tabs.html` (deleted).
- **Changed** Breadcrumbs on every page in this area now start with "Patient Records & Messages" instead of "Patients" or "Messages". This covers the patient record, its tabs, the History page, the Manage conversation page and Team messages. Evidence: `templates/portal/patient_workspace_base.html:5`, `templates/portal/patient_list.html:9`, `templates/portal/staff_inbox.html:27`, `templates/portal/team_inbox.html:27`, `templates/portal/message_management.html:3`, `templates/portal/patient_detail.html:27`, `portal/navigation.py:23-25`.

#### Patient Records & Messages → All patients
- **Changed** The page heading "Patients" is replaced by the shared header and tabs. The "N active patient records in <practice>" line now sits under the tabs. The browser tab title is "All patients | Patient Records & Messages | <practice>". Evidence: `templates/portal/patient_list.html:6-13`.
- No other change for admins: the "Find a patient" search, Clinician filter and Patient directory table are as before. The Doctor role's version differs (see below).

#### Patient Records & Messages → Patient messages
- **Changed** Patient messages uses a new two-column layout shared with Team messages and the record's Messages tab. The conversation list is on the left. The right-hand pane shows "Choose a conversation" (with a **New patient message** button) until you pick a conversation. Evidence: `templates/portal/staff_inbox.html:43-88`, `static/css/conversations.css`.
- **Changed** Each row in the list now shows:
  - the subject, in bold with a red count when unread
  - "With <patient>[, other clinicians in it]"
  - the latest message, starting with a bold "You:" if you sent it, plus a "Closed" pill on closed conversations
  - the time if the message was sent today, otherwise the date (with the year if it isn't this year), and the full date and time on hover

  Evidence: `templates/portal/includes/conversation_list_item.html:8-24`, `portal/inbox.py:155-165`, `portal/templatetags/workspace_ui.py:87-95`.
- **Removed** Several items from the old list are gone:
  - the three summary cards (Threads in view, Awaiting a reply, Answered)
  - the patient-initials avatar, "x ago" times and "Awaiting reply / Answered / No messages yet" labels on each row
  - the per-row "Open record" link and the conversation count above the list
  - the page intro and the footnote about private conversations

  Evidence: `git diff HEAD -- templates/portal/staff_inbox.html`, `static/css/console.css` (removed `.inbox-metrics`, `.inbox-thread*`).
- **Kept** The Everything / Awaiting a reply / Answered filters stay, now on a toolbar beside the search, and keep any search you have typed. Pagination stays at 20 conversations a page. Evidence: `templates/portal/staff_inbox.html:31-56`.
- **Added** A search box ("Search messages, patients, clinicians") filters the list as you type, with a clear (×) button. Every word must match one of: subject, message text, sender, patient name or record number, or a clinician in the conversation. Only conversations you are in are searched. An open conversation stays open while you search. Evidence: `templates/portal/includes/conversation_search.html`, `static/js/conversation_search.js`, `portal/inbox.py:99-114`.
- **Added** Each row with a patient message has a round envelope button at its top right: **Mark as read** (open envelope) when unread, **Mark as unread** (envelope with red dot) otherwise. Mark as read marks all the patient's messages read. Mark as unread makes the patient's latest message unread. You return to the list with nothing open and a "Conversation marked as read/unread." toast. Evidence: `templates/portal/includes/conversation_list_item.html:17-23`, `templates/portal/includes/read_state_icon.html`, `portal/views.py:742-770`, `care/messaging.py:25-61`.
- **Changed** An open conversation now has the following layout (it still opens in place, as it did in the last commit):
  - a header with the subject and the links **Open <first name>'s record**, **Mark as unread** (only if the patient has written) and **Add a clinician or close**
  - a "With <patient>, <clinicians>" line
  - chat bubbles: the patient on the left in blue, staff on the right in white, each with "name · 5 Oct, 14:30"
  - a one-line reply box ("Reply to <first name>…") with **Send reply**

  Enter sends and Shift+Enter starts a new line. The box grows as you type, the newest message is scrolled into view and the reply box gets focus. Evidence: `templates/portal/includes/patient_conversation.html:6-46`, `static/js/conversations.js`, `static/css/conversations.css`.
- **Removed** Several items in the open conversation are gone or reworded:
  - the "In this conversation: you, …" line and the "X added Y" history
  - the "Open record" button, replaced by the "Open <name>'s record" link
  - the "Add a clinician / close" button, now the link "Add a clinician or close"
  - the reply-form footnote "Replies are timestamped and attributed in the patient record."

  A closed conversation now reads "This conversation is closed. Start a new one." The "Start a new one" link opens a new conversation on the patient's record. Evidence: `git diff HEAD -- templates/portal/staff_inbox.html`, `templates/portal/includes/patient_conversation.html:44-46`, `portal/inbox.py:196`.
- **Changed** After you send a reply, or answer or withdraw an appointment suggestion, you come back to Patient messages with the same conversation, filter and search. Errors show inside the open conversation. Evidence: `portal/views.py:556-584`, `portal/appointment_views.py:49-83`, `portal/appointment_views.py:155-158`.
- **Added** **New patient message** opens a compose pane from the header button or the empty state. It has the heading "New patient message", the note "Only you and the patient can read it. You can add a clinician once it has started." and three fields:
  - **To**: "Choose a patient", listed as "Name · record number"
  - **Subject (optional)**: its placeholder shows the default subject, "Message from Dr <surname>" for clinicians or "Message from the practice" for others
  - **Message**

  There are **Cancel** and **Send** buttons. Sending opens the new conversation with the toast "Secure conversation started. Only you and the patient can read it." Only active patients with a login are listed; if there are none, a note explains why. Evidence: `templates/portal/staff_inbox.html:62-79`, `portal/inbox.py:67-90`, `portal/views.py:705-725`, `portal/record_messages.py:20-23`.
- **Changed** Unread now means "not yet read by the practice". A new field records staff reading separately from the patient's own read state. Opening a conversation marks the patient's messages read for everyone in it. The tab and sidebar counts use this new field. Evidence: `care/models.py:940-944`, `care/messaging.py:25-41`, `portal/inbox.py:20-26`, `portal/views.py:116`.

#### Patient Records & Messages → Team messages
- **Changed** Team messages uses the same two-column layout as Patient messages. The header has a **New team message** button at the top right. The empty state now reads "Choose a team conversation" and also has a **New team message** button. Evidence: `templates/portal/team_inbox.html:26-126`.
- **Changed** Each row shows:
  - the subject, in bold with a red count when unread
  - "With <colleagues other than you> · About <patient>"
  - the latest message, with a bold "You:" if you sent it
  - the time if sent today, otherwise the date

  Removed from rows: the member-count avatar, the "you, X, Y" member list, the "About" line and the "x ago" times. Evidence: `templates/portal/includes/conversation_list_item.html`, `portal/team_views.py:90-114`.
- **Added** A search box ("Search messages, colleagues, patients") filters the list as you type. It searches subject, message text, sender, colleagues in the conversation, and the patient's name or record number. Evidence: `templates/portal/team_inbox.html:31-35`, `portal/team_views.py:73-87`.
- **Added** You can now mark team conversations read or unread, which you couldn't before. Rows have the round envelope button, and an open conversation has a **Mark as unread** link (only when someone else has written in it). Read state stays per person, and Mark as unread goes back to just before the latest message from someone else. Evidence: `templates/portal/team_inbox.html:79-84`, `portal/team_views.py:213-225`, `care/team_messaging.py:118-126`, `portal/urls.py:34`.
- **Changed** An open team conversation has the following layout:
  - a header with the subject, **Open <first name>'s record** (when it is about a patient; this replaces the "About <patient link>" line) and **Mark as unread**
  - a people line: "With A, B · About <patient> · X added Y 5 Oct"
  - **Add a colleague** as an expandable link instead of a header button
  - chat bubbles: yours on the right ("You"), others on the left
  - a one-line "Reply…" box with **Send reply** (Enter sends), and the footnote "Only the people in this conversation see replies. The patient never does."

  Evidence: `templates/portal/team_inbox.html:74-117`.
- **Changed** The New team message form has the same fields as before: To (checkbox list of colleagues with their role), About a patient (optional), Subject and Message. It now sits in the right-hand pane with Cancel and Send buttons. Evidence: `templates/portal/team_inbox.html:55-73`, `portal/team_views.py:35-46`.
- **Removed** The "Messages" page heading and its intro text "Conversations with colleagues at …". Evidence: `git diff HEAD -- templates/portal/team_inbox.html`.

#### Patient Records & Messages → Patient record (heading and details row)
- **Changed** The record's breadcrumb is now "Patient Records & Messages › <patient> [› tab]". Evidence: `templates/portal/patient_workspace_base.html:5`.
- **Added** A pen next to the patient's **Title** (Mr, Mrs, Ms, Miss, Dr, Prof or Not recorded) lets the care team set it. The help text says "Printed on PathCare blood test forms.", and saving shows the toast "Title saved." Doctors, practice admins and Super admins can all edit it; where it appears differs by role (see below). The title can also come from the new screening-questionnaire question. Evidence: `templates/portal/includes/patient_title_editor.html`, `portal/prescription_views.py:147-182`, `portal/patient_workspace.py:163-165`, `practices/models.py:154-162`, `care/checkout.py:130`.
- **Changed** The Assigned clinician pen editor works as before. It was moved into a shared include so it can appear in the new details row. Evidence: `templates/portal/includes/patient_assigned_clinician.html`.

#### Patient Records & Messages → Patient record → Messages
- **Changed** The record's Messages tab now uses the same list-and-pane layout under a "Messages" card. The list on the left holds your conversations with this patient. Rows show "With <other clinicians>" only when other clinicians are in it, because the patient is implied. Nothing opens until you choose a conversation ("Choose a conversation" with a **Start a new conversation** button). Evidence: `templates/portal/includes/record_messages.html:3-46`, `portal/record_messages.py:26-72`, `portal/record_messages.py:75-78`.
- **Removed** Several items from the old Messages tab are gone:
  - the notice "Only conversations you are part of are shown…"
  - the "Start a new conversation" form with Subject and Message
  - list pagination (all your conversations now scroll in one list)
  - "Older messages / Newer messages" paging inside a conversation (the whole conversation now scrolls in a box)
  - the "Secure conversation · With …" sub-line and "Manage conversation" button, replaced by the "Add a clinician or close" link

  Evidence: `git diff HEAD -- templates/portal/patient_workspace_messages.html`, `portal/patient_workspace.py:117-134`.
- **Added** The Messages card header has a search box ("Search these conversations") and a **Start a new conversation** button. The search refreshes only the list, using a separate address (`portal:record-conversation-search`), so typing doesn't open anything, mark anything read, or log a record view for each letter. Evidence: `templates/portal/includes/record_messages.html:7-11`, `templates/portal/includes/record_conversation_list.html`, `portal/patient_workspace.py:240-253`, `portal/urls.py:67`.
- **Added** Starting a conversation from the record uses an inline box: "Write to <first name> to start a conversation…" with **Send**. There is no subject field; the subject is set automatically to "Message from Dr <surname>" (clinicians) or "Message from the practice" (others). Before any conversation exists, the card shows only "No messages with <first name> yet." and the box. If the patient has no login, it shows "<first name> doesn't have a login yet, so they can't read messages." instead. Errors keep what you typed. Evidence: `templates/portal/includes/record_messages.html:22-36`, `portal/views.py:673-702`, `portal/record_messages.py:44-50`.
- **Added** An open conversation looks like the Patient messages one (Mark as unread, Add a clinician or close, bubbles, Enter-to-send reply box) without the "Open record" link. Opening it marks the patient's messages read and records that the conversation was viewed. Replies and appointment-suggestion answers return to the record's Messages tab. Evidence: `templates/portal/includes/patient_conversation.html`, `portal/record_messages.py:86-106`, `portal/views.py:573`, `portal/appointment_views.py:63-65`.
- **Added** The envelope mark read/unread buttons appear on rows here too. After using one you return to this tab with nothing open, so the conversation stays unread. Evidence: `templates/portal/includes/record_conversation_list.html:8`, `portal/views.py:760-764`.
- **Kept** "Team conversations about this patient" stays below the messages, with **Message a colleague about this patient**. Its links now open Team messages at the top of the page instead of jumping to an anchor. Evidence: `templates/portal/patient_workspace_messages.html:2-7`.

#### Patient Records & Messages → Patient record → History
- **Removed** For staff with clinical access, the History page no longer has the "Current consent · <practice>" card in its sidebar. Evidence: `git diff HEAD -- templates/portal/record_detail.html`.
- **Changed** The "Back to patients" link in "More for this patient" now reads "Back to Patient Records & Messages". Evidence: `templates/portal/record_detail.html:71`.
- **Kept** "Export clinical record" (JSON) stays on the History page. Evidence: `templates/portal/record_detail.html:7`, `portal/record_views.py:369`.

#### Patient Records & Messages → Patient record → Appointments, Consultations, Notes, Weight, Tasks, Payments
- No visible change to these tabs in this diff. The Weight tab keeps its existing chart; the new doctor chart is on Overview only.

#### Patient Records & Messages → Patient record → Blood tests, Treatment (indirect changes)
- **Changed** A blood test requested from the Overview card now also appears as a row on the Blood tests tab, with the typed tests as the "Panel". The tab's own **Request blood tests** button still opens the older request page. Evidence: `care/pathcare.py:209-210`, `templates/portal/patient_workspace.html:44`.
- **Changed** Every renewal or change from the Overview creates a new authorisation on the Treatment tab, and the one it replaces now shows status "Cancelled". The **Authorise treatment** button and its separate page are unchanged. Evidence: `care/prescriptions.py:125-128`, `care/prescriptions.py:166-175`, `templates/portal/patient_workspace.html:45`.

#### Calendar & Bookings (calendar)
- **Changed** The page heading is now **Calendar & Bookings** (it was "Schedule and availability"). The line "Times shown in South Africa time (SAST)." under the heading was removed.
  Evidence: `portal/staff_views.py:128`, `templates/portal/staff_schedule.html:10-20`.
- **Added** Two tabs under the heading: **Calendar & Bookings** (the default) and **Working Pattern & Availability**. Working hours, special days and days off used to sit in a side column beside the appointments; they now have their own tab. The chosen tab is kept when you move between dates, save a form, or click "Next appointment day" or "Today".
  Evidence: `templates/portal/staff_schedule.html:21-24,84-86`, `portal/schedule_calendar.py:16-30`, `portal/staff_views.py:136,242-245`.
- **Changed** The "Table / Calendar" switch in the page header was replaced by a **Day / Month** switch inside the bookings card. The card header now has ‹ › arrows that step one day (Day view) or one month (Month view), a title ("Booked · Monday 5 October" or "October 2026"), a count ("3 booked", "Nothing booked" or "12 booked this month"), and a **Today** link that appears only when you are not on today. The old "Previous / Today / Next" buttons and the "Month view" label are gone. There is no Week view.
  Evidence: `templates/portal/staff_schedule.html:88-109`, `portal/staff_views.py:167-173`, `portal/schedule_calendar.py:85`.
- **Changed** Month view was rebuilt. Every booking now appears in its day as a clickable chip, such as "12:00 Nadia Mokoena", that opens the appointment details. Before, a day showed at most 3 previews with a status and "+N more". Cancelled bookings are left out of the chips and the counts. Each week's row grows to fit its busiest day. Clicking the date opens that day in Day view. Days outside the month are blank, and today has an outline. On a phone the chips show only the start time.
  Evidence: `templates/portal/staff_schedule.html:111-142`, `portal/schedule_calendar.py:47-87`, `static/css/schedule_availability.css:71-155`.
- **Added** When a single doctor's diary is shown, Month view colours each day by its state. A day off is red with a "Day off" tag, a special day shows its hours as a blue tag (for example "08:00–10:00"), and a non-working day is greyed out. A key below the grid explains the colours: Working day / Special hours / Day off / Not working.
  Evidence: `templates/portal/staff_schedule.html:121-140`, `care/availability.py:268-301` (`diary_day_states`), `portal/staff_views.py:160-166`.
- **Removed** Month view no longer shows the selected day's appointment table under the calendar. You click a day to open it in Day view instead.
  Evidence: `templates/portal/staff_schedule.html:111-188` (before: the calendar and the table rendered together).
- **Changed** The four day-count cards (Booked, Completed, Cancelled, No show) now show only in Day view on the Calendar & Bookings tab. Before, they showed on every view.
  Evidence: `templates/portal/staff_schedule.html:28-35`.
- **Changed** Day view table: the separate Duration column was merged into Type ("Consultation · 30 min"). A new **Room** column shows a **Join** button when the video room is open, and "Not open" or "—" otherwise. The "Join video consultation" link was removed from the row's actions. A coloured pill above the table shows the day's state: "Day off", "Special hours 09:00 – 13:00" or "Not a working day". The empty message now reads "Nothing booked on this day." or "Not a working day." (it was "There are no appointments for this date.").
  Evidence: `templates/portal/staff_schedule.html:144-185`.
- **Added** In Day view, a booking that now falls on the doctor's day off shows a red **Doctor unavailable** pill. One outside that day's special hours shows an amber **Outside special hours** pill. Its Room column shows "Not open".
  Evidence: `templates/portal/staff_schedule.html:171-172`, `care/availability.py:474-480` (`attention_kind`).
- **Added** A red **"Action needed"** banner appears in Day view when the viewed day is a day off or special day and bookings clash with it, for example "2 appointments sitting on a day this doctor is now unavailable". People allowed to change that diary also get an **Undo the change** button. It removes that date's days off and special day and shows the toast "Undone. The diary stands as it was before the change." The banner text differs for doctors and admins (see each role).
  Evidence: `templates/portal/staff_schedule.html:67-82`, `portal/availability_views.py:101-111,270-283`, `care/availability.py:200-215`, `portal/urls.py:52`.
- **Changed** The "Appointments needing attention" card now gives a specific reason for each booking: "On a day off", "Outside special hours" or "Outside current working hours". Before, every booking said "Outside current working hours or during time off". The card's intro and empty text now mention special hours and days off.
  Evidence: `portal/availability_views.py:31-35,94-96`, `templates/portal/staff_schedule.html:217-240`.
- **Changed** For a doctor's own bookings, the "Offer a new time" / "Discuss rebooking" link now opens the patient record's **Messages** tab at that conversation. It used to open the separate Messages inbox.
  Evidence: `portal/staff_views.py:227-232`.

#### Calendar & Bookings → Working Pattern & Availability
- **Changed** The tab is laid out in two columns: Working pattern on the left; Special days, then Days off, on the right. The Working pattern card itself and its description are unchanged.
  Evidence: `templates/portal/includes/schedule_availability.html:1-63`, `static/css/schedule_availability.css:132-141`.
- **Added** A new **Special days** card sets different hours on one date, replacing the working pattern for that date only. It can also make a normally non-working day a working day. The card lists upcoming special days, soonest first, as "Mon 5 Oct 2026 · 09:00 – 13:00", with "· overridden by a day off" where relevant, and a **Remove** button on each. A dashed **+ Add special day** button opens a form with Date, From and To (in 15-minute steps), plus **Set special hours** and **Cancel**. The form opens with the selected calendar date filled in. Empty state: "No special days coming up." Toasts: "Special hours set for Mon 5 Oct 2026. They replace the working pattern that day." and "Special hours removed. The working pattern applies again."
  Evidence: `templates/portal/includes/schedule_availability.html:64-127`, `portal/availability_forms.py:95-110`, `portal/availability_views.py:235-267`, `care/models.py:534-566`, `portal/urls.py:50-51`.
- **Changed** "Time off" is now **Days off**. Days off are whole days: the form asks only **From** and **To** dates, opens from a dashed **+ Add day off** button and has a **Cancel** button. Before, it had a Reason (Leave / Sick leave / Other time off) and date-and-time From / Until fields. The list shows dates only, for example "Mon 5 Oct 2026 – Wed 7 Oct 2026", soonest first, with no reason. Older entries that were part-day show their times. The button is now **Remove** (it was "Cancel time off"). The "Time off recorded here" subheading, the description and the help text under the list were removed. Empty state: "No days off coming up." Toasts: "Marked unavailable. Those days have left the booking calendar; existing appointments have not been moved." and "Day off removed. The working pattern applies again; its history is kept."
  Evidence: `templates/portal/includes/schedule_availability.html:129-186`, `portal/availability_forms.py:74-92`, `portal/availability_views.py:202-232`, `care/availability.py:129-137`.
- **Changed** With "All clinicians" selected, the placeholder card is now titled "Working pattern, days off and special days". It says doctors set their own working pattern, and that practice administrators and Super Admins can add or remove days off and special days for the doctor they choose.
  Evidence: `templates/portal/includes/schedule_availability.html:188-192`.

#### Calendar & Bookings → Book an appointment
- **Changed** The breadcrumb now reads "Calendar & Bookings › Book an appointment".
  Evidence: `templates/portal/appointment_booking.html:3`.
- **Changed** Bookings now respect special days. A time outside a doctor's special hours on that date is refused as unavailable, even if the weekly pattern would allow it. A special day on a usual day off makes that date bookable. Open-time suggestions follow the same rule.
  Evidence: `care/availability.py:326-335` (`ensure_working_time`), `care/availability.py:353-412` (`open_slots_for_day`).

#### Account menu → My profile
- **Changed** My profile for staff was redesigned and is now mostly read-only. It opens with an identity card: initials avatar, full name, email, and chips for the role (for example "Clinician"), the practice and "Active". The **Your details** card shows **Title** (above First name), First name, Last name, Email (your sign-in) and Contact number. A note says these are maintained centrally and asks you to tell the practice if anything is wrong.
  Evidence: `templates/accounts/profile.html:6-39`, `accounts/views.py:48-56,125-140`, `static/css/account_profile.css:33-78`.
- **Removed** Staff can no longer edit their own first and last name. The name form and its "Save profile" button are gone, and a direct POST is refused. Also removed from the staff page: the "Back to workspace" button, the "Account access history" link, the "Account created" / "Last sign-in" facts and the on-page "Sign out" button (Sign out is still in the account menu). Patients' My profile is unchanged.
  Evidence: `accounts/views.py:66-71`, `templates/accounts/profile.html:6-68` (the staff branch) compared with the old template.
- **Added** A **Security** card has two rows. **Password** shows "Last signed in …" and a **Change** button. **Active sessions** has a **Sign out others** button that signs this account out of every other device and keeps this one signed in. Its toasts are "Signed out of every other device. This one stays signed in." or "No other devices were signed in…".
  Evidence: `templates/accounts/profile.html:46-50`, `accounts/views.py:143-165`, `accounts/urls.py:18`.
- **Changed** The "Your practice access" card now shows only when the app runs more than one practice.
  Evidence: `templates/accounts/profile.html:62-68`.

#### Sign in → Forgot password (shared with patients)
- **Added** The sign-in page has a **Forgot password** link to a new emailed password-reset flow. Staff use it too. The full details are in Patient web app → Prospective patient / public pages.
  Evidence: `templates/accounts/login.html:33`, `accounts/urls.py:22-25`, `accounts/views.py:244-313` (reset views from line 261).

### Doctor / Clinician

#### Menu
- **Changed** Clinicians now get a single flat menu with an icon on each item, like the patient app: **Overview**, **My tasks**, **Patient Records & Messages** (with the red unread count), **Calendar & Bookings**. The collapsible Care / Operations / Insights / Settings sections are gone. The mobile bottom bar shows the same four items with short labels: Overview, Tasks, Records, Calendar. Before, it showed the sections Care, Operations, Insights and Settings.
  Evidence: `portal/navigation.py:16-46,49-55`, `templates/portal/includes/staff_navigation.html:2-6`, `templates/portal/includes/staff_mobile_navigation.html:4-5`, `templates/portal/includes/nav_icon.html` (new tasks / records / schedule icons), `static/css/console.css:199-202`, `static/css/workspace_polish.css:77-78`.
- **Removed** Operations (Subscriptions, Supply requests, Weekly shipping list, Dispatch history, Batches and stock, Catalogue) is gone from the clinician's menu, and those pages now refuse clinicians. Before, clinicians could open them read-only.
  Evidence: `portal/navigation.py:70-77`, `portal/operations_views.py:64`, `portal/treatment_views.py:152`, `portal/views.py:67-76`.
- **Removed** Insights (Metrics and cohorts, Activity statements) is gone, and those pages refuse clinicians. Before, a doctor could see Metrics and their own activity statements read-only.
  Evidence: `portal/navigation.py:83-86`, `portal/reporting_views.py:75`.
- **Removed** Settings → Review rules is gone, and the page now refuses clinicians. Before, doctors had a read-only view.
  Evidence: `portal/navigation.py:87-93`, `portal/review_views.py:17`.
- **Removed** Care → Consultations, Blood tests, Authorisations and Compounding records no longer appear in the clinician's menu. The pages themselves still open for clinicians by direct link; their access rule is unchanged.
  Evidence: `portal/navigation.py:34-46` (flat menu leaves them out), `portal/clinical_views.py:32,38` (unchanged clinical-access check).
- **Removed** The separate "Messages" menu item was removed; it is merged into Patient Records & Messages.
  Evidence: `portal/navigation.py:20-31`.

#### Overview
- **Removed** The count cards (Patients, Appointments today, Open tasks, Unread messages, Shipments due) no longer show for clinicians, on desktop or mobile.
  Evidence: `templates/portal/desktop_dashboard.html:34-49`, `templates/portal/mobile_dashboard.html:33-48`, `portal/views.py:129-134`.
- **Changed** The Patients table on a clinician's Overview now lists only the patients assigned to them. Before, it listed any active patients in the practice.
  Evidence: `portal/views.py:119-122,141`.

#### My tasks
- **Removed** The Open / Overdue / Completed count cards no longer show for clinicians.
  Evidence: `templates/portal/staff_tasks.html:19-25`.
- **Changed** The filter block lost its visible "Filter tasks" heading and its "Filter by workflow, task type, patient, or free-form tag." line. A teal filter-funnel icon now sits at the left of the filter row; it is hidden on tablet and phone widths. This applies to every role's task page.
  Evidence: `templates/portal/staff_tasks.html:27-31`, `static/css/console.css:983-986,1224-1225`.

#### Patient Records & Messages → All patients
- **Changed** A clinician (Doctor role) now sees only the patients assigned to them. The intro says "Your patients: the people assigned to you. Search by name or record details.", the **Clinician** filter is gone, and the count counts only their patients. Evidence: `portal/record_views.py:109-113`, `templates/portal/patient_list.html:18`, `templates/portal/patient_list.html:27-31`.

#### Patient Records & Messages → Patient messages
- **Changed** In **New patient message**, a doctor can only choose their own assigned patients who have a login. If there are none, the note adds ", and only your own patients are listed". The default subject is "Message from Dr <surname>". Evidence: `portal/inbox.py:67-73`, `templates/portal/staff_inbox.html:78`.
- **Kept** Appointment suggestions ("Offer a new time") still appear inside an open conversation for clinicians only. Evidence: `portal/record_messages.py:105-106`, `templates/portal/includes/patient_conversation.html:31`.

#### Patient Records & Messages → Patient record → heading and details row
- **Added** On the Overview tab, the record heading shows a prescription status pill:
  - "No prescription" (grey)
  - "Prescription expired <date>" (red)
  - "Prescription paused" (grey)
  - "Prescribed to <date>" (teal), plus "Renewal due" (amber) within 30 days of expiry

  An **Export record** button sits beside the pill. Evidence: `templates/portal/patient_workspace.html:4-12`, `care/prescriptions.py:87-98`.
- **Added** Under the name there is a details row: **Title** (with pen), **Date of birth**, **Location** and **Assigned clinician** (with pen). The record number, email and phone stay in the line under the name. Evidence: `templates/portal/patient_workspace.html:13-20`, `templates/portal/patient_workspace_base.html:8`.

#### Patient Records & Messages → Patient record → Overview
- **Changed** For clinicians the old Overview has been replaced by a clinical Overview: the count cards, the "Patient details" box and the "Choose a patient section above…" note are gone, and so is the Conversations count, because messages live on the Messages tab. The left column has the log and Summary data; the right column has Weight, Current prescription and Request blood tests. It becomes one column on narrower screens. Evidence: `portal/patient_workspace.py:144-160`, `templates/portal/includes/record_overview.html:3-143`, `static/css/record_overview.css:20`, `static/css/record_overview.css:171-172`.
- **Added** The **Log** card has **Clinical** and **Dispensed** chips that switch without reloading, with a count ("N entries" / "N shipments"):
  - **Clinical** lists, newest first, signed consult notes (date, who signed, note text), blood forms ("Blood form" tag, "PathCare form for <tests> sent.", or "Sent automatically" when no doctor requested it) and prescriptions ("Prescription" tag, with the line written when it was made).
  - **Dispensed** lists dispatched and delivered parcels, one line per medicine: "<medicine> <strength> × N pens", a "Month N" tag, and "Batch X, expiry MM/YYYY · courier <tracking>".

  Evidence: `templates/portal/includes/record_overview.html:5-49`, `portal/record_overview.py:31-77`, `static/js/record_overview.js:34-51`.
- **Added** Blood-form entries in the log have extras:
  - **View form**, which opens the stored PDF exactly as emailed in a new tab
  - an email status: "Emailed to <address>", "Email queued", or "Email failed" in red
  - for prescribers, a **Resend to patient** button (or **Resend** after a failure), which shows the toast "Blood test form sent again to <address>."

  Evidence: `templates/portal/includes/record_overview.html:20-32`, `portal/pathcare_views.py:97-124`.
- **Added** **Summary data** card. It shows a "Safety flag" pill if any flag is set and the hint "Click an underlined value to edit". The rows are:
  - Weight start → current → goal (only the goal can be edited, 40–250 kg)
  - BMI start → current
  - Medical history, Surgical history, Chronic medication and Allergies (free text up to 2,000 characters)

  Under a **Safety flags** subheading:
  - History of pancreatitis: No / Yes
  - Medullary thyroid cancer / MEN2: No / Yes / Unsure
  - Pregnant or breastfeeding: No / Yes / Not applicable

  A "Yes" shows as a red pill and "Unsure" (thyroid) as amber. Evidence: `templates/portal/includes/record_overview.html:51-81`, `templates/portal/includes/record_summary_value.html`, `care/clinical_summary.py:22-37`, `care/clinical_summary.py:95-101`.
- **Added** Editing summary data works inline: click the underlined value, then Enter or clicking away saves, picking an answer saves it straight away, and Escape cancels. Without JavaScript there are Save and Cancel buttons. Values start from the screening questionnaire and the patient's own medical profile, and those originals are never changed. A footer reads "Populated from the screening assessment on <date>" (or "No screening assessment on record") · "N edits since". An edit history lists each change as "date · editor · field: ~~old~~ → new". Evidence: `static/js/record_overview.js:147-165`, `portal/prescription_views.py:128-144`, `care/clinical_summary.py:1-7`, `care/clinical_summary.py:153-176`.
- **Added** **Weight** card with a new doctor's chart. Evidence: `templates/portal/includes/record_weight_chart.html`, `portal/record_chart.py`, `portal/record_overview.py:80-94`, `static/js/record_overview.js:116-145`. It shows:
  - the weight line and points
  - a shaded "Expected range, assuming 0.5 to 1 kg a week" (flat for the first 28 days)
  - a goal line and a dotted projection to the goal at 0.7 kg a week, with a "Goal X kg · around <date>" pill
  - bars for each dose shipped, against a right-hand mg scale
  - round consult dots; clicking one opens a "Consult note" pop-up with **Show in log** (scrolls to and highlights the entry) and **Close**
  - a key underneath

  If no goal is set, it says "Set a goal weight in Summary data to see the projection." If no weights are logged, it says "No weights logged".
- **Added** **Current prescription** card. Evidence: `templates/portal/includes/record_overview.html:105-125`, `care/prescriptions.py:77-84`. It shows:
  - Medicine, Dose, Authorised, Expires (amber when due, red with "· expired") and By
  - a note when the prescription is paused
  - "No prescription yet. One is written at the first consult." when there is none
- **Added** For prescribers only, the card has **Renew prescription** (when one exists) and **Change prescription**, or **Create prescription** when there is none. These are available to any doctor at the practice, not only the assigned one. Evidence: `templates/portal/includes/record_overview.html:119-124`, `portal/prescription_views.py:81-125`.
- **Added** **Renew prescription** dialog. Evidence: `templates/portal/includes/record_overview.html:196-223`, `care/prescriptions.py:153-180`, `static/js/record_overview.js:81-89`. It shows:
  - "<patient> · <current medicine and dose>"
  - "Renew for" chips from 1 to 6 months (6 chosen by default)
  - the current expiry, and a new expiry that updates as you pick
  - **Cancel** and **Renew prescription**

  The renewal runs from the later of today and the current expiry. Parcels not yet dispatched move to the renewed prescription. The toast reads "Prescription renewed to <date>. It is written to the clinical log under your name."
- **Added** **Change / Create prescription** dialog. Evidence: `templates/portal/includes/record_overview.html:224-272`, `care/prescriptions.py:58-74`, `care/prescriptions.py:183-225`, `static/js/record_overview.js:91-114`. It shows:
  - a **Medicine** list of the catalogue's main weight-management treatments that need authorisation
  - a **Dose** list that follows the medicine, e.g. "0.5 mg weekly"
  - "Valid for" from 1 to 6 months
  - an optional **Reason** that goes into the clinical log
  - a live "New prescription" preview
  - **Cancel** and **Save prescription**

  Choosing the current dose is refused with "That is the current prescription. Use Renew to extend it." A change replaces the current prescription from today and puts undispatched parcels on hold with "Prescription changed. Dispensing review is required before dispatch." If the catalogue has no main treatment, the dialog says "There are no main treatments in the catalogue to prescribe. Ask a practice administrator to add one."
- **Added** Safety flags in both prescription dialogs: if any flag is set, a red box lists them and a required tick appears: "I have reviewed this and still want to prescribe." Without the tick the save is refused. Evidence: `templates/portal/includes/record_safety_flags.html`, `care/prescriptions.py:107-113`.
- **Added** **Request blood tests** card, for prescribers only. Evidence: `templates/portal/includes/record_overview.html:127-141`, `templates/portal/includes/record_overview.html:159-194`, `static/js/record_overview.js:4-27`, `care/pathcare.py`, `portal/pathcare_views.py`. The card and its preview dialog work like this:
  - The card has a "Tests and relevant clinical data" box (up to 300 characters, e.g. "FBC, ferritin, TSH") and a **Request blood tests** button.
  - The button opens a preview dialog showing the filled PathCare form in a frame. There is a link to open it in a new tab.
  - Beside the form is a list of the values filled in: Referring doctor, PathCare code, Patient ID, Surname, First name(s), Title and Postal address. Missing values show in red as "Missing", and Medical aid no. shows as "Left blank".
  - The dialog ends with **Cancel** and **Email to <patient email>**.
  - Sending creates the blood test request, stores the form, emails it as a PDF attachment, adds it to the clinical log and shows "Blood test form emailed to <address>. It is in the clinical log."
  - If the doctor's PathCare name and code aren't on file, the card says so in red and the button is disabled.
  - If the patient has no email address, the dialog says "This patient has no email address on file."
- **Removed** The Messages-tab count card and the other overview cards are gone for clinicians; see the first bullet in this section. Evidence: `portal/patient_workspace.py:151-159`.

#### Patient Records & Messages → Patient record → Messages
- **Added** Prescribers also get a **Request blood tests** button in the Messages card header, which jumps to the card on the Overview. Evidence: `templates/portal/includes/record_messages.html:12`, `portal/record_messages.py:60`.

#### Patient Records & Messages → Patient record → Deliveries
- **Removed** The **Open delivery** button on each delivery row is hidden for the Doctor role, which no longer has the Operations shipment pages. Evidence: `templates/portal/patient_workspace.html:76`, `portal/views.py:67-76`.

#### Patient Records & Messages → Patient record → Export record
- **Added** The **Export record** button, on the record heading's Overview tab, downloads an Excel workbook named `patient-<id>-record.xlsx` instead of showing a page. Evidence: `templates/portal/patient_workspace.html:11`, `portal/record_overview.py:154-155`, `portal/record_workbook.py:166-179`, `portal/record_urls.py:13`. It has one sheet per record tab, in this order:
  - **Overview**: clinical history, dispensed history, current prescription, weight over time and summary data
  - **History**
  - **Appointments**
  - **Consultations** (signed only)
  - **Blood tests**
  - **Notes** (not private ones)
  - **Weight**
  - **Messages** (only conversations you are in)
  - **Tasks**
  - **Payments**
  - **Treatment**
  - **Deliveries**
  - **Export details**: who exported it, when, and what was left out

  Each sheet has a styled header row frozen at the top, and long text continues onto the rows below. The export is recorded in the audit trail. The History page's JSON "Export clinical record" is unchanged.
- **Changed** Dietitians (Doctor role, Dietitian type) get the same clinical Overview and can edit Summary data, view blood forms and export. They don't see the prescription buttons, the Request blood tests card or the Messages-tab blood tests button. Evidence: `portal/record_overview.py:141-180`, `practices/models.py:66-68`.

#### Calendar & Bookings
- **Removed** Clinicians no longer see the "View schedule" filter card (Date and "Whose diary"). They always see their own diary and move with the ‹ › arrows and Today. If a date in the address can't be shown, the error now says "That date can't be shown. Go to today."
  Evidence: `templates/portal/staff_schedule.html:37-62,65`.
- **Removed** The "Available time" card (open slots) is hidden from clinicians and no longer calculated for them, because booking patients in is the practice's job. The Clinician column is also hidden from the clinician's Day view table.
  Evidence: `portal/staff_views.py:186-191`, `templates/portal/staff_schedule.html:158,170,198-215`.
- **Kept** The **Book appointment** button and "Offer a new time" stay for clinicians.
  Evidence: `templates/portal/staff_schedule.html:15,176`. This matches James's Rev 3.10 decision.
- **Added** Clash banner wording for clinicians: "… sitting on a day you are now unavailable" or "… time outside your special hours". It adds: "The practice has been notified. Nobody is moved automatically: you or the practice can offer each patient a new time…". The clinician gets **Undo the change**.
  Evidence: `templates/portal/staff_schedule.html:71-78`.

#### Calendar & Bookings → Working Pattern & Availability
- **Kept** Clinicians still edit their own weekly working pattern (Save working pattern).
  Evidence: `templates/portal/includes/schedule_availability.html:14-49`.
- **Added** Clinicians can add and remove their own **special days**, and add and remove **days off** with the new date-only form. Both are described under All staff.
  Evidence: `portal/availability_views.py:60-61,151-166`.
- **Added** If a clinician's own day off or special day leaves existing bookings outside their hours, nobody is moved. Instead the practice gets a high-priority task (see Practice admin → Tasks). The clinician does not see that task in My tasks.
  Evidence: `care/availability.py:218-265`, `care/task_services.py:19-20`.

#### Account menu
- **Removed** "My access history" was removed from the clinician's account menu, and the page now refuses clinicians.
  Evidence: `templates/includes/account_menu.html:18`, `portal/privacy_views.py:241-243`.

#### Account menu → My profile
- **Added** Clinicians also see **HPCSA registration number** (read-only) and three details they can edit with a pen icon. Each detail is its own row, and the pen opens a small inline editor with **Save** and **Cancel**:
  - **How you sign consult notes** (example: "S. Marchant, MBChB")
  - **PathCare name** (example: "CZECH,DR J C")
  - **PathCare code** (example: "CZEJC0")

  If a save is rejected (for example, the text is too long), the editor reopens with your text and the reason. A successful save shows the toast "Your details have been updated.". The note reads "Use the pen to update how you sign consult notes or your PathCare details…".
  Evidence: `templates/accounts/profile.html:27-41`, `accounts/views.py:86-122`, `accounts/profile.py:22,80-102`, `accounts/urls.py:17`, `static/css/account_profile.css:60-78`.
- **Added** A **Your month to date** card ("So far this month") shows First consultations, Review consultations (review plus follow-up) and Messages answered.
  Evidence: `templates/accounts/profile.html:51-58`, `accounts/views.py:133-138`.

### Practice admin

#### Menu
- **Changed** The Care section now holds **Tasks**, **Patient Records & Messages** (with the red unread count) and **Calendar & Bookings**. Before, it held Tasks, Patients, Schedule and availability and Messages. Operations, Administration and Insights → Metrics and cohorts are unchanged. On mobile, the Care section icon carries the unread count.
  Evidence: `portal/navigation.py:60-86,96-110`, `templates/portal/includes/staff_navigation.html:12`.

#### Overview
- **Kept** Admins keep the count cards, including Shipments due.
  Evidence: `templates/portal/desktop_dashboard.html:34`, `portal/views.py:129-134`.
- **Changed** The "Unread messages" card now counts patient messages, in conversations you're in, that no staff member has opened yet. It uses the new staff read state, so "Mark as unread" puts a message back into the count. Before, it used the general read flag.
  Evidence: `portal/views.py:115-117`.

#### Tasks
- **Kept** The Open / Overdue / Completed count cards stay for admins.
  Evidence: `templates/portal/staff_tasks.html:19-25`.
- **Changed** The filter heading was removed and the filter icon added (as for clinicians).
  Evidence: `templates/portal/staff_tasks.html:27-31`.
- **Added** New automatic tasks appear when a doctor's own day off or special day clashes with existing bookings. For example: "Offer new times: 2 bookings clash with Dr Sam Example's days off from 5 Oct 2026 to 7 Oct 2026". Each task is high priority, due at the first affected booking and unassigned. Its description lists each booking (time, patient, type) and says: "Nobody has been moved. Agree a new time with each patient, then cancel and rebook." A change made by an admin raises no task, because the admin sees the clash on the page.
  Evidence: `care/availability.py:218-265`.

#### Patient Records & Messages → All patients
- **Kept** Practice admins still see every active patient, with the Clinician filter. Only the header, tabs and title changed (see All staff roles). Evidence: `portal/record_views.py:109-113`.

#### Patient Records & Messages → Patient messages / Team messages
- **Changed** Practice admins get the same new layout, search, read/unread buttons and compose pane. In **New patient message** they can choose any active patient with a login, and the default subject is "Message from the practice". They see only conversations they are in, and no appointment suggestions. Evidence: `portal/inbox.py:67-73`, `portal/record_messages.py:20-23`, `portal/record_messages.py:105-106`.

#### Patient Records & Messages → Patient record
- **Kept** Practice admins keep the administrative Overview: the cards (Appointments, Open tasks, Conversations, Deliveries) and the "Patient details" box. They don't see the clinical Overview, the prescription pill, **Export record** (the workbook download is refused for them) or the details row under the name. Evidence: `portal/patient_workspace.py:151-159`, `portal/record_views.py:298-299`, `portal/record_workbook.py:171`.
- **Added** The "Patient details" box gains a **Title** row with the pen editor, between Record number and Date of birth. Evidence: `templates/portal/patient_workspace.html:30`.
- **Changed** On the Messages tab, practice admins get the new messages layout, search and inline "Write to <first name>…" box, with the automatic subject "Message from the practice". They don't see a **Request blood tests** button and can't request bloods. Evidence: `portal/record_messages.py:60`, `portal/pathcare_views.py:35-39`.
- **Kept** Practice admins still see the **Open delivery** button on the Deliveries tab. Clinical tabs (Consultations, Blood tests, Notes, Treatment) stay hidden from them, and their History tab is still the administrative activity list. Evidence: `templates/portal/patient_workspace.html:76`, `portal/patient_workspace.py:41`, `portal/patient_workspace.py:263-265`.

#### Calendar & Bookings
- **Kept** Admins keep the "View schedule" filter card (Date, Whose diary). It now shows on both tabs. They also keep the "Available time" card and the Clinician column.
  Evidence: `templates/portal/staff_schedule.html:37-62,158,198-215`.
- **Added** Clash banner wording for admins: "… sitting on a day this doctor is now unavailable" or "… outside this day's special hours". It adds: "Nobody is moved automatically. Agree a new time with each patient, then cancel and rebook the appointment." When a doctor is chosen, admins get **Undo the change**.
  Evidence: `templates/portal/staff_schedule.html:71-78`.

#### Calendar & Bookings → Working Pattern & Availability
- **Added** Admins can now **add and remove days off and special days** for the doctor chosen in "Whose diary". Before, the whole panel was read-only for admins ("Doctors record and cancel their own time off"). The working pattern stays read-only for admins: "Read-only view. Doctors manage their own working hours."
  Evidence: `portal/availability_views.py:60-61,154-166`, `care/availability.py:55-72` (`_lock_diary`), `templates/portal/includes/schedule_availability.html:50-59,74,94,138,158`.

#### Operations → Catalogue (product page)
- **Added** The add/edit product form has a new field, **How often a dose is taken** (Weekly / Daily / As directed; new products default to As directed). It appears after Strength. It drives the dose labels (for example "2.5 mg weekly") that doctors see when changing a prescription. The read-only product view doesn't show it.
  Evidence: `portal/operations_forms.py:12-16`, `care/models.py:178-196`, `templates/portal/operations_fields.html` (renders every form field).

#### Administration → Leads (lead detail)
- **Added** A lead's screening answers now start with a **Title** row (Mr/Mrs/Ms/Miss/Dr/Prof, or "Not recorded"). It comes from the new optional Title question on the public screening questionnaire.
  Evidence: `portal/lead_views.py:81-93`, `care/forms.py:340-341`.

#### Account menu → My access history
- **Added** Three new events now have their own names in the history, where before they would have shown as "Recorded account activity": "Account password reset", "Signed out of other devices" and "Staff details updated".
  Evidence: `portal/privacy_views.py:44-47`.

#### Account menu → My profile
- **Changed** Admins see the new read-only My profile, with a "Read-only" chip on Your details. They get no pens, no HPCSA row and no month-to-date card. They can no longer edit their own name.
  Evidence: `templates/accounts/profile.html:21-28,42`, `accounts/views.py:66-71`.

### Super admin

Everything listed for Practice admin also applies to the Super admin. Only the differences are below.

#### Menu
- **Changed** Care now reads Tasks, **Patient Records & Messages** (with the unread count), Consultations, Blood tests, Authorisations, Compounding records, **Calendar & Bookings**. The Messages item was removed. Insights still has Activity statements, which is now shown only to Super admins (before: anyone with clinical access). Settings is unchanged: Review rules, Users and roles, Policy versions, Practice settings, plus Practices only in multi-practice mode.
  Evidence: `portal/navigation.py:60-93`.

#### Patient Records & Messages (menu and tabs)
- **Changed** Same as the practice admin: one **Patient Records & Messages** item under Care with the badge; All patients shows every patient with the Clinician filter; the messages tabs have the new layout, search, read/unread buttons and compose pane, with any patient with a login. Evidence: `portal/navigation.py:62-67`, `portal/inbox.py:67-73`.

#### Patient Records & Messages → Patient record
- **Added** Super admins have clinical access, so they get the clinical Overview, as clinicians do:
  - the log, Summary data (which they can edit), the weight chart and the Current prescription card
  - the heading prescription pill and **Export record**
  - the details row with the Title and Assigned clinician pens
  - **View form** on blood-form log entries

  Evidence: `portal/patient_workspace.py:144-149`, `portal/record_overview.py:150`, `portal/pathcare_views.py:117-119`.
- **Changed** A Super admin can renew, change or create prescriptions and request or resend blood tests only if their clinician type is Doctor. Otherwise those buttons and the Request blood tests card are hidden. Evidence: `portal/record_overview.py:141-180`, `portal/prescription_views.py:83-84`, `portal/pathcare_views.py:37-38`.
- **Changed** The History tab and Deliveries tab are as for clinicians, without the consent card, and **Open delivery** stays visible for Super admins. The Messages tab is as for other staff. A Super admin who is a doctor gets the subject "Message from Dr <surname>" and the Request blood tests button. Evidence: `templates/portal/record_detail.html`, `templates/portal/patient_workspace.html:76`, `portal/record_messages.py:20-23`.

#### Insights → Activity statements
- **Changed** Activity statements is now Super admin only. Doctors can no longer read their own statements.
  Evidence: `portal/navigation.py:85`, `portal/reporting_views.py:75`.

#### Settings → Users and roles → Add staff access
- **Added** A new **Title (new accounts only)** dropdown (Mr, Mrs, Ms, Miss, Dr, Prof) defaults to **Dr**. It is saved only when creating a new login, not when linking an existing account.
  Evidence: `practices/management_forms.py:65-66`, `practices/management_views.py:106`, `practices/management_services.py:93,107`.

#### Settings → Users and roles → Staff access (edit a staff member)
- **Added** The edit page now also has **Title**, **First name**, **Last name**, **Contact number**, **HPCSA registration number** ("Clinicians only."), **How consult notes are signed**, **PathCare referring name** and **PathCare doctor code**. The PathCare fields are marked "Doctors only… Needed to request blood tests." The Super Admin maintains these and the person sees them on My profile. The page description now explains this; before, it said the name could not be edited here.
  Evidence: `practices/management_forms.py:106-126`, `practices/management_views.py:120-151`, `practices/management_services.py:169-185`, `templates/portal/management_form.html` (renders every field).

#### Settings → Review rules
- **Changed** Review rules is now for Super admins only. Doctors lost their read-only view; practice admins were already refused.
  Evidence: `portal/review_views.py:17`.

#### Settings → Practice settings (event log)
- **Changed** In the Administration list, the event log line "Passwords and profiles changed" now reads "Passwords changed or reset, other devices signed out, and profiles or staff details updated".
  Evidence: `portal/practice_settings_views.py:56`.

#### Account menu → My profile
- **Added** A Super admin who also has a clinician type (for example "Super admin · Doctor") gets the clinician extras: the HPCSA row, the three pen-editable details and Your month to date. A Super admin without a clinician type sees the read-only version.
  Evidence: `accounts/views.py:132,138-140`, `accounts/profile.py:91-93`.

#### Account menu → Practice administration (Django admin, superusers)
- **Added** Doctor special days are now listed in Django admin, alongside working patterns and time off, for inspection. Changes still go through the portal.
  Evidence: `care/admin.py:187`.

---

## Not visible to users (backend-only changes in these edits)

- **Database migrations**:
  - `accounts/0002_staff_details`, `0003_pathcare_details` and `0004_user_title` add staff title, contact number, HPCSA
    number, note signature, PathCare name and PathCare code. They also backfill Title = Dr for existing doctors.
  - `care/0014_doctor_special_days` adds the special-day table and makes the day-off reason optional.
  - `care/0015_prescriptions_and_clinical_summary` adds dose frequency on products (seeded weekly or daily), the link from
    a prescription to the one it replaces with its reason and log line, and the Summary data tables with their edit history.
  - `care/0016_staff_read_state_and_pathcare_forms` adds the practice's own read state on patient messages, seeded from
    staff replies, and the stored PathCare form with its email status.
  - `practices/0004_patient_title` adds the patient's Title.
- **Staff read state is separate from the patient's.** Staff opening a conversation, or using Mark as unread, never changes
  the patient's badges (`care/models.py:940-944`, `care/messaging.py:25-61`).
- **Celery background work.** A new worker (`celery -A config worker`) runs `send_pathcare_form_email`, which retries 3 times
  2 minutes apart and then marks the email failed. Without `CELERY_BROKER_URL` or `REDIS_URL`, tasks run straight away in
  the web process, which also happens in tests (`config/celery.py`, `config/__init__.py`, `care/tasks.py`,
  `config/settings.py:199-207`).
- **Outgoing email.** Brevo SMTP is configured with `DJANGO_EMAIL_*`. With no host set, emails only go to the server log.
  Password reset checks for this; the PathCare email doesn't (see Open questions). Reset links last 1 hour
  (`config/settings.py:184-197`, `.env.example`).
- **Deployment.** These files change:
  - `.do/app.yaml.example` gains a `worker` component.
  - `docs/DEPLOYMENT.md` documents the broker and email settings.
  - `requirements.txt` adds `celery[redis]==5.6.3` and `pypdf==6.19.0`.
  - The blank PathCare form is committed in the repo (`care/pathcare/clinical_request_blank.pdf`, about 1.9 MB).
- **New audit events**:
  - `prescription.renewed/changed/created`
  - `summary.updated` (field name only)
  - `pathcare_form.created/resent/emailed/email_failed/viewed`
  - `patient.title_updated`
  - `message.marked_read/unread`
  - `patient.clinical_record_exported` (the workbook)
  - `account.password_reset`
  - `account.staff_details_updated`
  - special day set or removed
  - conflict task raised
  - other sessions ended
- **One switch for admin-only pages.** Pages marked admin-only now refuse the Clinician role (`portal/views.py:67-76`).
  Switching practice (multi-practice mode only) sends a clinician to Overview.
- **Summary data seeding.** Summary data is seeded from the screening questionnaire and the patient's medical profile.
  The originals are never changed (`care/clinical_summary.py`).
- **New addresses behind the screens**:
  - prescription renew, change and create
  - summary edit
  - patient title
  - five blood-test addresses
  - record conversation search
  - staff and team read state
  - New patient message
  - staff detail edit
  - sign out others
  - the password reset pages
- **Housekeeping.** Stylesheets and scripts carry version suffixes (`?v=…`) so browsers fetch the new files. About 175
  lines of unused inbox CSS were removed from `console.css`.

## Open questions and gaps found while checking

Items marked **verified** were confirmed in the code. The rest were read from the code but not run.

### Patient web app
1. **Blood-test emails can be marked "Sent" when nothing was sent (verified).** With no `DJANGO_EMAIL_HOST`, Django prints
   emails to the server log instead of sending them. The PathCare send then succeeds and marks the form "Emailed".
   Password reset checks that email is set up first, but the PathCare request doesn't
   (`care/pathcare.py:261-263`, `config/settings.py:194`).
2. **The patient can't see their PathCare form in the app.** It is only emailed. Should it be offered on their Blood tests
   page, in case the email is lost?
3. **Title is invisible to the patient.** It is chosen in the questionnaire or set by staff, but the patient can't see or
   change it in Account settings. Is that intended?
4. **Who a patient can message.** A patient booked with a clinician who was never assigned to them can no longer start a
   conversation with that clinician. A patient with no assigned clinician can't start one at all. Please confirm the rule.
5. **Staff see "Not a patient yet? Get started" too,** because the sign-in page is shared. Is that OK?
6. **Confirmation toasts fade after 3 seconds.** This includes the checkout confirmation ("Your initial consultation with Dr
   X is booked…"). Hover or keyboard focus pauses the fade; a touch doesn't.
7. **Patients don't get "Sign out others".** It is only on the staff My profile.
8. **Password reset limits are counted per web process.** They use the default in-memory cache because `CACHES` isn't set,
   so with several processes each one counts separately.
9. **Searching your own name returns every conversation.** My Messages search also matches the patient's own name and
   record number. Harmless, but it may confuse.

### Practice console
10. **Doctors can open records that aren't theirs (verified).** A doctor's All patients list shows only their patients, but
    the record page only checks that the patient belongs to the practice (`portal/patient_workspace.py:261`). Other records
    still open from Team messages, tasks, the calendar or a typed URL, and a patient conversation can be started there.
    Intended, for cover and handover, or should the record check assignment?
11. **Unread counts will jump after deploy (verified).** Migration 0016 counts an existing patient message as read only if
    staff replied after it. Messages that staff opened but never answered will show as unread again.
12. **Dietitians are addressed as "Dr" (verified).** Any clinician type, including dietitians, gets the automatic subject
    "Message from Dr <surname>" (`portal/record_messages.py:20-23`).
13. **Clinical list pages still open by URL.** Consultations, Blood tests, Authorisations and Compounding records left the
    clinician's menu but still open by URL. Should they be refused like Operations and Insights?
14. **Two ways to do the same job.** The Blood tests tab's "Request blood tests" and the Treatment tab's "Authorise
    treatment" still open the older pages, alongside the new Overview card and dialogs.
15. **Overview wording out of date.** The Recent conversations button still says "Open inbox", and a doctor's empty
    Patients table says "…in this practice" although it now lists only their own patients.
16. **Day-count cards.** Booked, Completed, Cancelled and No show still show for doctors in Day view, unlike Overview and
    My tasks. Keep them?
17. **"Back to conversation" on Manage conversation** always opens Patient messages, even when you came from the record.
18. **No record export for practice admins.** "Export record" is only for clinical staff.
19. **No paging on the record's Messages tab.** It now loads every conversation and every message at once; Patient
    messages pages 20 at a time.
20. **Staff no longer see "X added Y · date" lines** in patient conversations. The patient still sees them, and Team
    messages still shows them.
21. **An audit event with no label.** A clinician's own pen edit (`account.staff_details_updated`) has no label in My access
    history. This only matters for a Super admin who is also a doctor.
22. **The calendar has Day and Month views only.** There is no Week view.
