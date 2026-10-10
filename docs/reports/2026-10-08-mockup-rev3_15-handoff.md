# Meridian Health: Mockup Rev 3.15 handoff (code → mockup), 8 Oct 2026

**For:** the Claude chat that maintains the interactive mockup HTML.

**Input file:** the latest mockup, Rev 3.14 (built from the Rev 3.14 handoff). If your copy of the Rev 3.14 handoff has sections "1a" and "1b" on the Day and Month views, it was a later draft. Where the two differ, this file wins.

**Output file:** `meridian-health-app-mock-rev3_15__08_10_2026.html`. Change `<title>` to "Meridian Health — System Mock (Rev 3.15)" and mark new CSS/JS blocks with `/* Rev 3.15: … */` comments.

**Why:** After the Rev 3.14 handoff, James reviewed the Calendar & Bookings Day, Week and Month views. This document lists every change since then. Only the calendar changed.

## Ground rules

- **The app wins.** Quoted text is the exact wording used in the app; reuse it.
- Keep every mock device from earlier handoffs: sample data, role picker, toasts, the dark-mode toggle (put new colours on the existing tokens).
- If something below is already in your mockup, check the wording and colours and move on.

## At a glance

1. **New calendar colours:** Leave is **orange** everywhere, and red now means only a **cancelled** booking (§1).
2. **Week view:** status pills move to the top right of each card. Special-hours and Leave days are filled, with outlined tags at the top right (§2).
3. **Day view:** the day's pill becomes a full-width coloured band (§3).
4. **Month view:** every booking shows, coloured by status. Days are filled, tags are outlined, empty days are blank, and there's a new booking-colour key (§4).

---

## 1. Calendar colours

These apply across Day, Week and Month. Use them as tokens.

**Days:**

| Day | Day fill | Day border | Tag fill | Tag border | Text |
|---|---|---|---|---|---|
| Leave | `#fff3e6` (light orange) | `#f5d3ae` | `#fde3c6` | `#f0c08a` | `#a14b0b` |
| Special hours | `#edf6fc` (light blue) | `#c9e2f3` | `#dbedf9` | `#b9daf1` | `#1072aa` |
| Not working | the grey canvas colour (unchanged) | unchanged | n/a | n/a | `#5b7175` |

**Bookings (Month view chips):**

| Status | Fill | Border | Text |
|---|---|---|---|
| Booked | teal (unchanged) | `#c4e4de` | teal |
| Completed | `#e6f4dc` (green) | `#b9dca2` | `#36701c` |
| Cancelled | `#fde9e6` (red) | `#efc4be` | `#a73f37`, struck through |
| No show | `#eef1f1` (grey) | `#d3dcdc` | `#5b7175` |

**"Doctor unavailable"** (the marker on a booking left on a Leave day) is now **orange**, using the Leave tag colours. It keeps its small dot. It was rose before.

**Not changed:**
- the red "**Action needed**" alert at the top of the page
- Week view's rose "**Cancelled**" pill
- the amber "**Outside special hours**" and "**Needs rescheduling**" markers

---

## 2. Week view

**Mock refs:** Calendar & Bookings, Week view.

**Booking cards:**
- The first line of each card holds the time ("09:30", teal, bold) on the left and the status pill on the **right**: "**Completed**", "**Cancelled**" or "**No show**". The pill sits at the card's top right and wraps under the time only if the card is very narrow.
- Still **no "Booked" pill**.
- The patient's name, "type · duration" and (for an admin viewing everyone) the clinician's name follow as before.
- Attention markers stay at the **bottom** of the card: "Doctor unavailable" (now orange), "Outside special hours" and "Needs rescheduling".
- Card colours don't change: teal for booked and completed, white with greyed text for cancelled and no show.

**Day columns:**
- **Leave:** the whole column is light orange (§1). An outlined orange "**Leave**" tag sits at the **top right**, beside the date.
- **Special hours:** the whole column is light blue (§1). An outlined blue tag sits at the **top right**, with two lines:
  - "**Special hours**" (smaller, about 0.56rem, semi-bold)
  - the clock-with-star icon and the hours, e.g. "**08:00–12:00**" (about 0.6rem, bold)
