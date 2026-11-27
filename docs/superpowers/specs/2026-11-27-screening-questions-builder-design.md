# Screening questions builder: draft, preview and publish

Date: 27 Nov 2026 · Status: approved by Andre ("approved, write the plan")

Mockups: [2026-11-27-screening-questions-builder-mockups.html](2026-11-27-screening-questions-builder-mockups.html) (Andre chose layout **B**, the editor beside a live preview).

## Why

The public screening questionnaire (`/screening/`, also embedded on the practice's WordPress page) is hard-coded: a Django form (`care/forms.py` `EligibilityQuestionnaireForm`) laid out by hand in `templates/portal/questionnaire.html`. Changing one word needs a developer. Andre wants the practice's doctors to be able to change the questionnaire over time from Settings: reword questions, add new ones, reorder and retire them. Some fields must stay fixed because the app depends on them.

## How it works today (what this builds on)

- **Sections on the form:** practice (hidden in single-practice mode), *Who you are* (title, first name, last name, email, mobile, ID/passport), *Height and weight* (BMI shown live), five yes/no screening questions (`adult`, `weight_related_condition`, `pregnancy`, `thyroid_history` with Yes/No/Not sure, `pancreatitis`), *Chronic medication and allergies* (`health_context`, optional long text), *How did you hear about us?* (`heard_from`, optional multi-choice), consent tick (wording from Settings → Policy versions).
- **Storage:** name, email, phone and ID go on `Lead`. Everything else goes into `ScreeningQuestionnaire.answers` (JSON), with `screening_reasons`, `screening_rules_version` and `consent_snapshot` added by `care/intake.py` `save_intake`.
- **Rules:** `care/intake.py` `screening_outcome` hard-codes the checks (BMI 27/30, the yes/no answers). They run only when `DEBUG` is on. On a live server every lead is `PENDING` with no reasons. Checkout (`care/checkout.py` `can_check_out`) only works in DEBUG and needs `screening_rules_version == 'prototype-rev2.6-demo'` and status `CLEARED`.
- **Answer keys are read by name elsewhere:** lead detail page (`portal/lead_views.py`, fixed label list), the doctor's medical profile prefill (`care/clinical_summary.py` `seed_values`: height, weight, weight-related condition, medication, pancreatitis, thyroid), Insights (`care/insights.py`: `heard_from`), checkout (`title`), patient summary (`bmi`), and the edit flow (`portal/intake_views.py` prefills from keys in `EligibilityQuestionnaireForm.base_fields`).
- **Versioning precedent:** Settings → Policy versions (`ConsentDocument`): numbered versions, old wording kept, and each acceptance stores a copy of what was accepted.

## Goals

1. A **Settings → Screening questions** tab (Super Admins) with the live questionnaire, its versions, and a draft editor.
2. **Today's hard-coded questionnaire becomes Version 1, already published** (Andre's requirement). Every existing submission is linked to Version 1. With Version 1 live, the public form looks and behaves exactly as it does today.
3. Three tiers of questions:
   - **Locked** (cannot be edited or removed): title, first/last name, email, mobile, ID/passport, height, weight, consent.
   - **Core** (reword only: question text and help text): the five yes/no screening questions, *Chronic medication and allergies*, *How did you hear about us?*. Their keys, answer type, answer options and required-ness stay fixed, because the rules, the medical profile prefill and Insights read them.
   - **Custom** (add, edit, reorder, retire): new questions of type Yes/No, One choice, Several choices, Short text, Long text or Number, each optional or required.
4. **Draft → preview → publish.** Edits go to one draft version, the doctor sees a live patient preview beside the editor, and nothing reaches the public form until **Publish**. Published versions never change.
5. **Flag for review** (Andre chose B). On Yes/No, One-choice and Several-choices custom questions, each answer can be ticked *Flag for review*, with a reason. The patient sees the reason, so the editor says so.
   - Choosing a flagged answer adds its reason to the lead's `screening_reasons` and records the flag.
   - **A flag never declines.**
   - In DEBUG, any flag makes the outcome `REFERRED` ("Needs clinician review"), as the core checks do.
   - On a live server the status stays `PENDING` (unchanged behaviour) and the reasons are shown on the lead page.
6. Every lead keeps the version it answered. The lead page shows answers with that version's wording, with flagged answers highlighted.

## Non-goals

