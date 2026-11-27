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
