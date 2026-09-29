"""Permission-aware, aggregate-only data for the technical admin overview."""

from datetime import datetime, time, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncWeek
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, ClinicalTask, Invoice, LabRequest, PatientSubscription, Shipment

from .models import Patient
from .tenancy import enabled_companies, multi_practice_enabled, scope_queryset


PERIOD_OPTIONS = (30, 90, 180)


def _visible_queryset(admin_site, request, model):
    """Use the registered admin's permission and row restrictions, never widen them."""
    model_admin = admin_site._registry.get(model)
    if model_admin is None or not model_admin.has_module_permission(request):
        return None
    if not model_admin.has_view_or_change_permission(request):
        return None
    # scope_queryset intentionally does nothing in multi-practice mode. The
    # overview still excludes archived practices in that mode.
    return scope_queryset(model_admin.get_queryset(request)).filter(company__is_active=True)


def _changelist_url(admin_site, model, **filters):
    meta = model._meta
    url = reverse(f'{admin_site.name}:{meta.app_label}_{meta.model_name}_changelist')
    return f'{url}?{urlencode(filters)}' if filters else url


def _weekly_counts(queryset, field, start, end, weeks):
    if queryset is None:
        return None
    grouped = (
        queryset.filter(**{f'{field}__gte': start, f'{field}__lt': end})
        .order_by()
        .annotate(week=TruncWeek(field, tzinfo=timezone.get_current_timezone()))
        .values('week')
        .annotate(total=Count('pk'))
    )
    counts = {row['week'].date(): row['total'] for row in grouped}
    return [counts.get(week, 0) for week in weeks]


