from django.db import transaction
from django.utils import timezone

from practices.models import Company

from .models import ScreeningFormVersion, ScreeningQuestion
from .screening_v1 import VERSION_1_NOTE, VERSION_1_QUESTIONS

MOVABLE_CORE_KEYS = ('adult', 'weight_related_condition', 'pregnancy', 'thyroid_history', 'pancreatitis')
FIXED_CORE_KEYS = ('health_context', 'heard_from')
CORE_KEYS = MOVABLE_CORE_KEYS + FIXED_CORE_KEYS


def live_version(company):
    """The published screening form version; Version 1 is created only for a company with no version at all."""
    published = ScreeningFormVersion.objects.filter(company=company, status=ScreeningFormVersion.Status.PUBLISHED).first()
    if published is not None:
        return published  # fast path: no lock on the common read path
    with transaction.atomic():
        Company.objects.select_for_update().get(pk=company.pk)
        published = ScreeningFormVersion.objects.filter(company=company, status=ScreeningFormVersion.Status.PUBLISHED).first()
        if published is not None:
            return published
        if ScreeningFormVersion.objects.filter(company=company).exists():
            raise ScreeningFormVersion.DoesNotExist(f'{company} has screening versions but none is published.')
        version = ScreeningFormVersion.objects.create(
            company=company, number=1, status=ScreeningFormVersion.Status.PUBLISHED,
            published_at=timezone.now(), note=VERSION_1_NOTE)
        ScreeningQuestion.objects.bulk_create([
            ScreeningQuestion(company=version.company, version=version, **{**q, 'options': [dict(o) for o in q['options']]})
            for q in VERSION_1_QUESTIONS
        ])
        return version


def movable_questions(version):
    return list(version.questions.exclude(key__in=FIXED_CORE_KEYS).order_by('position'))


def questions_by_key(version):
    return {q.key: q for q in version.questions.all()}


def evaluate_flags(version, custom_answers):
    """Flag entries for every chosen flagged option, in the order the patient sees the questions."""
    if version is None:
        return []
    Type = ScreeningQuestion.AnswerType
    flags = []
    for question in movable_questions(version):
        if question.tier != ScreeningQuestion.Tier.CUSTOM or question.answer_type not in (Type.YES_NO, Type.SINGLE, Type.MULTIPLE):
            continue
        answer = custom_answers.get(question.key)
        chosen = answer if isinstance(answer, list) else [answer]
        for option in question.options:
            if option.get('flag') and option.get('value') in chosen:
                flags.append({'key': question.key, 'answer': option['value'], 'reason': option.get('reason', '')})
    return flags


_YES_NO_LABELS = {'yes': 'Yes', 'no': 'No', 'unsure': 'Not sure'}
NOT_RECORDED = 'Not recorded'


def _display_value(question, value):
    """One saved answer as staff read it: option labels from the answered version, text as typed."""
    if value in (None, '', []):
        return NOT_RECORDED
    labels = dict(_YES_NO_LABELS) if question is None or question.answer_type in ('yes_no', 'yes_no_unsure') else {}
    if question is not None:
        labels.update({option.get('value'): option.get('label', option.get('value')) for option in question.options})
    if isinstance(value, list):
        return ', '.join(str(labels.get(item, item)) for item in value)
    return str(labels.get(value, value))


def display_answers(questionnaire):
    """The lead's answers in the wording of the version they answered: [{'label', 'value', 'flag_reason'}].

    Locked details first, then that version's movable block, then medication and heard-from. A flag shows when it was
    flagged at submission (answers['flags']), whatever the version's options say now."""
    from practices.models import Patient

    answers = questionnaire.answers or {}
    version = questionnaire.form_version or live_version(questionnaire.company)
    by_key = questions_by_key(version)
    reasons = {}
    for flag in answers.get('flags', []):
        if flag.get('reason'):
            reasons.setdefault(flag.get('key'), []).append(flag['reason'])
    rows = []

    def add(label, value, key=None):
        rows.append({'label': label, 'value': value or NOT_RECORDED, 'flag_reason': '; '.join(reasons.get(key, []))})

    add('Title', dict(Patient.Title.choices).get(answers.get('title'), ''))
    add('Height (cm)', answers.get('height_cm') and str(answers['height_cm']))
    add('Weight (kg)', answers.get('weight_kg') and str(answers['weight_kg']))
    add('BMI at submission', answers.get('bmi') and str(answers['bmi']))
    custom = answers.get('custom') or {}
    for question in movable_questions(version):
        value = custom.get(question.key) if question.tier == ScreeningQuestion.Tier.CUSTOM else answers.get(question.key)
        add(question.label, _display_value(question, value), question.key)
    for key, fallback in (('health_context', 'Chronic medication and allergies'), ('heard_from', 'How did you hear about us?')):
        question = by_key.get(key)
        add(question.label if question else fallback, _display_value(question, answers.get(key)), key)
    return rows


