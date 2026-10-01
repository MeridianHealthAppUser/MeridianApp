"""Read-only summaries for the patient pages: what is current, what is next and what needs doing.

Every value comes from a stored record. Nothing here predicts a result, sets a
target, invents a price or implies that a payment is taken.
"""

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import DateTimeField, DurationField, ExpressionWrapper, F, IntegerField, Value
from django.db.models.functions import Cast
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from care.models import (
    Appointment, AppointmentProposal, LabRequest, Lead, PatientMedicalProfile, PatientSubscription,
    ScreeningQuestionnaire, Shipment, TreatmentAuthorization, WeightEntry,
)
from care.treatment import authorization_is_current
from video.access import attach_video_join


# A follow-up is prompted this long before an authorisation lapses, so there is
# still time to book one. Weight check-ins are prompted weekly.
RENEWAL_WINDOW_DAYS = 45
WEIGHT_PROMPT_DAYS = 7


def current_plan(company, patient):
    return PatientSubscription.objects.for_company(company).filter(patient=patient).exclude(
        status=PatientSubscription.Status.CANCELLED,
    ).select_related(
        'authorization__product', 'authorization__prescribed_by', 'authorization__company', 'authorization__patient',
    ).order_by('-created_at', '-pk').first()


def current_authorization(company, patient, plan=None):
    """The authorisation the patient is treated under today, preferring their plan's."""
    if plan is not None and plan.authorization_id and authorization_is_current(plan.authorization):
        return plan.authorization
    today = timezone.localdate()
    candidates = TreatmentAuthorization.objects.for_company(company).filter(
        patient=patient, status=TreatmentAuthorization.Status.ACTIVE, starts_on__lte=today, expires_on__gte=today,
    ).select_related('product', 'prescribed_by', 'company', 'patient').order_by('-expires_on', '-pk')
    return next((item for item in candidates[:10] if authorization_is_current(item)), None)


def latest_authorization(company, patient):
    return TreatmentAuthorization.objects.for_company(company).filter(patient=patient).select_related(
        'product', 'prescribed_by',
    ).order_by('-expires_on', '-pk').first()


def next_consult(request, company, patient):
    """The next booked consultation that has not finished, with its video join state."""
    now = timezone.now()
    duration = ExpressionWrapper(
        Cast('duration_minutes', IntegerField()) * Value(timedelta(minutes=1)), output_field=DurationField(),
    )
    appointment = Appointment.objects.for_company(company).filter(
        patient=patient, status=Appointment.Status.BOOKED,
    ).annotate(
        scheduled_ends_at=ExpressionWrapper(F('starts_at') + duration, output_field=DateTimeField()),
    ).filter(scheduled_ends_at__gte=now).select_related('clinician').order_by('starts_at', 'pk').first()
    if appointment is not None:
        attach_video_join([appointment], request.user.pk, allowed_role='patient', now=now)
    return appointment


def next_delivery(company, patient):
    """A parcel on its way, otherwise the next one scheduled."""
    shipments = Shipment.objects.for_company(company).filter(patient=patient)
    in_transit = shipments.filter(status=Shipment.Status.DISPATCHED, delivered_at__isnull=True).order_by('-dispatched_at', '-pk').first()
    if in_transit is not None:
        return in_transit
    return shipments.filter(
        status__in=(Shipment.Status.DRAFT, Shipment.Status.HELD, Shipment.Status.READY),
        scheduled_for__gte=timezone.localdate(),
    ).order_by('scheduled_for', 'pk').first()


def pending_proposals(request, company, patient):
    """Suggested appointment times this patient can answer, by the same rules as Messages."""
    return list(AppointmentProposal.objects.for_company(company).filter(
        patient=patient, recipient=request.user, status=AppointmentProposal.Status.PENDING,
        proposer_role=AppointmentProposal.ProposerRole.DOCTOR,
        thread__company=company, thread__patient=patient, thread__is_closed=False,
        appointment__company=company, appointment__patient=patient,
    ).exclude(proposed_by=request.user).select_related('proposed_by', 'appointment', 'thread').order_by('proposed_starts_at', 'pk')[:5])


def open_lab_requests(company, patient):
    return list(LabRequest.objects.for_company(company).filter(
        patient=patient, status=LabRequest.Status.REQUESTED,
    ).select_related('requested_by').order_by('due_on', 'requested_on', 'pk')[:5])


def medical_profile(company, patient):
    return PatientMedicalProfile.objects.for_company(company).filter(patient=patient).first()


def intake_answers(company, patient):
    """Questionnaire answers from the enquiry this patient record was created from."""
    lead = Lead.objects.for_company(company).filter(converted_patient=patient).first()
    if lead is None:
        return {}
    questionnaire = ScreeningQuestionnaire.objects.for_company(company).filter(lead=lead, stage=1).first()
    return questionnaire.answers if questionnaire else {}


def latest_weight(company, patient):
    return WeightEntry.objects.for_company(company).filter(patient=patient).order_by('-recorded_on', '-pk').first()


