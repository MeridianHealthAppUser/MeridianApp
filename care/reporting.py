"""Evidence-based local reports and explicit-rate activity statements, no payments."""

from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Avg, Count, ExpressionWrapper, F, FloatField, OuterRef, Q, Subquery
from django.db.models.functions import Cast, TruncDate, TruncMonth
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient
from practices.tenancy import require_enabled_company, require_multi_practice, scope_queryset
from .models import (Appointment, DoctorActivityStatement, LabRequest, Lead,
                     PatientMessage, PatientSubscription, PracticeSettings, Shipment, WeightEntry)
from .operations import require_operations_actor
from .services import record_audit

# What a doctor is paid for, in the order Dr Czech lists them (Nov 2026): activity statements and the monthly
# report (care.activity_reports) both count these, at a rate each.
ACTIVITIES = (('initial', 'New consults'), ('follow_up', 'Follow-up consults'), ('messages', 'Messages answered'),
              ('review', 'Blood results reviewed'))
# The appointment types each kind of consult is. A six-month review is a follow-up consult.
CONSULT_TYPES = {'initial': ('initial',), 'follow_up': ('follow_up', 'review', 'ad_hoc')}
# A consult that was cancelled or missed didn't take place, so it never counts.
NOT_HELD = ('cancelled', 'no_show')
REPORT_TIMEZONE = ZoneInfo('Africa/Johannesburg')


def reporting_today():
    return timezone.localdate(timezone=REPORT_TIMEZONE)


def period_bounds(start, end):
    if (not isinstance(start, date) or isinstance(start, datetime)
            or not isinstance(end, date) or isinstance(end, datetime)
            or end < start or (end - start).days > 366):
        raise ValidationError('Choose an inclusive reporting period of up to 367 days.')
    try:
        lower = timezone.make_aware(datetime.combine(start, time.min), REPORT_TIMEZONE)
        upper = timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), REPORT_TIMEZONE)
        # Validate the conversion performed by the database driver at extreme dates.
        lower.astimezone(datetime_timezone.utc)
        upper.astimezone(datetime_timezone.utc)
    except (OverflowError, ValueError):
        raise ValidationError('Choose reporting dates within the supported calendar range.') from None
    return lower, upper


def report_companies(actor, company, scope):
    require_enabled_company(company)
    memberships = CompanyMembership.objects.filter(user=actor, user__is_active=True, company__is_active=True, is_active=True,
                                                     role__in=('doctor', 'practice_admin', 'super_admin'))
    memberships = scope_queryset(memberships)
    if not memberships.filter(company=company).exists():
        raise PermissionDenied('An active practice membership is required.')
    if scope == 'current':
        return [company.pk]
    if scope == 'all':
        require_multi_practice()
        return list(memberships.values_list('company_id', flat=True))
    raise ValidationError('Choose current practice or all permitted practices.')


def _daily_record_counts(queryset, field):
    """Aggregate in SAST regardless of a worker or request's active timezone."""
    rows = queryset.order_by().annotate(day=TruncDate(field, tzinfo=REPORT_TIMEZONE)).values('day').annotate(count=Count('pk'))
    return {row['day']: row['count'] for row in rows}


def _status_counts(queryset, choices):
    counts = dict(queryset.order_by().values('status').annotate(count=Count('pk')).values_list('status', 'count'))
    rows = [{'key': key, 'label': str(label), 'count': counts.pop(key, 0)} for key, label in choices]
    # Preserve totals even for malformed imports without displaying raw imported
    # text or treating an unknown value as a clinically valid status.
    if counts:
        rows.append({'key': 'other', 'label': 'Other recorded status', 'count': sum(counts.values())})
    return rows


def _operational_analytics(*, new_patients, appointments, shipments, plans, weight_distribution, start, end):
    """Bounded aggregate-only chart data; never names, record IDs or lead data."""
    series = {
        'new_patients': _daily_record_counts(new_patients, 'created_at'),
        'appointments': _daily_record_counts(appointments, 'starts_at'),
        'dispatches': _daily_record_counts(shipments, 'dispatched_at'),
    }
    daily = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        daily.append({'date': day, **{key: counts.get(day, 0) for key, counts in series.items()}})
    return {
        'daily': daily,
        'appointment_status': _status_counts(appointments, Appointment.Status.choices),
        'plan_status': _status_counts(plans, PatientSubscription.Status.choices),
        'weight_distribution': weight_distribution,
    }


