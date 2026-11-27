# Screening Questions Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Super Admins edit the public screening questionnaire from Settings (draft, live preview, publish), with today's form as published Version 1. Custom answers can flag a lead for clinician review.

**Architecture:**
- Versions and questions are company-scoped rows (`ScreeningFormVersion`, `ScreeningQuestion`).
- The public form is built from a version. Locked fields stay in code, core questions take their wording from the version, and custom questions become dynamic fields.
- A service module (`care/screening.py`) owns Version 1, the live version, flags and every builder change. The builder pages call it.

**Tech Stack:** Django (existing project), SQLite locally / PostgreSQL in production, vanilla JS and CSS in `static/`, Django `TestCase`.

**Spec:** `docs/superpowers/specs/2026-11-27-screening-questions-builder-design.md` (approved). Mockups are in `docs/superpowers/specs/2026-11-27-screening-questions-builder-mockups.html`, layout **B**.

## Global Constraints

- Run tests with `.venv/bin/python manage.py test <label>`. The full suite is `.venv/bin/python manage.py test --parallel auto`, and every existing test must keep passing.
- Every query is company-scoped (`Model.objects.for_company(company)` or `company=`). Builder pages are **Super Admin only**: check `membership.role == CompanyMembership.Role.SUPER_ADMIN`, otherwise `PermissionDenied` (403), as in `portal/journey_views.py`.
- Staff views use `LoginRequiredMixin, StaffCompanyRequiredMixin, View` and `@method_decorator(never_cache, name='dispatch')`.
- Every builder change calls `care.services.record_audit(company=, actor=, action=, target=, request=, metadata=)`. Metadata carries **no** patient answers.
- Core answer keys never change: `adult`, `weight_related_condition`, `pregnancy`, `thyroid_history`, `pancreatitis`, `health_context`, `heard_from`. `answers['screening_rules_version']` and the DEBUG-only core checks in `care/intake.py` `screening_outcome` keep today's behaviour. A flag never sets `DECLINED`.
- Custom answers go under `answers['custom'][key]`, flags under `answers['flags']` (`[{key, answer, reason}]`).
- Limits:
  - Question label ≤ 300 characters, help text ≤ 500.
  - Choice questions have 2 to 12 options. Option label ≤ 120.
  - A ticked flag needs a reason, ≤ 300.
  - At most 25 custom questions per version.
  - Short text answers ≤ 200 characters, long text ≤ 2000.
- Custom keys are `c_` + `slugify(label)` (≤ 50 characters), with `-2`, `-3`… if taken. They are unique across **all** of the company's versions and never reused.
- Exact copy:
  - Preview banner: "Preview, not submitted".
  - Flag reason hint: "The patient sees this reason."
  - Publish dialog: "New enquiries on the website and your WordPress page get these questions from now on. Leads that already answered keep Version N−1."
  - Token error (unchanged): "This questionnaire has expired or belongs to another browser. Reload it before submitting."
  - Locked block notes: "The app needs these" / "Used for BMI" / "Wording comes from Policy versions".
  - Core read-only note: "The eligibility checks and the medical profile use this answer".
- Migrations: the latest is `care/migrations/0069_policy_versions_and_accepted_wording.py`. Use `0070` (schema) and `0071` (data), or the next free numbers when you start.
- Branch: work on `screening-builder`, never on `main`. Stage only the files your task names. Never `git add -A` or `git commit -a`: the working tree holds unrelated uncommitted work from other sessions.

## Review Focus

1. **Publishing while a patient has the form open.** Their submit must save against the version they saw (Task 5 test `test_submission_after_publish_saves_against_shown_version`).
2. **A stale editor tab after publish or discard.** Saving a question to a version that is no longer the draft must be refused, not written into a published version (Task 7 test `test_saves_to_a_published_or_missing_draft_are_refused`).
3. **Forged edits to a core question.** Posting a different `answer_type`, options, `required` or key for a core row must leave them unchanged (Task 7 test `test_core_questions_only_take_label_and_help_text`).
4. **HTML in doctor-written text.** A label or reason containing `<script>` must render escaped on the public form, the preview and the lead page (Task 3 test `test_custom_labels_are_escaped`, Task 6 test `test_flag_reasons_are_escaped_on_lead_page`).
5. **Leads with no version.** Demo or legacy questionnaires with `form_version` null must still render the lead page, using the company's Version 1 (Task 6 test `test_questionnaire_without_version_uses_version_1`).

---

### Task 1: Version and question models

**Files:**
- Modify: `care/models.py` (add after `ScreeningQuestionnaire`, around line 266; add `screening_flagged` to `Lead`)
- Create: `care/migrations/0070_screening_form_versions.py` (via `makemigrations`)
- Test: `care/test_screening.py`