def format_kg(value):
    """A stored weight without trailing zeros, as patients write it: 93.60 becomes 93.6."""
    try:
        text = f'{Decimal(str(value)):f}'
    except (InvalidOperation, TypeError, ValueError):
        return value
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number > 0 else None


def medical_information(*, profile, intake, weight):
    """Rows for the read-only Medical information card, from the patient's own record."""
    answers = profile.answers if profile else {}
    height = _decimal(intake.get('height_cm'))
    rows = [('Height', f'{format_kg(height)} cm' if height else 'Not recorded')]
    if weight is not None:
        rows.append(('Weight', f'{format_kg(weight.weight_kg)} kg · logged {date_format(weight.recorded_on, "j M")}'))
    elif _decimal(intake.get('weight_kg')):
        rows.append(('Weight', f'{format_kg(intake["weight_kg"])} kg · from your questionnaire'))
    else:
        rows.append(('Weight', 'Not recorded'))
    if height and weight is not None:
        bmi = (weight.weight_kg / ((height / 100) ** 2)).quantize(Decimal('0.1'))
        intake_bmi = _decimal(intake.get('bmi'))
        rows.append(('BMI', f'{bmi}' + (f' · {intake_bmi.quantize(Decimal("0.1"))} at your enquiry' if intake_bmi else '')))
    elif _decimal(intake.get('bmi')):
        rows.append(('BMI', f'{_decimal(intake["bmi"]).quantize(Decimal("0.1"))} at your enquiry'))
    medication = answers.get('medications') or ''
    if medication:
        rows.append(('Current medication', medication))
    elif intake.get('health_context', '').strip():
        rows.append(('Medication and allergies', intake['health_context'].strip()))
    else:
        rows.append(('Current medication', 'Not recorded'))
    for key, label in (('allergies', 'Allergies'), ('medical_history', 'Medical history'), ('surgical_history', 'Previous operations')):
        rows.append((label, answers.get(key) or 'Not recorded'))
    return rows


def _task(title, body, actions, *, urgent=False):
    return {'title': title, 'body': body, 'urgent': urgent,
            'actions': [{'label': label, 'url': url, 'primary': primary} for label, url, primary in actions]}


def patient_tasks(*, authorization, plan, consult, weight, profile, labs, proposals):
    """What needs doing, most urgent first. Each task disappears once its record changes."""
    today = timezone.localdate()
    book_url = reverse('portal:patient-appointments') + '#book'
    tasks = []
    if authorization is not None and consult is None and (authorization.expires_on - today).days <= RENEWAL_WINDOW_DAYS:
        expires = date_format(authorization.expires_on, 'j F')
        tasks.append(_task(
            f'Book a follow-up consult before {expires} to keep your treatment running',
            f'Your authorisation for {authorization.product} ends on {expires}. Your follow-up consult with '
            f'{authorization.prescribed_by.full_name} is where it can be renewed. If it lapses, deliveries are held '
            'until your doctor renews it.',
            [('Book a follow-up consult', book_url, True)], urgent=True,
        ))
    elif authorization is None and plan is not None:
        tasks.append(_task(
            'Your treatment authorisation has ended',
            'Deliveries on your plan are held until your doctor renews your authorisation. Book a consult so they '
            'can review your treatment.',
            [('Book a consult', book_url, True)], urgent=True,
        ))
    for proposal in proposals:
        local = timezone.localtime(proposal.proposed_starts_at)
        when = f'{date_format(local, "j F")} at {local:%H:%M}'
        tasks.append(_task(
            'Reply to a suggested appointment time',
            f'{proposal.proposed_by.full_name} suggested {when} SAST. Nothing changes until you accept it.',
            [('Open the conversation', reverse('portal:patient-messages') + f'?thread={proposal.thread_id}'
              f'#appointment-proposals-{proposal.thread_id}', True)],
        ))
    for lab in labs:
        due = f' Please have it done by {date_format(lab.due_on, "j F")}.' if lab.due_on else ''
        tasks.append(_task(
            'Get your blood tests done',
            f'{lab.requested_by.full_name} asked for {lab.panel_name} on {date_format(lab.requested_on, "j F")}.{due} '
            'When you have the report, upload it so your doctor can review it.',
            [('Upload your results', reverse('portal:patient-lab-detail', args=[lab.pk]), True)],
        ))
    if profile is None or not profile.revision:
        tasks.append(_task(
            'Complete your medical profile',
            'Tell your doctor about your medication, history and daily routine before you meet. Every section is '
            'optional and you can come back to it.',
            [('Complete my medical profile', reverse('portal:patient-medical-profile'), True)],
        ))
    if weight is None or (today - weight.recorded_on).days >= WEIGHT_PROMPT_DAYS:
        tasks.append(_task(
            'Log this week’s weight',
            'It goes onto the chart your doctor reads at your follow-up consults.',
            [('Log my weight', reverse('portal:patient-dashboard') + '#weight-log', True)],
        ))
    return tasks
