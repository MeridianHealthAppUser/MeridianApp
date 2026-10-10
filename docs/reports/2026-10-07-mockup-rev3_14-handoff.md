# Meridian Health: Mockup Rev 3.14 handoff (code → mockup), 7 Oct 2026

**For:** the Claude chat that maintains the interactive mockup HTML.

**Input file:** the latest mockup, Rev 3.13 (built from the Rev 3.13 handoff, including its §8 answers). If you also applied §9 of the Rev 3.13 handoff, this file repeats that section with later changes. Where the two differ, this file wins.

**Output file:** `meridian-health-app-mock-rev3_14__07_10_2026.html`. Change `<title>` to "Meridian Health — System Mock (Rev 3.14)" and mark new CSS/JS blocks with `/* Rev 3.14: … */` comments.

**Why:** Later on 7 Oct the app changed again, from James's answers to a review of the practice console. This document lists every change since the Rev 3.13 handoff, so the mockup matches the app again.

## Ground rules

- **The app wins.** Quoted text is the exact wording used in the app; reuse it, including the curly apostrophes (’).
- Keep every mock device from earlier handoffs: sample data, role picker, toasts, the dark-mode toggle (put new colours on the existing tokens).
- **Single practice always.** Never show a practice name or "in this practice" in staff wording, unless a section below says so.
- **"Clinician role"** means the Doctor role in the role picker, which covers doctors and dietitians. Super Admins and practice admins aren't affected by §3.
- If something below is already in your mockup, check the wording and move on.

## At a glance

1. **Calendar & Bookings gets a Week view:** **Day · Week · Month** (§1).
2. **The calendar's "Offer a new time" opens the conversation with the suggestion form ready** (§2).
3. **Who can open a patient record (Clinician role):**
   - current patients can be changed
   - covered patients can be read and noted
   - past patients can only be read
   - anyone else's record won't open
   - All patients marks past and covered patients (§3)
4. **Overview wording** for doctors and admins, desktop and mobile (§4).
5. **Conversations:**
   - the subject uses the clinician's title
   - "X added Y" lines for staff
   - paging on the record's Messages tab
   - Back to conversation goes back where you came from (§5)
6. **Smaller changes:**
   - old list pages send clinicians away
   - two new toasts
   - an access-history label
   - the Summary data Cancel fix (§6)
7. **No visible change:** file storage, email and background-task settings (§7).

---

## 1. Calendar & Bookings: Week view

**Mock refs:** the Calendar & Bookings page and its Day/Month toggle.

**Switcher:** the existing toggle now reads **Day · Week · Month**, in that order.