# --- Builder services: draft, edit, reorder, publish, discard -------------------------------------------------------
import copy
import json

from django.core.exceptions import ValidationError
from django.utils.text import slugify

from .services import record_audit

STALE_DRAFT = 'This draft was published or discarded. Reload the page.'
MAX_CUSTOM_QUESTIONS = 25
_Status = ScreeningFormVersion.Status


def _lock(company):
    return Company.objects.select_for_update().get(pk=company.pk)


def _draft(company):
    draft = ScreeningFormVersion.objects.filter(company=company, status=_Status.DRAFT).first()
    if draft is None:
        raise ValidationError(STALE_DRAFT)
    return draft


def draft_version(company):
    return ScreeningFormVersion.objects.filter(company=company, status=_Status.DRAFT).first()


def new_custom_key(company, label, exclude_pk=None):
    """`c_` + slug of the label (50 characters at most), `-2`, `-3`... if any version of the company already uses it."""
    used = set(ScreeningQuestion.objects.filter(company=company).exclude(pk=exclude_pk).values_list('key', flat=True))
    base = ('c_' + (slugify(label) or 'question'))[:50].rstrip('-_')
    key, n = base, 2
    while key in used:
        suffix = f'-{n}'
        key, n = base[:50 - len(suffix)] + suffix, n + 1
    return key


@transaction.atomic
def start_draft(*, company, actor, request=None):
    _lock(company)
    existing = draft_version(company)
    if existing is not None:
        return existing
    live = live_version(company)
    number = max(ScreeningFormVersion.objects.filter(company=company).values_list('number', flat=True)) + 1
    draft = ScreeningFormVersion.objects.create(company=company, number=number, status=_Status.DRAFT, based_on=live, created_by=actor)
    ScreeningQuestion.objects.bulk_create([
        ScreeningQuestion(
            company=company, version=draft, key=q.key, tier=q.tier, answer_type=q.answer_type, label=q.label,
            help_text=q.help_text, required=q.required, options=copy.deepcopy(q.options), number_min=q.number_min,
            number_max=q.number_max, position=q.position)
        for q in live.questions.all()
    ])
    record_audit(company=company, actor=actor, action='screening.draft_started', target=draft, request=request,
                 metadata={'number': draft.number, 'based_on': live.number})
    return draft


@transaction.atomic
def add_question(*, company, actor, request=None):
    _lock(company)
    draft = _draft(company)
    movable = movable_questions(draft)
    if sum(q.tier == ScreeningQuestion.Tier.CUSTOM for q in movable) >= MAX_CUSTOM_QUESTIONS:
        raise ValidationError(f'A form can have at most {MAX_CUSTOM_QUESTIONS} custom questions.')
    label = 'New question'
    row = ScreeningQuestion.objects.create(
        company=company, version=draft, key=new_custom_key(company, label), tier=ScreeningQuestion.Tier.CUSTOM,
        answer_type=ScreeningQuestion.AnswerType.YES_NO, label=label, required=False,
        options=[{'value': 'yes', 'label': 'Yes', 'flag': False, 'reason': ''},
                 {'value': 'no', 'label': 'No', 'flag': False, 'reason': ''}],
        position=max((q.position for q in movable), default=0) + 1)
    record_audit(company=company, actor=actor, action='screening.question_added', target=row, request=request,
                 metadata={'key': row.key, 'label': row.label})
    return row


def _locked_draft_row(question):
    """The question re-read under the company lock, only if its version is still the company's draft."""
    company = question.company
    _lock(company)
    draft = _draft(company)
    row = ScreeningQuestion.objects.filter(pk=question.pk, company=company, version=draft).first()
    if row is None:
        raise ValidationError(STALE_DRAFT)
    return row


_CUSTOM_FIELDS = ('answer_type', 'label', 'help_text', 'required', 'options', 'number_min', 'number_max')