- A rules builder: custom answers clearing or declining a lead, or editing the BMI thresholds or core checks.
- Flags on text or number answers (e.g. "flag if over 65"). This could come later.
- Conditional questions (show X only if Y), multiple pages, file uploads, translations.
- Custom answers feeding the medical profile, Insights or email journeys.
- Editing locked fields, changing core answer options (including the *How did you hear about us?* choices), or reordering the locked sections, *Medication and allergies* or *How did you hear about us?*.
- Letting doctors who aren't Super Admins edit (Claude's call; Andre can change it).

## Anti-goals (a failure even if it works)

- An edit or publish changing what an existing lead shows.
- A draft, or a half-finished question, reaching the public form or the WordPress page.
- A patient losing their typed answers because a new version was published while they were filling in the form.
- A core key being renamed or removed, silently breaking the medical profile prefill, Insights, checkout or the BMI.
- A flag turning someone away, or the live-server behaviour (every lead `PENDING`) changing for core questions.
- Version 1 rendering differently from today's form in any way a patient would notice.

## Approaches considered

**Storage and publishing**

| Option | Trade-off | Outcome |
|---|---|---|
| 1. Draft and publish versions, questions stored as rows per version | Preview before going live, old versions frozen, reuses the Policy versions idea. More to build. | **Chosen by Andre** |
| 2. One live list, each submission saves a copy of the questions | Simplest, but every saved edit (and typo) is live on the public form at once, with no preview. | Rejected |
| 3. Whole questionnaire as one JSON document per version | Easy to copy a version, but editing, reordering and validating single questions gets clumsy. | Rejected |

**Editor layout**

| Option | Trade-off | Outcome |
|---|---|---|
| A. List with a pop-up editor, preview in a new tab | Matches the app's dialogs, works on smaller screens, less to build. | Rejected |
| B. List, edit panel and a live phone-width patient preview side by side | You see the patient's view while writing. Needs a wide screen and more to build. | **Chosen by Andre** |

**What a custom answer can do:** A) information only, B) flag for review, C) full rules. **B chosen by Andre.**

## Design

### Data model (`care/models.py`)

`ScreeningFormVersion(CompanyScopedModel)`
- `number`: positive integer, 1, 2, 3 per company (unique with company).
- `status`: `draft` / `published` / `superseded`. At most one `draft` and one `published` per company (conditional unique constraints).
- `based_on`: the version it was copied from (nullable). `created_by`, `published_by` (nullable users), `published_at`.
- `note`: short text, e.g. "Set up from the existing form" for Version 1.

`ScreeningQuestion(CompanyScopedModel)`
- `version` (FK, cascades only for drafts; published versions are never deleted), `position`.
- `key`: slug, stable across versions. Core keys are the existing answer keys. Custom keys are generated once from the first wording (`c_` prefix), unique across the company's whole history, and never reused, even after retirement.
- `tier`: `core` / `custom`. Locked fields are **not** rows: they are fixed in code and shown in the builder as greyed, read-only blocks.
- `answer_type`: `yes_no`, `yes_no_unsure` (core thyroid only), `single`, `multiple`, `short_text`, `long_text`, `number`.
- `label` (≤ 300), `help_text` (≤ 500), `required`.
- `options`: JSON list of `{value, label, flag, reason}` (2 to 12 options for choice types; `reason` required when `flag` is ticked, ≤ 300).
- `number_min` / `number_max` (optional, for number).

`ScreeningQuestionnaire.form_version`: new FK (nullable for safety, backfilled for all existing rows).

### Version 1 (data migration)

- For every company (including inactive ones), create Version 1 as `published`, `published_at` = migration time, `note` "Set up from the existing form". Every `ScreeningQuestionnaire` has a non-null company (`CompanyScopedModel`, PROTECT), so none is left without a Version 1.
- Version 1 is a **frozen literal definition**: plain data in `care/screening_v1.py`, not read from the form or template at runtime. The data migration carries its own copy, because migrations mustn't import app code that will change. A test asserts the two copies are identical.
- Its core rows copy **today's patient-visible wording** from `questionnaire.html` (where the template's text differs from the form's `help_text`, the template's wins, e.g. the medication help text "Include the names and doses of any regular medication, and any allergies. If none, say "None"."). Answer types, options and required-ness are copied from today's form fields as they are.
- Every existing `ScreeningQuestionnaire` row gets `form_version` = its company's Version 1.
- Practices created later, and demo seeding, get Version 1 lazily: a `live_version(company)` helper creates it from `care/screening_v1.py` under a company lock if none exists.

### Public form

