"""The clinical Overview of a patient record: the log, the prescription, the weight chart and summary data.

Shown to clinicians and Super Admins. Practice administrators keep the administrative Overview.
The log shows clinical entries only (signed consult notes, blood forms and prescriptions) and what
was dispensed. Everything else stays in the audit trail and the History tab.
"""

from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from care.clinical_summary import (
    ANSWER_LABELS, FIELDS, FLAG_CHOICES, flag_level, get_summary, screening, summary_safety_flags,
)
from care.models import (
    ClinicalEncounter, LabRequest, MedicationProduct, PatientSummaryEdit, Shipment, TreatmentAuthorization, WeightEntry,
)
from care.prescriptions import (
    MONTH_CHOICES, current_prescription, expiry_preview, prescription_label, prescription_options, prescription_state,
    renewal_expiry, strength_mg,
)

from .record_chart import doctor_weight_chart
from .workflow_context import make_workflow_context


LOG_LIMIT = 200


def signed_consults(company, patient):
    """Signed consult notes, newest first: log entries, and the dots on the weight chart."""
    return [{'id': f'consult-{encounter.pk}', 'kind': 'consult', 'at': encounter.occurred_at,
             'by': (encounter.signed_by or encounter.clinician).full_name, 'tag': '',
             'text': encounter.clinical_summary, 'consult_id': encounter.pk}
            for encounter in ClinicalEncounter.objects.for_company(company).filter(
                patient=patient, status=ClinicalEncounter.Status.SIGNED,
            ).select_related('clinician', 'signed_by').order_by('-occurred_at', '-pk')[:LOG_LIMIT]]


def clinical_log(company, patient):
    entries = signed_consults(company, patient)
    for lab in LabRequest.objects.for_company(company).filter(patient=patient).select_related(
        'requested_by', 'pathcare_form',
    ).defer('result_summary').order_by('-created_at', '-pk')[:LOG_LIMIT]:
        form = getattr(lab, 'pathcare_form', None)
        entries.append({'id': f'blood-{lab.pk}', 'kind': 'blood', 'at': lab.created_at,
                        'by': lab.requested_by.full_name if lab.requested_by_id else '', 'automatic': not lab.requested_by_id,
                        'tag': 'Blood form', 'form': form,
                        'text': f'PathCare form for {form.tests} sent.' if form else f'Blood form for {lab.panel_name} sent.'})
    for authorization in TreatmentAuthorization.objects.for_company(company).filter(
        patient=patient, product__company=company,
    ).select_related('product', 'prescribed_by').order_by('-created_at', '-pk')[:LOG_LIMIT]:
        text = authorization.log_text or (
            f'Prescription recorded: {prescription_label(authorization)}, valid to {authorization.expires_on:%-d %b %Y}.')
        entries.append({'id': f'prescription-{authorization.pk}', 'kind': 'prescription', 'at': authorization.created_at,
                        'by': authorization.prescribed_by.full_name, 'tag': 'Prescription', 'text': text})
    entries.sort(key=lambda entry: entry['at'], reverse=True)
    return entries


def dispensed(company, patient):
    """Shipments that left the dispensary, newest first, one line per medicine."""
    rows = []
    for shipment in Shipment.objects.for_company(company).filter(
        patient=patient, status__in=(Shipment.Status.DISPATCHED, Shipment.Status.DELIVERED),
    ).prefetch_related('items__product', 'items__batch').defer('dispatch_snapshot').order_by(
        '-dispatched_at', '-scheduled_for', '-pk',
    )[:LOG_LIMIT]:
        day = timezone.localdate(shipment.dispatched_at) if shipment.dispatched_at else shipment.scheduled_for
        for item in shipment.items.all():
            unit = 'pen' if item.quantity == 1 else 'pens'
            detail = []
            if item.batch_id:
                detail.append(f'Batch {item.batch.batch_number}, expiry {item.batch.expires_on:%m/%Y}')
            if shipment.tracking_number:
                detail.append(f'courier {shipment.tracking_number}')
            rows.append({'id': f'shipment-{shipment.pk}-{item.pk}', 'date': day, 'product': item.product,
                         'title': f'{item.product.name} {item.product.strength} × {item.quantity} {unit}'.replace('  ', ' '),
                         'tag': f'Month {shipment.cycle_number}', 'detail': ' · '.join(detail)})
    return rows


def record_chart(company, patient, summary, log, shipments):
    weights = [(entry.recorded_on, entry.weight_kg) for entry in WeightEntry.objects.for_company(company).filter(
        patient=patient,
    ).order_by('recorded_on', 'pk')]
    main = [row for row in shipments if row['product'].category == MedicationProduct.Category.WEIGHT_MANAGEMENT
            and strength_mg(row['product']) is not None]
    medicine = main[0]['product'].name if main else None
    ladder = [strength_mg(product) for product in MedicationProduct.objects.for_company(company).filter(name=medicine)] if medicine else []
    doses = [{'date': row['date'], 'dose': strength_mg(row['product']),
              'title': f'{row["date"]:%-d %b %Y} · {row["product"].name} {row["product"].strength} shipped'}
             for row in reversed(main) if row['product'].name == medicine]
    consults = [{'date': timezone.localdate(entry['at']), 'id': entry['consult_id'], 'by': entry['by']}
                for entry in log if entry['kind'] == 'consult']
    return doctor_weight_chart(weights=weights, goal=summary.goal_weight_kg, shipments=doses, consults=consults,
                               dose_max=max([value for value in ladder if value is not None], default=None))


def _bmi(weight, height):
    if not weight or not height:
        return None
    return (Decimal(weight) / ((Decimal(height) / 100) ** 2)).quantize(Decimal('0.1'))