**Header row (left to right):**
- **‹** and **›** arrows (labels "Previous week" / "Next week"). They move a week at a time.
- The heading "**Week 41 · 5 Oct 2026 – 11 Oct 2026**". Use the ISO week number; weeks start on Monday. Across a year end it reads e.g. "Week 1 · 29 Dec 2025 – 4 Jan 2026".
- On the right: "**11 booked this week**" (cancelled bookings aren't counted), a "**This week**" link only when today isn't in the week shown, then the Day · Week · Month toggle.

**The week:** seven day columns, Monday to Sunday (`ol` labelled "Week 41, Monday to Sunday").
- **Column head:** small uppercase weekday ("MON"), then the date ("5 Oct"). Clicking either opens that day in Day view. Today's column has the Month view's teal outline.
- **Day tags, as in the Month view:**
  - **Leave:** the whole column is rose, with "Leave" at the top right
  - **Special hours:** the clock-with-star icon and the hours, e.g. "08:00–12:00", in a small blue chip under the date
  - **Not working:** a grey column
- **Bookings:** in time order, one card each, which opens the appointment's details. Each card shows:
  - the time ("09:30", teal, bold)
  - the patient's name (bold)
  - "type · duration", e.g. "Follow-up · 30 min" (muted)
  - for an admin viewing all clinicians, also the clinician's name
- **Status pills:** there is **no "Booked" pill**. A booking is booked unless it says otherwise. Only these show a pill:
  - **Completed**
  - **Cancelled** (rose)
  - **No show**
- **Cancelled and no-show bookings** stay listed, on a white card with greyed text.
- **Attention markers**, as in Day view, on a card with an amber left edge:
  - "**Doctor unavailable**" (rose pill)
  - "**Outside special hours**" (amber pill)
  - "**Needs rescheduling**" (amber badge)
- **Empty days:** "Nothing booked", or "**Not working**" on a non-working day.
- **Key** below the week, as in Month: Working day · Special hours · Leave · Not working.

**Responsive:**
- **Wider than 75rem:** seven columns.
- **40rem–75rem:** each day becomes a row, with the date on the left and its bookings beside it.
- **40rem or less:** days stack, with the date column on the left. There's no horizontal scroll at 390px.

**Sample data:** use the week of 5–11 Oct 2026. Show:
- a Friday on Leave with one booking marked "Doctor unavailable"
- a Thursday with special hours 08:00–12:00 and a 14:00 booking marked "Outside special hours"
- a cancelled booking on Tuesday
- a no-show on Monday
- Saturday and Sunday not working

---

## 2. "Offer a new time" from the calendar

**Mock refs:** Day view rows ("Offer a new time" / "Discuss rebooking"), and the staff conversation (Patient messages and the record's Messages tab).

- When a doctor clicks **Offer a new time** (or **Discuss rebooking** for a cancelled or no-show booking) on their calendar, open that patient's conversation on the record's Messages tab.
  - The **Appointment suggestions** block shows with the "**Suggest a different appointment time**" panel **already open**.
  - That appointment is selected in its **Appointment** field.
  - Fields and button are as on the patient side: "Suggested date and time (SAST)", "Message (optional)", **Send suggestion**.
- Otherwise nothing changes from Rev 3.13: staff see the suggestions block only when the conversation already has a suggestion, with Accept / Decline, and no "Suggest…" bar.

---

## 3. Who can open a patient record (Clinician role)

**Mock refs:** All patients, the patient record (header and every tab), Patient messages.

A clinician's link to a patient decides what they can do:

| Link to the patient | Opens the record | Writes consultation and general notes | Changes anything else |
|---|---|---|---|
| **Current clinician** (assigned now) | Yes | Yes | Yes |
| **Covering** (booked with the patient as one-off cover, from the booking until 14 days after the appointment) | Yes | Yes | No |
| **Past patient** (assigned before, or covering that has ended) | Yes | No | No |
| **None** | No | No | No |

A cancelled booking doesn't count as cover. Super Admins and practice admins work as before.

### 3a. All patients (doctor's list)
- **Intro text:** "**Your patients: the people assigned to you, and those you looked after before or are covering. Search by name or record details.**"
- **Who is listed:** current patients, past patients and covered patients.
- **Name pill:** after the name of anyone who isn't theirs now, a small pill:
  - "**Covering**": blue, `#e6f3fb` background, `#1d5f86` text
  - "**Past patient**": amber, `#fff8eb` background, `#6b5217` text
- **Sample data:** add one row of each to the doctor's list.

### 3b. A record they can't open
- If a clinician reaches a record that was never theirs (from a Team message "About …" link, a task, or a typed URL), they land on **All patients**.
- They see the error toast "**You can only open the records of patients who are, or were, assigned to you.**"
- **In the mock:** wire one Team message "About …" link, for a patient who isn't the doctor's, to show this.

### 3c. Covering: the record
- **Notice:** a notice sits between the record header (the quick facts line) and the tabs:
  - pale amber background `#fff8eb`, border `#f0dcb0`, text `#6b5217`, rounded like other notices
  - it reads: "**You’re covering Thandi’s appointment on 9 Oct, so you can read this record and write notes, but not change anything else. Sam Marchant is Thandi’s clinician.**"
- **Still there:**
  - every tab
  - **New consultation note** on the Consultations tab
  - the **Add a general clinical note** form and **Edit tags** on their own notes on the Notes tab
  - Export record
  - replying in conversations they're in
- **Hidden:**
  - the Summary data pens and the hint "Click an underlined value to edit"
  - **Renew prescription** / **Change prescription** on the Current prescription card (the card itself stays)
  - the **Request blood tests** card
  - the title and assigned-clinician editors in the header
  - **Request blood tests** on the Blood tests tab and **Authorise treatment** on the Treatment tab
  - **Start a new conversation** on the Messages tab
- **If they try to change anything else anyway** (only reachable outside the UI): the error toast "**As cover for Thandi’s appointment you can write notes; only Sam Marchant changes the rest of the record.**" They stay on the record.

### 3d. Past patient: the record
- **Notice:** the same amber notice reads: "**You looked after Thandi before, so you can read this record but not change it. Sam Marchant is Thandi’s clinician now.**"
- **Hidden:** everything hidden in 3c, plus **New consultation note** and the general note form.
- **If they try to change anything** (only reachable outside the UI): the error toast "**Only Thandi’s current clinician can change this record. You can still read it.**"
- **Messages tab:**
  - they can read and reply in conversations they're already in
  - there's no **Start a new conversation**
  - a closed conversation says just "This conversation is closed." (no "Start a new one" link)
  - if they're in no conversation with the patient, the pane reads "**No conversations**" / "**You aren’t in any conversations with Thandi.**"

### 3e. Patient messages
- In a conversation the clinician is in, but for a patient whose record they can't open (e.g. added to someone else's patient's conversation):
  - there's **no "Open Thandi’s record" link**
  - there's **no Patient notes** accordion
- They read and reply as normal.

---

## 4. Overview wording

**Mock refs:** the staff Overview (desktop) and the mobile dashboard.

| Where | Doctor | Admin / Super Admin |
|---|---|---|
| Welcome subtext | "A focused view of your patients, clinical follow-ups and conversations." | same |
| Upcoming appointments card subtitle | "**Your booked appointments**" | "**Booked appointments**" |
| Its button | "**View calendar**" (was "View Schedule") | same |
| Its empty state | "You have no upcoming appointments." | "There are no upcoming appointments yet." |
| Patients card subtitle | "**Patients assigned to you**" | "**People currently receiving care**" |
| Its button | "**View patients**" (was "View Patients") | same |
| Its empty state | "**No patients are assigned to you yet.**" | "There are no patient records yet. Add the first patient to begin a secure clinical record." |
| Recent conversations subtitle | "**Your latest patient conversations**" | same |
| Its button | "**View patient messages**" (was "Open inbox") | same |

**Mobile dashboard (staff):**
- the top-right button reads "**Messages**" (was "Inbox")
- the Messages card subtitle is "Your latest patient conversations" and its button "View patient messages"
- empty appointments: "You have no upcoming appointments." (doctor) / "There are no upcoming appointments yet."
- empty tasks: "There are no open tasks."

"in this practice" and "within Meridian Health" are gone from both pages. The patient app is unchanged.

---

## 5. Conversations

### 5a. New conversation subject uses the clinician's title
- The automatic subject of a conversation a staff member starts is "Message from" plus the **title from their profile** and their surname: "**Message from Dr Marchant**", "**Message from Ms Botha**" (a dietitian).
- With no title it uses the full name ("Message from Ana Silva"). Practice admins: "Message from the practice".
- In the mock, give the sample dietitian the title Ms.

### 5b. "X added Y" lines for staff
- Staff now see who added a clinician to a patient conversation, as the patient and Team messages already do.
- **Where:** the muted line under the conversation title.
  - **Patient messages:** appended after "With …", separated by " · ": "With Thandi Weightchart, Lerato Botha · **Sam Marchant added Lerato Botha 5 Oct**".
  - **The record's Messages tab:** shown on its own: "**You added Lerato Botha 5 Oct**".
- It says "You" when the viewer did the adding.

### 5c. Paging on the record's Messages tab
- The record's conversation list pages **20 at a time**, like Patient messages.
- Both lists now end with the same bar: "**Newer**" (button) · "**1–20 of 34 conversations**" · "**Older**" (button).
  - Patient messages used to say "Previous" / "Page 1 of 2" / "Next".
  - "Newer" shows only after page 1; "Older" only when there are more.
- Opening a conversation keeps the list on its page.
- **Sample data:** give one patient 23 conversations to show the bar.

### 5d. Manage conversation: Back goes back
- "**Back to conversation**" on the Manage conversation page returns to where it was opened: the record's Messages tab, or Patient messages.
- After **Apply conversation action** it still knows where to go back to.

---

## 6. Smaller changes

### 6a. Old list pages
Clinicians (Doctor role) who open the practice-wide list pages by URL go back to the **Overview** with an error toast. Super Admins still see these pages.
- Consultations: "**Consultation notes are on each patient’s record, under Consultations.**"
- Blood tests: "**Blood tests are on each patient’s record, under Blood tests.**"
- Authorisations: "**Treatment authorisations are on each patient’s record, under Treatment.**"
- Compounding: "**Your compounding records open from My tasks.**"

These pages are already gone from the doctor's menu, so there's nothing to add unless the mock links to them.

### 6b. PathCare form when email isn't set up yet
- Until the practice's outgoing email is set up, **Send** in the Request blood tests dialog still stores the form, but doesn't email it.
- The clinical log entry shows the rose "**Email failed**" status with **Resend**.
- The error toast reads: "**Email isn’t set up yet, so the blood test form wasn’t emailed. It is in the clinical log, where you can view it, and resend it once email is set up.**"
- The mock can keep its "Emailed to …" sample; this is just the other state.

### 6c. My access history
- A clinician editing their own note signature or PathCare details on My profile now shows as "**Own staff details updated**" (was "Recorded account activity").

### 6d. Summary data editor
- In the Overview's Summary data, pressing **Cancel** now throws the edit away; before, it saved it.
- Pressing **Save** sends once.
- Leaving the field by clicking elsewhere, or pressing Enter, still saves; Escape still cancels.
- If the mock's editor saves when the field loses focus, make Cancel and Save behave this way too.

---

## 7. No visible change (for information only)

- **Patient files:** PathCare forms and uploaded lab reports are stored in a private DigitalOcean Space (or the database until one is set up). They're served the same way as before.
- **Email and background tasks:** password reset and PathCare email work once outgoing email is set up. Before then, Forgot password points people to support, as already mocked.
- **Unread counts:** messages staff had already opened stay read after the update.

---

## Don't change

- **Day view:** keeps its "Booked" pill; only Week view drops it.
- **Two-factor sign-in:** still no code-entry screen and no "On" status anywhere.
- **The patient app:** nothing in §3 or §4 affects it. Patients still write to their current and past clinicians and the practice admins.
- **The public landing page and the Django admin backend:** leave them as they are.

## Check when done

- **Calendar:**
  - the switcher reads Day · Week · Month
  - Week starts Monday, with the heading "Week 41 · 5 Oct 2026 – 11 Oct 2026"
  - no "Booked" pill in Week view
  - cancelled bookings are listed greyed
  - the days stack on a phone
- **Offer a new time:** clicking it on the doctor's calendar opens the conversation with the suggest panel open and the appointment chosen.
- **Doctor's All patients:** one "Covering" and one "Past patient" row.
- **Covered patient's record:**
  - the covering notice shows
  - the consultation and general note forms are there
  - no pens, no prescription buttons and no blood test card
- **Past patient's record:**
  - the "You looked after …" notice shows
  - no edit controls anywhere
  - no Start a new conversation
- **Never-assigned record:** reached from a Team message, it lands on All patients with the toast.
- **Overview:** no "Open inbox", "View Schedule", "View Patients" or "in this practice".
- **Conversations:**
  - subjects use titles ("Message from Ms Botha")
  - "added" lines show for staff
  - paging bars read "Newer · 1–20 of N conversations · Older"
  - Back to conversation returns to the record when opened from there
