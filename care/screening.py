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