def operational_metrics(*, actor, company, scope, start, end):
    lower, upper = period_bounds(start, end)
    company_ids = report_companies(actor, company, scope)
    all_patients = Patient.objects.filter(company_id__in=company_ids)
    patients = all_patients.filter(is_active=True)
    new_patients = all_patients.filter(created_at__gte=lower, created_at__lt=upper)
    appointments = Appointment.objects.filter(company_id__in=company_ids, patient__company_id=F('company_id'), starts_at__gte=lower, starts_at__lt=upper)
    shipments = Shipment.objects.filter(company_id__in=company_ids, patient__company_id=F('company_id'), dispatched_at__gte=lower, dispatched_at__lt=upper, status__in=('dispatched', 'delivered'))
    plans = PatientSubscription.objects.filter(company_id__in=company_ids, patient__company_id=F('company_id'))
    labs = LabRequest.objects.filter(company_id__in=company_ids, patient__company_id=F('company_id'), created_at__gte=lower, created_at__lt=upper)
    rows = [
        ('Active patient records now', patients.count()),
        ('New patient records in period', new_patients.count()),
        ('Active local plans now', plans.filter(status='active').count()),
        ('Paused plans now', plans.filter(status='paused').count()),
        ('Plans cancelled in period', plans.filter(cancelled_at__gte=lower, cancelled_at__lt=upper).count()),
        ('Appointments scheduled in period', appointments.count()),
        ('Completed appointments in period', appointments.filter(status='completed').count()),
        ('No-show appointments in period', appointments.filter(status='no_show').count()),
        ('Cancelled appointments in period', appointments.filter(status='cancelled').count()),
        ('Dispatches recorded in period', shipments.count()),
        ('Delivered from those dispatches', shipments.filter(status='delivered').count()),
        ('Lab requests created in period', labs.count()),
        ('Those lab requests reviewed', labs.filter(status='reviewed').count()),
    ]
    admin_ids = list(CompanyMembership.objects.filter(user=actor, is_active=True, company_id__in=company_ids, role__in=('practice_admin', 'super_admin')).values_list('company_id', flat=True))
    if admin_ids:
        leads = Lead.objects.filter(company_id__in=admin_ids, created_at__gte=lower, created_at__lt=upper)
        rows.extend((('New enquiries in practices you administer', leads.count()), ('Converted enquiries from that group', leads.filter(converted_patient__isnull=False).count())))
    # Only actual recorded weights, no imputed baselines, diagnosis or success claim.
    weights = WeightEntry.objects.filter(company_id=OuterRef('company_id'), patient_id=OuterRef('pk'), recorded_on__gte=start, recorded_on__lte=end, weight_kg__gt=0)
    paired = patients.annotate(first_weight=Subquery(weights.order_by('recorded_on', 'pk').values('weight_kg')[:1]),
                               last_weight=Subquery(weights.order_by('-recorded_on', '-pk').values('weight_kg')[:1]),
                               first_weight_date=Subquery(weights.order_by('recorded_on', 'pk').values('recorded_on')[:1]),
                               last_weight_date=Subquery(weights.order_by('-recorded_on', '-pk').values('recorded_on')[:1]))
    paired = paired.filter(first_weight__gt=0, last_weight__isnull=False).exclude(first_weight_date=F('last_weight_date')).annotate(
        recorded_change=ExpressionWrapper((Cast(F('last_weight'), FloatField()) - Cast(F('first_weight'), FloatField())) * 100.0 / Cast(F('first_weight'), FloatField()), output_field=FloatField()))
    weight_summary = paired.aggregate(count=Count('pk'), mean=Avg('recorded_change'),
        decrease=Count('pk', filter=Q(last_weight__lt=F('first_weight'))),
        unchanged=Count('pk', filter=Q(last_weight=F('first_weight'))),
        increase=Count('pk', filter=Q(last_weight__gt=F('first_weight'))))
    weight_stats = {key: weight_summary[key] for key in ('count', 'mean')}
    cohorts = list(new_patients.annotate(month=TruncMonth('created_at', tzinfo=REPORT_TIMEZONE)).values('month').annotate(records=Count('pk')).order_by('month'))
    analytics = _operational_analytics(new_patients=new_patients, appointments=appointments, shipments=shipments, plans=plans,
        weight_distribution={key: weight_summary[key] for key in ('decrease', 'unchanged', 'increase', 'count')}, start=start, end=end)
    return dict(metrics=rows, company_ids=company_ids, practices=Company.objects.filter(pk__in=company_ids).order_by('name'),
                cohorts=cohorts, weight_stats=weight_stats, analytics=analytics, start=start, end=end, scope=scope)


