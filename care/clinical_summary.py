"""Summary data on the patient record: an editable layer over the screening questionnaire.

Until a clinician edits it, the values are read from the patient's screening questionnaire and
their own medical profile, and nothing is written. The first edit stores the summary. Every edit
is kept with the field, the old and new value, who made it and when. The questionnaire and the
patient's profile are never changed.
"""

from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q

from practices.models import CompanyMembership

from .clinical import _lock_context
from .models import Lead, PatientClinicalSummary, PatientMedicalProfile, PatientSummaryEdit, ScreeningQuestionnaire, WeightEntry
from .services import record_audit


FIELDS = {
    'goal_weight_kg': 'Goal weight',
    'medical_history': 'Medical history',
    'surgical_history': 'Surgical history',
    'chronic_medication': 'Chronic medication',
    'allergies': 'Allergies',
    'pancreatitis': 'History of pancreatitis',
    'thyroid_cancer': 'Medullary thyroid cancer / MEN2',
    'pregnancy': 'Pregnant or breastfeeding',
}
TEXT_FIELDS = ('medical_history', 'surgical_history', 'chronic_medication', 'allergies')
FLAG_CHOICES = {
    'pancreatitis': ('no', 'yes'),
    'thyroid_cancer': ('no', 'yes', 'unsure'),
    'pregnancy': ('no', 'yes', 'not_applicable'),
}
ANSWER_LABELS = dict(PatientClinicalSummary.Answer.choices)


def _decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number.quantize(Decimal('0.1')) if number > 0 else None


def screening(company, patient):
    """The submitted screening questionnaire for the enquiry this patient came from, if any."""
    lead = Lead.objects.for_company(company).filter(converted_patient=patient).first()
    if lead is None:
        return None
    return ScreeningQuestionnaire.objects.for_company(company).filter(lead=lead, stage=1).first()


def seed_values(company, patient):
    questionnaire = screening(company, patient)
    answers = questionnaire.answers if questionnaire and isinstance(questionnaire.answers, dict) else {}
    profile = PatientMedicalProfile.objects.for_company(company).filter(patient=patient).first()
    own = profile.answers if profile and isinstance(profile.answers, dict) else {}

    def text(source, key):
        value = source.get(key)
        return value.strip() if isinstance(value, str) else ''

    def answer(key, allowed):
        value = answers.get(key)
        return value if value in allowed else ''

    first_weight = WeightEntry.objects.for_company(company).filter(patient=patient).order_by('recorded_on', 'pk').first()
    return {
        'height_cm': _decimal(answers.get('height_cm')),
        'start_weight_kg': _decimal(answers.get('weight_kg')) or (_decimal(first_weight.weight_kg) if first_weight else None),
        'medical_history': text(own, 'medical_history') or (
            'Weight-related condition reported at screening' if answers.get('weight_related_condition') == 'yes' else ''),
        'surgical_history': text(own, 'surgical_history'),
        'chronic_medication': text(own, 'medications') or text(answers, 'health_context'),
        'allergies': text(own, 'allergies'),
        'pancreatitis': answer('pancreatitis', FLAG_CHOICES['pancreatitis']),
        'thyroid_cancer': answer('thyroid_history', FLAG_CHOICES['thyroid_cancer']),
        'pregnancy': answer('pregnancy', FLAG_CHOICES['pregnancy']),
        'seeded_from': questionnaire.submitted_at if questionnaire else None,
    }


def get_summary(company, patient):
    """The stored summary, or an unsaved one filled from the screening. Reading never writes."""
    stored = PatientClinicalSummary.objects.for_company(company).filter(patient=patient).first()
    if stored is not None:
        return stored
    return PatientClinicalSummary(company=company, patient=patient, **seed_values(company, patient))


def flag_level(field, value):
    """'flag' for a Yes, 'caution' for an unsure thyroid answer, otherwise ''."""
    if value == 'yes':
        return 'flag'
    if field == 'thyroid_cancer' and value == 'unsure':
        return 'caution'
    return ''


def summary_safety_flags(company, patient, summary=None):
    summary = summary or get_summary(company, patient)
    return [
        {'field': field, 'label': FIELDS[field], 'value': ANSWER_LABELS[getattr(summary, field)],
         'level': flag_level(field, getattr(summary, field))}
        for field in FLAG_CHOICES if flag_level(field, getattr(summary, field))
    ]


def display_value(field, value):
    if value in (None, ''):
        return 'Not recorded'
    if field == 'goal_weight_kg':
        return f'{value.normalize():f} kg' if isinstance(value, Decimal) else f'{value} kg'
    if field in FLAG_CHOICES:
        return ANSWER_LABELS.get(value, value)
    return value


def _require_clinical_access(company, actor):
    if not CompanyMembership.objects.filter(
        company=company, company__is_active=True, user=actor, user__is_active=True, is_active=True,
    ).filter(Q(clinician_type__in=CompanyMembership.CLINICIAN_TYPES) | Q(role=CompanyMembership.Role.SUPER_ADMIN)).exists():
        raise PermissionDenied('Only a clinician or a Super Admin can edit summary data.')


def _clean(field, value):
    if field not in FIELDS:
        raise ValidationError('Choose a summary field to edit.')
    if not isinstance(value, str):
        raise ValidationError('Enter a value.')
    value = value.strip()
    if field == 'goal_weight_kg':
        try:
            number = Decimal(value)
        except InvalidOperation:
            number = None
        if number is None or not number.is_finite() or not 40 <= number <= 250:
            raise ValidationError('Give a goal weight between 40 and 250 kg.')
        return number.quantize(Decimal('0.1'))
    if field in FLAG_CHOICES:
        if value not in FLAG_CHOICES[field]:
            raise ValidationError('Choose one of the listed answers.')
        return value
    if len(value) > 2000:
        raise ValidationError('Keep this to 2,000 characters or fewer.')
    return value


@transaction.atomic
def edit_summary(*, company, patient, actor, field, value, request=None):
    company, patient = _lock_context(company, patient, actor)
    _require_clinical_access(company, actor)
    new = _clean(field, value)
    summary = PatientClinicalSummary.objects.select_for_update().for_company(company).filter(patient=patient).first()
    if summary is None:
        summary = PatientClinicalSummary(company=company, patient=patient, **seed_values(company, patient))
        summary.full_clean()
        summary.save()
    old = getattr(summary, field)
    if old == new or (old in (None, '') and new in (None, '')):
        return summary
    setattr(summary, field, new)
    summary.full_clean()
    summary.save()
    PatientSummaryEdit.objects.create(
        company=company, patient=patient, summary=summary, field=field, edited_by=actor,
        old_value=display_value(field, old), new_value=display_value(field, new),
    )
    # The audit trail names the field only; the clinical values stay in the edit history.
    record_audit(company=company, actor=actor, patient=patient, action='summary.updated', target=summary,
                 request=request, metadata={'field': field})
    return summary