**Interfaces:**
- Produces:
  - `ScreeningFormVersion(CompanyScopedModel)`:
    - `Status` = `DRAFT='draft'`, `PUBLISHED='published'`, `SUPERSEDED='superseded'`.
    - Fields: `number` (PositiveInteger), `status` (default draft), `based_on` (FK self, null, SET_NULL, `related_name='+'`), `created_by` / `published_by` (FK user, null, SET_NULL, `related_name='+'`), `published_at` (DateTime null), `note` (CharField 200, blank).
    - Constraints: `unique_screening_version_number` (company, number); `one_screening_draft_per_company` (company, condition status=draft); `one_live_screening_version_per_company` (company, condition status=published).
    - Ordering `('-number',)`.
  - `ScreeningQuestion(CompanyScopedModel)`:
    - `Tier` = `CORE`, `CUSTOM`.
    - `AnswerType` = `YES_NO 'yes_no' 'Yes / No'`, `YES_NO_UNSURE 'yes_no_unsure' 'Yes / No / Not sure'`, `SINGLE 'single' 'One choice'`, `MULTIPLE 'multiple' 'Several choices'`, `SHORT_TEXT 'short_text' 'Short text'`, `LONG_TEXT 'long_text' 'Long text'`, `NUMBER 'number' 'Number'`.
    - Fields: `version` (FK, CASCADE, `related_name='questions'`), `key` (SlugField 60), `tier`, `answer_type`, `label` (300), `help_text` (500, blank), `required` (bool, default False), `options` (JSONField, default list), `number_min` / `number_max` (Decimal 10,2, null), `position` (PositiveSmallInteger).
    - Constraint `unique_screening_question_key_per_version` (version, key). Ordering `('position', 'pk')`.
  - `ScreeningQuestionnaire.form_version`: FK `ScreeningFormVersion`, null, blank, PROTECT, `related_name='submissions'`.
  - `Lead.screening_flagged`: BooleanField default False.

- [ ] **Step 1: Create the branch.** Run `git checkout -b screening-builder`.
- [ ] **Step 2: Write the failing tests** in `care/test_screening.py`, class `ScreeningModelTests(TestCase)`, with one `Company` per test:
  - `test_one_draft_and_one_published_version_per_company`: a second `DRAFT` for the same company raises `IntegrityError` (inside `transaction.atomic()`). So does a second `PUBLISHED`. Two `SUPERSEDED` are fine, and another company may have its own draft.
  - `test_question_keys_are_unique_within_a_version`: a duplicate `key` in one version raises `IntegrityError`. The same key in two versions is fine.
- [ ] **Step 3: Run** `.venv/bin/python manage.py test care.test_screening`. Expected: errors (models not defined).
- [ ] **Step 4: Add the models and fields.** Run `.venv/bin/python manage.py makemigrations care -n screening_form_versions`.
- [ ] **Step 5: Run** the same test. Expected: PASS.
- [ ] **Step 6: Commit** `git add care/models.py care/migrations/0070_*.py care/test_screening.py && git commit -m "Add screening form version and question models"`

### Task 2: Version 1, the live version and demo data

**Files:**
- Create: `care/screening_v1.py`, `care/screening.py`, `care/migrations/0071_screening_version_1.py`
- Modify: `care/demo.py:476-485` (set `form_version`)
- Test: `care/test_screening.py`

**Interfaces:**
- Consumes: the Task 1 models.
- Produces:
  - `care/screening_v1.py`: `VERSION_1_NOTE = 'Set up from the existing form'` and `VERSION_1_QUESTIONS: tuple[dict, ...]`. Each dict has keys `key, tier, answer_type, label, help_text, required, options, position`, where `options` is a list of `{value, label, flag: False, reason: ''}`. The values are below.
  - `care/screening.py`:
    - `MOVABLE_CORE_KEYS = ('adult', 'weight_related_condition', 'pregnancy', 'thyroid_history', 'pancreatitis')`
    - `FIXED_CORE_KEYS = ('health_context', 'heard_from')`
    - `CORE_KEYS = MOVABLE_CORE_KEYS + FIXED_CORE_KEYS`
    - `live_version(company) -> ScreeningFormVersion`: the published version. If the company has no version at all, it creates Version 1 inside `transaction.atomic()` after `Company.objects.select_for_update().get(pk=company.pk)`.
    - `movable_questions(version) -> list[ScreeningQuestion]`: rows not in `FIXED_CORE_KEYS`, by position.
    - `questions_by_key(version) -> dict[str, ScreeningQuestion]`
  - Migration `0071`: `create_version_1(apps, schema_editor)` with its **own** literal copy of the Version 1 data. For every `Company` it creates Version 1 (published, `published_at=timezone.now()`, note) and its rows if the company has no version. It then sets `form_version` on every `ScreeningQuestionnaire` of that company where it is null. Reverse is `migrations.RunPython.noop`.

Version 1 rows (patient-visible wording from `templates/portal/questionnaire.html:50-82`):

| pos | key | type | required | label | help_text |
|---|---|---|---|---|---|
| 1 | adult | yes_no | yes | Are you 18 or older? | |
| 2 | weight_related_condition | yes_no | yes | Do you have a weight-related condition? | High blood pressure, type 2 diabetes, sleep apnoea, fatty liver, high cholesterol. |
| 3 | pregnancy | yes_no | yes | Are you pregnant, breastfeeding, or planning a pregnancy within a year? | |
| 4 | thyroid_history | yes_no_unsure | yes | Any personal or family history of medullary thyroid cancer or MEN2? | |
| 5 | pancreatitis | yes_no | yes | Have you ever had pancreatitis? | |
| 90 | health_context | long_text | no | Chronic medication and allergies | Include the names and doses of any regular medication, and any allergies. If none, say “None”. |
| 91 | heard_from | multiple | no | How did you hear about us? | Optional. Tick all that apply. |