@transaction.atomic
def save_question(*, question, actor, cleaned, request=None):
    row = _locked_draft_row(question)
    fields = ('label', 'help_text') if row.tier == ScreeningQuestion.Tier.CORE else _CUSTOM_FIELDS
    changed = []
    for name in fields:
        if name in cleaned and getattr(row, name) != cleaned[name]:
            setattr(row, name, cleaned[name])
            changed.append(name)
    if row.tier == ScreeningQuestion.Tier.CUSTOM and 'label' in changed and not ScreeningQuestion.objects.filter(
            company=row.company, key=row.key).exclude(pk=row.pk).exists():
        # A question no version has published yet still takes its key from its label; a published key never changes.
        row.key = new_custom_key(row.company, row.label, exclude_pk=row.pk)
        changed.append('key')
    row.save()
    record_audit(company=row.company, actor=actor, action='screening.question_changed', target=row, request=request,
                 metadata={'key': row.key, 'fields': changed})
    return row


@transaction.atomic
def retire_question(*, question, actor, request=None):
    row = _locked_draft_row(question)
    if row.tier != ScreeningQuestion.Tier.CUSTOM:
        raise ValidationError('Built-in questions cannot be retired.')
    key, label = row.key, row.label
    company = row.company
    row.delete()
    record_audit(company=company, actor=actor, action='screening.question_retired', request=request,
                 metadata={'key': key, 'label': label})


@transaction.atomic
def reorder(*, company, actor, keys, request=None):
    _lock(company)
    draft = _draft(company)
    by_key = {q.key: q for q in movable_questions(draft)}
    if len(keys) != len(set(keys)) or set(keys) != set(by_key):
        raise ValidationError('The order does not match the questions on this draft. Reload the page.')
    for position, key in enumerate(keys, start=1):
        if by_key[key].position != position:
            ScreeningQuestion.objects.filter(pk=by_key[key].pk).update(position=position)
    record_audit(company=company, actor=actor, action='screening.reordered', target=draft, request=request,
                 metadata={'keys': list(keys)})


def _question_form(row):
    from .forms import ScreeningQuestionForm
    data = {'answer_type': row.answer_type, 'label': row.label, 'help_text': row.help_text,
            'options': json.dumps(row.options), 'number_min': row.number_min, 'number_max': row.number_max}
    if row.required:
        data['required'] = 'on'
    return ScreeningQuestionForm(data, question=row)


def change_summary(draft, live):
    """What changed between two versions, compared by key: added, reworded, retired (labels) and reordered (bool)."""
    new = {q.key: q for q in draft.questions.order_by('position', 'pk')}
    old = {q.key: q for q in live.questions.order_by('position', 'pk')} if live is not None else {}
    compared = ('label', 'help_text', 'answer_type', 'required', 'options')
    reworded = [q.label for key, q in new.items()
                if key in old and any(getattr(q, f) != getattr(old[key], f) for f in compared)]
    new_order = [q.key for q in movable_questions(draft) if q.key in old]
    old_order = [q.key for q in movable_questions(live) if q.key in new] if live is not None else []
    return {'added': [q.label for k, q in new.items() if k not in old], 'reworded': reworded,
            'retired': [q.label for k, q in old.items() if k not in new], 'reordered': new_order != old_order}


@transaction.atomic
def publish_draft(*, company, actor, request=None):
    _lock(company)
    draft = _draft(company)
    custom = list(draft.questions.filter(tier=ScreeningQuestion.Tier.CUSTOM).order_by('position'))
    if len(custom) > MAX_CUSTOM_QUESTIONS:
        raise ValidationError(f'A form can have at most {MAX_CUSTOM_QUESTIONS} custom questions.')
    for row in custom:
        form = _question_form(row)
        if not form.is_valid():
            problems = '; '.join(m for errors in form.errors.values() for m in errors)
            raise ValidationError(f'“{row.label}” needs fixing before you publish: {problems}')
    live = ScreeningFormVersion.objects.filter(company=company, status=_Status.PUBLISHED).first()
    changes = change_summary(draft, live)
    if live is not None:
        live.status = _Status.SUPERSEDED
        live.save(update_fields=['status'])
    draft.status = _Status.PUBLISHED
    draft.published_by = actor
    draft.published_at = timezone.now()
    draft.save(update_fields=['status', 'published_by', 'published_at'])
    record_audit(company=company, actor=actor, action='screening.published', target=draft, request=request,
                 metadata={'number': draft.number, 'changes': changes})
    return draft


@transaction.atomic
def discard_draft(*, company, actor, request=None):
    _lock(company)
    draft = _draft(company)
    number = draft.number
    draft.delete()
    record_audit(company=company, actor=actor, action='screening.draft_discarded', request=request,
                 metadata={'number': number})
