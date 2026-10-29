"""Read-only summaries for the patient pages: what is current, what is next and what needs doing.

Every value comes from a stored record. Nothing here predicts a result, sets a
target, invents a price or implies that a payment is taken: the payments a treatment
timeline shows are the catalogue dose's reference prices, labelled as such.
"""

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Case, DateTimeField, DurationField, ExpressionWrapper, F, IntegerField, OuterRef, Subquery, Value, When
from django.db.models.functions import Cast
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from care.models import (
    Appointment, LabRequest, Lead, PatientMedicalProfile, PatientSubscription,
    ScreeningQuestionnaire, Shipment, TreatmentAuthorization, WeightEntry,
)
from care.treatment import current_authorizations
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
    candidates = current_authorizations().filter(company=company, patient=patient).select_related(
        'product', 'prescribed_by', 'company', 'patient',
    )
    preference = [Case(When(pk=plan.authorization_id, then=Value(0)), default=Value(1))] if plan and plan.authorization_id else []
    return candidates.order_by(*preference, '-expires_on', '-pk').first()


def previous_prescriptions(company, patient):
    """The other prescriptions previously shown in My Medications, including issued future plan steps."""
    prescriptions = TreatmentAuthorization.objects.for_company(company).filter(
        patient=patient, patient__company=company, product__company=company, product__requires_authorisation=True,
    )
    current_products = current_authorizations(prescriptions).order_by().values('product_id')
    latest = prescriptions.filter(product_id=OuterRef('product_id')).order_by('-expires_on', '-pk').values('pk')[:1]
    return prescriptions.exclude(product_id__in=current_products).filter(pk=Subquery(latest)).select_related(
        'product', 'prescribed_by',
    ).order_by('-starts_on', '-pk')


def weight_reference_course(company, patient, authorization):
    """Only the dates Home needs, from one scoped query regardless of renewal count.

    Follow replacements and starting-plan steps in memory. Superseded prescriptions
    retain their historical dates; an unreplaced cancelled prescription is not a course.
    """
    if authorization is None or authorization.status == TreatmentAuthorization.Status.CANCELLED:
        return None
    rows = list(TreatmentAuthorization.objects.for_company(company).filter(
        patient=patient, patient__company=company, product__company=company,
    ).values('pk', 'starts_on', 'expires_on', 'status', 'plan_key', 'replaces_id'))
    by_id = {row['pk']: row for row in rows}
    if authorization.pk not in by_id or by_id[authorization.pk]['status'] == TreatmentAuthorization.Status.CANCELLED:
        return None
    plans = {}
    for row in rows:
        if row['plan_key']:
            plans.setdefault(row['plan_key'], []).append(row['pk'])
    members, expanded_plans, pending = {}, set(), [authorization.pk]
    while pending:
        pk = pending.pop()
        if pk in members or pk not in by_id:
            continue
        row = members[pk] = by_id[pk]
        if row['replaces_id']:
            pending.append(row['replaces_id'])
        key = row['plan_key']
        if key and key not in expanded_plans:
            expanded_plans.add(key)
            pending.extend(plans[key])
    replaced_from = {}
    for row in members.values():
        if row['replaces_id']:
            previous = row['replaces_id']
            replaced_from[previous] = min(replaced_from.get(previous, row['starts_on']), row['starts_on'])
    segments = []
    for row in members.values():
        if row['status'] == TreatmentAuthorization.Status.CANCELLED and row['pk'] not in replaced_from:
            continue
        end = row['expires_on']
        if row['pk'] in replaced_from:
            end = min(end, replaced_from[row['pk']] - timedelta(days=1))
        if end >= row['starts_on']:
            segments.append((row['starts_on'], end))
    if not segments:
        return None
    return {'started_on': min(start for start, _ in segments), 'ends_on': max(end for _, end in segments)}


HOW_OFTEN = {'weekly': 'once a week', 'daily': 'once a day'}
PARCEL_STATUS = {Shipment.Status.DISPATCHED: 'shipped', Shipment.Status.DELIVERED: 'delivered'}


def _pens(quantity, product):
    return f'{quantity} × {product} pen{"" if quantity == 1 else "s"}'


