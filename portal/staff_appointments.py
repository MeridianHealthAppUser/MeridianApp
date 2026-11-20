"""Staff appointment lists in the format of a patient's My Appointments (Nov 2026): Calendar's Appointments tab (where
Calendar opens) and the Appointments tab of a patient's record share this list and its include
(templates/portal/includes/staff_appointment_list.html).

Upcoming (where a list opens) and Past are split by scheduled end, as on My Appointments: an appointment under way
stays on Upcoming until it's due to finish. Filters (Status, Date, Clinician, Appointment type) carry across the two,
except Status, whose choices differ. Each row has small buttons, in the order and size of My Appointments': on Upcoming,
a booked appointment gets Open meeting room (the doctor's own, greyed out until the room opens, then switched on by
static/js/appointment_video.js without a reload), Edit and Cancel (a plain button, not red) when this person may, each
in a dialog, then Details. On Past, the booked clinician's own appointments still booked get Complete and No-show, icon
buttons each confirmed in a dialog, as attendance is recorded on the appointment's page (only once it has ended, so
never on Upcoming); then Details. Staff keep an appointment's page, where the consultation note is written.
"""

from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.utils import timezone

from care.models import Appointment, ClinicalEncounter
from practices.models import CompanyMembership
from video.access import attach_video_join

from .filter_menus import choice_section, filter_menu, ticked_ids, ticked_values
from .patient_summary import with_scheduled_end
from .patient_views import APPOINTMENT_TAB_STATUSES, APPOINTMENT_TABS, APPOINTMENT_WHEN_CHOICES, _when_bounds
from .record_access import no_records
from .workflow_context import make_workflow_context

# Which of Upcoming and Past is shown. Not "tab", which a record already uses for its own tabs.
LIST_PARAM = 'list'


def can_cancel_from_list(membership, user, appointment, now):
    """Cancel, from a staff list, as the Calendar's Day view allows it: a practice administrator any appointment,
    anyone else their own until it starts. Others cancel on the appointment's page."""
    if appointment.status != Appointment.Status.BOOKED:
        return False
    if membership.role == CompanyMembership.Role.PRACTICE_ADMIN:
        return True
    return appointment.clinician_id == user.pk and appointment.starts_at > now


