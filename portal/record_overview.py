"""The clinical Overview of a patient record: the log, the prescription, the weight chart and summary data.

Shown to clinicians and Super Admins. Practice administrators keep the administrative Overview.
The log shows clinical entries only (signed consult notes, notes, blood forms, blood results and
prescriptions) and what was dispensed. Consult notes and notes lead; the rest are smaller lines.
Everything else stays in the audit trail and the History tab.
"""

from decimal import Decimal

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from care.clinical_summary import (
    ANSWER_LABELS, FIELDS, FLAG_CHOICES, flag_level, get_summary, screening, summary_safety_flags,
)
from care.models import (
    BloodResult, ClinicalEncounter, ClinicalNote, LabRequest, MedicationDose, MedicationProduct, PatientSummaryEdit,
    Shipment, TreatmentAuthorization, WeightEntry,
)
from care.prescriptions import (
    DEFAULT_WEEKS, WEEK_CHOICES, current_prescription, dose_mg, expiry_preview, prescription_label, prescription_options,
    prescription_state, renewal_expiry,
)

from .record_chart import doctor_weight_chart
from .workflow_context import make_workflow_context


LOG_LIMIT = 200
# The log's main entries: what clinicians wrote. Blood forms, blood results and prescriptions are smaller lines.
LEAD_KINDS = {'consult', 'note'}


def signed_consults(company, patient):
    """Signed consult notes, newest first: log entries, and the dots on the weight chart."""
    return [{'id': f'consult-{encounter.pk}', 'kind': 'consult', 'at': encounter.occurred_at,
             'by': (encounter.signed_by or encounter.clinician).full_name, 'tag': '',
             'text': encounter.clinical_summary, 'consult_id': encounter.pk}
            for encounter in ClinicalEncounter.objects.for_company(company).filter(
                patient=patient, status=ClinicalEncounter.Status.SIGNED,
            ).select_related('clinician', 'signed_by').order_by('-occurred_at', '-pk')[:LOG_LIMIT]]


def clinical_log(company, patient, membership=None, viewer=None):
    """Newest first. A clinician's private note is in their own log only; without a viewer, no private notes."""
    entries = signed_consults(company, patient)
    visible = Q(is_private=False)
    if membership is not None and membership.is_clinician:
        visible |= Q(author=viewer)
    for note in ClinicalNote.objects.for_company(company).filter(
        visible, patient=patient, signed_encounter__isnull=True,
    ).select_related('author').order_by('-created_at', '-pk')[:LOG_LIMIT]:
        entries.append({'id': f'note-{note.pk}', 'kind': 'note', 'at': note.created_at,
                        'by': note.author.full_name if note.author_id else '', 'private': note.is_private,
                        'tag': '' if note.note_type == ClinicalNote.NoteType.GENERAL else note.get_note_type_display(),
                        'text': note.body})
    for result in BloodResult.objects.for_company(company).filter(patient=patient).select_related(
        'recorded_by', 'file',
    ).defer('file__content').order_by('-created_at', '-pk')[:LOG_LIMIT]:
        entries.append({'id': f'result-{result.pk}', 'kind': 'result', 'at': result.created_at,
                        'by': result.recorded_by.full_name if result.recorded_by_id else '', 'tag': 'Blood results',
                        'result': result,
                        'text': f'{result.lab_name}, taken {date_format(result.taken_on, "j M Y")}: {result.results}'})
    for lab in LabRequest.objects.for_company(company).filter(patient=patient).select_related(
        'requested_by', 'pathcare_form',
    ).defer('result_summary').order_by('-created_at', '-pk')[:LOG_LIMIT]:
        form = getattr(lab, 'pathcare_form', None)
        entries.append({'id': f'blood-{lab.pk}', 'kind': 'blood', 'at': lab.created_at,
                        'by': lab.requested_by.full_name if lab.requested_by_id else '', 'automatic': not lab.requested_by_id,
                        'tag': 'Blood form', 'form': form,
                        'text': f'PathCare form for {form.tests} sent.' if form else f'Blood form for {lab.panel_name} sent.'})
    # A starting plan is one line, on its first step.
    for authorization in TreatmentAuthorization.objects.for_company(company).filter(
        patient=patient, product__company=company,
    ).exclude(plan_step__gt=1, replaces__isnull=True).select_related('product', 'prescribed_by').order_by('-created_at', '-pk')[:LOG_LIMIT]:
        text = authorization.log_text or (
            f'Prescription recorded: {prescription_label(authorization)}, valid to {authorization.expires_on:%-d %b %Y}.')
        entries.append({'id': f'prescription-{authorization.pk}', 'kind': 'prescription', 'at': authorization.created_at,
                        'by': authorization.prescribed_by.full_name, 'tag': 'Prescription', 'text': text})
    entries.sort(key=lambda entry: entry['at'], reverse=True)
    for entry in entries:
        entry['lead'] = entry['kind'] in LEAD_KINDS
    return entries


