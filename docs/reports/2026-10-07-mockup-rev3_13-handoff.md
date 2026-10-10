# Meridian Health: Mockup Rev 3.13 handoff (code → mockup), 7 Oct 2026

**For:** the Claude chat that maintains the interactive mockup HTML.

**Input file:** the latest mockup, Rev 3.12 (built from the 6 Oct handoff). If you only have Rev 3.11, apply the 6 Oct handoff first.

**Output file:** `meridian-health-app-mock-rev3_13__07_10_2026.html`. Change `<title>` to "Meridian Health — System Mock (Rev 3.13)" and mark new CSS/JS blocks with `/* Rev 3.13: … */` comments.

**Why:** On 7 Oct the app changed again, mostly from Dr Czech's own edits to the mockup. This document lists only those changes, so the mockup matches the app again.

## Ground rules

- **The app wins.** Quoted text is the exact wording used in the app; reuse it.
- Keep every mock device from the 6 Oct handoff: sample data, role picker, toasts, the dark-mode toggle (put new colours on the existing tokens).
- **"Mock refs"** name the functions and IDs from the Rev 3.11/3.12 file where each change lands. If a name has drifted, find the equivalent.
- If something below is already in your mockup, check the wording and move on.

## At a glance

1. **Roboto everywhere:** no serif headings anywhere (§1).
2. **Patient record → Weight tab:** three summary cards, then the Overview's weight chart on the left and the check-in history on the right (§2).
3. **Calendar & Bookings, day view:** the four count cards are gone (§3).
4. **Staff My profile:**
   - no practice tag
   - a **Two-factor authentication** row reading "Not set up yet"
   - month to date is **Provisional**, with a **Provisional total** row (§4)
5. **An open patient conversation**, on the Patient messages tab and the record's Messages tab:
   - a **Patient notes** accordion under the reply box
   - **Appointment suggestions** only when there is one (§5)
6. **Patient messages and Team messages fill the window to the bottom on desktop.** Opening Patient notes doesn't shrink the conversation (§6).
7. **Patient app, My Messages:** each row's last line uses the console layout (§7; skip it if it's already done from the updated §C4).

---

## 1. Type: Roboto everywhere

**Mock refs:** the root CSS (font variables, `body`, `h1`/`h2` rules), and any view with its own heading font.

Dr Czech's brand guidance: the site font is **Roboto**. **Quicksand** is the logo's font, and the logo is an image, so nothing else uses it.

- Set every surface to Roboto: the practice console, the patient app, sign-in, the video room and print sheets. Use the stack `'Roboto', -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif`. The mock can load Roboto from Google Fonts (weights 400, 500, 600, 700); the app self-hosts it.
- **No serif anywhere.** Search the file for `Georgia`, `serif` (not `sans-serif`), `Fraunces` and `Inter`, and replace them.
- **Page headings** use Roboto at **weight 500** with `letter-spacing: -.02em`. That covers "Welcome, Sam.", "Patient Records & Messages", the patient's name on the record, "Calendar & Bookings", "My profile" and sign-in's "Welcome back". Keep their current sizes.
- Form fields and buttons use the page font as well.
- **Colours don't change.** The brand palette (`#48c1c9 #46afcf #1599de #29698b #0097b2`) is already the patient app's. The console keeps its own teal for now.

## 2. Patient record → Weight tab

**Mock refs:** the record's Weight tab under `renderEMR()`, and the Overview's Weight card chart (`#chart2`, `emrChart()`). This replaces §B8b of the 6 Oct handoff if your mockup follows that.

The tab no longer has its own simple line chart. It has two rows.

**Row 1: three summary cards** across the full width, white cards of equal width:

| Card | Big figure | Grey line below |
|---|---|---|
| Starting weight | "94.00 kg" | the first check-in's date, "24 Apr 2026" |
| Latest weight | "84.60 kg" | the latest check-in's date, "18 Aug 2026" |
| Change | "-9.40 kg" ("+" when it went up) | "Since first check-in · 5 check-ins" |

The number is large and bold; "kg" is smaller and lighter.

**Row 2: two equal columns.** Below about 1000px they stack into one column, chart first.