def staff_appointment_list(request, company, membership, queryset, *, action_url, hidden=(), show_patient,
                           clinician_filter=True):
    """The list's context for a queryset of appointments this person may already see. action_url and hidden (pairs
    of query names and values, such as a record's tab) are where its tabs, Filters and pages point. show_patient adds
    the Patient column; clinician_filter=False leaves out the Clinician filter, as for a doctor, who sees only their
    own."""
    from .appointment_detail_views import AppointmentTimeForm, can_change_time, can_record_attendance

    now = timezone.now()
    hidden = list(hidden)
    tab = request.GET.get(LIST_PARAM) if request.GET.get(LIST_PARAM) in dict(APPOINTMENT_TABS) else 'upcoming'
    # Only the clinicians in these appointments.
    people = get_user_model().objects.filter(pk__in=queryset.order_by().values('clinician_id')).order_by(
        'first_name', 'last_name', 'pk') if clinician_filter else []
    # What's ticked. A value that isn't offered (a status the tab doesn't have, someone not in the list) shows nothing
    # rather than everything, and opens Filters to say so, as a record's other tables do; "all", as older links
    # sent for no filter, is no filter.
    when = request.GET.get('when', '')
    raw = {name: [value for value in ticked_values(request.GET.getlist(name)) if value != 'all']
           for name in ('status', 'type', 'clinician')}
    offered = {'status': set(APPOINTMENT_TAB_STATUSES[tab]), 'type': set(Appointment.Type.values),
               'clinician': {str(person.pk) for person in people}}
    if not clinician_filter:
        raw['clinician'] = []
    invalid = when not in dict(APPOINTMENT_WHEN_CHOICES) or any(set(raw[name]) - offered[name] for name in raw)
    when = '' if invalid else when
    statuses, types = [value for value in raw['status'] if value in offered['status']], [value for value in raw['type'] if value in offered['type']]
    clinicians = ticked_ids([value for value in raw['clinician'] if value in offered['clinician']])
    appointments = with_scheduled_end(queryset.select_related('patient', 'clinician'))
    if invalid:
        appointments = appointments.none()
    if tab == 'upcoming':
        appointments = appointments.filter(scheduled_ends_at__gt=now).order_by('starts_at', 'pk')
    else:
        appointments = appointments.filter(scheduled_ends_at__lte=now).order_by('-starts_at', '-pk')
    if when:
        start, end = _when_bounds(when)
        appointments = appointments.filter(starts_at__gte=start, starts_at__lt=end)
    if clinicians:
        appointments = appointments.filter(clinician_id__in=clinicians)
    if types:
        appointments = appointments.filter(appointment_type__in=types)
    if statuses:
        appointments = appointments.filter(status__in=statuses)
    filters = {key: value for key, value in {'status': statuses, 'when': when, 'clinician': clinicians, 'type': types}.items() if value}

    def on_tab(value, **query):
        # Upcoming is the list's own address, so only Past is spelt out. Status stays with its own tab.
        if value != tab:
            query.pop('status', None)
        query = urlencode({**dict(hidden), **({LIST_PARAM: value} if value != 'upcoming' else {}), **query}, doseq=True)
        return f'{action_url}?{query}' if query else action_url

    page = Paginator(appointments, 20).get_page(request.GET.get('page'))
    sections = [
        choice_section('status', 'Status', [(status.value, status.label) for status in APPOINTMENT_TAB_STATUSES[tab]], statuses),
        choice_section('when', 'Date', APPOINTMENT_WHEN_CHOICES, [when], icon='date', pick=True, default=''),
    ]
    if clinician_filter:
        sections.append(choice_section('clinician', 'Clinician', [(person.pk, person.titled_name) for person in people], clinicians, icon='person'))
    sections.append(choice_section('type', 'Appointment type', Appointment.Type.choices, types, icon='type'))
    menu = filter_menu(action=action_url, clear_url=on_tab(tab), label='Filter appointments',
                       hidden=hidden + ([(LIST_PARAM, tab)] if tab != 'upcoming' else []), sections=sections)
    menu['invalid'] = invalid
    rows = list(page.object_list)
    context = dict(
        page_obj=page, is_paginated=page.has_other_pages(), pagination_query=on_tab(tab, **filters).partition('?')[2],
        appointments=rows, appointment_tab=tab, appointment_list_show_patient=show_patient,
        # Practice administrators don't open patient records, so the name isn't a link for them.
        appointment_list_record_links=not no_records(membership),
        appointment_tabs=[{'label': label, 'url': on_tab(value, **filters), 'active': value == tab} for value, label in APPOINTMENT_TABS],
        filter_menu=menu, video_server_time=now,
    )
    if tab != 'upcoming':
        # Past is a history: Details, and Complete and No-show for the booked clinician while it's still booked
        # (care.appointment_lifecycle: No show not once a consultation note is signed).
        attendance = [appointment for appointment in rows if appointment.status == Appointment.Status.BOOKED
                      and can_record_attendance(membership, request.user, appointment, now)]
        signed = set(ClinicalEncounter.objects.for_company(company).filter(
            appointment__in=attendance, status=ClinicalEncounter.Status.SIGNED).values_list('appointment_id', flat=True)) if attendance else set()
        for appointment in attendance:
            appointment.attendance_context = make_workflow_context(request, company, 'appointment-status', appointment)
            appointment.can_mark_no_show = appointment.pk not in signed
        return context
    # Open meeting room for the doctor's own appointments, by the same room policy as everywhere else.
    attach_video_join(rows, request.user.pk, allowed_role='doctor' if membership.is_clinician else None, now=now)
    for appointment in rows:
        if appointment.status != Appointment.Status.BOOKED:
            continue
        appointment.show_meeting_room = appointment.video_join_opens_at is not None
        if can_change_time(membership, request.user, appointment):
            appointment.time_form = AppointmentTimeForm(appointment=appointment, membership=membership, auto_id=f'time_{appointment.pk}_%s')
            appointment.time_context = make_workflow_context(request, company, 'appointment-time', appointment)
        if can_cancel_from_list(membership, request.user, appointment, now):
            appointment.cancel_context = make_workflow_context(request, company, 'appointment-status', appointment)
    # The live script runs only when a row has a meeting room.
    context['appointment_list_video'] = any(getattr(appointment, 'show_meeting_room', False) for appointment in rows)
    return context