def dispensed(company, patient):
    """Shipments that left the dispensary, newest first, one line per medicine: the dose it was sent for, then the
    pens (Semaglutide 0.25 mg weekly, 1 × 1 mg pen)."""
    rows = []
    for shipment in Shipment.objects.for_company(company).filter(
        patient=patient, status__in=(Shipment.Status.DISPATCHED, Shipment.Status.DELIVERED),
    ).prefetch_related('items__product').defer('dispatch_snapshot').order_by(
        '-dispatched_at', '-scheduled_for', '-pk',
    )[:LOG_LIMIT]:
        day = timezone.localdate(shipment.dispatched_at) if shipment.dispatched_at else shipment.scheduled_for
        for item in shipment.items.all():
            unit = 'pen' if item.quantity == 1 else 'pens'
            detail = [f'{item.quantity} × {item.product.strength} {unit}'.replace('  ', ' ')]
            if item.batch_note:
                detail.append(f'Batch {item.batch_note}')
            if shipment.tracking_number:
                detail.append(f'courier {shipment.tracking_number}')
            rows.append({'id': f'shipment-{shipment.pk}-{item.pk}', 'date': day, 'product': item.product, 'dose': item.dose,
                         'title': f'{item.product.name} {item.dose or item.product.strength}'.strip(),
                         'tag': f'Parcel {shipment.cycle_number}', 'detail': ' · '.join(detail)})
    return rows


def record_chart(company, patient, summary, log, shipments):
    weights = [(entry.recorded_on, entry.weight_kg) for entry in WeightEntry.objects.for_company(company).filter(
        patient=patient,
    ).order_by('recorded_on', 'pk')]
    # Oldest first. Each main medicine shipped gets its own bars and colour, scaled to its own top dose.
    main = [row for row in reversed(shipments) if row['product'].category == MedicationProduct.Category.WEIGHT_MANAGEMENT
            and dose_mg(row['dose']) is not None]
    names = list(dict.fromkeys(row['product'].name for row in main))
    ladders = {}
    for row in main:
        ladders[row['product'].name] = max(ladders.get(row['product'].name, 0), dose_mg(row['dose']))
    for dose in MedicationDose.objects.for_company(company).filter(pen__name__in=names).select_related('pen'):
        ladders[dose.medicine] = max(ladders.get(dose.medicine, 0), dose.amount_mg)
    medicines = [{'name': name, 'dose_max': ladders.get(name)} for name in names]
    doses = [{'date': row['date'], 'dose': dose_mg(row['dose']), 'medicine': row['product'].name,
              'title': f'{row["date"]:%-d %b %Y} · {row["title"]} shipped'}
             for row in main]
    consults = [{'date': timezone.localdate(entry['at']), 'id': entry['consult_id'], 'by': entry['by']}
                for entry in log if entry['kind'] == 'consult']
    return doctor_weight_chart(weights=weights, goal=summary.goal_weight_kg, shipments=doses, consults=consults,
                               medicines=medicines)


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
    log = clinical_log(company, patient, membership, request.user)
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
        # Export record downloads the record as an Excel workbook, one sheet per tab. Super Admins only.
        'export_url': reverse('portal:staff-patient-record-workbook', args=[patient.pk])
        if membership.role == membership.Role.SUPER_ADMIN else '',
    }
    context.update(summary_context(company, patient, summary))
    if prescriber:
        from .prescription_views import ChangePrescriptionForm, InitiatePrescriptionForm, RenewPrescriptionForm

        first = next(iter(options.values()), [None])[0]
        context.update(
            renew_form=RenewPrescriptionForm(initial={'weeks': str(DEFAULT_WEEKS), 'current': prescription.pk if prescription else None}),
            change_form=ChangePrescriptionForm(options=options, initial={
                'weeks': str(DEFAULT_WEEKS), 'current': prescription.pk if prescription else None,
                'medicine': prescription.product.name if prescription else (first.medicine if first else ''),
                'dose': prescription.dose_id if prescription else (first.pk if first else None),
            }),
        )
        context.update(
            prescription_context_token=make_workflow_context(request, company, 'patient-prescription', patient=patient),
            renew_weeks=[{'weeks': weeks, 'expires_on': renewal_expiry(prescription, weeks, today)} for weeks in WEEK_CHOICES]
            if prescription else [],
            change_weeks=[{'weeks': weeks, 'expires_on': expiry_preview(weeks, today)} for weeks in WEEK_CHOICES],
            prescription_choices=[{'name': name, 'doses': doses} for name, doses in options.items()],
        )
        context.update(initiate_context(company, prescription, today, InitiatePrescriptionForm))
    if context['can_edit_summary']:
        context['summary_context_token'] = make_workflow_context(request, company, 'patient-summary', patient=patient)
    if prescriber:
        context.update(bloods_context(request, company, patient))
    return context


def initiate_context(company, prescription, today, form_class):
    """Initiate prescription: the starting plans by medicine, each with the lengths it allows and, for each, its
    steps with their dates, so the dialog can show what will be prescribed before it is saved."""
    import json

    from care.starting_plans import offered_plans, plan_schedule, plan_summary, plan_week_choices, schedule_text

    plans = offered_plans(company)
    for plan in plans:
        plan.summary = plan_summary(plan)
        plan.week_choices = plan_week_choices(plan)
        plan.default = plan.default_weeks if plan.default_weeks in plan.week_choices else plan.week_choices[-1]
        plan.previews = json.dumps({str(weeks): schedule_text(plan_schedule(plan, weeks, today)) for weeks in plan.week_choices})
    medicines = list(dict.fromkeys(plan.medicine for plan in plans))
    first = plans[0] if plans else None
    return {
        'starting_plans': plans, 'starting_plan_medicines': medicines,
        'initiate_form': form_class(plans=plans, initial={
            'current': prescription.pk if prescription else None, 'medicine': first.medicine if first else '',
            'plan': first.pk if first else None, 'weeks': str(first.default) if first else str(DEFAULT_WEEKS)}),
        'initiate_weeks': [{'weeks': weeks, 'expires_on': expiry_preview(weeks, today)} for weeks in WEEK_CHOICES],
    }


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
