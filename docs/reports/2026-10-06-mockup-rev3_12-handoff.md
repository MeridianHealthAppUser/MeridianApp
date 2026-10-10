# Meridian Health: Mockup Rev 3.12 handoff (code → mockup), 6 Oct 2026

**For:** the Claude chat that maintains the interactive mockup HTML.

**Input file:** `meridian-health-app-mock-rev3_11__05_10_2026.html` (Rev 3.11).

**Output file:** `meridian-health-app-mock-rev3_12__06_10_2026.html`. Change `<title>` to "Meridian Health — System Mock (Rev 3.12)" and mark new CSS/JS blocks with `/* Rev 3.12: … */` comments, as earlier revisions did.

**Why:** James uses the mockup to show Dr Czech current progress. The codebase has moved past the mockup: it built Rev 3.10/3.11, then made more changes on 5–6 Oct. This document lists every visible difference found by comparing the Rev 3.11 mockup with the current code, including the uncommitted work. The goal is that the mockup shows what the app actually does.

## Ground rules

- **The app wins.** Where the mockup and this document disagree, follow this document. Quoted text is the exact wording used in the app; reuse it.
- **Keep the mock devices:**
  - the sample data
  - the role picker on the console sign-in
  - toasts that stand in for side effects
  - the PathCare form picture with yellow overlays
  - the video room
  - the dark-mode toggle (keep every new colour on the existing tokens so dark mode still works)