- Options for `yes_no` are Yes/No (`yes`/`no`). `yes_no_unsure` adds `unsure` / "Not sure". `heard_from` options are `Lead.HEARD_FROM` in order.
- All rows are tier `core`.

- [ ] **Step 1: Write the failing tests** in `care/test_screening.py`, class `VersionOneTests(TestCase)`:
  - `test_live_version_creates_version_1_once`: `live_version(company)` returns `number == 1`, `status == 'published'`, `note == VERSION_1_NOTE`, and 7 questions whose `(key, label, help_text, required, answer_type)` equal `VERSION_1_QUESTIONS`. A second call returns the same pk.
  - `test_version_1_matches_todays_form`: for each of the five yes/no keys, the row's option values equal `[value for value, _ in EligibilityQuestionnaireForm.base_fields[key].choices]`, and `required` equals the field's `required`. `heard_from` options equal `Lead.HEARD_FROM`.
  - `test_migration_copy_matches_module`: `importlib.import_module('care.migrations.0071_screening_version_1').VERSION_1_QUESTIONS == care.screening_v1.VERSION_1_QUESTIONS`.
  - `test_migration_backfills_existing_questionnaires`: create a Lead and a `ScreeningQuestionnaire` with no version, then call the migration's `create_version_1(django.apps.apps, None)`. The questionnaire's `form_version.number` is 1. Calling it again creates nothing new.
- [ ] **Step 2: Run** `.venv/bin/python manage.py test care.test_screening`. Expected: FAIL / ImportError.
- [ ] **Step 3: Implement** `care/screening_v1.py`, the `care/screening.py` functions above, and migration `0071`. The migration exposes `VERSION_1_QUESTIONS` at module level for the comparison test.
- [ ] **Step 4: In `care/demo.py:476`,** add `'form_version': live_version(meridian)` to the questionnaire's defaults.
- [ ] **Step 5: Run** `.venv/bin/python manage.py migrate`, then `.venv/bin/python manage.py test care.test_screening care.test_demo_data care.test_demo_catalogue practices.test_single_practice`. Expected: PASS.
- [ ] **Step 6: Commit** `git add care/screening.py care/screening_v1.py care/migrations/0071_*.py care/demo.py care/test_screening.py && git commit -m "Version 1 of the screening questions from today's form"`

### Task 3: The public form built from a version

**Files:**
- Modify: `care/forms.py:315-411` (`EligibilityQuestionnaireForm`)
- Test: `care/test_screening_form.py`

**Interfaces:**
- Consumes: `live_version`, `movable_questions`, `questions_by_key`, `CORE_KEYS`.
- Produces:
  - `EligibilityQuestionnaireForm(data=None, *, version: ScreeningFormVersion, bound_practice=None, **kwargs)`. `version` is required. `self.version` is kept.
  - `custom_field_name(key: str) -> str`, which returns `f'custom_{key}'` (module-level in `care/forms.py`).
  - `form.screening_items -> list[tuple[ScreeningQuestion, BoundField]]`: the movable block in order, used by the template.
  - `form.fixed_items -> dict[str, tuple[ScreeningQuestion, BoundField]]`: keys `health_context`, `heard_from`.
  - `form.cleaned_answers()`: today's dict plus `'custom': {key: value}`. Values are a str for yes_no/single/text, a list for multiple, `str(Decimal)` or `''` for number, and `''` or `[]` when an optional question is left blank.

- [ ] **Step 1: Write the failing tests** in `care/test_screening_form.py`. Use a company, `v1 = live_version(company)`, and a helper that builds a published v2 with extra custom rows (create rows directly; Task 7's services aren't needed):
  - `test_version_1_labels_and_help_come_from_the_version`: change the v1 `pancreatitis` label in the DB to "Pancreatitis ever?". The form's `fields['pancreatitis'].label` equals it. Choices and required are unchanged.
  - `test_each_custom_type_validates`:
    - Required yes_no with no answer → error.
    - single with an unknown value → error.
    - multiple `['a','b']` → `['a','b']`.
    - short_text with 201 characters → error.
    - number with `number_min=18`, value `17` → error. Value `40` → `'40'`.
    - Optional left blank → `''` / `[]`.
  - `test_cleaned_answers_keep_core_keys_and_add_custom`: valid data gives top-level `adult`… unchanged, and `answers['custom'] == {'c_surgery': 'yes'}`.
  - `test_custom_labels_are_escaped`: a custom label `<script>x</script>` renders escaped via `str(form['custom_c_x'].label_tag())`, with no raw `<script>`.