- `EligibilityQuestionnaireForm` takes the version. Locked fields are unchanged. Core fields keep their Django field definitions, with label and help text from the version. Custom questions are added as fields named `custom_<key>`, built from their type (`ChoiceField` + radios, `MultipleChoiceField` + checkboxes, `CharField` with max 200 / 2000, `DecimalField` with optional min/max).
- The template's five hand-written screening fieldsets become one loop over the version's movable block (core yes/no and custom questions, in `position` order). Core questions keep today's markup. Custom questions use the same card styles.
- *Medication and allergies* and *How did you hear about us?* stay where they are, with their wording from the version.
- Custom Yes/No questions are radios with no default and no "Not sure". A doctor who wants "Not sure" uses One choice. Optional questions may be left blank (saved as `''` or `[]`).
- The progress counter ("N of M answered") counts only the required choice questions in that block, as it counts today's five. Optional questions and text or number questions are excluded.
- **Version in the signed token.** The token already holds a per-practice map of consent fingerprints (`notices`). It gains a matching per-practice map of version ids (`{practice_id: version_id}`, one entry in single-practice mode).
  - On POST the token is decoded and checked **before** the form is built, because the form needs the version to define its fields.
  - A missing, tampered or expired token, or a version id that isn't one of that practice's published or superseded versions, re-renders the live version with the posted values that still fit. It shows today's error: "This questionnaire has expired or belongs to another browser. Reload it before submitting."
- A submission is validated and saved against **the version it was shown**, even if a newer one was published meanwhile (published versions are immutable, so this is safe, and no answers are lost). The lead links to that version.
- **Edit flow (`?edit=1`).** The patient gets the **live** version, with answers prefilled for keys that still exist. As today, saving an edit **replaces** the submission in place: the same `stage=1` row, with `form_version` re-pointed to the version shown in that edit. The lead page then shows the latest answers with the wording they were given under.
  - Answers to questions retired since the earlier submission are not kept. That matches today, where an edit overwrites the earlier answers.
  - This is the patient changing their own enquiry, not the practice's edits changing history, so it doesn't conflict with the anti-goal.
- WordPress embed: same page, so no change beyond checking that height auto-resize still works.
- Multi-practice mode (off by default): each practice has its own versions. Claude's call (revised while planning): choosing a different practice reloads the form with `?practice=<id>` for that practice's live version. The practice is the first question, so nothing typed is lost. Rendering every practice's questions side by side would make a different form per practice inside one page. The server checks that the posted practice matches the version it validates against.

### Saving and flags (`care/intake.py`)

- Custom answers are saved under `answers['custom'][key]`, so they can never collide with system keys (`bmi`, `screening_reasons`, …). Core answers stay at their current top-level keys, so nothing downstream changes.
- `answers['flags']` = list of `{key, answer, reason}`. Flag reasons are added to `answers['screening_reasons']` (deduplicated) after the core reasons.
- `screening_outcome`: unchanged for core checks. In DEBUG, any flag also makes the status `REFERRED`. In production the status stays `PENDING`, with flag reasons stored.
- The result page already lists `screening_reasons` to the patient. The builder warns: "The patient sees this reason."
- `screening_rules_version` stays as it is (checkout depends on it). The form version is recorded separately.

### Settings → Screening questions (Super Admins)

**Overview** (`/settings/screening/`, new tab beside the existing Settings tabs; page registered in `portal/page_navigation.py` with the `super_admin` area):
- A "Live questionnaire" card: version number, published date and by whom, question count. Buttons: *Preview as patient*, *Edit questions* (creates the draft by copying the live version, or opens the existing draft).
- A versions table: number, status, published, by, leads answered, *View* (read-only list plus preview).

**Draft editor (layout B):**
- **Top bar:** "Draft · Version N" and a summary of changes since the live version (added, reworded, retired, reordered), with *Discard draft* and *Publish version N*.
- **Left, the questionnaire list:**
  - Locked sections are greyed, with a lock and a short reason ("The app needs these", "Used for BMI", "Wording comes from Policy versions").
  - The movable block has drag handles, plus *Move up* / *Move down* for keyboard use. Core rows show "Core · reword only".
  - Custom rows show their type, required-ness and a ⚑ badge when any answer flags, plus *Retire*.
  - *+ Add question* sits at the end of the movable block.
- **Middle, the edit panel** for the selected question: answer type, question, help text, answers (each with *Flag for review* and its reason), required.
  - For core rows only the question and help text are editable. The rest is shown read-only with why ("The eligibility checks and the medical profile use this answer").