def _kg(value):
    return f'{Decimal(value).quantize(Decimal("0.1"))}' if value is not None else '—'


def summary_context(company, patient, summary):
    latest = WeightEntry.objects.for_company(company).filter(patient=patient).order_by('-recorded_on', '-pk').first()
    current = latest.weight_kg if latest else summary.start_weight_kg
    start_bmi, current_bmi = _bmi(summary.start_weight_kg, summary.height_cm), _bmi(current, summary.height_cm)
    texts = [{'field': field, 'label': FIELDS[field], 'value': getattr(summary, field)} for field in
             ('medical_history', 'surgical_history', 'chronic_medication', 'allergies')]
    flags = [{'field': field, 'label': FIELDS[field], 'value': getattr(summary, field),
              'display': ANSWER_LABELS.get(getattr(summary, field), 'Not recorded'),
              'level': flag_level(field, getattr(summary, field)),
              'choices': [(choice, ANSWER_LABELS[choice]) for choice in FLAG_CHOICES[field]]}
             for field in FLAG_CHOICES]
    edits = list(PatientSummaryEdit.objects.for_company(company).filter(patient=patient).select_related('edited_by')[:50]) \
        if summary.pk else []
    for edit in edits:
        edit.label = FIELDS.get(edit.field, edit.field)
    questionnaire = screening(company, patient)
    seeded = summary.seeded_from or (questionnaire.submitted_at if questionnaire else None)
    return {
        'summary': summary, 'summary_weight': {'start': _kg(summary.start_weight_kg), 'current': _kg(current),
                                               'goal': _kg(summary.goal_weight_kg) if summary.goal_weight_kg else ''},
        'summary_bmi': {'start': start_bmi, 'current': current_bmi},
        'summary_texts': texts, 'summary_flags': flags, 'summary_edits': edits,
        'summary_edit_count': PatientSummaryEdit.objects.for_company(company).filter(patient=patient).count() if summary.pk else 0,
        'summary_seeded_from': seeded,
        'summary_has_flag': any(flag['level'] for flag in flags),
    }


def record_overview_context(request, company, membership, patient, *, log_filter='clinical', open_dialog=''):
    prescription = current_prescription(company, patient)
    today = timezone.localdate()
    summary = get_summary(company, patient)
    log = clinical_log(company, patient)
    shipped = dispensed(company, patient)
    from .record_access import can_change_record

    # A clinician assigned to this patient before, but not now, reads the Overview without changing it.
    changeable = can_change_record(membership, request.user, patient)
    prescriber = membership.is_prescriber and changeable
    options = prescription_options(company, prescription) if prescriber else {}
    context = {
        'record_overview': True,
        'log_filter': log_filter if log_filter in ('clinical', 'dispensed') else 'clinical',
        'clinical_log': log, 'dispensed_log': shipped,
        'prescription': prescription, 'prescription_state': prescription_state(prescription, today),
        'prescription_label': prescription_label(prescription) if prescription else '',
        'prescription_days_left': (prescription.expires_on - today).days if prescription else None,
        'can_prescribe': prescriber,
        'can_edit_summary': membership.has_clinical_access and changeable,
        'weight_chart': record_chart(company, patient, summary, log, shipped),
        'safety_flags': summary_safety_flags(company, patient, summary),
        'open_dialog': open_dialog,
        # Export record downloads the record as an Excel workbook, one sheet per tab.
        'export_url': reverse('portal:staff-patient-record-workbook', args=[patient.pk]),
    }
    context.update(summary_context(company, patient, summary))
    if prescriber:
        from .prescription_views import ChangePrescriptionForm, RenewPrescriptionForm

        first = next(iter(options.values()), [None])[0]
        context.update(
            renew_form=RenewPrescriptionForm(initial={'months': '6', 'current': prescription.pk if prescription else None}),
            change_form=ChangePrescriptionForm(options=options, initial={
                'months': '6', 'current': prescription.pk if prescription else None,
                'medicine': prescription.product.name if prescription else (first.name if first else ''),
                'product': prescription.product_id if prescription else (first.pk if first else None),
            }),
        )
        context.update(
            prescription_context_token=make_workflow_context(request, company, 'patient-prescription', patient=patient),
            renew_months=[{'months': months, 'expires_on': renewal_expiry(prescription, months, today)} for months in MONTH_CHOICES]
            if prescription else [],
            change_months=[{'months': months, 'expires_on': expiry_preview(months, today)} for months in MONTH_CHOICES],
            prescription_choices=[{'name': name, 'doses': doses} for name, doses in options.items()],
        )
    if context['can_edit_summary']:
        context['summary_context_token'] = make_workflow_context(request, company, 'patient-summary', patient=patient)
    if prescriber:
        context.update(bloods_context(request, company, patient))
    return context


def bloods_context(request, company, patient):
    """The Request blood tests card and its preview: the values the PathCare form will be filled with."""
    import uuid

    from care.pathcare import DOCTOR_DETAILS_MISSING, form_summary, form_values, patient_email

    summary = form_summary(form_values(company=company, patient=patient, doctor=request.user))
    blocked = any(row['missing'] and row['blocking'] for row in summary)
    return {
        'can_request_bloods': True, 'pathcare_summary': summary, 'pathcare_blocked': blocked,
        'pathcare_blocked_message': DOCTOR_DETAILS_MISSING if blocked else '',
        'pathcare_has_missing': any(row['missing'] for row in summary),
        'patient_email': patient_email(company, patient),
        'bloods_context_token': make_workflow_context(request, company, 'patient-bloods', patient=patient),
        'bloods_submission_key': uuid.uuid4(),
    }