- [ ] **Step 2: Run** `.venv/bin/python manage.py test care.test_screening_form`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - In `__init__`, set the core fields' `label` / `help_text` from `questions_by_key(version)`.
  - For each custom row, add a field: yes_no and single → `ChoiceField` + `RadioSelect`; multiple → `MultipleChoiceField` + `CheckboxSelectMultiple`; short_text `CharField(max_length=200)`; long_text `CharField(max_length=2000, widget=Textarea(rows=4), strip=False)`; number → `DecimalField(min_value=number_min, max_value=number_max, max_digits=10, decimal_places=2)`. Use `required` from the row.
  - Choices come from `options` (`value`, `label`).
- [ ] **Step 4: Update every existing caller** that builds `EligibilityQuestionnaireForm` to pass `version=live_version(company)`. That is `portal/intake_views.py:149` and `:156` (Task 5 refines these) and any test that builds it (`grep -rn "EligibilityQuestionnaireForm(" --include=*.py .`).
- [ ] **Step 5: Run** `.venv/bin/python manage.py test care.test_screening_form portal.test_lead_intake portal.test_questionnaire_embed portal.test_questionnaire_checkout`. Expected: PASS.
- [ ] **Step 6: Commit** `git add care/forms.py care/test_screening_form.py portal/intake_views.py && git commit -m "Build the screening form from its version"` (also add any test files changed in Step 4).

### Task 4: Saving custom answers and flags

**Files:**
- Modify: `care/screening.py` (add `evaluate_flags`), `care/intake.py:56-141`
- Test: `care/test_screening_intake.py`

**Interfaces:**
- Consumes: `form.version`, `form.cleaned_answers()['custom']`.
- Produces:
  - `evaluate_flags(version, custom_answers: dict) -> list[dict]`: `{'key', 'answer', 'reason'}` for every chosen option whose `flag` is true (yes_no, single, multiple), in question order. A multiple-choice question gives one entry per flagged option chosen.
  - `screening_outcome(data, flags=()) -> tuple[status, reasons, rules_version]`:
    - DEBUG: core reasons + flag reasons (deduplicated, order kept). `REFERRED` if any, else `CLEARED`. Rules version `'prototype-rev2.6-demo'`.
    - Not DEBUG: `(PENDING, flag reasons, 'clinical-review-required')`.
  - `save_intake` additionally:
    - Sets `answers['flags']` and `answers['form_version'] = version.number`.
    - Sets `ScreeningQuestionnaire.form_version = form.version` in `update_or_create` defaults.
    - Sets `Lead.screening_flagged = bool(flags)`, which is included in `values` so the "changed" check notices it.

- [ ] **Step 1: Write the failing tests** in `care/test_screening_intake.py`. Use a published version with custom `c_surgery` (yes_no, Yes flagged with reason "Previous weight-loss surgery needs a clinician's review.") and `c_meds` (multiple, two flagged options sharing one reason):
  - `test_evaluate_flags_lists_flagged_choices_only`
  - `test_debug_flag_refers_and_adds_reason_after_core_reasons` (`@override_settings(DEBUG=True)`): status `REFERRED`, and the reason is last in `answers['screening_reasons']`.
  - `test_production_flag_keeps_pending_with_reason` (`DEBUG=False`): status `PENDING`, the reasons contain only the flag reason, and `lead.screening_flagged` is True.
  - `test_duplicate_reasons_are_listed_once`
  - `test_flags_never_decline`: no input ever produces `DECLINED`.
  - `test_saved_questionnaire_links_its_version`: `form_version == version` and `answers['form_version'] == version.number`.
  - `test_flagged_lead_cannot_check_out` (DEBUG): `care.checkout.can_check_out(lead, questionnaire)` is False.
- [ ] **Step 2: Run** `.venv/bin/python manage.py test care.test_screening_intake`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces above in `care/screening.py` and `care/intake.py`.
- [ ] **Step 4: Run** `.venv/bin/python manage.py test care.test_screening_intake portal.test_lead_intake portal.test_questionnaire_checkout`. Expected: PASS.
- [ ] **Step 5: Commit** `git add care/screening.py care/intake.py care/test_screening_intake.py && git commit -m "Save custom screening answers and flag leads for review"`

### Task 5: Public questionnaire view, token and template

**Files:**
- Modify: `portal/intake_views.py:110-195` (`QuestionnaireView`), `templates/portal/questionnaire.html:50-82`, `static/js/questionnaire.js`
- Create: `templates/portal/includes/screening_question.html` (one question card, core or custom)
- Test: `portal/test_questionnaire_versions.py`

**Interfaces:**
- Consumes: `live_version`, `EligibilityQuestionnaireForm(version=…)`, `custom_field_name`, `form.screening_items`, `form.fixed_items`.
- Produces:
  - Token payload: today's `{key, owner, notices}` plus `versions: {str(practice_id): version_id}` for every practice in `notice_context()`.
  - `QuestionnaireView.read_token(self) -> dict`: decodes `request.POST['submission_token']`. Raises `signing.BadSignature` on a bad, tampered, expired or different-browser token.
  - `QuestionnaireView.version_for(self, practice_id: str, payload: dict) -> ScreeningFormVersion`: looks up the id in `payload['versions']`. The version must belong to that practice and have status published or superseded, otherwise `BadSignature`.
  - The question card include renders each question with `id="screening-q-{{ key }}"`. Required choice questions (core and custom) carry `data-screening-question`, so `questionnaire.js` counts only those.
  - The template has `{% if preview %}` hooks for Task 8: a banner, no form `action`, a disabled submit.