def _parcel_line(item, medicine):
    """'1 × 1.0 mg pen' for the prescribed medicine's pens; anything else by its full name."""
    if item.product.name == medicine and item.product.strength:
        return _pens(item.quantity, item.product.strength)
    return f'{item.quantity} × {item.product}'


def treatment_course(authorization, today=None):
    """The treatment a prescription belongs to, for the patient's timeline: the prescriptions it has been on, oldest
    first, each with the dates it was in effect. That is a starting plan's steps and the renewals and changes before
    and after it, back through the prescriptions each one replaced. A prescription replaced early ends the day before
    its replacement starts; one cancelled before it took effect (a plan's later steps after a change) is left out."""
    from care.parcels import shipping_day_from, shipping_weekday
    from care.treatment import step_in_effect

    today = today or timezone.localdate()
    current = step_in_effect(authorization, today)
    members, seen, queue = [], set(), [current]
    while queue:
        prescription = queue.pop()
        if prescription.pk in seen:
            continue
        seen.add(prescription.pk)
        members.append(prescription)
        if prescription.plan_key:
            queue.extend(TreatmentAuthorization.objects.filter(
                company_id=prescription.company_id, patient_id=prescription.patient_id, plan_key=prescription.plan_key,
            ).select_related('dose__pen', 'product', 'prescribed_by'))
        if prescription.replaces_id:
            queue.append(TreatmentAuthorization.objects.select_related('dose__pen', 'product', 'prescribed_by').get(
                pk=prescription.replaces_id))
    replaced_from = {}
    for prescription in members:
        if prescription.replaces_id:
            replaced_from[prescription.replaces_id] = min(replaced_from.get(prescription.replaces_id, prescription.starts_on),
                                                          prescription.starts_on)
    segments = []
    for prescription in members:
        if prescription.status == TreatmentAuthorization.Status.CANCELLED and prescription.pk not in replaced_from:
            continue
        end = prescription.expires_on
        if prescription.pk in replaced_from:
            end = min(end, replaced_from[prescription.pk] - timedelta(days=1))
        if end >= prescription.starts_on:
            segments.append({'prescription': prescription, 'starts_on': prescription.starts_on, 'ends_on': end})
    segments.sort(key=lambda segment: (segment['starts_on'], segment['prescription'].pk))
    started, ends = segments[0]['starts_on'], max(segment['ends_on'] for segment in segments)
    # The patient's weeks start with their pens: week 1 is the calendar week of the first parcel, and each week after
    # has its parcel day, a week apart.
    first_parcel = shipping_day_from(started, shipping_weekday(current.company))
    first_week = first_parcel - timedelta(days=first_parcel.weekday())
    total = max(1, round((ends - started).days / 7))
    return {'current': current, 'segments': segments, 'started_on': started, 'ends_on': ends, 'total': total,
            'first_week': first_week, 'first_parcel': first_parcel, 'before_start': today < first_week,
            'week': min(total, max(1, (today - first_week).days // 7 + 1)),
            'progress': min(100, max(0, round(100 * (today - first_week).days / max(1, (ends - first_week).days))))}


def _starts_with_pens(course, day):
    """The week a dose starting on day begins for the patient: the first week whose parcel day is on or after it."""
    weeks = max(0, -(-(day - course['first_parcel']).days // 7))
    return {'week': weeks + 1, 'week_starts_on': course['first_week'] + timedelta(weeks=weeks)}


def treatment_overview(company, patient, authorization, today=None):
    """What the patient sees about the prescription they are on: the dose and how often it is taken, the pens each
    parcel brings, the week of treatment, the next dose step and the next parcel. Every value is the prescription's
    or the catalogue dose's. None when the prescription isn't for a catalogue dose."""
    if authorization is None or not authorization.dose_id:
        return None
    today = today or timezone.localdate()
    course = treatment_course(authorization, today)
    current = course['current']
    dose = current.dose
    later = next((segment for segment in course['segments'] if segment['starts_on'] > today), None)
    parcels = Shipment.objects.for_company(company).filter(patient=patient).prefetch_related('items__product')
    upcoming = parcels.filter(status__in=(Shipment.Status.DRAFT, Shipment.Status.READY, Shipment.Status.HELD),
                              scheduled_for__gte=today).order_by('scheduled_for', 'pk').first()
    on_its_way = parcels.filter(status=Shipment.Status.DISPATCHED).order_by('-dispatched_at', '-pk').first()
    return {
        'medicine': current.product.name, 'dose': current.max_dose, 'amount': f'{dose.amount_text} mg',
        'how_often': HOW_OFTEN.get(dose.frequency, 'as your doctor directed'), 'supply': dose.dispatch_text,
        'week': course['week'], 'weeks': course['total'], 'progress': course['progress'],
        'first_week': course['first_week'], 'before_start': course['before_start'], 'ends_on': course['ends_on'],
        'next_step': later and _starts_with_pens(course, later['starts_on']) | {'dose': later['prescription'].max_dose},
        'next_parcel': upcoming and {'date': upcoming.scheduled_for, 'held': upcoming.status == Shipment.Status.HELD,
                                     'contents': [_pens(item.quantity, item.product) for item in upcoming.items.all()]},
        'on_its_way': on_its_way and {'date': timezone.localdate(on_its_way.dispatched_at) if on_its_way.dispatched_at else on_its_way.scheduled_for,
                                      'tracking': on_its_way.tracking_number},
        'prescribed_by': current.prescribed_by,
    }


def _dose_of(prescription, with_medicine):
    amount = f'{prescription.dose.amount_text} mg' if prescription.dose_id else prescription.max_dose
    return f'{prescription.product.name} {amount}' if with_medicine else amount


def week_range(start):
    """'19–25 Oct 2026', '26 Oct–1 Nov 2026' or '28 Dec 2026–3 Jan 2027': a week's first and last day."""
    end = start + timedelta(days=6)
    if start.year != end.year:
        return f'{start:%-d %b %Y}–{end:%-d %b %Y}'
    if start.month != end.month:
        return f'{start:%-d %b}–{end:%-d %b %Y}'
    return f'{start:%-d}–{end:%-d %b %Y}'


def _segment_on(segments, day):
    return next((segment for segment in segments if segment['starts_on'] <= day <= segment['ends_on']), None)


def treatment_timeline(company, patient, authorization, today=None):
    """The treatment week by week, for a catalogue dose, with where the patient is now. Week 1 is the week of the
    first parcel. Each week has the doctor's decisions made in it (starting, renewing or changing the prescription;
    one made before week 1 is in week 1), the pens planned or sent, the dose in effect on its parcel day (so a new
    dose starts with its pens) with any change from the week before, and the payments due: each dose's reference
    price in the first of its weeks and every billing period after."""
    if authorization is None or not authorization.dose_id:
        return None
    today = today or timezone.localdate()
    course = treatment_course(authorization, today)
    segments, total = course['segments'], course['total']
    first_week, first_parcel = course['first_week'], course['first_parcel']
    week_of = lambda day: min(total, max(1, (day - first_week).days // 7 + 1))
    with_medicine = len({segment['prescription'].product.name for segment in segments}) > 1
    medicine = course['current'].product.name

    weeks, previous = [], None
    for number in range(1, total + 1):
        day = first_week + timedelta(weeks=number - 1)
        segment = _segment_on(segments, first_parcel + timedelta(weeks=number - 1)) or next(
            (segment for segment in segments if segment['starts_on'] <= day + timedelta(days=6) and segment['ends_on'] >= day), None)
        prescription = segment['prescription'] if segment else None
        dose = _dose_of(prescription, with_medicine) if prescription else None
        change = ''
        if dose and previous and dose != previous[0]:
            before = previous[1]
            # The same medicine in a different pen is still a step up or down.
            if before.product.name == prescription.product.name and before.dose_id and prescription.dose_id:
                change = f'{"up" if prescription.dose.amount_mg > before.dose.amount_mg else "down"} from {_dose_of(before, False)}'
            else:
                change = f'was {_dose_of(before, True)}'
        weeks.append({'number': number, 'date': day, 'range': week_range(day), 'dose': dose, 'change': change,
                      'segment': segment, 'events': [], 'parcels': [], 'payments': [],
                      'current': day <= today < day + timedelta(days=7), 'past': day + timedelta(days=7) <= today,
                      # Before week 1, it is next: the patient is waiting for their first pens.
                      'next': number == 1 and course['before_start']})
        if prescription:
            previous = (dose, prescription)

    for segment in segments:
        prescription = segment['prescription']
        if prescription.dose_id and prescription.dose.price is not None:
            own = [week for week in weeks if week['segment'] is segment]
            for week in own[::prescription.dose.billing_weeks]:
                week['payments'].append(prescription.dose.price)

    for parcel in Shipment.objects.for_company(company).filter(
        patient=patient, scheduled_for__range=(course['started_on'], course['ends_on']),
    ).exclude(status=Shipment.Status.CANCELLED).prefetch_related('items__product').order_by('scheduled_for', 'pk'):
        day = timezone.localdate(parcel.dispatched_at) if parcel.dispatched_at else parcel.scheduled_for
        if parcel.status == Shipment.Status.HELD:
            when = 'on hold'
        elif parcel.status in PARCEL_STATUS:
            when = f'{PARCEL_STATUS[parcel.status]} {day:%-d %b}'
        else:
            when = f'ships {day:%a %-d %b}'
        weeks[week_of(day) - 1]['parcels'].append({
            'contents': ', '.join(_parcel_line(item, medicine) for item in parcel.items.all()), 'when': when})

    # The doctor's decisions, in a word: the first prescription, a new dose plan over one, a renewal or a change.
    # What changed shows in the Dosage column.
    # Nothing in effect before it, it starts the treatment, even if it replaced one that never took effect.
    for segment in segments:
        prescription = segment['prescription']
        replaced = prescription.replaces if prescription.replaces_id else None
        if prescription.plan_key and prescription.plan_step and prescription.plan_step > 1 and replaced is None:
            continue  # A plan's later step is the plan, not a new decision.
        if replaced is not None and replaced.dose_id == prescription.dose_id and replaced.product_id == prescription.product_id \
                and not (prescription.plan_key and prescription.plan_step == 1):
            text = 'Prescription renewed'
        elif not any(other['starts_on'] < segment['starts_on'] for other in segments):
            text = 'Prescription started'
        elif prescription.plan_key and prescription.plan_step == 1:
            text = 'Prescription reinitiated'
        else:
            text = 'Prescription adjusted'
        written = timezone.localdate(prescription.created_at)
        weeks[week_of(written) - 1]['events'].append({'text': text, 'date': written, 'by': prescription.prescribed_by.full_name})
    return {'weeks': weeks, 'total': total, 'first_week': first_week, 'ends_on': course['ends_on']}


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


def medical_information(*, profile, intake, weight, recorded_height=None):
    """Rows for the read-only Medical information card, from the patient's own record. Height is the one their
    clinician recorded in Summary data, when there is one, so a corrected height shows here too."""
    answers = profile.answers if profile else {}
    height = _decimal(recorded_height) or _decimal(intake.get('height_cm'))
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


def billing_tasks(plan):
    """What a subscription's billing needs from the patient: a debit order to set up or approve, or a payment that
    didn't go through. Most urgent first, like the rest."""
    from care.billing import patient_summary

    if plan is None or plan.status not in ('active', 'payment_retry'):
        return []
    summary = patient_summary(plan)
    if not summary['action']:
        return []
    details = {
        'no_mandate': 'Your parcels go out once each payment is in. Set up a DebiCheck debit order or a card to start.',
        'pending': 'Your debit order is set up but not approved yet. Approve it in your banking app, or set it up again.',
        'update': 'Your treatment or its price changed. Approve your debit order for the new amount so your parcels keep going out.',
        'failed': 'Your next parcel waits until it’s paid. Pay now, or set up your debit order again if your details changed.',
        'retrying': 'We’ll try again soon. If your bank details or card changed, update them so it goes through.',
    }
    return [_task(summary['text'], details.get(summary['state'], ''),
                  [(summary['action'], reverse('portal:patient-billing'), True)], urgent=summary['state'] != 'pending')]


def patient_tasks(*, authorization, plan, consult, weight, profile, labs):
    """What needs doing, most urgent first. Each task disappears once its record changes."""
    today = timezone.localdate()
    book_url = reverse('portal:patient-appointments') + '?book=1'
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