- **Left: the Weight card**, the same as the Overview's Weight card:
  - the eyebrow "**WEIGHT**", and on the right a grey pill "**Goal 75 kg · around 22 Nov**" once a goal is set
  - **the same chart as the Overview:**
    - the weight line with points
    - the expected range band
    - the dotted goal line labelled "Goal 75 kg"
    - the dashed projection to the goal
    - purple **dose bars** on a right-hand **mg** scale, labelled with the dose where it changes
    - dark **consult dots** on the time axis
    - month labels along the bottom
  - the same key:
    - Weight
    - Expected range, assuming 0.5 to 1 kg a week
    - Projected to goal
    - Dose shipped (mg, right scale)
    - Consultation — click to open the note
  - With no goal set: "**Set a goal weight in Summary data on the Overview to see the projection.**" ("Overview" is a link to the record's Overview tab.)
  - With one check-in: "**One check-in recorded. A trend line appears after the next check-in.**"
- **Right: the Check-in history card**:
  - the eyebrow "**CHECK-IN HISTORY**", with "**Newest first**" in grey on the right
  - a table with small uppercase grey headings **DATE · WEIGHT · CHECK-IN NOTE** and no shaded header band
  - each row has:
    - the date, "18 Aug 2026"
    - the weight in bold, "84.60 kg"
    - the note, or "—", with "**Recorded by Nadia Mokoena**" in small grey below
  - notes longer than about 120 characters fold into a "**View note**" toggle
  - **10 rows per page.** With more, a footer reads "**Newer check-ins**" · "**Page 1 of 2**" · "**Older check-ins**".
  - a grey footnote: "**Recorded measurements only; the chart line connects check-ins, not daily measurements.**"

**Consult dots** open the same **Consult note** popup as the Overview. It has:
- the eyebrow "Consult note", the patient's name, and "date · author"
- the note text
- the buttons **Show in log** and **Close**

On the Weight tab, **Show in log** goes to the record's **Overview** and briefly highlights that entry in the Clinical log.

**With no weights:** one card, the eyebrow "WEIGHT" with "**0 check-ins**" on the right, and "**There are no weight entries in this record yet. The chart will appear after the first check-in.**"

**Practice administrators** see the same layout, but the chart shows only the weight line, the expected range and the months. They see no goal pill, goal line, projection, dose bars or consult dots, and the key lists only Weight and Expected range.

## 3. Calendar & Bookings: no count cards on the day view

**Mock refs:** `#a-schedule`, `renderSchedule()`.

- **Remove** the four small cards above the day view: **Booked · Completed · Cancelled · No show**.
- The day card's header keeps its count, e.g. "**1 booked**", next to **Today** and the **Day / Month** switch.

## 4. Staff My profile

**Mock refs:** `#a-me`, `renderMe()`, `ME_EXTRA`, `#me-details`, `#me-mtd`.

**Identity card (top):** the chips under the name and email are just the role ("**Doctor**") and "**Active**". **Remove the practice tag** ("Meridian Health"); Dr Czech says it isn't needed.

**Security card:** three rows, in this order:

1. **Password**, unchanged:
   - "Last changed …" or "Not changed yet"
   - "Last reset with Forgot password …" or "Never reset with Forgot password"
   - "Last signed in …"
   - a **Change** button
2. **Two-factor authentication**, new. Show just the title, with a plain grey chip "**Not set up yet**" on the right.
   - **Don't show "On"**, and **don't show** "Required on every staff account, so this cannot be switched off" (Dr Czech struck that line out).
   - Two-factor isn't built yet. When it is, the plan is:
     - an emailed code at sign-in, required for all staff
     - "trust this browser for 30 days"
     - only a Super Admin can reset it
   - James is still to discuss it with Dr Czech, so don't mock the sign-in step.
3. **Active sessions**, unchanged: "You stay signed in on this device." with a **Sign out others** button.

**Your month to date card** (clinicians only):

- The chip on the right reads "**Provisional**" (not "So far this month").
- Rows:
  - **First consultations** 12
  - **Review consultations** 38
  - **Messages answered** 96
  - then a new last row, **Provisional total**, with the amount on the right: "**R14 350.00**"
- Style the total row's label darker and semi-bold, and the amount in bold teal at a slightly larger size. Use a space between thousands and always two decimals ("R14 350.00", "R300.00").
- The amount is this month's activity at the rates on the doctor's latest approved activity statement. With no approved statement, the amount reads "**No rates on file yet**" in grey.
- **No explanatory paragraph** under the card. Dr Czech struck out "Your own statement, which is a payslip…", so remove it if the mock has it.

## 5. An open patient conversation

**Mock refs:** the Patient messages tab (`#a-messages`, `renderStaffInbox()`, `openThread()`) and the record's Messages tab (`emrThreads()`, `emrThread`). Both use the same conversation pane, so change it once.

The pane's order, top to bottom:

1. subject and links
2. "With …"
3. the chat bubbles
4. appointment suggestions, only when there is one
5. the reply box
6. **Patient notes**

**5a. Appointment suggestions: only when there is one**

- By default the doctor's conversation has **no** "Appointment suggestions" block. Remove the empty "No appointment suggestions have been made in this conversation." block and the "**Suggest a different appointment time**" bar.
- The block appears only when the conversation has a suggestion, for example when the patient has suggested a new time. It then shows:
  - the heading "Appointment suggestions" and "Times shown in South Africa time (SAST)."
  - each suggestion with its status, the suggested and original times, and **Accept** / **Decline**
  - still no "Suggest a different appointment time" bar for staff, except after the calendar's "Offer a new time" (§9b)
- **Patients keep** "Suggest a different appointment time" in their My Messages conversation.
- To show it in the mock, give one sample thread a pending suggestion from the patient.

**5b. Patient notes accordion** (clinicians and the Super Admin only; **practice administrators never see it**)

The notes sit under the reply box, so the doctor can read them while replying without leaving the conversation.

- **Closed by default.** The summary row is a rounded, bordered box containing:
  - a small chevron (pointing right when closed, down when open)
  - "**Patient notes**" in semi-bold
  - a small teal count chip, e.g. "**6**"
  - on the right, in grey: "**Consult notes and general notes, newest first**"
- **Opened**, it shows a list, newest first. Each note has:
  - a grey meta line: "**Phone call** · 7 Oct 2026, 06:22 · Sam Marchant", with the note type in bold. Add "· Only visible to you" on your own private notes.
  - the note text
  - signed consult notes are labelled "**Consult note**" and end with an "**Open consultation**" link
  - general notes use their type: "Consultation note", "Review note", "Phone call" or "System note"
  - thin dividers between notes
- **Footer:** "On Nadia's record: **Notes** · **Consultations**", both links. When there are more than 100 notes, prefix it with "Showing the latest 100."
- **With no notes:** "No notes for Nadia yet."
- **Visibility:** another clinician's private notes and unsigned consult drafts never appear.
- **Sample data for Nadia (newest first):**
  - "Phone call · 7 Oct 2026, 06:22 · Sam Marchant — Phoned: nausea for two days after each dose, eating small meals. Advised to stay on 1.7 mg for now."
  - the consult notes the record's Clinical log already has (e.g. "Consult note · 21 Jul 2026 · Sam Marchant — Nausea settled. Increased dose to 1.0 mg weekly.")

## 6. Patient messages and Team messages fill the window (desktop)

**Mock refs:** the two-pane block on `#a-messages` and the Team messages view.

- On wide screens (about **1024px+ wide and 576px+ tall**), the two-pane block runs down to about **24px above the bottom of the window**, with a minimum height of about 480px. Narrower screens keep today's heights.
- The **conversation list** scrolls inside the left pane.
- The **chat bubbles** fill the right pane's free height and scroll inside it, with the reply box and the Patient notes row below.
- **Opening Patient notes must not shrink the bubbles.** The notes drop down below the reply box at their full length, and the right pane scrolls so the doctor can read them. Closing them returns the pane to its normal state.
- The record's Messages tab keeps its current height.

## 7. Patient app, My Messages: row layout

**Mock refs:** `#p-inbox`, `renderPatientInbox()`, `.msg-item`.

Skip this if your mockup already follows the updated §C4 of the 6 Oct handoff.

- Line 1: the subject, with the red count badge on the right.
- Line 2: "**With Sam Marchant**" in grey (kept for patients).
- Line 3, laid out like the console's conversation rows:
  - on the left, the latest message, prefixed with a bold "**You:**" when the patient sent it and a grey "Closed" pill on closed threads
  - on the right, the **time if today** ("06:24"), otherwise the date ("5 Oct")
- Remove the separate "sender · date, time" line.

## 8. Answers to the mockup's questions (7 Oct)

**8a. Notes tab = the Patient notes list.** The app now matches the mock: the record's **Notes** tab shows the same list as the accordion.
- Signed consult notes are labelled "**Consult note**" and end with an **Open consultation** link.
- General notes keep their type label ("Phone call", "Review note", …), their tags and, for your own notes, **Edit tags**.
- Everything is newest first, 20 per page.

**8b. Patient-side suggestions live in My Messages, reached from My Appointments.** Patients don't suggest from a bar they find on their own. The app's flow is:

1. **My Appointments:** an upcoming appointment has a secondary **Suggest a new time** button. A cancelled or no-show appointment shows **Ask to rebook** instead.
2. The button opens **My Messages** at the patient's conversation with that clinician. The patient's conversation has the **Appointment suggestions** block below the bubbles, and its "**Suggest a different appointment time**" panel is already open, with that appointment chosen.
3. The panel has three fields:
   - **Appointment**: a select
   - **Suggested date and time (SAST)**
   - **Message (optional)**, with the placeholder "Add a note about the suggested time…"
   - and a **Send suggestion** button
4. The suggestion then shows in that conversation with the chips "**Reschedule**" and "**Awaiting response**", with the suggested and original times. The patient can use **Withdraw suggestion**.
5. On the doctor's side (§5a) the same conversation now shows the Appointment suggestions block, with **Accept** / **Decline**.
6. Once the doctor answers, My Appointments shows the new time.

If there's no conversation with that clinician yet, the button reads "**Message Sam Marchant**" and starts a new message with the subject "My appointment".

So in the mock, wire My Appointments' **Suggest a new time** to open that patient's My Messages conversation with the suggest panel open. Patients always see the Appointment suggestions block in their own conversations. If nothing has been suggested, it says "No appointment suggestions have been made in this conversation."

---

## 9. Later on 7 Oct: further changes

### 9a. Calendar & Bookings: Week view
- **Switcher:** reads **Day · Week · Month** (same toggle).
- **Header:** "**Week 41 · 5 Oct 2026 – 11 Oct 2026**" (ISO week number, weeks start Monday). Beside it: ‹ › arrows, "N booked this week", and "This week" when today isn't in the week shown.
- **Layout:** seven day columns, Monday to Sunday.
  - Each column head is the weekday and date, and opens that day in Day view. Today is outlined, as in Month.
  - Day tags are as in Month: **Leave** (rose day) and special hours (clock-with-star icon and the hours).
  - Each booking card shows the time, patient and "type · duration". There's **no "Booked" pill**: a booking is booked unless it says otherwise, so only Completed, Cancelled and No show get a pill. Attention markers ("Doctor unavailable", "Outside special hours", "Needs rescheduling") show with an amber edge.
  - Cancelled and no-show bookings stay listed in a quieter style.
  - Empty days say "Nothing booked", or "Not working" on a non-working day.
- **Responsive:**
  - Between 40rem and 75rem wide, each day becomes a row, with the date on the left.
  - At 40rem or less, the days stack.
- The key (Working day, Special hours, Leave, Not working) sits below, as in Month.

### 9b. "Offer a new time" from the calendar
- A doctor's **Offer a new time** or **Discuss rebooking** on the calendar opens the patient's conversation with the **Suggest a different appointment time** panel open and that appointment chosen.
- Otherwise, staff see the suggestions block only when a conversation already has a suggestion (§5a).

### 9c. Who can open a patient record (Clinician role)
- **Who can open it:** a clinician opens a record only if the patient is assigned to them now or was before, or they're booked with the patient as cover. Super Admins and practice admins aren't limited.
- **Never assigned:** they're sent to **All patients** with an error toast: "**You can only open the records of patients who are, or were, assigned to you.**"
- **Covering a one-off appointment:** a clinician booked with a patient who isn't theirs can read the record and write consultation and general notes, but change nothing else. This lasts from the booking until 14 days after the appointment; a cancelled booking gives no access.
  - The same amber notice reads: "**You're covering Thandi's appointment on 9 Oct, so you can read this record and write notes, but not change anything else. Sam Marchant is Thandi's clinician.**"
  - "New consultation note" and the Notes form stay; every other edit control is hidden.
- **Assigned before, or cover that has ended:** they can read everything. A notice sits between the record header and the tabs on a pale amber background: "**You looked after Thandi before, so you can read this record but not change it. Sam Marchant is Thandi's clinician now.**"
  - Every way to change the record is hidden:
    - the Summary data pens
    - Renew/Change prescription and Request blood tests
    - New consultation note, the Notes form and Edit tags
    - the title and assigned-clinician editors
    - Start a new conversation
  - They can still read and reply in conversations they're already in.
- **Patient messages:** in a conversation the clinician is in but whose record they can't open, there's no "Open Thandi's record" link and no Patient notes.
- **All patients (Clinician role):** the list also includes past and covered patients, with a small pill after the name: "**Covering**" (blue) or "**Past patient**" (amber). The intro reads "Your patients: the people assigned to you, and those you looked after before or are covering."

### 9d. Smaller changes
- **New conversation subject:** uses the clinician's title from their profile, e.g. "Message from **Ms** Botha" for a dietitian. "Dr" only appears when that's their title.
- **Old list pages:** clinicians no longer reach the practice-wide Consultations, Blood tests, Authorisations and Compounding lists. Opening one by URL returns them to the Overview with a toast, e.g. "Blood tests are on each patient's record, under Blood tests."
- **Overview wording (doctor):**
  - Patients card: "**Patients assigned to you**", button "View patients", empty "**No patients are assigned to you yet.**"
  - Appointments card: "**Your booked appointments**", button "View calendar", empty "You have no upcoming appointments."
  - Recent conversations card: "**Your latest patient conversations**", button "**View patient messages**" (was "Open inbox").
  - Admins see "People currently receiving care" and "Booked appointments".
  - "in this practice" is gone everywhere.
- **Manage conversation:** "Back to conversation" returns to where you opened it, the record's Messages tab or Patient messages.
- **Paging:** the record's Messages tab now pages 20 conversations at a time, as Patient messages does. Both lists end with "**Newer** · 1–20 of 34 conversations · **Older**".
- **Added clinicians:** staff conversations now show "Dr Marchant added Ms Botha 5 Oct" (or "You added …") under the conversation title, as Team messages and the patient do.

---

## Don't change

- **Two-factor sign-in:** no code-entry screen and no "On" status anywhere (§4).
- **Console colours:** keep the console teal; only the type changes.
- **The public landing page and the Django admin backend:** leave them as they are for now.

## Check when done

- No serif font remains anywhere; headings are Roboto 500.
- **Weight tab:**
  - three cards, then chart left and history right; they stack on a narrow window
  - consult dots open the note
  - an admin sees no dose bars, dots or goal
- **Calendar day view:** no Booked/Completed/Cancelled/No show cards.
- **My profile:**
  - chips are "Doctor" and "Active" only
  - Security has the Password, Two-factor authentication ("Not set up yet") and Active sessions rows
  - month to date says "Provisional" and ends with "Provisional total R14 350.00"
- **Conversations:**
  - no suggestions block in a thread without a suggestion
  - a patient's pending suggestion shows Accept / Decline
  - Patient notes is closed by default, opens below the reply box and doesn't shrink the bubbles
  - an admin never sees Patient notes
- On a large window, Patient messages and Team messages reach the bottom of the window.
- **Calendar:** Day · Week · Month. Week starts Monday, with the "Week 41 · …" header, and the days stack on a phone.
- **Record (Clinician role):**
  - a past clinician sees the amber read-only notice and no edit controls
  - a doctor who was never assigned never sees the record
- Search the file for "So far this month", "Open inbox", "in this practice", "Required on every staff account" and "Meridian Health" chips on My profile. None should remain where this document removes them. "Suggest a different appointment time" shows on the staff side only after "Offer a new time" (§9b).