- [ ] **Step 1: Write the failing tests** in `portal/test_questionnaire_versions.py`. Set it up like `portal/test_lead_intake.py:21-37`, but with `@override_settings(DEBUG=True, MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='meridian-health')`: one company `slug='meridian-health'`, `PracticeSettings`, and both consent documents.
  - `test_version_1_page_shows_todays_questions_in_order`: GET `portal:questionnaire`. The five core labels appear in the order adult → pancreatitis, then "Chronic medication and allergies", then "How did you hear about us?". The progress text reads "0 of 5 answered".
  - `test_custom_question_appears_and_is_saved`: publish v2 with a required `c_surgery` after `pancreatitis` (create rows directly). GET shows its label after pancreatitis and the progress text "0 of 6 answered". POST with `custom_c_surgery=yes` saves `answers['custom']['c_surgery'] == 'yes'`.
  - `test_multi_practice_form_uses_the_chosen_practices_version` (`@override_settings(MULTI_PRACTICE_ENABLED=True)`, with a second enabled company and its own consent documents): give the second company a published v2 that rewords `pancreatitis`. GET `?practice=<second pk>` shows that wording, and GET for the first shows Version 1's.
  - `test_submission_after_publish_saves_against_shown_version`: GET (v1 token), publish v2 with a required custom question, then POST the v1 answers. Expected: redirect to the result page, and the questionnaire's `form_version.number == 1`.
  - `test_tampered_version_is_rejected`: re-sign the payload with another company's version id (same `TOKEN_SALT`). Expected: 200 with the exact error text "This questionnaire has expired or belongs to another browser. Reload it before submitting." and no Lead.
  - `test_edit_uses_live_version_and_drops_retired_answers`: submit on v2 with `c_surgery`, publish v3 without it, open `?edit=1`, and resubmit. The same questionnaire row now has `form_version.number == 3`, `'c_surgery'` is not in `answers['custom']`, and `ScreeningQuestionnaire.objects.count()` is unchanged.
- [ ] **Step 2: Run** `.venv/bin/python manage.py test portal.test_questionnaire_versions`. Expected: FAIL.
- [ ] **Step 3: Implement the view.**
  - GET picks the version with `live_version(practice)` (the practice from `initial['practice']`, or the lead's company when editing). The edit prefill adds `{custom_field_name(k): v for k, v in answers.get('custom', {}).items() if k in questions_by_key(version)}`.
  - POST calls `read_token()` first, then `version_for(posted practice)`, then builds the form, then calls `save_intake`.
  - On `BadSignature`, build the form with the posted practice's live version (or the default practice's), bound to `request.POST`. Add the existing error text and render.
  - In multi-practice mode, `questionnaire.js` navigates to `?practice=<id>` when the practice select changes (not when editing). It replaces today's consent-only toggle for the questions.
- [ ] **Step 4: Implement the template.** Replace the five hand-written fieldsets (`questionnaire.html:50-74`) with `{% for question, field in form.screening_items %}{% include 'portal/includes/screening_question.html' %}{% endfor %}`. Medication and heard-from use `form.fixed_items`. Core markup stays byte-for-byte the same apart from the label and help source.
- [ ] **Step 5: Run** `.venv/bin/python manage.py test portal.test_questionnaire_versions portal.test_lead_intake portal.test_questionnaire_embed portal.test_questionnaire_checkout portal.test_patient_web_app`. Expected: PASS.
- [ ] **Step 6: Commit** `git add portal/intake_views.py templates/portal/questionnaire.html templates/portal/includes/screening_question.html static/js/questionnaire.js portal/test_questionnaire_versions.py && git commit -m "Public questionnaire renders and validates its screening version"`

### Task 6: Lead page and leads list

**Files:**
- Modify: `care/screening.py` (add `display_answers`), `portal/lead_views.py:20-106`, `templates/portal/staff_lead_detail.html:43-48`, `templates/portal/staff_leads.html:31`, `static/css/leads.css`
- Test: `portal/test_screening_leads.py`