- **Tag shape:** radius about 0.4rem, 1px border, padding about 0.14rem × 0.32rem, text about 0.6rem.
- **Narrow columns:** if the special-hours tag doesn't fit beside the date, it drops just below the date, still on the right. In the row layouts below 75rem, the tag sits under the date on the left.

**Key** below the week, for one clinician's diary: Working day · Special hours · Leave · Not working. Each has a small coloured square. **Special hours is now a light blue square**, not the clock icon, and Leave is an orange square.

---

## 3. Day view: the day band

**Mock refs:** Day view, the pill between the card header and the bookings table.

- Replace the pill with a **full-width band** across the card, between the header (date and arrows) and the table's column headings.
- The band's words are **centred**, bold (about 0.78rem), with **no pill or outline**. Padding is about 0.65rem × 1.15rem, with a 1px bottom border in the band's border colour.
- **Leave:** "**Leave**", light orange (§1).
- **Special hours:** "**Special hours** (clock-with-star icon) **08:00 – 12:00**", light blue (§1). The words come first, then the icon, then the hours, with spaces around the dash.
- **Not a working day:** "**Not a working day**", grey canvas colour.
- On a working day with normal hours there's no band.
- On a phone the band spans the card the same way.
- The table's "Booked" status pill stays as it is.

---

## 4. Month view

**Mock refs:** Calendar & Bookings, Month view.

**Bookings:**
- **Every booking shows**, cancelled ones included, in time order. Each chip is coloured by its status (§1). Cancelled chips are also **struck through**, so they don't rely on colour alone.
- The "**N booked this month**" count, and each day's count in its label, still **leave cancelled bookings out**.
- Each chip's screen-reader label ends with its status, e.g. "Appointment details: 08:45, Ayesha Patel, cancelled".

**Days:**
- **Leave:** filled light orange, with an **outlined** orange "**Leave**" tag at the top right.
- **Special hours:** filled light blue, with an **outlined** blue tag at the top right: the clock-with-star icon and the hours on one line, e.g. "08:00–12:00".
- **Empty days are blank:** remove the "—" from days with nothing booked.

**Keys** below the month, on one line when they fit (about 2rem apart), otherwise wrapping:
1. **Booked · Completed · Cancelled · No show**, each with a small square in its chip colour. This one is **always shown**, including for an admin viewing everyone.
2. Then, for one clinician's diary only: **Working day · Special hours · Leave · Not working**. Special hours is a light blue square; Leave is orange.

**Phones:**
- Chips still show only the time, now in their status colour.
- Day tags stay hidden, as before, but the orange and blue day fills show.

**Sample data:** October 2026, with the week of 5–11 Oct as in Rev 3.14:
- Monday: two completed bookings and a no show
- Tuesday: a cancelled booking and a completed one
- Wednesday: four booked
- Thursday: special hours 08:00–12:00, with bookings at 09:00 and 14:00
- Friday: Leave, with one booking
- Saturday and Sunday: not working

---

## Don't change

- **Week view card colours:** keep them as they are (teal; white for cancelled and no show). The status colours in §1 are for Month view chips only.
- **The Day view table:** keep its status pills, including "Booked".
- **Everything outside Calendar & Bookings.**

## Check when done

- Nothing about Leave is red anywhere: the Day band, Week and Month fills and tags, the keys and "Doctor unavailable" are all orange.
- Red appears only on cancelled bookings, plus the "Action needed" alert.
- **Week view:**
  - "Completed", "Cancelled" and "No show" sit at the top right of their cards, beside the time
  - Thursday is blue, with the two-line "Special hours / (icon) 08:00–12:00" tag at the top right
  - Friday is orange, with an outlined "Leave" tag at the top right
- **Day view:**
  - Friday shows a centred orange "Leave" band
  - Thursday shows a centred blue "Special hours (icon) 08:00 – 12:00" band
  - Saturday shows a grey "Not a working day" band
  - none of them is a pill
- **Month view:**
  - the cancelled booking shows red and struck through
  - completed ones show green and the no show grey
  - "11 booked this month" still leaves the cancelled one out
  - empty days are blank
  - both keys show on one line on desktop