- **Right, live preview:** the real questionnaire page in a staff-only preview mode for the draft, shown at phone width in an iframe. It has a "Preview, not submitted" banner.
  - **Its own Super-Admin-only URL**, framed with `X-Frame-Options: SAMEORIGIN` (`xframe_options_sameorigin`, as `portal/pathcare_views.py` already uses). The public page's WordPress `frame-ancestors` setting does not apply to it, so only Meridian's own pages can embed the preview.
  - **It never creates a lead.** The form has no action, its submit button is disabled, and the view accepts GET only (POST returns 405).
  - The consent section shows the live Policy versions, as a patient would see them. The policies aren't part of the draft.
  - Edits autosave to the draft (debounced), then the preview reloads and scrolls to the edited question.
  - Below about 1100px wide, the preview folds into a *Preview* button that opens it in a dialog.
- **Publish:**
  - The confirm dialog shows the change summary and says: "New enquiries on the website and your WordPress page get these questions from now on. Leads that already answered keep Version N−1."
  - The whole draft is validated first.
  - Then, atomically under a company lock: draft → `published`, previous → `superseded`.
- **Discard:** a confirm, then the draft and its rows are deleted.
- **Audit (`record_audit`):** draft started, question added / changed / retired / reordered, published (with version number and change summary), discarded.
- **Concurrency:** one draft per company. Saves are per question, and the last save wins. Publish locks the company row.

### Lead page

- Answers are listed with the wording of the version the lead answered: locked fields, then the movable block in that version's order, medication, *How did you hear about us?*.
- Flagged answers are highlighted with their reason. Custom answers to questions since retired still show (they live in that version).
- The version number is shown ("Answered Version 2").

### Leads list

- On a live server every lead is `PENDING`, so flags need to be findable without opening each lead. A lead with any flag gets a ⚑ marker beside its screening status. A *Flagged* option is added to the existing Screening filter in `portal/lead_views.py`.
- `screening_reasons` is read only by the lead page and the patient's result page (checked). The other readers of `answers` (medical profile prefill, Insights, checkout, the edit flow) read specific core keys, so the new `custom` and `flags` keys don't affect them.

## Testing

- **Migration:** Version 1 exists per company, matches today's wording exactly, and all existing questionnaires point to it. `live_version` creates the same Version 1 for a new practice.
- **Version 1 parity:** the rendered public form's questions, labels, help texts, options and required-ness match today's. All existing questionnaire, embed and checkout tests still pass unchanged.
- **Custom types:** each renders, validates (required, lengths, number min/max, option values) and saves under `answers['custom']`.
- **Flags:** reasons are added and deduplicated. DEBUG → `REFERRED`, production → `PENDING` with reasons. Never `DECLINED`. Checkout is blocked when flagged (DEBUG).
- **Version in token:**
  - A submission after a newer publish saves against the shown version.
  - Editing gets the live version and drops retired answers.
  - A tampered version id is rejected.
- **Builder:**
  - Super Admin only.
  - One draft at a time.
  - Core rows can't change type, options, required or key.
  - Retired keys are never reused.
  - Publish is atomic and supersedes the old version.
  - Discard removes only the draft.
  - The preview never creates a lead and isn't reachable by the public.
- **Lead page:** shows the answered version's wording and highlights flags.
- **Leads list:** the ⚑ marker and *Flagged* filter find flagged leads.
- **Patient edit after a new publish:** replaces the submission and re-points `form_version`. Retired answers are gone. Any other leads are untouched.

## Decisions

| Area | Decision |
|---|---|
| Storage | Andre: draft and publish versions (approach 1). |
| Flags | Andre: flag for review only (B). |
| Editor | Andre: layout B, editor beside a live preview. |
| Version 1 | Andre: today's hard-coded form is the first published version. |
| Who edits | Claude's call: Super Admins only, like Practice settings. |
| Locked fields | Claude's call: fixed in code, not stored as rows. |
| Custom answer storage | Claude's call: under `answers['custom']`. |
| Version at submit | Claude's call: the version the patient was shown, carried in the signed token. |
| Preview | Claude's call: the real page in a staff-only preview mode (same-origin framing, GET only), in an iframe, reloaded after each autosave. |
| Multi-practice | Claude's call (revised while planning): changing the practice reloads the form for that practice's version, with a per-practice version map in the token. |
| Version 1 source | Claude's call: a frozen literal in `care/screening_v1.py`, copied into the migration, with a test that they match. |
| Patient edits | Claude's call: replace the submission in place, as today, and re-point it to the version shown. |
| Leads list | Claude's call: ⚑ marker and a *Flagged* filter, so flags can be found on a live server. |
| Custom Yes/No | Claude's call: no "Not sure" (use One choice for that). Optional questions can be left blank. |

## Left to the builder

Migration numbering, URL names, the drag-and-drop mechanism (native or a small library, with the keyboard alternative), debounce timing, exact visual styling within the existing design system, and how the change summary is computed.