**Interfaces:**
- Consumes: `answers['custom']`, `answers['flags']`, `Lead.screening_flagged`, `live_version`.
- Produces:
  - `display_answers(questionnaire) -> list[dict]` with `{'label', 'value', 'flag_reason'}`.
    - Order: locked (title, height, weight, BMI, as today's `lead_views.py:88`), then the movable block of `questionnaire.form_version` (falling back to `live_version(company)` when null), then medication, then heard-from.
    - Labels come from that version. Yes/no values display as Yes / No / Not sure, choice values as their option labels, and blanks as "Not recorded".
  - `LeadFilterForm.flags = TickedChoiceField(label='Flags', choices=(('flagged', 'Flagged for review'),))`. It filters `screening_flagged=True`, and its section is added to `ticked_sections(...)`.
  - `LeadDetailView` context adds `form_version_number`.

- [ ] **Step 1: Write the failing tests** in `portal/test_screening_leads.py`. Log in as a practice admin, as `LeadPageView` allows (`portal/lead_views.py:31-35`).
  - `test_lead_page_uses_the_answered_versions_wording`: a lead answered v2 ("Bariatric surgery?"), then v3 rewords it. The page shows the v2 wording and "Answered Version 2".
  - `test_flagged_answer_is_highlighted_with_reason`: the response contains the reason inside an element with class `staff-lead-flag`.
  - `test_flag_reasons_are_escaped_on_lead_page`: a reason `<b>x</b>` appears as `&lt;b&gt;x&lt;/b&gt;`.
  - `test_questionnaire_without_version_uses_version_1`: `form_version=None` gives 200 and shows Version 1's labels.
  - `test_leads_list_marks_and_filters_flagged`: on `portal:staff-lead-history`, a flagged lead shows `⚑`, and `?flags=flagged` lists only flagged leads.
- [ ] **Step 2: Run** `.venv/bin/python manage.py test portal.test_screening_leads`. Expected: FAIL.
- [ ] **Step 3: Implement.** Replace the fixed `fields` list in `LeadDetailView` with `display_answers`. Add the ⚑ marker (with `aria-label="Flagged for review"`) beside the screening status pill in `staff_leads.html:31`. Add the `staff-lead-flag` style to `static/css/leads.css`.
- [ ] **Step 4: Run** `.venv/bin/python manage.py test portal.test_screening_leads portal.test_lead_intake portal.test_followups`. Expected: PASS.
- [ ] **Step 5: Commit** `git add care/screening.py portal/lead_views.py templates/portal/staff_lead_detail.html templates/portal/staff_leads.html static/css/leads.css portal/test_screening_leads.py && git commit -m "Lead pages show the answered version and screening flags"`

### Task 7: Builder services (draft, edit, reorder, publish, discard)

**Files:**
- Modify: `care/screening.py`, `care/forms.py` (add `ScreeningQuestionForm`)
- Test: `care/test_screening.py` (class `BuilderServiceTests`)

**Interfaces:**
- Consumes: the models and `live_version`.
- Produces (all `@transaction.atomic`, all lock the company with `select_for_update()`, all call `record_audit`):
  - `draft_version(company) -> ScreeningFormVersion | None`
  - `start_draft(*, company, actor, request=None) -> ScreeningFormVersion`: returns the existing draft. Otherwise it copies the live version's rows into `number = max + 1`, `based_on = live`. Audit `screening.draft_started`.
  - `add_question(*, company, actor, request=None) -> ScreeningQuestion`: a custom yes_no row, label "New question", not required, Yes/No options unflagged, at the end of the movable block, key from `new_custom_key`. Refused (`ValidationError`) at 25 custom questions. Audit `screening.question_added`.
  - `save_question(*, question, actor, cleaned: dict, request=None) -> ScreeningQuestion`:
    - Core rows take only `label` and `help_text`.
    - Custom rows take everything in `ScreeningQuestionForm.cleaned_data`.
    - Refused if `question.version.status != 'draft'`.
    - Audit `screening.question_changed` with `{'key': ..., 'fields': [...]}`.
  - `retire_question(*, question, actor, request=None)`: custom rows in a draft only. Deletes the row. Audit `screening.question_retired`.
  - `reorder(*, company, actor, keys: list[str], request=None)`: `keys` must equal the draft's movable keys as a set. Rewrites positions 1..n. Audit `screening.reordered`.
  - `publish_draft(*, company, actor, request=None) -> ScreeningFormVersion`:
    - Re-validates every custom row with `ScreeningQuestionForm`, and raises `ValidationError` naming the question if any fails.
    - Moves the old published version to superseded, then the draft to published, setting `published_by` and `published_at`.
    - Audit `screening.published` with `{'number': n, 'changes': change_summary(...)}`.
  - `discard_draft(*, company, actor, request=None)`: deletes the draft. Audit `screening.draft_discarded`.
  - `change_summary(draft, live) -> dict`: `{'added': [labels], 'reworded': [labels], 'retired': [labels], 'reordered': bool}`, compared by key.
  - `new_custom_key(company, label) -> str`: see Global Constraints.
  - `ScreeningQuestionForm(forms.Form)`:
    - Fields: `answer_type` (custom types only: yes_no, single, multiple, short_text, long_text, number), `label`, `help_text`, `required`, `options`, `number_min`, `number_max`.
    - `options` is a JSON string of `[{value?, label, flag, reason}]`. An item whose `value` already exists on the question keeps it, so renaming an answer never changes what past leads stored. An item with no `value` gets `slugify(label)`, made unique within the question with `-2`, `-3`.
    - Enforces the limits in Global Constraints. yes_no options are fixed to Yes/No (labels editable, values `yes`/`no`).

- [ ] **Step 1: Write the failing tests:**
  - `test_start_draft_copies_live_and_returns_existing_draft`
  - `test_core_questions_only_take_label_and_help_text`: `save_question` on the `adult` core row with `answer_type='short_text'`, new options and `required=False` changes only the label and help text.
  - `test_custom_keys_are_never_reused`: add "Surgery?", publish, start a draft, retire it, add "Surgery?" again. The new key is `c_surgery-2`.
  - `test_saves_to_a_published_or_missing_draft_are_refused`: hold a draft row, publish, then `save_question` on that row raises `ValidationError`. After `discard_draft`, `ScreeningQuestion.objects.filter(pk=row.pk).exists()` is False.
  - `test_question_form_limits`: 1 option → error. 13 options → error. A flag with no reason → error. A 301-character label → error. A 26th custom question → `ValidationError`.
  - `test_renaming_an_answer_keeps_its_value`: a single-choice option `{value: 'sleeve', label: 'Sleeve'}` saved with label "Gastric sleeve" keeps `value == 'sleeve'`. A new option "Band" gets `value == 'band'`.
  - `test_reorder_requires_exactly_the_movable_keys`
  - `test_publish_supersedes_and_records_changes`: v1 becomes superseded and v2 published. The audit event `screening.published` has metadata `changes['added'] == ['Surgery?']`.
  - `test_publish_refuses_an_invalid_draft`: corrupt a row's options in the DB to `[]`, and publish raises with the question's label in the message.
- [ ] **Step 2: Run** `.venv/bin/python manage.py test care.test_screening`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces above.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git add care/screening.py care/forms.py care/test_screening.py && git commit -m "Screening builder services: draft, edit, reorder, publish, discard"`

### Task 8: Settings pages, preview and navigation

**Files:**
- Create: `portal/screening_views.py`, `portal/screening_urls.py`, `templates/portal/screening_questions.html` (overview), `templates/portal/screening_version.html` (read-only), `templates/portal/screening_draft.html` (editor shell), `templates/portal/includes/screening_builder_list.html`, `templates/portal/includes/screening_question_panel.html`
- Modify: `portal/urls.py` (include `portal.screening_urls` beside `portal.journey_urls`, line 29), `portal/navigation.py:43-47` (add tab `('screening', 'Screening questions', 'portal:screening-questions')` after Users and roles), `portal/page_navigation.py` (`STAFF_PAGES['screening-questions'] = Page('Screening questions', 'screening', 'super_admin')`; add `'screening-version', 'screening-draft'` to `HTML_PAGE_ROUTES`), `templates/portal/questionnaire.html` (the preview branch from Task 5), `docs/USER_GUIDE.md` (a short "Screening questions" section)
- Test: `portal/test_screening_builder.py`

**Interfaces:**
- Consumes: all of Task 7, `live_version`, and `QuestionnaireView`'s rendering (preview reuses `portal/questionnaire.html` with `preview=True`).
- Produces these URL names, all under `settings/screening/`:

| Name | Path | Method | Does |
|---|---|---|---|
| `screening-questions` | `` | GET | overview: live card + versions table with leads-answered counts |
| `screening-version` | `versions/<int:number>/` | GET | read-only list of that version |
| `screening-draft-start` | `draft/start/` | POST | `start_draft`, redirect to editor |
| `screening-draft` | `draft/` | GET | editor (`?q=<key>` selects a question); 404 if no draft |
| `screening-question-add` | `draft/questions/new/` | POST | `add_question`, redirect `screening-draft?q=<key>` |
| `screening-question-save` | `draft/questions/<slug:key>/` | POST | `ScreeningQuestionForm` → `save_question`; JSON `{ok: true, row_html}` or 400 `{ok: false, errors}` |
| `screening-question-retire` | `draft/questions/<slug:key>/retire/` | POST | `retire_question`, redirect |
| `screening-reorder` | `draft/order/` | POST | `keys` list → `reorder`; JSON `{ok}` |
| `screening-publish` | `draft/publish/` | POST | `publish_draft`, message + redirect to overview; on error redirect back with the message |
| `screening-discard` | `draft/discard/` | POST | `discard_draft`, redirect to overview |
| `screening-preview` | `preview/<int:number>/` | GET only | `xframe_options_sameorigin`; renders `portal/questionnaire.html` with `preview=True` for that version and its company's live consent documents |

- [ ] **Step 1: Write the failing tests** in `portal/test_screening_builder.py`. Use the fixture pattern of `care/test_journeys.py:104-116` (`BillingFixture` + a `super_admin` membership + `login()`).
  - `test_only_super_admins_reach_the_builder`: the practice admin and the doctor get 403 on every GET and POST URL above. The Super Admin gets 200 on the overview, which shows "Version 1" and "Set up from the existing form".
  - `test_settings_tab_lists_screening_questions`: the overview response contains a tab link to `reverse('portal:screening-questions')` labelled "Screening questions".
  - `test_editor_flow_start_add_save_publish`: start → add → save (label "Surgery?", Yes flagged with a reason) returns JSON `ok`. Publish redirects. The overview then shows Version 2 as published.
  - `test_save_with_errors_returns_400_json`: options `[]` → 400 with `errors['options']`.
  - `test_preview_is_get_only_same_origin_and_creates_no_lead`: GET gives 200, `X-Frame-Options: SAMEORIGIN`, contains "Preview, not submitted", and the submit button has `disabled`. POST gives 405. `Lead.objects.count()` is unchanged.
  - `test_publish_dialog_text`: the editor contains the exact publish dialog copy with N and N−1 filled in.
- [ ] **Step 2: Run** `.venv/bin/python manage.py test portal.test_screening_builder`. Expected: FAIL.
- [ ] **Step 3: Implement the views, URLs and navigation.** Every view checks Super Admin first, before it looks anything up.
- [ ] **Step 4: Implement the templates.**
  - **Editor shell:** a top bar (Draft · Version N, the change summary, Discard and Publish buttons opening `app-dialog` dialogs through `data-modal-open`).
  - **Left list** (`screening_builder_list.html`):
    - Locked blocks greyed, with 🔒 and the notes from Global Constraints.
    - Movable rows with a drag handle, Move up / Move down buttons, tier tags ("Core · reword only", "Custom"), ⚑ when flagged, Edit, and Retire for custom rows.
    - Fixed core rows (medication, heard-from) after the block.
  - **Middle:** one `screening_question_panel.html` per question, hidden unless selected, with a `<form>` posting to `screening-question-save`. Core panels show only label and help as inputs, plus the read-only note. Under each flag reason is the hint "The patient sees this reason."
  - **Right:** `<iframe class="screening-preview" src="{% url 'portal:screening-preview' draft.number %}" title="Patient preview">`.
  - Match the mockup's tiers and badges using the existing console styles.
- [ ] **Step 5: Add the user guide section** in `docs/USER_GUIDE.md`, about 10 lines: where the tab is, the tiers, draft → preview → publish, what a flag does on a live server versus local.
- [ ] **Step 6: Run** `.venv/bin/python manage.py test portal.test_screening_builder portal.test_page_navigation portal.test_staff_menu`. Expected: PASS.
- [ ] **Step 7: Commit** `git add portal/screening_views.py portal/screening_urls.py portal/urls.py portal/navigation.py portal/page_navigation.py templates/portal/screening_*.html templates/portal/includes/screening_builder_list.html templates/portal/includes/screening_question_panel.html templates/portal/questionnaire.html docs/USER_GUIDE.md portal/test_screening_builder.py && git commit -m "Settings > Screening questions: overview, editor, preview, publish"`

### Task 9: Editor behaviour (autosave, live preview, reorder, narrow screens)

**Files:**
- Create: `static/js/screening_builder.js`, `static/css/screening_builder.css` (both linked from `screening_draft.html` with `?v=20261127a`)
- Test: `portal/test_screening_builder.py` (markup hooks) plus a browser check

**Interfaces:**
- Consumes: the Task 8 URLs and markup.
- Template hooks the JS relies on (Task 8 must render them; this task's test asserts them):
  - `[data-screening-list]`, with rows `[data-question-key]`
  - `[data-screening-panel="<key>"]` forms
  - `[data-move="up|down"]`
  - `[data-reorder-url]`
  - `iframe.screening-preview`
  - `[data-preview-open]` (the narrow-screen button) and `dialog#screening-preview-dialog`
- Behaviour:
  - Selecting a row shows its panel and updates `?q=`.
  - Input in a panel autosaves with `fetch` (POST, `X-CSRFToken` from the panel's `csrfmiddlewaretoken`, `Accept: application/json`), debounced 600 ms. On `ok`, it swaps the row with `row_html`, reloads the iframe, and on load scrolls the iframe to `#screening-q-<key>`. On 400, it shows the errors beside the fields.
  - Drag (native HTML5 drag events) or Move up / Move down posts the new key order to `[data-reorder-url]`, then reloads the preview.
  - At ≤ 1100px the preview column is hidden by CSS and `[data-preview-open]` opens the dialog with the iframe.

- [ ] **Step 1: Write the failing test** `test_editor_renders_the_hooks_the_script_needs`: the editor HTML contains each hook selector above, once per question where relevant.
- [ ] **Step 2: Run** `.venv/bin/python manage.py test portal.test_screening_builder`. Expected: FAIL until the hooks exist, then PASS.
- [ ] **Step 3: Implement** `screening_builder.js`, registered through `window.MeridianPages?.register` like `static/js/shipping_workflow.js:109`, and the CSS.
- [ ] **Step 4: Browser check** (Playwright MCP, dev server at `http://127.0.0.1:8001`, signed in as the demo Super Admin). Expected results:
  1. Settings shows the "Screening questions" tab and Version 1.
  2. Edit questions → Add question → type "Have you had weight-loss surgery?". The preview shows the new question within about a second, scrolled into view.
  3. Tick "Flag for review" on Yes with an empty reason. The reason field shows an error and nothing breaks.
  4. Move it above "Have you ever had pancreatitis?". The preview order follows.
  5. At 1000px wide the preview folds into the Preview button.
  6. Publish. The public `/screening/` page shows the question, and answering Yes leads to a lead with ⚑ in Lead history.
- [ ] **Step 5: Run the full suite** `.venv/bin/python manage.py test --parallel auto`. Expected: all pass (about 2,400 tests).
- [ ] **Step 6: Commit** `git add static/js/screening_builder.js static/css/screening_builder.css templates/portal/screening_draft.html templates/portal/includes/screening_builder_list.html templates/portal/includes/screening_question_panel.html portal/test_screening_builder.py && git commit -m "Screening editor: autosave, live preview, reorder, narrow screens"`
