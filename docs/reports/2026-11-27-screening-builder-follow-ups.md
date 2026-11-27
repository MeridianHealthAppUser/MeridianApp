# Screening questions builder: follow-ups

Date: 27 Nov 2026 · State: `main` at `b1c77e8`

These are the small items the task reviews and the whole-branch review rated **"can wait"**. Nothing here blocks use. Items fixed later (in the final fix wave or the polish) are not listed.

Spec: `docs/superpowers/specs/2026-11-27-screening-questions-builder-design.md`. Plan: `docs/superpowers/plans/2026-11-27-screening-questions-builder.md`.

## Behaviour worth a look first

1. **Cancel, then reopen Publish within a second.** If you cancel the Publish dialog while it says "Saving your last change…" and reopen it before that save finishes, the publish still goes ahead when the save completes. The window is under a second, and the reopened dialog shows "Saving…" with Publish disabled. Fix: in `guard()` (`static/js/screening_builder.js`), record the cancel with a `close` listener for the whole flush instead of reading `dlg.open` at the end.
2. **Browser Back with a failed last edit.** The browser's own Back/Forward buttons no longer warn about unsaved changes on the editor, because autosave opts the panels out of the app's unsaved-changes check. If the very last edit then fails to save (for example an empty answer label), it is lost without a warning. Fix: have the builder mark a form `data-autosave-pending` while it is unsaved or saving, and make `mayLeave()` (`static/js/app_navigation.js`) skip only `[data-autosave]:not([data-autosave-pending])`.
3. **Retire straight after editing the same question does nothing.** The save replaces the row, so the click lands on a form that is no longer on the page; a second click works. Fix: if `!form.isConnected`, find the replacement form by its `action`.
4. **Patient preview shows the staff header.** The preview renders the real questionnaire while you're signed in, so it shows "Demo data" and your account chip instead of the patient's header. Fix: when `preview` is set, render the public header (needs a small change in `templates/base.html`).
5. **Public form: a broken practice can take the form down.** `notice_context()` (`portal/intake_views.py`) calls `live_version` for every enabled practice; if one practice had versions but none published (not reachable through the app), the public form would error for all. Fix: skip that practice instead.
6. **Public form: a very small publish race.** A GET signs one live version into the token and renders the form with a second `live_version` call (`portal/intake_views.py`). If a publish lands between the two, the patient sees the new version but submits against the old one. Fix: render with the version id already in the notice item.

## Editor polish (staff-only, cosmetic or rare)

- Rapid Move up / Move down clicks send overlapping reorder requests; send only the latest order.
- A save that lands mid-drag can duplicate the dragged row until reload.
- A late save can re-highlight the previously selected row (`row_html` is rendered with `selected=True`).
- `select()` calls `history.replaceState` directly; the app convention is `MeridianNavigation?.replaceUrl`.
- `showErrors` treats a `<select>`'s `length` as a field list (not reachable from the UI today).
- After a refusal (stale draft), rows stay draggable.
- At 1180px and under, autosave still reloads the hidden preview frame.
- Answer rows' `aria-label`s aren't renumbered after add/remove; after Move to an end, focus stays on a now-disabled button.
- `flush()` keeps an "invalid"/"error" result from an earlier pass even if a later pass saves; it should decide from the end state.
- `flushFailed()` scrolls the panel, not the invalid field, and doesn't focus it.
- In `guard()`, cleanup runs in `.then` (should be `.finally`), and the submit button is re-enabled just before `form.submit()` (a fast double click can post twice; the server refuses the second as stale).
- A save that turns stale during a flush drops one click silently.

## Code and robustness

- `check_draft` (`portal/screening_views.py`) uses `isdigit()`, which accepts characters such as "²" that `int()` rejects (a 500 for a Super Admin only). Use `isdecimal()` with a length cap, or catch `ValueError`.
- `check_draft` runs outside the services' company lock; add, reorder, publish and discard re-resolve the draft (a microsecond discard-and-restart race). Passing the checked draft id into the services closes it.
- `ScreeningQuestionForm`: `bool('false')` reads as a ticked flag; option values aren't type-checked (only reachable by editing the database).
- `add_question(label=…)` doesn't validate the label length (all current callers pass the default).
- `save_question` saves and audits even when nothing changed (every autosave writes an audit row).
- The custom answer fields fall back to short text for an unknown `answer_type`; raise instead.
- Custom questions are queried twice when cleaning a submission (`care/forms.py`).
- The "required choice question" rule is written in three places (template, `screening_total`, form); a `ScreeningQuestion.is_choice` property or a total on the form would keep them together.
- `QuestionnaireView.post()` calls `is_valid()` twice; checking `payload is None` first reads more clearly.
- The medication textarea's screen-reader label is still hard-coded, so a reworded `health_context` isn't read out. It is kept as is for Version 1 parity; change it with care.
- `care/screening.py` has imports in the middle of the module, and `_display_value` uses answer-type strings instead of the enum.
- `staff-lead-answer--flagged` (lead page) has no style; `theme.css` may still hold two tokens (`--c-1d6f7a`, `--c-d8e2e6`) that only old fallbacks used.
- No same-company `clean()` checks on `ScreeningQuestion.version`, `ScreeningFormVersion.based_on` and `ScreeningQuestionnaire.form_version` (services always set the company, and `save_intake` checks it).

## Test gaps

- Models and Version 1: no test for `unique_screening_version_number`; `movable_questions` / `questions_by_key` untested; the backfill test covers one company only; on SQLite the "no lock" test relies on its query count.
- Form: no test for a number above the maximum, the order of the core block, which field errored, or the full answer key set.
- Intake: the flag-order test can't tell dict order from question order; no replay-that-changes-a-flag test; no `version=None` save test; the cross-practice refusal test should pin its message.
- Public view: no test with a draft version id in the token, or that the bad-token page shows the live version.
- Lead pages: the flag reason isn't asserted inside its highlight; the ⚑ marker count; option labels, "Not recorded" and row order untested.
- Builder services: forged `key`, a stale save after a new draft, the stale message for retire/reorder/publish/discard, a reason over 300 characters, min > max, `reordered=True`, publishing single/multiple/number rows saved through the form.
- Builder views: another practice's version/preview number (404), 403-before-404 without a draft, a valid core save, the non-JSON error redirect, and that rendered forms carry `name="draft_id"` with the draft's pk.
- Editor script: no automated browser test (behaviour was checked by hand in a browser).