def doctor_activity(*, company, doctor, start, end):
    """What a doctor did from start to end (inclusive days): (counts, source_ids), each keyed by ACTIVITIES.

    A consult counts when it takes place, not when it was booked, so a booking cancelled later never counts: any of
    the doctor's appointments starting in the period, unless cancelled or a no-show. One still marked booked counts
    once its time has come. Counts also hold initial_completed and follow_up_completed, how many of those consults
    were marked completed, for information only. Messages answered are the doctor's messages in patient
    conversations; blood results reviewed are the blood tests they marked reviewed in the period. Demo patients never
    count."""
    lower, upper = period_bounds(start, end)
    appointments = Appointment.objects.for_company(company).filter(
        patient__company=company, patient__demo_data_set__isnull=True, clinician=doctor, starts_at__gte=lower, starts_at__lt=upper,
    ).exclude(status__in=NOT_HELD).filter(Q(status='completed') | Q(starts_at__lte=timezone.now())).order_by('pk')
    consults = {key: list(appointments.filter(appointment_type__in=types).values_list('pk', 'status')) for key, types in CONSULT_TYPES.items()}
    source_ids = {
        'initial': [pk for pk, _ in consults['initial']],
        'follow_up': [pk for pk, _ in consults['follow_up']],
        'messages': list(PatientMessage.objects.for_company(company).filter(thread__company=company, thread__patient__company=company, thread__patient__demo_data_set__isnull=True, sender=doctor, created_at__gte=lower, created_at__lt=upper).order_by('pk').values_list('pk', flat=True)),
        'review': list(LabRequest.objects.for_company(company).filter(patient__company=company, patient__demo_data_set__isnull=True, reviewed_by=doctor, reviewed_at__gte=lower, reviewed_at__lt=upper).order_by('pk').values_list('pk', flat=True)),
    }
    counts = {key: len(source_ids[key]) for key, _ in ACTIVITIES}
    for key, rows in consults.items():
        counts[f'{key}_completed'] = sum(status == 'completed' for _, status in rows)
    return counts, source_ids


def practice_rates(company):
    """The practice's rate for each activity (Insights > Activity statements > Monthly reports), as explicit_rates
    gives them; the defaults until a Super Admin changes them."""
    row = PracticeSettings.objects.for_company(company).first() or PracticeSettings(company=company)
    return explicit_rates({key: getattr(row, f'activity_rate_{key}') for key, _ in ACTIVITIES})


def activity_amount(rates, counts):
    """Each activity's count at its rate, added up."""
    return sum((Decimal(rates[key]) * counts[key] for key, _ in ACTIVITIES), Decimal('0.00'))


def explicit_rates(rates):
    """Validate both form input and saved/imported statement rates before use."""
    clean_rates = {}
    for key, label in ACTIVITIES:
        try:
            rate = Decimal(str(rates[key]))
            if not rate.is_finite() or rate < 0 or rate > 100000 or rate.as_tuple().exponent < -2:
                raise ValueError
        except (ValueError, TypeError, KeyError, InvalidOperation):
            raise ValidationError(f'Enter an explicit non-negative rate with up to two decimal places for {label.lower()}.') from None
        clean_rates[key] = str(rate.quantize(Decimal('.01')))
    return clean_rates