def build_admin_dashboard(admin_site, request):
    """Return a small data contract containing counts, never clinical records."""
    try:
        period = int(request.GET.get('days', 30))
    except (TypeError, ValueError):
        period = 30
    if period not in PERIOD_OPTIONS:
        period = 30

    now = timezone.now()
    today = timezone.localdate(now)
    first_date = today - timedelta(days=period - 1)
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(first_date, time.min), tz)
    end = timezone.make_aware(datetime.combine(today + timedelta(days=1), time.min), tz)
    first_week = first_date - timedelta(days=first_date.weekday())
    weeks = []
    week = first_week
    while week <= today:
        weeks.append(week)
        week += timedelta(days=7)

    querysets = {
        model: _visible_queryset(admin_site, request, model)
        for model in (Patient, Appointment, PatientSubscription, Invoice, ClinicalTask, LabRequest, Shipment)
    }
    patients = querysets[Patient]
    appointments = querysets[Appointment]
    subscriptions = querysets[PatientSubscription]
    invoices = querysets[Invoice]
    tasks = querysets[ClinicalTask]
    labs = querysets[LabRequest]
    shipments = querysets[Shipment]

    dashboard = {
        'period': period,
        'period_options': list(PERIOD_OPTIONS),
        'today': today,
        'scope_label': 'All enabled practices' if multi_practice_enabled() else 'Meridian Health',
        # This tenancy lookup exposes only whether provisioning is complete.
        'has_practice': enabled_companies().exists(),
        'metrics': [],
        'activity': {
            'labels': [week.strftime('%d %b') for week in weeks],
            'patients': _weekly_counts(patients, 'created_at', start, end, weeks),
            'appointments': _weekly_counts(appointments, 'starts_at', start, end, weeks),
            'has_data': False,
        },
        'appointment_statuses': [],
        'appointment_total': None,
        'workload': [],
        'quick_links': [],
    }
    activity = dashboard['activity']
    activity['has_data'] = any(activity['patients'] or []) or any(activity['appointments'] or [])
    activity['rows'] = [{
        'label': label,
        'patients': activity['patients'][index] if activity['patients'] is not None else None,
        'appointments': activity['appointments'][index] if activity['appointments'] is not None else None,
    } for index, label in enumerate(activity['labels'])]

    def metric(key, label, value, detail, model, icon, tone, **filters):
        dashboard['metrics'].append({
            'key': key, 'label': label, 'value': value, 'detail': detail,
            'url': _changelist_url(admin_site, model, **filters), 'icon': icon, 'tone': tone,
        })

    if patients is not None:
        metric('patients', 'Active patients', f'{patients.filter(is_active=True).count():,}',
               'Current patient register', Patient, 'fas fa-user-friends', 'teal', is_active__exact='1')

    if appointments is not None:
        statuses = dict(
            appointments.filter(starts_at__gte=start, starts_at__lt=end).order_by()
            .values('status').annotate(total=Count('pk')).values_list('status', 'total')
        )
        total = sum(statuses.values())
        dashboard['appointment_total'] = total
        status_tones = {
            Appointment.Status.BOOKED: 'blue', Appointment.Status.COMPLETED: 'teal',
            Appointment.Status.CANCELLED: 'slate', Appointment.Status.NO_SHOW: 'rose',
        }
        dashboard['appointment_statuses'] = [{
            'label': label, 'count': statuses.get(status, 0),
            'percent': round(100 * statuses.get(status, 0) / total, 1) if total else 0,
            'tone': status_tones[status],
        } for status, label in Appointment.Status.choices]
        metric('appointments', 'Appointments', f'{total:,}', f'Last {period} days · includes today',
               Appointment, 'fas fa-calendar-check', 'blue',
               starts_at__gte=start.isoformat(), starts_at__lt=end.isoformat())

    if subscriptions is not None:
        metric('subscriptions', 'Active subscriptions',
               f'{subscriptions.filter(status=PatientSubscription.Status.ACTIVE).count():,}',
               'Plans currently marked active', PatientSubscription, 'fas fa-heartbeat', 'violet',
               status__exact=PatientSubscription.Status.ACTIVE)

    if invoices is not None:
        outstanding = invoices.filter(status=Invoice.Status.ISSUED).aggregate(total=Sum('total'), count=Count('pk'))
        amount = outstanding['total'] or Decimal('0.00')
        metric('invoices', 'Outstanding invoices', f'R {amount:,.2f}',
               f"{outstanding['count']:,} issued invoices · all dates", Invoice, 'fas fa-file-invoice-dollar', 'amber',
               status__exact=Invoice.Status.ISSUED)

    def workload(label, count, detail, model, icon, tone, **filters):
        dashboard['workload'].append({
            'label': label, 'count': count, 'detail': detail, 'icon': icon, 'tone': tone,
            'url': _changelist_url(admin_site, model, **filters),
        })

    if tasks is not None:
        open_statuses = (ClinicalTask.Status.OPEN, ClinicalTask.Status.IN_PROGRESS)
        counts = tasks.filter(status__in=open_statuses).aggregate(
            open=Count('pk'), overdue=Count('pk', filter=Q(due_at__lt=now)),
        )
        workload('Open clinical tasks', counts['open'], 'Open and in progress', ClinicalTask,
                 'fas fa-clipboard-list', 'blue', status__in=','.join(open_statuses))
        workload('Overdue tasks', counts['overdue'], 'Open tasks past their due time', ClinicalTask,
                 'fas fa-clock', 'rose', status__in=','.join(open_statuses), due_at__lt=now.isoformat())

    if labs is not None:
        workload('Lab reviews waiting', labs.filter(status=LabRequest.Status.UPLOADED).count(),
                 'Results uploaded, awaiting review', LabRequest, 'fas fa-flask', 'violet',
                 status__exact=LabRequest.Status.UPLOADED)

    if shipments is not None:
        pending_statuses = (Shipment.Status.DRAFT, Shipment.Status.HELD, Shipment.Status.READY)
        workload('Pending dispatch', shipments.filter(status__in=pending_statuses).count(),
                 'Draft, held and ready shipments', Shipment, 'fas fa-shipping-fast', 'amber',
                 status__in=','.join(pending_statuses))

    for model, label, description, icon in (
        (Patient, 'Patient register', 'Browse practice patient records', 'fas fa-user-friends'),
        (Appointment, 'Appointments', 'Inspect your appointment schedule', 'fas fa-calendar-alt'),
        (ClinicalTask, 'Clinical tasks', 'Review outstanding care work', 'fas fa-clipboard-check'),
        (Invoice, 'Invoices', 'Review billing and invoice status', 'fas fa-file-invoice-dollar'),
    ):
        if querysets[model] is not None:
            dashboard['quick_links'].append({
                'label': label, 'description': description, 'icon': icon,
                'url': _changelist_url(admin_site, model),
            })
    return dashboard
