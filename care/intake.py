"""Public enquiry capture. Payment and patient-account conversion are absent.

The mock's screening checks are for local preview only, not clinical approval.
Production submissions remain pending until a reviewed clinical workflow exists.
"""

import hashlib
import json
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from practices.models import Company

from .models import ConsentDocument, ConsentRecord, Lead, ScreeningQuestionnaire
from .services import record_audit


CONSENT_KINDS = (ConsentDocument.Kind.SERVICE, ConsentDocument.Kind.TELEHEALTH)
DRAFT_VERSION = 'draft-demo-2026-09'


def practice_notices(company):
    documents = []
    for kind in CONSENT_KINDS:
        document = ConsentDocument.objects.for_company(company).filter(
            kind=kind, is_active=True, effective_from__lte=timezone.localdate(),
        ).order_by('-effective_from', '-created_at', '-pk').first()
        if document:
            documents.append({'kind': kind, 'document_id': document.pk, 'version': document.version,
                              'title': document.title, 'body': document.body})
        elif settings.DEBUG:
            documents.append({
                'kind': kind, 'document_id': None, 'version': DRAFT_VERSION,
                'title': 'Draft service and privacy notice' if kind == 'service' else 'Draft telehealth consent',
                'body': (
                    f'Demo notice for {company.name}: your contact details and answers are saved as an enquiry '
                    'for the authorised practice team. Submission does not create a login or patient account. '
                    'This draft must be replaced with approved practice terms before live intake.'
                    if kind == 'service' else
                    'You consent to assessment by video consultation. A physical examination is not possible '
                    'by video, and a prescription is not guaranteed. This is draft demonstration wording.'
                ),
            })
    return documents


def notice_fingerprint(documents):
    return hashlib.sha256(json.dumps(documents, sort_keys=True).encode()).hexdigest()


def screening_outcome(data):
    if not settings.DEBUG:
        return Lead.ScreeningStatus.PENDING, [], 'clinical-review-required'
    # Mirror the supplied mock for demo navigation only. Use unrounded BMI at
    # thresholds: a displayed 27.00 must not round a sub-threshold value up.
    bmi = data['weight_kg'] / (data['height_cm'] / 100) ** 2
    reasons = []
    checks = (
        (data['adult'] == 'no', 'The prototype consultation pathway is for adults aged 18 or older.'),
        (data['pregnancy'] == 'yes', 'Your pregnancy or breastfeeding answer needs a clinician’s review.'),
        (data['thyroid_history'] != 'no', 'Your thyroid/MEN2 history answer needs a clinician’s review.'),
        (data['pancreatitis'] == 'yes', 'Your pancreatitis history needs a clinician’s review.'),
        (bmi < Decimal('27'), 'Your BMI is below the prototype’s screening threshold.'),
        (Decimal('27') <= bmi < Decimal('30') and data['weight_related_condition'] == 'no',
         'The prototype requires a weight-related condition at a BMI from 27 to below 30.'),
    )
    for needs_review, reason in checks:
        if needs_review:
            reasons.append(reason)
    return (Lead.ScreeningStatus.REFERRED if reasons else Lead.ScreeningStatus.CLEARED,
            reasons, 'prototype-rev2.6-demo')


def save_intake(*, form, submission_key, expected_notices, request=None):
    """Persist only an enquiry, with atomic consent capture and replay handling."""
    if not form.is_valid():
        raise ValidationError('Correct the questionnaire before submitting.')
    data = form.cleaned_data
    company = data['practice']
    with transaction.atomic():
        company = Company.objects.select_for_update().filter(pk=company.pk).first()
        if company is None or not company.is_active:
            raise ValidationError('This practice is no longer accepting enquiries.')
        documents = practice_notices(company)
        if len(documents) != len(CONSENT_KINDS) or notice_fingerprint(documents) != expected_notices:
            raise ValidationError('The practice notices have changed or are unavailable. Reload the questionnaire and read them before accepting again.')
        status, reasons, rules_version = screening_outcome(data)
        values = {field: data[field] for field in ('first_name', 'last_name', 'email', 'phone', 'id_number')}
        answers = form.cleaned_answers()
        answers.update(screening_reasons=reasons, screening_rules_version=rules_version, consent_snapshot=documents)
        values.update(bmi=answers['bmi'], screening_status=status)
        lead = Lead.objects.select_for_update().filter(submission_key=submission_key).first()
        created = lead is None
        if created:
            try:
                with transaction.atomic():
                    lead = Lead.objects.create(company=company, submission_key=submission_key, **values)
            except IntegrityError:
                # The unique submission key also handles two concurrent posts.
                lead = Lead.objects.select_for_update().get(submission_key=submission_key)
                created = False
        if lead.company_id != company.pk:
            raise ValidationError('This enquiry belongs to another practice. Start a new questionnaire to choose a different practice.')
        if lead.converted_patient_id or lead.stage in (Lead.Stage.CONVERTED, Lead.Stage.CLOSED):
            raise ValidationError('This enquiry can no longer be changed through the public form.')
        questionnaire = ScreeningQuestionnaire.objects.for_company(company).filter(lead=lead, stage=1).first()
        changed = created or questionnaire is None or questionnaire.answers != answers or any(
            str(getattr(lead, field)) != str(value) for field, value in values.items()
        )
        if not changed:
            return lead
        for field, value in values.items():
            setattr(lead, field, value)
        lead.stage = Lead.Stage.QUESTIONNAIRE
        lead.full_clean()
        lead.save()
        ScreeningQuestionnaire.objects.update_or_create(
            company=company, lead=lead, stage=1,
            defaults={'status': ScreeningQuestionnaire.Status.SUBMITTED, 'answers': answers,
                      'submitted_at': timezone.now(), 'reviewed_by': None, 'clinical_notes': ''},
        )
        for document in documents:
            # A single checked acceptance covers these two displayed notices;
            # it never silently creates marketing consent or an account.
            ConsentRecord.objects.get_or_create(
                company=company, lead=lead, consent_type=document['kind'],
                document_id=document['document_id'], document_version=document['version'], accepted=True,
                defaults={'accepted_at': timezone.now(), 'source': 'public-questionnaire',
                          'ip_address': (request.META.get('REMOTE_ADDR') or None) if request else None},
            )
        record_audit(company=company, actor=None, action='lead.created' if created else 'lead.updated',
                     target=lead, request=request, metadata={'screening_status': status, 'stage': lead.stage})
        return lead