def provisional_total(*, company, doctor, counts):
    """Activity so far at the practice's rates, the ones the monthly report uses (care.activity_reports); None when
    a saved rate can't be used.

    An estimate for the doctor's own My profile, not a statement: nothing is saved or paid."""
    try:
        return activity_amount(practice_rates(company), counts)
    except ValidationError:
        return None


@transaction.atomic
def create_activity_statement(*, company, actor, doctor, start, end, rates, request=None):
    company = Company.objects.select_for_update().get(pk=company.pk)
    require_operations_actor(company, actor, catalogue=True)
    period_bounds(start, end)
    if end >= reporting_today():
        raise ValidationError('Statements can only cover completed dates, ending before today.')
    if not CompanyMembership.objects.filter(company=company, user=doctor, user__is_active=True, is_active=True, clinician_type__in=CompanyMembership.CLINICIAN_TYPES).exists():
        raise ValidationError('Choose an active clinician in this practice.')
    # Never pay/count the same period twice through overlapping statements.
    if DoctorActivityStatement.objects.for_company(company).filter(doctor=doctor, period_start__lte=end, period_end__gte=start).exists():
        raise ValidationError('A statement already overlaps these dates for this doctor. Open that statement instead.')
    clean_rates = explicit_rates(rates)
    counts, source_ids = doctor_activity(company=company, doctor=doctor, start=start, end=end)
    amount = activity_amount(clean_rates, counts)
    record = DoctorActivityStatement(company=company, doctor=doctor, period_start=start, period_end=end,
                                     counts=counts, rates=clean_rates, source_ids=source_ids, amount=amount, prepared_by=actor)
    record.full_clean()
    record.save()
    record_audit(company=company, actor=actor, action='activity_statement.created', target=record, request=request)
    return record


@transaction.atomic
def approve_activity_statement(*, statement, actor, expected_updated, confirm=False, request=None):
    company = Company.objects.select_for_update().get(pk=statement.company_id)
    require_operations_actor(company, actor, catalogue=True)
    statement = DoctorActivityStatement.objects.select_for_update().for_company(company).get(pk=statement.pk)
    if confirm is not True:
        raise ValidationError('Confirm that you have reviewed the activity and entered rates. No payment is made.')
    if statement.approved_at:
        return statement
    if statement.updated_at.isoformat() != expected_updated:
        raise ValidationError('The statement changed. Reload before approval.')
    counts, ids = doctor_activity(company=company, doctor=statement.doctor, start=statement.period_start, end=statement.period_end)
    if counts != statement.counts or ids != statement.source_ids:
        raise ValidationError('Source activity changed since preparation. Refresh the draft before approval.')
    amount = activity_amount(explicit_rates(statement.rates), counts)
    if amount != statement.amount:
        raise ValidationError('The stored total does not match the activity and rates. Refresh the draft before approval.')
    statement.approved_by, statement.approved_at = actor, timezone.now()
    statement.save(update_fields=('approved_by', 'approved_at', 'updated_at'))
    record_audit(company=company, actor=actor, action='activity_statement.approved', target=statement, request=request)
    return statement


@transaction.atomic
def refresh_activity_statement(*, statement, actor, expected_updated, request=None):
    company = Company.objects.select_for_update().get(pk=statement.company_id)
    require_operations_actor(company, actor, catalogue=True)
    statement = DoctorActivityStatement.objects.select_for_update().for_company(company).get(pk=statement.pk)
    if statement.approved_at or statement.updated_at.isoformat() != expected_updated:
        raise ValidationError('Only an unchanged draft may be refreshed. Approved statements are immutable.')
    statement.counts, statement.source_ids = doctor_activity(company=company, doctor=statement.doctor, start=statement.period_start, end=statement.period_end)
    statement.amount = activity_amount(explicit_rates(statement.rates), statement.counts)
    statement.full_clean()
    statement.save(update_fields=('counts', 'source_ids', 'amount', 'updated_at'))
    record_audit(company=company, actor=actor, action='activity_statement.refreshed', target=statement, request=request)
    return statement