- **Type: Roboto everywhere**, as Dr Czech gave it (the logo's Quicksand is part of the logo image). Page headings (the record's patient name, "Calendar & Bookings", "Welcome, Sam.", "My profile", sign-in's "Welcome back") are Roboto weight 500 with `letter-spacing: -.02em`; no Georgia or other serif anywhere, the video room included.
- **Don't delete mockup pages that aren't built yet** (payout run, email journeys, practice profile, the medications catalogue's blood-test schedule, dispatch screens, and so on) unless a section below says so. §D lists the mock-only items James may want to flag or hide.
- **"Mock refs"** name the functions, IDs and constants in the Rev 3.11 file where each change lands.
- **Priority:**
  - **P1:** what Dr Czech sees as a doctor (§A, §B1–B12).
  - **P2:** admin and Super Admin differences (§B13–B15).
  - **P3:** the patient web app (§C).

## At a glance

1. Toasts move to the top right, have a × button and fade after 5 seconds; errors stay until closed. This applies to both surfaces.
2. The doctor's sidebar becomes a flat list with icons: **Overview**, **My tasks**, **Patient Records & Messages**, **Calendar & Bookings**. Doctors land on a new **Overview** page.
3. Patient Records & Messages gets three tabs: **All patients** (the default), **Patient messages** and **Team messages**. All three message lists share one design: a list with search, read/unread envelope buttons, and a reading pane.
4. The patient record gains:
   - a header with Title, DOB, Location and Assigned clinician (Title and Assigned clinician have pen editors), the prescription pill and an Excel **Export record**
   - a tab row (Overview, History, Appointments, Consultations, Blood tests, Notes, Weight, Messages, Tasks, Payments, Treatment, Deliveries)
   - the conversation moves off the top of the record and onto the **Messages** tab
5. The record Overview is reordered: the log and Summary data on the left; Weight, Current prescription and Request blood tests on the right. Other changes:
   - Thyroid gets an "Unsure" answer.
   - Prescription dialogs show safety flags with a required tick.
   - Button wording changes ("Renew prescription", "Save prescription").
   - Blood-form log entries show email status and **Resend to patient**.
   - The PathCare preview shows "Medical aid no. — Left blank".
   - The **Weight tab** uses the Overview's weight chart (dose bars, consult dots), with the check-in history beside it (§B8b).
6. On Calendar & Bookings:
   - **"Days off" is now "Leave"** everywhere.
   - Special hours get a **clock-with-star icon**, and a month cell's tag sits top right, opposite the date.
   - Month view shows each booking as a chip.
   - Day view gains a Clinician column (admins only), a Room column and row actions. It has **no** count cards (Booked, Completed, Cancelled, No show): remove them if the mock has them.
   - The clash banner has only **Undo the change**.
   - **Appointments needing attention** replaces "What happens when a time is moved".
   - Special days and Leave lists come first, and their forms open from dashed "+ Add …" buttons.
7. Staff **My profile** gains:
   - a Title row above First name
   - an HPCSA row and three pen-editable rows for clinicians
   - a Security card with two password dates, a Two-factor authentication row reading "Not set up yet", and **Sign out others**
   - a month-to-date card with a "Provisional" chip and a **Provisional total** row; no practice tag beside the role
8. Patient app:
   - a new sign-in page and a Forgot-password flow
   - a Title question in the questionnaire
   - My Messages gets search, red count badges, a bold "You:", the "Waiting for a reply" pill removed, and a **To** list that includes the practice administrator
   - Account settings gets a **Title and contact details** card
   - the blood-test task opens a request page with the **PathCare form** (Open / Download) and the upload

## Deliberate decisions (don't revert these to the Rev 3.11 mockup)

These were James's calls when Rev 3.10/3.11 were built. The mockup should show them as they are.

- **Doctors keep Overview** in their menu.
- **Patient conversations stay private to their participants.** A practice administrator sees only threads they are in, not every thread in the practice.
- **The record's conversation lives on the Messages tab**, not at the top of the record. The record Overview shows no messages.
- **Patient Records & Messages opens on All patients.** The tab order is All patients, Patient messages, Team messages.
- **The sidebar is 272px (17rem)**, not 256px, so "Patient Records & Messages" and its badge fit on one line.
- **The sidebar badge counts patient unread plus team unread.**
- **Calendar & Bookings has two tabs.** The Working pattern description stays.
- **Doctors keep "Offer a new time"** on their calendar rows.
- **Safety flags warn, then allow.** The prescription dialogs list them in red and require the tick "I have reviewed this and still want to prescribe."
- **There is no script document,** so buttons say "Renew prescription" and "Save prescription", never "… create script".
- **Any practice doctor can prescribe,** including a Super Admin who is a doctor. A dietitian can't.
- **The PathCare postal address is the patient's shipping address.** The medical aid number is never stored and is left blank.
- **No 2FA "trusted browser"** was built.
- **Staff details are kept by the Super Admin** in Users and roles. A clinician can pen-edit only their note signature, PathCare name and PathCare code.
- **Users only ever sign out themselves.** "Sign out others" ends your own other sessions.

---

## A. Both surfaces

### A1. Toasts (P1)

**Mock refs:** `toast()`, `.toast`, `#toast`, `body.dark .toast`, `#vroom.on ~ .toast`.

**Now in the mock:** one dark pill, bottom centre, gone after 2.4 s.

**Change it to:**

- **Position:** a stack fixed at the **top right**: `top: .75rem; right: 1rem`, max width `min(24rem, 100vw - 2rem)`, gap `.5rem`. It sits above the console header, modals (`#mdl`) and the video room.
- **Each toast** is a small card: rounded `.6rem`, `padding: .75rem 1rem`, `font-size: .9rem`, and a soft shadow `0 12px 32px rgba(16,61,59,.16)`. Text on the left, a **×** close button on the right (`aria-label="Dismiss"`).
- **Confirmations:**
  - Console: background `#eef8f5`, border `#b9ddd6`, text `#1b635d`.
  - Patient app: the patient accent tokens (teal-l background, teal-b border, teal-d text).
  - They fade out after **5 seconds**. Hovering or focusing one pauses the fade.
- **Errors:** background `#fff4f2`, border `#efc9c4`, text `#8e3e36`. They **stay until closed**.
- **New toasts stack below older ones.** Use `role="status"` for confirmations and `role="alert"` for errors.
- **Signature:** change `toast(message)` to `toast(message, kind='success')` and keep existing calls working. Use `toast(msg,'error')` for the error cases listed below.
- **Dark mode:** keep a dark variant (`body.dark`, `body.dark-phone`).

---

## B. Practice console

### B1. Sidebar and account menu (P1)

**Mock refs:** `#anav`, `aIdx`, `aNavOf`, `STAFF`, `adminSignIn()`, `ago()`, `.admin{grid-template-columns:256px 1fr}`, `#menu-staff`.

**Doctor (Clinician) menu**

A **flat list with an icon on each item**, like the patient app, with no section headings:

1. **Overview** (grid/home icon)
2. **My tasks** (checklist icon)
3. **Patient Records & Messages** (folder/records icon) with the red unread badge at the right
4. **Calendar & Bookings** (calendar icon)

Other rules for doctors:

- Set `STAFF.doctor.land = 'overview'`.
- Doctors **do not** see Operations, Insights or Setup. They also lose the old "Messages" item, "Consultations / Blood tests / Authorisations / Compounding records", and "My access history".
- The active item keeps the teal-l background and teal-d text.

**Practice Administrator and Super Admin menu**

The app keeps section groups for these roles: an **Overview** link at the top, then collapsible sections. The app's sections are:

- **Care:**
  - Tasks
  - Patient Records & Messages (badge)
  - *(Super Admin only)* Consultations, Blood tests, Authorisations, Compounding records
  - Calendar & Bookings
- **Operations:** Subscriptions, Supply requests, Weekly shipping list, Dispatch history, Batches and stock, Catalogue.
- **Administration:** Leads, Dropouts, Data requests.
- **Insights:** Metrics and cohorts, plus Activity statements (Super Admin only).
- **Settings (Super Admin only):** Review rules, Users and roles, Policy versions, Practice settings.

**Minimum change:** add the **Overview** item at the top for admins too, and rename the "Clinical" heading to **"Care"**. Fully restructuring the admin menu is optional: several mock pages (payout run, email journeys, practice profile) aren't in the app yet. Ask James before removing them.

**Badge and width**

- The badge on Patient Records & Messages = **unread patient messages + unread team messages** in the user's scope. It is red `#D92D20`, white bold text, "9+" above nine, and hidden at zero (this style is already in the mock).
- **Sidebar width:** change `256px` to **272px**.

**Account menu (`#menu-staff`)**

- Items: **My profile**, **Change password**, **My access history** (not for doctors), then **Sign out**.
- Add the missing **Change password** item. It can toast "Opening Change password" in the mock.

### B2. New: Overview page, the doctor's landing page (P1)

**Mock refs:** add `<div id="a-overview" class="aview">`, add `'overview'` to `aviews` and `aIdx`, and add `renderOverview()`.

**Breadcrumb:** none.

**Header:**

- Heading: **"Welcome, Sam."** (the user's first name).
- Sub-line: "A focused view of your patients, clinical follow-ups, and conversations within Meridian Health."

**Count cards:**

- **Doctors get none.**
- **Admins and Super Admins** get a row of count cards: **Patients**, **Appointments today**, **Open tasks**, **Unread messages**, and (admins only) **Shipments due**.

**Two cards side by side,** equal width and equal height, stacking on narrow screens:

- **Upcoming appointments**
  - Sub: "Scheduled care at Meridian Health"
  - Quiet button: **View Schedule** (goes to Calendar & Bookings)
  - Rows: time (bold) with "Thu, 27 Aug" under it, the patient's name, "Follow-up · Dr Sam Marchant", and a status pill such as "Booked".
  - Empty: "There are no upcoming appointments in this practice yet."
- **Open tasks**
  - Sub: "Keep follow-ups moving"
  - Quiet button: **See all** (goes to My tasks)
  - Rows: task title (bold); "Patient · assignee"; a priority chip ("High"); "Due 2 Sep, 09:00"; and a quiet **Complete** button, or the workflow button for clinical tasks.
  - Empty: "No open clinical tasks. The care team is up to date."

**Patients card,** full width:

- Sub: "People currently receiving care at Meridian Health"
- Primary button: **View Patients** (goes to All patients)
- Table columns: Patient (initials avatar, name, and email under it), Record, Assigned clinician, Status ("Active" pill), and an **Open** link.
- **A doctor sees only patients assigned to them** (for Dr Marchant: Nadia Mokoena, Riaan Botha, Lerato Khumalo, Carla Fourie, Sipho Ndlovu).

**Recent conversations card,** full width:

- Sub: "Patient messages in this practice"
- Quiet button: **Open inbox** (goes to the Patient messages tab)
- Rows: initials avatar; subject (bold); patient name; "2 days ago"; and an **Open conversation** link that opens the record's Messages tab with that thread.

### B3. My tasks (P1, small)

**Mock refs:** `#a-tasks`.

**What the app has:**

- **Header:** heading "My tasks", sub "Tasks assigned to you or created by you in Meridian Health.", and a primary **Create task** button at the top right.
- **No Open / Overdue / Completed count cards for doctors.** Admins keep them.
- **One filter row:**
  - a teal **funnel icon** on the left (hidden on narrow widths)
  - Status, Task type, Patient and Free-form tag fields
  - **Apply filters**
  - no "Filter tasks" heading or intro line
- **The task list card:** heading "My tasks", sub "Workflow status and free-form tags are kept separate.", and an "N tasks" count.

**Minimum change:** remove the "6 open · 2 overdue" pill and the "What needs you today" intro, then add the header button and the filter row above the existing table. The rest of this page is in §D (mock-only).

### B4. Patient Records & Messages: page header and tabs (P1)

**Mock refs:** `#a-subs`, `#a-messages`, `renderPmTabs()`, `#pm-tabs-m`, `#pm-tabs-s`, `ago('messages')`, `aNavOf`.

**One page, three tabs:**

- Heading: **"Patient Records & Messages"**.
- On the message tabs, a primary button sits at the right of the heading: **New patient message** on Patient messages, **New team message** on Team messages. All patients has no button.
- Underline tabs, in this order:
  1. **All patients** (the default; the sidebar link goes here)
  2. **Patient messages** (red badge = unread patient messages)
  3. **Team messages** (red badge = unread team messages)
- **Breadcrumb:** "Patient Records & Messages".
- The sidebar item stays highlighted on all three tabs and while a patient record is open.

**All patients tab**

- A count line under the tabs: "**N active patient records in Meridian Health.**"
- **"Find a patient" card:**
  - Doctor intro: "Your patients: the people assigned to you. Search by name or record details."
  - Admin intro: "Search your patient directory by name or record details, or filter by clinician."
  - Fields: Search, and **Clinician** (not shown to doctors).
  - Button: **Search**.
- **"Patient directory" card:**
  - Sub: "Open a record to view care activity or add a task."
  - Count: "N matching".
  - The mock's existing table is fine.
- **A doctor sees only their own patients.** Filter `SUBS` by Dr Marchant for the doctor role.

### B5. Patient messages tab (P1)

**Mock refs:** `#a-messages`, `#am-list`, `renderStaffInbox()`, `staffThreads()`, `msgFilter()`, `openThread()`, `THREADS`, `staffUnread()`.

**Now in the mock:** a full-width list of rows. Clicking a row jumps into the patient record.

**App layout:** a **two-pane inbox**, the same component the record's Messages tab (§B9) and Team messages (§B6) use.

**Toolbar** above the panes:

- Filter links on the left: **Everything / Awaiting a reply / Answered**.
- A **search box** on the right with a magnifier icon, placeholder "**Search messages, patients, clinicians**", and a × button to clear it.
- The list filters as you type. Every word must match the subject, message text, sender, patient name or record number, or a clinician in the thread.

**Left pane (about 320px):**

- One row per conversation:
  - Line 1: the **subject** (bold when unread), with the red count badge at the right.
  - Line 2: "**With Nadia Mokoena**" (plus other clinicians in it, comma-separated).
  - Line 3: the latest message, prefixed with a bold **"You:"** when you sent it, and a grey "Closed" pill on closed threads.
  - At the right of line 3: the **time if today** ("08:12"), otherwise the date ("3 Sep"). The full date and time shows on hover.
- **Envelope button:** a small round button at the top right of each row that has patient messages.
  - Unread rows show an **open envelope** that marks the row read (tooltip "Mark as read").
  - Read rows show an **envelope with a red dot** that marks it unread (tooltip "Mark as unread").
  - Using it closes any open conversation and toasts "**Conversation marked as read.**" or "**Conversation marked as unread.**"
- No avatars, no "Awaiting reply" labels on rows, and no per-row "Open record" link.
- Empty list: "No conversations match this filter." While searching: "No conversations match "…"."

**Right pane:**

- **Nothing open:**
  - **"Choose a conversation"**, with "Select a conversation to read it. New messages stay unread until you open them."
  - A primary **New patient message** button.
- **A conversation open:**
  - Header: the subject (h3), then links on the right: **Open Nadia's record**, **Mark as unread** (only if the patient has written), and **Add a clinician or close** (the mock can toast).
  - Under it: "**With Nadia Mokoena, Dr Sam Marchant**".
  - A scrolling box of **chat bubbles**:
    - patient messages on the **left in light blue** (`--blue-l` background, `--blue-b` border)
    - staff messages on the **right in white** with a `--line` border
    - each bubble has a meta line "**Nadia Mokoena · 3 Sep, 08:12**"
  - Opening a thread marks its patient messages read for the practice (this is already the mock's `m.seen` rule).
  - Footer: a **one-line auto-growing textarea** with placeholder "**Reply to Nadia…**" and a primary **Send reply** button. **Enter sends; Shift+Enter makes a new line.**
  - A closed thread shows "This conversation is closed. Start a new one." in place of the reply box.
  - **No "Appointment suggestions" block** between the bubbles and the reply box. It appears only when the conversation has a suggestion (e.g. the patient suggested a new time), listing it with **Accept** / **Decline**. Staff never get the "Suggest a different appointment time" bar; patients keep it in My Messages.
  - **Patient notes** (clinicians and Super Admins only; never practice administrators): a closed accordion under the reply box.
    - Summary row: a chevron, "**Patient notes**", a small teal count chip ("6"), and on the right in grey "Consult notes and general notes, newest first".
    - Opened: a scrolling list, newest first. Each note shows a grey meta line "**Phone call** · 7 Oct 2026, 06:22 · Sam Marchant" (the type in bold; "· Only visible to you" on your private notes), then the note text. Signed consult notes are labelled "**Consult note**" and have an "**Open consultation**" link.
    - Footer: "On Nadia's record: **Notes** · **Consultations**" (links), and "Showing the latest 100." when there are more.
    - The same accordion appears under the conversation on the record's Messages tab (§B9).
- **Desktop height:** on wide screens the two-pane block runs down to the bottom of the window (about 24px above it). The list and the bubbles scroll inside it. Opening Patient notes **doesn't shrink the bubbles**: the notes drop down below the reply box and the right pane scrolls to read them. Team messages (§B6) does the same. Narrower screens keep the current heights.
- **New patient message** (from the header button or the empty state):
  - pane title "**New patient message**"
  - note "**Only you and the patient can read it. You can add a clinician once it has started.**"
  - Fields: **To** ("Choose a patient", options "Nadia Mokoena · MER-000123"); **Subject (optional)** with placeholder "Message from Dr Marchant" for a doctor or "Message from the practice" for admins; **Message**.
  - Buttons: **Cancel** and **Send**. Send toasts "**Secure conversation started. Only you and the patient can read it.**" and opens the new thread.
  - **A doctor can only pick their own patients who have a login.**

**Scope:**

- A doctor sees threads with their own patients.
- **A practice administrator sees only threads they are a participant in.** Patients can now write to the practice administrator (see §C4), so give Lindiwe at least one thread. For example, make Thabo Molefe's "Debit came off twice" thread a patient→**Lindiwe Mahlangu (practice administrator)** thread.
- The Super Admin sees threads they are in.
- Rename the `THREADS[].doctor` field to something like `staff` so a thread can be with an administrator.

**Drop** the "Monthly message report" Super Admin bar from this tab. It isn't in the app, so move it to §D.

### B6. New: Team messages tab (P1)

**Mock refs:** add `TEAM_THREADS`, `renderTeamInbox()` and a `#a-team` view, or a mode on `#a-messages`.

**Layout:** the same two-pane design and toolbar as §B5.

- The search placeholder is "**Search messages, colleagues, patients**".
- There are no Everything / Awaiting / Answered filters.

**Rows:**

- subject (bold with red count when unread)
- "**With Lindiwe Mahlangu · About Riaan Botha**": the colleagues other than you, then the patient if there is one
- latest message with a bold "You:" when it's yours
- time or date
- envelope read/unread button

**Empty pane:** "**Choose a team conversation**" plus a **New team message** button.

**Open conversation:**

- Header: subject, plus links **Open Riaan's record** (when it's about a patient) and **Mark as unread** (when someone else has written).
- People line: "**With Lindiwe Mahlangu · About Riaan Botha · Lindiwe Mahlangu added Dr Joshua Czech 5 Oct**".
- An expandable **Add a colleague** link.
- Bubbles: yours on the right labelled "You", others on the left.
- A one-line "**Reply…**" box with **Send reply**, and the footnote "**Only the people in this conversation see replies. The patient never does.**"

**New team message:**

- Fields: **To** (a checkbox list of colleagues with their role, e.g. "Lindiwe Mahlangu · Practice Administrator"), **About a patient (optional)**, **Subject**, **Message**.
- Buttons: **Cancel** and **Send**.

**Suggested sample threads** for Dr Marchant:

1. "Riaan's authorisation lapsed": with Lindiwe Mahlangu, about Riaan Botha, one unread from Lindiwe.
2. "Cover for Wednesday clinic": with Dr Joshua Czech, no patient, read.
3. "Nadia's courier date in October": with Lindiwe, about Nadia Mokoena, last message "You: …".

### B7. Patient record: header (P1)

**Mock refs:** `#a-emr`, `#emr-name`, `#emr-meta`, `#emr-pills`, `renderEMR()`, `EMRP`.

**Clinical staff** (doctors, and Super Admins who are clinicians) see the following.

**Breadcrumb:** "Patient Records & Messages › Nadia Mokoena". Remove the "Clinical ·" prefix.

**Under the name,** one muted line: "**MER-000123 · nadia.m@example.co.za · 082 555 0114**" (record number · login email · phone). Age, city and ID are **not** on this line any more.

**A new "quick facts" row** of four labelled values, in small caps:

- **Title:** the value "Ms" plus a **pen** button. The pen opens a small inline editor: a select (Not recorded, Mr, Mrs, Ms, Miss, Dr, Prof), help text "**Printed on PathCare blood test forms.**", and **Save** / **Cancel** buttons. Save toasts "**Title saved.**" and should update `PT_EXTRA[...].title`.
- **Date of birth:** "15 Apr 1992".
- **Location:** "Cape Town".
- **Assigned clinician:** "Dr Sam Marchant" plus a **pen**. The pen opens a select of clinicians and Save.

**Right side of the heading,** shown on the Overview tab only:

- The prescription pill(s): "**Prescribed to 24 Oct**" (teal) plus "**Renewal due**" (amber) when 30 days or fewer remain. The other states are "**Prescription expired 12 Aug**" (rose), "**Prescription paused**" (grey) and "**No prescription**" (grey). The mock already has most of these.
- A quiet **Export record** button. In the app it downloads an **Excel workbook** (`patient-<id>-record.xlsx`) with one sheet per record tab. Change the mock toast to "**Record exported as an Excel workbook, one sheet per tab**".

**A tab row under the header,** underline style:

- The tabs, in order: **Overview · History · Appointments · Consultations · Blood tests · Notes · Weight · Messages · Tasks · Payments · Treatment · Deliveries**.
- Overview is active by default.
- Make **Overview** and **Messages** fully working. The other tabs can show a short placeholder card: "This section is in the app; it isn't modelled in the mock."
- On phones the tab row becomes one dropdown button showing the current section.

### B8. Patient record: Overview tab (P1)

**Mock refs:** `.emr-g`, `#tl`, `renderTL()`, `#rx-rows`, `renderRx()`, `#chart2`, `emrChart()`, `#bt-card`, `#sum-rows`, `renderSummary()`, `SUM_F`, `SUM_FLAGS`, `FLAG_OPTS`.

**1. Remove the Messages card** (`#emr-msgs`) from the top of the record. It moves to the Messages tab (§B9).

**2. Column order changes.** Two equal columns, stacking to one column on narrower screens:

- **Left column:** the **Log** card, then the **Summary data** card underneath it.
- **Right column:** **Weight**, then **Current prescription**, then **Request blood tests**.

**3. Log card**

- **Chips and count:** keep the **Clinical / Dispensed** chips and the count ("6 entries" / "5 shipments").
- **Empty states:** "No clinical entries yet." and "Nothing dispensed yet."
- **Clinical entries:**
  - date ("24 Aug 2026")
  - the author's full name, or "**Sent automatically**" for rule-sent forms
  - a tag: "Blood form" (blue) or "Prescription" (green); consult notes have none
  - the text
- **Blood-form entries have an action line** under the text:
  - a **View form** link (in the mock, the existing `viewSentForm()` modal)
  - a status: "**Emailed to nadia.m@example.co.za**", "**Email queued**", or "**Email failed**" in rose
  - for prescribers, a small **Resend to patient** button (or **Resend** after a failure). It toasts "**Blood test form sent again to nadia.m@example.co.za.**"
- **Dispensed entries:** "**Semaglutide 1.0 mg × 4 pens**", a teal "**Month 5**" tag, and "**Batch SG-2612, expiry 04/2027 · courier MER-441-209-ZA**". This is unchanged.

**4. Summary data card**

- Header: "Summary data", plus a rose "**Safety flag**" pill when any flag is Yes, and the hint "**Click an underlined value to edit**" (shown to clinicians only).
- Rows:
  - Weight start → current → goal (only the goal is editable, 40–250 kg)
  - BMI start → current
  - Medical history, Surgical history, Chronic medication and Allergies (empty shows "Not recorded")
- A **"Safety flags"** subheading, then three rows:
  - History of pancreatitis: **No / Yes**
  - Medullary thyroid cancer / MEN2: **No / Yes / Unsure** (add "Unsure" to `FLAG_OPTS.mtc`; show "Unsure" as an **amber** pill)
  - Pregnant or breastfeeding: **No / Yes / Not applicable**
  - "Yes" is a rose pill. Picking an answer saves straight away.
- Footer: "Populated from the screening assessment on 12 Apr 2026 · unedited since", or "· 2 edits since" once edited. The edit history list is "**date · editor · field: ~~old~~ → new**". This is unchanged.

**5. Weight card**

- Unchanged, except:
  - With no goal set, show "**Set a goal weight in Summary data to see the projection.**"
  - With no weights, show "**No weights logged**".
- The key order and wording stay as in the mock:
  - Weight
  - Expected range, assuming 0.5 to 1 kg a week
  - Projected to goal
  - Dose shipped (mg, right scale)
  - Consultation — click to open the note

**6. Current prescription card**

- Rows: Medicine, Dose ("1.0 mg weekly"), Authorised, Expires (amber when due, rose with "· expired"), By.
- When paused, a note: "**This prescription is paused. Undispatched parcels are on hold.**"
- With no prescription: "**No prescription yet. One is written at the first consult.**"
- Buttons, for prescribers only: **Renew prescription** (primary) and **Change prescription** (secondary), or **Create prescription** (primary) when there is none.

**7. Request blood tests card** (prescribers only)

- Unchanged layout: the label "Tests and relevant clinical data", a textarea with a 300-character maximum, the note "Fills a PathCare request form with the patient's details, emails it to them and adds it to the clinical log.", and the **Request blood tests** button.
- If the doctor's PathCare name or code isn't on file, a red line shows above the box: "**Your PathCare name and code are not on file, so the form can't be filled. Ask the practice to add them in Users and roles.**" The button is then disabled.

**8. Consult note popup** (clicking a consult dot)

- Unchanged: an eyebrow "Consult note", the patient name, "date · author", the note text, and the buttons **Show in log** / **Close**.

### B8b. Patient record: Weight tab (P1)

**Mock refs:** the record's Weight tab; reuse the Overview's Weight card chart (§B8.5).

The Weight tab no longer has its own simple line chart. It now has two rows:

**1. Three summary cards** across the full width, each a white card:

| Card | Big figure | Line below |
|---|---|---|
| Starting weight | "94.00 kg" | the first check-in's date, e.g. "24 Apr 2026" |
| Latest weight | "83.40 kg" | the latest check-in's date |
| Change | "-10.60 kg" ("+" when it went up) | "Since first check-in · 6 check-ins" |

**2. Two equal columns** below, stacking to one column on narrower screens (like the Overview):

- **Left: the Weight card**, the same as the Overview's:
  - the eyebrow "WEIGHT", with the grey goal pill on the right ("Goal 75 kg · around 9 Dec") once a goal is set
  - the same chart: weight line, expected range, goal line, projection, dose bars on the right mg scale, and consult dots under the axis
  - the same key below it
  - With no goal set: "**Set a goal weight in Summary data on the Overview to see the projection.**" ("Overview" is a link.)
  - With one check-in: "**One check-in recorded. A trend line appears after the next check-in.**"
- **Right: the Check-in history card**:
  - the eyebrow "CHECK-IN HISTORY", with "Newest first" on the right
  - a table with small uppercase headings: **Date · Weight · Check-in note**
  - each row shows the date ("18 Aug 2026"), the weight in bold ("84.60 kg"), and the note (or "—") with "Recorded by …" in grey below
  - notes longer than 120 characters fold into "View note"
  - 10 rows per page, with "Newer check-ins" / "Page 1 of 2" / "Older check-ins" below the table
  - the footnote: "Recorded measurements only; the chart line connects check-ins, not daily measurements."

**Consult dots** open the same Consult note popup as on the Overview (§B8.8). Here, **Show in log** goes to the Overview and highlights that entry in the Clinical log.

**With no weights:** one card, the eyebrow "WEIGHT" with "0 check-ins" on the right, and "There are no weight entries in this record yet. The chart will appear after the first check-in."

**Practice administrators** (§B15) see the same layout, but the chart shows only the weight line and the expected range. They see no goal pill, projection, dose bars or consult dots, and the key lists only Weight and Expected range.

### B9. Patient record: Messages tab (P1)

**Mock refs:** reuse the §B5 component; `emrThreads()`, `emrThread`.

**A "Messages" card** with a header row:

- On the left, the eyebrow "Messages".
- On the right:
  - a search box "**Search these conversations**"
  - a primary **Start a new conversation** button
  - for prescribers, a secondary **Request blood tests** button that jumps to the card on the Overview tab

**Below the header:**

- **The two-pane inbox,** limited to this patient's conversations that you are in.
- **Row line 2** shows "With …" **only** when other clinicians are in it, because the patient is implied.
- **The open conversation** is like §B5, but has **no** "Open Nadia's record" link and no "With …" line.
- **Empty pane:** "**Choose a conversation**", "Select a conversation to read it. New messages stay unread until you open them.", and **Start a new conversation**.
- **Starting a conversation:**
  - The pane shows "New conversation" and "Write below to start a new conversation with Nadia."
  - Below that, a one-line box "**Write to Nadia to start a conversation…**" and **Send**.
  - There is no subject field. The subject is automatic: "**Message from Dr Marchant**" for clinicians, "**Message from the practice**" for others.
- **Before any conversation exists,** the card shows only "**No messages with Nadia yet.**" and the box.
- **No login:** "Nadia doesn't have a login yet, so they can't read messages."

**Below the Messages card,** a second panel, "**Team conversations about this patient**":

- note: "Only you and the colleagues in each conversation can see these. The patient never does."
- a list of team threads ("Subject · you, Lindiwe Mahlangu · 1 unread")
- a secondary button **Message a colleague about this patient**, which opens Team messages in compose with the patient filled in

### B10. Prescription dialogs (P1)

**Mock refs:** `openRenewRx()`, `confirmRenewRx()`, `openChangeRx()`, `confirmChangeRx()`, `rxOptions()`.

**Renew dialog**

- Title: "Renew prescription". Sub: "Nadia Mokoena · Semaglutide 1.0 mg weekly".
- "**Renew for**" chips from 1 to 6 months (6 chosen by default).
- Rows: Current expiry, and New expiry (teal).
- The **safety-flag box** (below).
- Buttons: **Cancel** and **Renew prescription**. Replace "Renew and create script".
- Toast: "**Prescription renewed to 24 Apr 2027. It is written to the clinical log under your name.**"
- Log line: "Prescription renewed for 6 months: Semaglutide 1.0 mg weekly, valid to 24 Apr 2027." This is already in the mock.

**Change / Create dialog**

- Title: "Change prescription" or "Create prescription". Sub: "Nadia Mokoena · currently Semaglutide 1.0 mg weekly".
- Fields: **Medicine** select, **Dose** select (options "0.5 mg weekly"), **Valid for** chips from 1 to 6 months, **Reason** "(optional, goes in the clinical log)" with placeholder "e.g. Side effects – nausea, vomiting", and a "**New prescription**" teal preview row.
- Help text: "**Listed from the main treatments in the practice catalogue.** The current prescription is replaced from today." Replace "Listed from this patient's authorised medications in My Medications".
- The **safety-flag box** (below).
- Buttons: **Cancel** and **Save prescription**. Replace "Create script".
- Picking the current dose shows the error "**That is the current prescription. Use Renew to extend it.**"
- Success toast: "**Prescription saved: Semaglutide 0.5 mg weekly to 6 Apr 2027. It is written to the clinical log under your name.**"

**Safety-flag box** (both dialogs; shown only when any summary flag is Yes):

- A rose box headed "**Safety flag**", listing each flag as "History of pancreatitis: Yes".
- A **required** checkbox: "**I have reviewed this and still want to prescribe.**" Saving without the tick is refused with an inline error.
- To demo it, set one flag to Yes in Riaan Botha's sample summary.

### B11. Request blood tests: the PathCare preview (P1)

**Mock refs:** `previewBloods()`, `pcSummary()`, `PC_MEDAID`, `PC_DOCS`, `sendBloods()`.

- **Keep the form picture with yellow overlays** (mock only). In the app the dialog shows the real filled PDF in a frame with a link "**Open the filled form in a new tab**".
- Dialog title "**Request blood tests**", sub "**Nadia Mokoena · PathCare clinical request form**". The form sits on the left; the list of values sits on the right on wide screens.
- **List rows:**
  - Referring doctor
  - PathCare code
  - Patient ID
  - Surname
  - First name(s)
  - Title
  - Postal address (the shipping address)
  - **Medical aid no. → "Left blank"** in muted text. Set `PC_MEDAID = ''` and stop printing "NOT".
  - Tests
- **Missing values** show "**Missing**" in rose. With any missing, add "A missing value leaves that box blank. You can still send the form."
- **Always show** "**The medical aid number isn't stored. You or the patient can write it on the form.**"
- Buttons: **Cancel** and **Email to nadia.m@example.co.za**. With no email on file, show "**This patient has no email address on file.**" in place of the send button.
- **Success toast:** "**Blood test form emailed to nadia.m@example.co.za. It is in the clinical log.**"
- **Error toasts** (show one, so the demo has an error case):
  - "Something went wrong making the blood test form, so nothing was sent. Try again in a moment."
  - "The blood test form is in the clinical log, but the email to … couldn't be sent. Use Resend in the log to try again."
- After sending, the new log entry shows "Emailed to …" and **Resend to patient** (§B8).

### B12. Calendar & Bookings (P1)

**Mock refs:** `#a-schedule`, `renderSchedule()`, `renderMonth()`, `dayState()`, `dayRows()`, `#sched-dayinfo`, `.cal-*`, `#sched-alert`, `rescheduleOne()`, `rescheduleAll()`, `clearTimeOff()`, `#sched-open-card`, `#sched-moved-card`, `#sched-who`.

**Header**

- Heading "**Calendar & Bookings**". Drop the "Who is working, and what is booked" / "Your calendar and bookings" headings and the "Clinical ·" breadcrumb prefix.
- Buttons at the top right: primary **Book appointment**, and secondary **Next appointment day: 3 Sep** (to the next date with bookings).
- The tabs **Calendar & Bookings** / **Working Pattern & Availability** stay.

**No count cards.** The four Booked · Completed · Cancelled · No show cards were removed (7 Oct); the day card's header keeps "1 booked".

**Admins:** a "**View schedule**" card replaces `#sched-who`. It has the sub "Showing Dr Sam Marchant's appointments." (or "Showing appointments for all clinicians."), the fields **Date** and **Whose diary** (with an "All clinicians" option), and a **View schedule** button. **Doctors don't get this card.**

**Clash banner** (`#sched-alert`):

- A rose "**Action needed**" pill and a heading.
- Heading for admins: "2 appointments sitting on a day this doctor is now unavailable" or "… sitting on time outside this day's special hours".
- Heading for doctors: "… sitting on a day you are now unavailable" or "… time outside your special hours".
- Body for admins: "Nobody is moved automatically. Agree a new time with each patient, then cancel and rebook the appointment. Nothing changes for the patient until then."
- Body for doctors: "The practice has been notified. Nobody is moved automatically: you or the practice can offer each patient a new time, and nothing changes until the patient confirms."
- **The only button is "Undo the change"**, which toasts "**Undone. The diary stands as it was before the change.**" **Remove "Offer all of them a new time".**

**Bookings card header**

- On the left: "‹ **Booked · Thursday 27 August** ›" (one day per step), or "‹ **August 2026** ›" (one month per step).
- On the right: the count ("5 booked" / "Nothing booked" / "21 booked this month"), a **Today** link shown only when you are not on today, and the **Day | Month** toggle.

**Day view**

- **Day-state pill** above the table:
  - rose "**Leave**"
  - blue "**[icon] Special hours 08:00 – 10:00**"
  - grey "**Not a working day**"
- **Table columns:**
  - **Time**
  - **Patient** (link to the record)
  - **Type**, e.g. "Follow-up · 15 min"
  - **Clinician** (admins only)
  - **Status:** a status pill such as "Booked", or rose "**Doctor unavailable**" on leave, or amber "**Outside special hours**"
  - **Room:** a secondary **Join** button (opens the mock video room), or "Not open", or "—"
  - Row actions, for doctors too: **Appointment details**, **Offer a new time** and **Open record**
- **Empty table:** "Nothing booked on this day." or "Not a working day."

**Month view** (`renderMonth()`):

- **Each cell:**
  - The **date number at the top left**. Clicking it opens that day in Day view.
  - **The day's tag at the top right, opposite the date:**
    - Leave days: "**Leave**" in rose text, and the whole cell is tinted rose (`--rose-l` / `--rose-b`).
    - Special days: a blue tag with the **special-hours icon** and the hours, e.g. "**[icon] 08:00–10:00**" (`--blue-l` background, `--blue` text).
  - Under that, **one chip per booking**: "**08:00 Sipho Ndlovu**", with the time bold and a teal-l chip. Long names are cut with "…". Clicking a chip opens the appointment details.
  - Cancelled bookings aren't shown.
  - A working day with nothing booked shows "—".
  - Not-working days are greyed out with no dash.
  - Days outside the month are blank.
  - Today has a teal outline.
- **Phones:** chips show the time only, and the tag is hidden.
- **Key** (shown only when one doctor's diary is shown): "Working day · **[icon] Special hours** · **Leave** · Not working". The special-hours entry uses the icon in place of a swatch.
- **Replace the mock's "N booked" count** in each cell with the chips.

**Special-hours icon** (use it exactly):

- It has `stroke: currentColor`, `fill: none`, `stroke-width: 2`, round caps and joins, and size `1.2em`. The star is filled.
- Use it in four places: the month tag, the Day view pill, the key, and the "Special days" card heading on the next tab (coloured `#1072aa`).

```html
<svg class="special-hours-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><circle cx="9.5" cy="14.5" r="7.5"/><path d="M9.5 10.5v4l2.6 1.6"/><path class="special-hours-icon__star" d="M19.2 0.7 20.2 3.4 23.1 3.5 20.9 5.3 21.6 8.1 19.2 6.5 16.8 8.1 17.5 5.3 15.3 3.5 18.2 3.4Z"/></svg>
```
```css
.special-hours-icon{fill:none;flex:0 0 auto;height:1.2em;width:1.2em;stroke:currentColor;stroke-linecap:round;stroke-linejoin:round;stroke-width:2}
.special-hours-icon__star{fill:currentColor;stroke-width:1}
```

**Below the bookings card**

- **Available time** (admins only; replaces `#sched-open-card`):
  - Sub: "Open availability for the selected day, after appointments and leave."
  - A list of "**08:30–09:00 · Dr Sam Marchant**". The rows are not clickable.
  - Empty: "No available times for this date."
  - Note: "Where a working pattern is saved, these are 30-minute consultation windows with start times every 15 minutes. Up to 100 available windows are shown. These times are not bookings or reservations."
- **Appointments needing attention** (all roles; **replaces `#sched-moved-card`**, "What happens when a time is moved"):
  - Sub: "Future bookings at Meridian Health outside working hours, outside special hours or on leave."
  - A count pill "**2 affected**".
  - Rows: the patient in bold; "27 Aug 2026, 10:30 SAST · Follow-up"; the reason "**On leave**", "**Outside special hours**" or "**Outside current working hours**"; and a secondary **Review appointment** button.
  - Empty: "No future bookings in this view need attention because of working hours, special hours or leave."
  - Note: "Existing appointments stay booked. Agree a new time with the patient before changing an appointment."

**Rename every visible "Day off" / "Days off" to "Leave".** Code names (`timeOff`, `addTimeOff`, `#off-from` and so on) can stay.

### B12b. Calendar & Bookings → Working Pattern & Availability (P1)

**Mock refs:** `#sched-pane-wp`, `renderWorkingPattern()`, `saveWorkingPattern()`, `addSpecialDay()`, `removeSpecialDay()`, `addTimeOff()`, `removeTimeOff()`, `#sp-list`, `#off-list`.

**Two columns:** Working pattern on the left; **Special days** above **Leave** on the right. This order is already in the mock.

**Working pattern card**

- Keep the card. Under the title, add the description: "The standing week at this practice, in South Africa time (SAST). Saving these hours changes future booking availability, never an appointment already booked."
- Save toast: "**Your weekly working hours have been saved. Existing appointments have not been changed.**"
- **Admins see it read-only:** a list of "Monday 08:00–16:00 / Not working" and the line "**Read-only view. Doctors manage their own working hours.**"

**Special days card**

- Heading: "**[icon] Special days**". Sub: "Different hours on a specific date. These replace your working pattern for that day only." (or "this doctor's" for admins).
- **The list comes first,** soonest first. Each row reads "**Thu 27 Aug 2026**" over "08:00 – 10:00", with "· overridden by leave" when relevant, and a secondary **Remove** button.
- Empty: "No special days coming up."
- **Below the list, a dashed full-width button "+ Add special day".** It opens the form inline: **Date** (prefilled with the selected calendar date), **From** and **To** (15-minute steps), then **Set special hours** (primary) and **Cancel**.
- Toasts:
  - "**Special hours set for Thu 27 Aug 2026. They replace the working pattern that day.**"
  - "**Special hours removed. The working pattern applies again.**"

**Leave card** (replaces "Days off")

- Heading: "**Leave**", with no description.
- **List first,** soonest first. Each row is "**Thu 27 Aug 2026**" or "**Thu 27 Aug 2026 – Fri 28 Aug 2026**", with a **Remove** button.
- Empty: "**No leave coming up.**"
- **A dashed "+ Add leave" button** opens **From** and **To** date fields, then **Mark unavailable** and **Cancel**.
- Toasts:
  - "**Marked unavailable. Those days have left the booking calendar; existing appointments have not been moved.**"
  - "**Leave removed. The working pattern applies again; its history is kept.**"
- Form errors to use where the mock validates:
  - "Leave must include today or a later date."
  - "Add at most one year of leave at a time."

**"All clinicians" selected** (admins): one card, "**Working pattern, leave and special days**", reading "Choose a clinician in the schedule filter to see their working pattern, leave and special days at this practice." and "Doctors set their own working pattern. Practice administrators and Super Admins can add or remove leave and special days for the doctor they choose."

**Who can do what:**

- Doctors edit their own working pattern, special days and leave.
- **Admins and Super Admins can add and remove leave and special days** for the chosen doctor, but **not** the working pattern.

**When a doctor's own change clashes** with bookings, nobody is moved. The practice gets a high-priority, unassigned task in **Tasks**, for example: "**Offer new times: 2 bookings clash with Dr Sam Marchant's leave on 27 Aug 2026**". Its description says "Nobody has been moved. Agree a new time with each patient, then cancel and rebook." Optionally add this task to the admin's task list in the mock after a doctor adds leave over Thursday.

### B13. Staff My profile (P1 for doctors, P2 for the rest)

**Mock refs:** `#a-me`, `renderMe()`, `ME_EXTRA`, `#me-details`, `#me-mtd`.

**Breadcrumb and header:** "My profile". Then the identity card: avatar initials, full name, email, and chips for role, practice and "Active". This is the same as the mock.

**Your details card**

- The "**Read-only**" chip shows **only for non-clinicians**.
- Rows, in order:
  - **Title** (new, above First name)
  - First name
  - Last name
  - Email (your sign-in)
  - Contact number
  - for clinicians: **HPCSA registration number** (read-only)
  - for clinicians: three **pen-editable** rows: **How you sign consult notes** ("S. Marchant, MBChB"), **PathCare name** ("MARCHANT,DR S") and **PathCare code** ("MARSA0")
- The pen opens a text box inline with **Save** / **Cancel**. Save toasts "**Your details have been updated.**"
- Note for clinicians: "Use the pen to update how you sign consult notes or your PathCare details. Everything else is maintained centrally: if it's wrong, let the practice know and it will be corrected for you."
- Note for others: "These details are maintained centrally. If anything here is wrong, let the practice know and it will be corrected for you."
- **Keep `PC_DOCS` in step:** editing the PathCare name or code should update what the PathCare preview prints.

**Security card**

- **Password row:**
  - "**Last changed 14 Jun 2026, 10:12**" (or "Not changed yet")
  - "**Last reset with Forgot password 2 Sep 2026, 08:40**" (or "Never reset with Forgot password")
  - "**Last signed in 6 Oct 2026, 07:55**"
  - a **Change** button
- **Active sessions row:** "You stay signed in on this device." with a **Sign out others** button. It toasts "**Signed out of every other device. This one stays signed in.**"
- **Two-factor authentication row**, between Password and Active sessions: just the title, with a plain grey chip "**Not set up yet**" on the right where the mock has "On". Remove the line "Required on every staff account, so this cannot be switched off". Two-factor isn't built yet (planned: an emailed code, required for staff, with "trust this browser for 30 days").

**Your month to date card** (clinicians only)

- The chip reads "**Provisional**" (as in the mock).
- Rows: **First consultations**, **Review consultations**, **Messages answered**, then **Provisional total** with the amount on the right in bold teal, e.g. "**R14 350.00**" (space between thousands, two decimals).
- The total is this month's activity at the rates on the doctor's latest approved statement. With no approved statement, the row reads "**No rates on file yet**".
- **No explanatory paragraph** under the card (Dr Czech struck out "Your own statement, which is a payslip…").
- In the identity card at the top, the role chips are just **Doctor** and **Active**. **Remove the practice tag** ("Meridian Health").

### B14. Users and roles (Super Admin) (P2)

**Mock refs:** `#a-users`, `#user-form`, `newUser()`, `editUser()`, `saveUser()`, `USERS`.

**Add-user form:** add a **Title** select (Mr, Mrs, Ms, Miss, **Dr** default, Prof) labelled "**Title (new accounts only)**".

**Edit-user form** gains:

- **Title**, **First name**, **Last name**
- **Contact number**
- **HPCSA registration number**, with help "Clinicians only."
- **How consult notes are signed**
- **PathCare referring name** and **PathCare doctor code**, with help "Doctors only. Needed to request blood tests."
- The page explains that the Super Admin maintains these and the person sees them on My profile.

**Mock-only:** the financial-permission picks can stay.

### B15. A practice administrator opening a patient record (P2)

**Mock refs:** `renderEMR()` under `adminRole==='manager'`.

The admin doesn't get the clinical Overview, so show the **administrative Overview** for this role:

- **Four link cards** with counts: Appointments, Open tasks, Conversations, Deliveries.
- A **"Patient details"** box with these rows:
  - Record number
  - **Title** (with pen)
  - Date of birth
  - Contact number
  - Location
  - **Assigned clinician** (with pen)
  - Practice
- The note "Choose a patient section above. Each section shows only this patient's records at this practice; clinical History can include other permitted practices when a care purpose is selected."

**Not shown to admins:**

- the prescription pill
- **Export record**
- the quick-facts row
- the Consultations, Blood tests, Notes and Treatment tabs

**On the admin's Messages tab:** the automatic subject is "Message from the practice", and there is no Request blood tests button.

**On the admin's Weight tab:** the weights and expected range only (§B8b).

---

## C. Patient web app (P3)

### C1. Sign in

**Mock refs:** `#p-signin`, `signIn()`, `renderPublicNav()`.

- **No header bar on this page.** The Meridian logo sits centred above a **single card**. The "Trouble signing in?" side card and the two-column layout go.
- **Inside the card:**
  - A round **back-arrow** button ("Back to home") beside the eyebrow "**Secure sign in**".
  - Heading "**Welcome back**", then "Use the email address associated with your Meridian account."
  - **Email address** field (placeholder "Enter your email") and **Password** field (placeholder "Enter your password").
  - The checkbox "**Keep me signed in on this device**".
  - The primary button **Sign in**, full width, with a full-width secondary **Forgot password** button under it.
  - The help line: "Need help accessing your account? Please contact your practice administrator or email Meridian support."
  - At the bottom: "**Not a patient yet? Start with the questionnaire below.**" and a full-width secondary **Get started** button (goes to the questionnaire).
- **Remove "Mock sign-in — any details will let you through."** or keep it as a tiny note; James's choice.
- **The console sign-in** (`#admin-login`) is the same page in the app; staff and patients share it. Keep the mock's role picker, but add the **Forgot password** button there too.

### C2. New: Forgot-password flow

**Mock refs:** add `p-reset`, `p-reset-done`, `p-reset-new` and `p-reset-complete` views, or one view with steps.

All four are single cards in the sign-in style:

1. **"Forgot password"** (eyebrow "Password reset"):
   - "Enter the email address you sign in with. If it belongs to an account, we'll email you a link to choose a new password. The link works once, for one hour."
   - an **Email address** field
   - **Email me a reset link** (primary) and **Back to sign in** (secondary)
2. **"Check your email":** the link works once for one hour, check spam. Button **Back to sign in**.
3. **"Choose a new password"** (from the emailed link):
   - "This signs you out everywhere else you're signed in."
   - two new-password fields, with the password rules under the first
   - **Save new password**
   - An expired link shows "**This link has expired**" with **Request a new link** and **Back to sign in**.
4. **"Password updated":** "Your new password is saved. Sign in with it to continue." Button **Sign in**.

### C3. Screening questionnaire

**Mock refs:** `#p-screen`, `screenSubmit()`.

- **Step line:** a round **back-arrow** button ("Back to home") beside "**Step 1 of 3 · Screening · Stage 1 of 2**" (capital S on "Stage").
- **"Who you are" card:** add an optional **Title** select ("Choose a title", Mr, Mrs, Ms, Miss, Dr, Prof) **on its own row, half width**, right after First name and Last name. The field order becomes:
  - First name + Last name
  - Title
  - Email address + **Mobile number** (rename the "Contact number" label)
  - ID / passport number
- **Carry the title** into the lead and on to the patient record. It is printed on the PathCare form.

### C4. My Messages

**Mock refs:** `#p-inbox`, `renderPatientInbox()`, `pickPatientThread()`, `composeMsg()`, `suggestSubject()`, `sendNewThread()`, `.msg-item`, `hasUnread()`.

**Toolbar:**

- **Start a new message** on the left.
- The count "**2 conversations**" (or "**1 conversation matching "bloods"**" while searching).
- A **search box at the far right**: magnifier, placeholder "**Search your messages**", × to clear. It filters as you type and matches the subject, message text, sender and clinicians' names.
- No match: "**No conversations match "…"**".

**Thread rows:**

- Line 1: the subject, with a **red count badge** (1–9, then "9+") at the right of the subject. This **replaces the red dot**, and the subject is bold when unread.
- Line 2: "**With Dr Sam Marchant**".
- Line 3: **the same layout as the console rows (§B5)**:
  - On the left, the latest message, cut with "…", prefixed with a bold **"You:"** when you sent it, and a "Closed" pill when closed.
  - On the right, the **time if today** ("08:12"), otherwise the **date** ("3 Sep"; with the year if not this year). The full date and time shows on hover.
  - There is no separate sender/time line.
- **Remove the amber "Waiting for a reply" pill.**

**Compose (New message):**

- Heading "**New message**". Sub: "**Only you and the person you choose can read this conversation · kept in your medical record**".
- **To** is now a **select**, in this order:
  1. the assigned clinician first: "**Dr Sam Marchant (your clinician)**"
  2. any clinicians assigned before (no note), e.g. "Dr N Pillay"
  3. the practice's Practice administrators: "**Lindiwe Mahlangu (practice administrator)**"
- Super Admins appear only if they are a doctor assigned to the patient, now or before. Other clinicians never appear.
- **Subject:** placeholder "**What can we help with?**", with chips **Side effects · My dose · My delivery · My appointment · Something else**. Change "Billing" to "My appointment".
- **Message**, then **Send** / **Cancel**.
- A patient with no assigned clinician sees "You can message a clinician once Meridian Health assigns one to you."
- **Optional:** the mock's "suggested subject" logic and its "why" line aren't in the app. The app pre-fills a subject only when a link passes one. Keep it or drop it; James's call.

**An open thread:**

- Header: the subject, then "**With Dr Sam Marchant · kept in your medical record · times in SAST**". When someone was added: "Dr Sam Marchant added Dr N Pillay · 5 Oct, 09:10".
- Bubbles: yours are labelled "You"; others show the sender's name; the timestamp is "3 Sep 2026, 08:12".
- **Reply:** the footer reads "Your reply goes into your medical record, and your doctor is asked to respond." with **Send**.

**Nothing selected:** "**Choose a conversation**", "Select a conversation to read it. New replies stay unread until you open them.", and **Start a new message**.

**Sending** toasts "Your message has been sent to …".

**Add a sample thread** between Nadia and Lindiwe (practice administrator) so the To list and the admin inbox (§B5) agree. For example: "Invoice for my medical aid".

### C5. Account settings

**Mock refs:** `#p-account`, `renderAccount()`, `#ac-details`, `#ac-pay`.

**Heading row:** "Account settings", with secondary buttons **Privacy and preferences** and **Change password** at the right.

**Card 1, "Personal details"** (read-only):

- Name, Email, ID / passport, Date of birth, Practice, **Your clinician** ("Dr Sam Marchant"), Record number.
- A full-width **Request a change** button, and the note "Changes to your name, ID or date of birth go through your care team, so your record stays matched to your ID."

**Card 2 (new), "Title and contact details":**

- Sub: "Update these yourself. Changes apply only to your record at Meridian Health."
- Fields:
  - **Title**: select (No title, Mr, Mrs, Ms, Miss, Dr, Prof), help "**Printed on your blood test forms.**"
  - **Contact number**
  - **City**
- A primary **Save details** button. It toasts "**Your details have been saved for this practice.**"

**Card 3, "Recorded consents":** one row per consent ("Terms of service · Version 2026-04-01 · 12 Apr 2026, 20:12 SAST", with an "Accepted" pill).

**Payment methods** aren't on this page in the app (see §D). Ask James whether to keep the mock's card.

### C6. Blood tests: the PathCare form for the patient

**Mock refs:** `TASKS` (`id:'bloods'`), `doneTask('bloods')`. Add a `p-labs` view and a `p-lab` view.

**My Treatment task "Get your blood tests done":**

- Body: "Dr Sam Marchant asked for HbA1c, U&E, LFT and lipids on 2 September. When you have the report, upload it so your doctor can review it."
- **One button: Upload your results.** It opens the request page.
- This replaces "Download request form" and "Blood tests done ✓".

**Request page** ("**Your blood-test request**"):

- Breadcrumb "My Treatment › Blood tests › Request", and the button **All blood-test requests**.
- A card "**Your PathCare form**":
  - "Take it, printed or on your phone, to any PathCare collection point. We also emailed it to you on 2 Sep 2026."
  - buttons **Open form** (new tab; in the mock, open the same form image) and **Download PDF**
- "Once your results are ready, upload the report here for your doctor to review. Check that the report is yours and matches this request."
- A file field with help "One PDF report, no larger than 5 MB. You cannot replace it after uploading.", and **Upload report**.
- **After uploading, the PathCare form card disappears.** The form is withdrawn once results are in.

**List page** ("**Your blood tests**", breadcrumb "My Treatment › Blood tests"):

- Lead: "See the tests your doctor has requested at Meridian Health, get the PathCare form to take to your blood test, and upload your report when it is ready."
- A table: Panel / tests, Requested by, Due, Status ("Requested"), and Action (**Open request** plus **Download form** while the form is available).

**The email** the patient gets with the form now adds the line "You can also open it in the Meridian app under Blood tests until you upload your results." Optional: show it in a mock email box.

### C7. Indirect changes to Home and My Treatment (optional)

- When the doctor renews or changes the prescription in the console, the patient's Home pills, "Your medicine" and "Authorisation expires" update to match. A changed medicine or dose puts undispatched deliveries on "Held".
- New "updates" entries appear: "Treatment authorisation renewed", "Treatment authorised" and "Laboratory request created".

---

## D. In the mock but not in the app (ask James: keep, flag, or remove)

None of these were found in the code. Recommendation for a progress demo: keep them but add a small grey "Not built yet" chip, or hide them.

**Console:**

- My tasks: the "**Today's consults**" card. The app's equivalent is the **Join** button in Calendar & Bookings' Room column.
- My tasks: the "**Request blood tests**" panel card. The app requests bloods from the patient record only.
- My tasks: "**Scripts to the compounder**" and the "**Patient messages**" link card.
- Patient messages: the Super Admin "**Monthly message report**" download.
- My profile: the 2FA row and the earnings total (already removed in §B13).
- These Operations / Insight / Setup screens don't exist in the app under those names:
  - **Failed payments**
  - **Doctor payout run** (and its rate editor)
  - **Email journeys**
  - **Practice profile**
  - the **Medications catalogue** with blood-test schedules (the app's Operations → Catalogue is simpler, but it does now have "**How often a dose is taken**": Weekly / Daily / As directed)
- The app's admin sections are listed in §B1.

**Patient app:**

- Account settings: **Payment methods** (DebiCheck debit order, card update).
- The My Messages suggested-subject rules ("Consult on DD.MM.YY" with a "why" line).
- Blood tests: the "Blood tests done ✓" self-report button.

## E. Sample-data changes

- **Lindiwe Mahlangu** becomes a message recipient for patients:
  - add a Nadia ↔ Lindiwe thread ("Invoice for my medical aid")
  - move Thabo's "Debit came off twice" to Lindiwe
- **Team threads** for Dr Marchant: see §B6.
- **Riaan Botha's summary:** set "History of pancreatitis: Yes" so the safety-flag pill and the prescription tick can be demoed.
- **Titles** (`PT_EXTRA`) feed the record quick-facts row and the PathCare preview.
- **Doctor's own patients:** Dr Marchant's are Nadia, Riaan, Lerato, Carla and Sipho. The doctor's All patients, Overview Patients table and New patient message list show only these.
- **Calendar demo:** give Dr Marchant one Leave entry and one Special day in August. The month view then shows the rose "Leave" cell and the blue "[icon] 08:00–10:00" tag, and Thursday 27 Aug shows the clash banner.
- **Password dates** on My profile: "Last changed 14 Jun 2026, 10:12" and "Last reset with Forgot password 2 Sep 2026, 08:40".

## F. Check when done

- **Dark mode:** toggle it on every changed console and patient page. The new pills, chips, badges, bubbles and toasts must stay readable.
- **Phone widths (about 390px):**
  - the record tabs become one dropdown
  - month chips show times only
  - the message panes stack, with the list above the pane
  - toasts stay within the screen
- **Roles:** sign in as Doctor, Practice Administrator and Super Admin, and check:
  - the menu
  - the record Overview (clinical for Doctor and Super Admin; administrative for the admin)
  - who sees which threads
  - the calendar controls (filter card, Clinician column and Available time for admins only)
- **No leftover old wording:** search the file for "Day off", "Days off", "create script", "Create script", "Waiting for a reply", "So far this month" and "cannot be switched off". None should remain in visible text.
