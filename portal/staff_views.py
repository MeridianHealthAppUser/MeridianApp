"""Dedicated staff workspaces; the overview remains a summary dashboard."""

from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.shortcuts import redirect
from django.db.models import Case, F, IntegerField, Q, Value, When
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.generic import TemplateView

from care.models import Appointment, ClinicalTask
from care.availability import appointments_requiring_attention, attention_kind, day_diaries, diary_day_states
from care.task_services import visible_tasks
from practices.models import CompanyMembership, Patient

from .staff_forms import PatientDirectoryFilterForm, ScheduleFilterForm, TaskFilterForm, practice_doctors
from .schedule_calendar import (
    MAX_DATE, MIN_DATE, calendar_range, month_context, schedule_section, schedule_url, week_context, week_range,
)
from .clinical_tasks import attach_clinical_task_links
from .filter_menus import choice_section, filter_menu, list_tools, ticked_ids
from .navigation import uses_flat_menu
from .people_tags import person_tag
from .record_access import limit_patient_choices
from .views import StaffCompanyRequiredMixin


class StaffPageView(LoginRequiredMixin, StaffCompanyRequiredMixin, TemplateView):
    http_method_names = ('get', 'head', 'options')
    nav_section = ''
    page_title = ''

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            company=self.company, active_membership=self.membership,
            nav_section=self.nav_section, page_title=self.page_title,
            is_doctor=self.membership.role == CompanyMembership.Role.DOCTOR, is_clinician=self.membership.is_clinician,
        )
        return context

    def paginate(self, queryset, **filters):
        paginator = Paginator(queryset, 20)
        page = paginator.get_page(self.request.GET.get('page'))
        return {
            'page_obj': page, 'paginator': paginator, 'is_paginated': page.has_other_pages(),
            'pagination_query': urlencode({key: value for key, value in filters.items() if value not in ('', None)}, doseq=True),
        }


@method_decorator(never_cache, name='dispatch')
class PatientListView(StaffPageView):
    template_name = 'portal/patient_list.html'
    nav_section = 'patients'
    page_title = 'Patient Records & Messages'

    def get(self, request, *args, **kwargs):
        # Practice administrators don't browse patient records: their Messages opens on Patient messages.
        if self.membership.role == CompanyMembership.Role.PRACTICE_ADMIN:
            return redirect('portal:staff-inbox')
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from .record_views import patient_directory_context

        context.update(patient_directory_context(self))
        return context


class StaffTaskListView(StaffPageView):
    template_name = 'portal/staff_tasks.html'
    nav_section = 'tasks'
    page_title = 'Tasks'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = timezone.now()
        queryset = visible_tasks(self.company, self.request.user, self.membership).select_related(
            'patient', 'assigned_to', 'created_by', 'encounter_signing', 'lab_review_request', 'data_request',
        ).prefetch_related('tags')
        # Clinicians and practice administrators have the same simple list: My tasks, no count cards.
        # Administrators still see every task in the practice, as unassigned ones are theirs to pick up.
        simple = uses_flat_menu(self.membership)
        if simple:
            context['page_title'] = 'My tasks'
        context['simple_tasks'] = simple
        open_statuses = (ClinicalTask.Status.OPEN, ClinicalTask.Status.IN_PROGRESS)
        open_tasks = queryset.filter(status__in=open_statuses)
        context['metrics'] = {
            'open': open_tasks.count(),
            'overdue': open_tasks.filter(due_at__lt=now).count(),
            'completed': queryset.filter(status=ClinicalTask.Status.DONE).count(),
        }
        data = self.request.GET.copy()
        data.setdefault('status', 'open')
        data.setdefault('kind', 'all')
        if context['is_doctor']:
            # Clinicians filter by status, type and patient; a task's free-form tags still show on it.
            data.setlist('tag', [])
        form = TaskFilterForm(data, company=self.company)
        limit_patient_choices(form.fields['patient'], self.membership, self.request.user)
        sections = self.filter_sections(form, data, queryset, tags=not context['is_doctor'])
        filters = {}
        if form.is_valid():
            for term in form.cleaned_data['q'].split():
                queryset = queryset.filter(Q(title__icontains=term) | Q(description__icontains=term)
                                           | Q(patient__first_name__icontains=term) | Q(patient__last_name__icontains=term)
                                           | Q(assigned_to__first_name__icontains=term) | Q(assigned_to__last_name__icontains=term))
            status = form.cleaned_data['status']
            patients = form.cleaned_data['patient']
            kind = form.cleaned_data['kind']
            tags = form.cleaned_data['tag']
            if status == 'open':
                queryset = queryset.filter(status__in=open_statuses)
            elif status != 'all':
                queryset = queryset.filter(status=status)
            if patients:
                queryset = queryset.filter(patient__in=patients)
            if kind != 'all':
                queryset = queryset.filter(patient__isnull=kind == 'general')
            if tags:
                queryset = queryset.filter(tags__in=tags).distinct()
            filters = {'q': form.cleaned_data['q'], 'status': status, 'patient': [patient.pk for patient in patients], 'kind': kind,
                       'tag': [tag.pk for tag in tags]}
        else:
            queryset = queryset.none()
        queryset = queryset.annotate(
            priority_order=Case(
                When(priority=ClinicalTask.Priority.URGENT, then=Value(0)),
                When(priority=ClinicalTask.Priority.HIGH, then=Value(1)),
                When(priority=ClinicalTask.Priority.NORMAL, then=Value(2)),
                default=Value(3), output_field=IntegerField(),
            ),
        ).order_by(F('due_at').asc(nulls_last=True), 'priority_order', '-created_at', '-pk')
        context.update(self.paginate(queryset, **filters))
        tasks = list(context['page_obj'].object_list)
        attach_clinical_task_links(tasks, self.request.user, self.membership)
        for task in tasks:
            # The patient as a tag, as Messages shows them.
            task.patient_tag = person_tag(str(task.patient), 'patient') if task.patient_id else None
            task.is_actionable = task.status in open_statuses and not task.is_clinical_workflow
            task.is_overdue = task.status in open_statuses and task.due_at is not None and task.due_at < now
        count = context['page_obj'].paginator.count
        # The table's tools at its top right, as on the other lists: the count, Filters and the search.
        context.update(filter_form=form, tasks=tasks, count_label=f'{count} task{"" if count == 1 else "s"}',
                       **list_tools(action=reverse('portal:staff-tasks'), data=self.request.GET, form=form, sections=sections,
                                    label='Filter tasks', placeholder='Task, patient or person'))
        return context

    def filter_sections(self, form, data, tasks, tags=True):
        """Status and Task type pick one; Patient and Free-form tag tick any. Patients are the ones in your tasks."""
        ticked = data.getlist('patient')
        patients = Patient.objects.for_company(self.company).filter(is_active=True).filter(
            Q(pk__in=tasks.order_by().values('patient')) | Q(pk__in=[value for value in ticked if value.isdecimal()]),
        ).order_by('last_name', 'first_name', 'pk')
        sections = [
            choice_section('status', 'Status', form.fields['status'].choices, [data.get('status')], pick=True,
                           default='open', errors=form['status'].errors),
            choice_section('kind', 'Task type', form.fields['kind'].choices, [data.get('kind')], icon='list', pick=True,
                           default='all', errors=form['kind'].errors),
            choice_section('patient', 'Patient', [(patient.pk, str(patient)) for patient in patients], ticked,
                           icon='person', errors=form['patient'].errors),
        ]
        if tags:
            sections.append(choice_section('tag', 'Free-form tag', [(tag.pk, tag.name) for tag in form.fields['tag'].queryset.order_by('name')],
                                           data.getlist('tag'), icon='tag', errors=form['tag'].errors))
        return sections


class ScheduleBookingMixin:
    """Calendar's tabs: Create appointment opens the booking form in a modal, its patients limited as a doctor's are;
    every appointment in the practice a member may see, which for a doctor is their own."""

    def booking_dialog_context(self):
        from .appointment_detail_views import staff_booking_form
        from .workflow_context import make_workflow_context
        booking_form = staff_booking_form(self.company, membership=self.membership)
        limit_patient_choices(booking_form.fields['patient'], self.membership, self.request.user)
        return dict(booking_form=booking_form, booking_context=make_workflow_context(self.request, self.company, 'staff-booking'))

    def schedule_appointments(self):
        queryset = Appointment.objects.for_company(self.company).filter(patient__company=self.company, patient__is_active=True)
        if self.membership.role == CompanyMembership.Role.DOCTOR:
            queryset = queryset.filter(clinician=self.request.user)
        return queryset


@method_decorator(never_cache, name='dispatch')
class StaffAppointmentsView(ScheduleBookingMixin, StaffPageView):
    """Calendar's first tab, Appointments (Nov 2026): every appointment the member may see, as a patient's My
    Appointments lists theirs (portal/staff_appointments.py), with the patient's name. A doctor sees their own, as on
    the Calendar tab. Calendar opens here from the menu and other links to the section (James, 20 Nov 2026); links to
    a particular day or week still open the Calendar tab."""

    template_name = 'portal/staff_appointments.html'
    nav_section = 'schedule'
    page_title = 'Calendar'

    def get_context_data(self, **kwargs):
        from .staff_appointments import staff_appointment_list

        context = super().get_context_data(**kwargs)
        context.update(self.booking_dialog_context())
        context.update(staff_appointment_list(
            self.request, self.company, self.membership, self.schedule_appointments(),
            action_url=reverse('portal:schedule-appointments'), show_patient=True, clinician_filter=not context['is_doctor']))
        # The other two tabs open as they do from the menu, on today in Day view; with one clinician ticked, theirs.
        chosen = [] if context['is_doctor'] else ticked_ids(self.request.GET.getlist('clinician'))
        clinician = chosen[0] if len(chosen) == 1 else None
        day = timezone.localdate()
        context.update(
            schedule_section='appointments', appointments_tab_url=reverse('portal:schedule-appointments'),
            bookings_tab_url=schedule_url(day=day, clinician=clinician),
            availability_tab_url=schedule_url(day=day, clinician=clinician, section='availability'),
        )
        return context


@method_decorator(never_cache, name='dispatch')
class StaffAppointmentVideoStatusView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    """Open meeting room on a staff appointment list switches on by itself when the room opens, as on a patient's My
    Appointments: the same script (static/js/appointment_video.js) asks here. Only the member's own appointments as
    their doctor, as only those have a room for them."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        from .patient_views import appointment_video_status

        appointments = Appointment.objects.for_company(self.company).filter(
            patient__company=self.company, patient__is_active=True, clinician=request.user)
        return appointment_video_status(request, appointments,
                                        allowed_role='doctor' if self.membership.is_clinician else None)


@method_decorator(never_cache, name='dispatch')
class StaffScheduleView(ScheduleBookingMixin, StaffPageView):
    """Calendar's Calendar tab (Day, Week and Month) and its Working hours & availability tab (?section=availability)."""

    template_name = 'portal/staff_schedule.html'
    nav_section = 'calendar'
    page_title = 'Calendar'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Create appointment opens the booking form in a modal over the calendar.
        from .workflow_context import make_workflow_context
        context.update(self.booking_dialog_context())
        # view=table is Day, view=week is Week and view=calendar is Month.
        schedule_view = self.request.GET.get('view', 'table')
        if schedule_view not in ('table', 'week', 'calendar'):
            schedule_view = 'table'
        # Two tabs share the diary picker and the clash banner; the chosen one is kept in every link and redirect.
        section = schedule_section(self.request.GET.get('section'))
        # The menu marks the tab that's open (navigation.SCHEDULE_TABS).
        context['nav_section'] = 'availability' if section == 'availability' else 'calendar'
        context.update(schedule_view=schedule_view, calendar_weeks=[], calendar_month=None,
                       prev_month_url=None, next_month_url=None, prev_day_url=None, next_day_url=None,
                       week_days=[], week_start=None, prev_week_url=None, next_week_url=None,
                       today=timezone.localdate(), weekday_labels=('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'))
        data = self.request.GET.copy()
        data.setdefault('date', timezone.localdate().isoformat())
        if context['is_doctor']:
            data.setdefault('clinician', str(self.request.user.pk))
        form = ScheduleFilterForm(data, company=self.company, actor=self.request.user, membership=self.membership)
        queryset = self.schedule_appointments().select_related('patient__user', 'clinician')
        selected_date = selected_clinician = None
        next_appointment_url = next_appointment_date = None
        diaries = []
        filters = {}
        if form.is_valid():
            selected_date = form.cleaned_data['date']
            selected_clinician = form.cleaned_data['clinician']
            if selected_clinician:
                queryset = queryset.filter(clinician=selected_clinician)
            if schedule_view == 'calendar':
                first_day, last_day = calendar_range(selected_date)
                context.update(month_context(
                    appointments=queryset, selected_date=selected_date, clinician=selected_clinician,
                    states=diary_day_states(
                        company=self.company, clinician=selected_clinician, first_day=first_day, last_day=last_day,
                    ) if selected_clinician else None,
                ))
            elif schedule_view == 'week':
                first_day, last_day = week_range(selected_date)
                context.update(week_context(
                    appointments=queryset, selected_date=selected_date, clinician=selected_clinician, company=self.company,
                    flag_attention=not context['is_doctor'],
                    states=diary_day_states(
                        company=self.company, clinician=selected_clinician, first_day=first_day, last_day=last_day,
                    ) if selected_clinician else None,
                ))
            else:
                context.update(
                    prev_day_url=schedule_url(day=selected_date - timedelta(days=1), clinician=selected_clinician)
                    if selected_date > MIN_DATE else None,
                    next_day_url=schedule_url(day=selected_date + timedelta(days=1), clinician=selected_clinician)
                    if selected_date < MAX_DATE else None,
                )
            start = timezone.make_aware(datetime.combine(selected_date, time.min))
            end = start + timedelta(days=1)
            next_booking = queryset.filter(
                status=Appointment.Status.BOOKED, starts_at__gte=max(end, timezone.now()),
            ).order_by('starts_at', 'pk').first()
            if next_booking:
                next_appointment_date = timezone.localdate(next_booking.starts_at)
                next_appointment_url = schedule_url(
                    day=next_appointment_date, clinician=selected_clinician, view=schedule_view, section=section,
                )
            queryset = queryset.filter(starts_at__gte=start, starts_at__lt=end)
            filters = {'date': selected_date.isoformat(), 'clinician': selected_clinician.pk if selected_clinician else '', 'view': schedule_view}
            # Doctors don't see open time: booking someone in is the practice's job. It's one day's, so Day view only.
            diaries = [] if context['is_doctor'] or schedule_view != 'table' else day_diaries(
                company=self.company,
                clinicians=[selected_clinician] if selected_clinician else practice_doctors(self.company),
                day=selected_date,
            )
        else:
            queryset = queryset.none()
        context['metrics'] = {status: queryset.filter(status=status).count() for status in Appointment.Status.values}
        context['day_booked'] = sum(context['metrics'].values()) - context['metrics'][Appointment.Status.CANCELLED]
        queryset = queryset.order_by('starts_at', 'pk')
        context.update(self.paginate(queryset, **filters))
        appointments = list(context['page_obj'].object_list)
        from video.access import attach_video_join

        attach_video_join(appointments, self.request.user.pk, allowed_role='doctor' if context['is_clinician'] else None)
        # Clinicians see each booking's status; the practice also sees which fall outside working hours or on leave.
        attention = {} if context['is_doctor'] else {appointment.pk: attention_kind(appointment) for appointment in appointments_requiring_attention(
            company=self.company, clinician=selected_clinician,
        ).filter(pk__in=[appointment.pk for appointment in appointments])}
        # A new time is arranged by the practice, when the patient contacts support. Practice administrators move or
        # cancel an appointment from its row in Day view, each in a dialog; everyone else opens its details.
        manage_bookings = self.membership.role == CompanyMembership.Role.PRACTICE_ADMIN and schedule_view == 'table'
        if manage_bookings:
            from .appointment_detail_views import AppointmentTimeForm
        now = timezone.now()
        for appointment in appointments:
            appointment.attention = attention.get(appointment.pk)
            appointment.needs_attention = appointment.attention is not None
            # A doctor's own appointments in Day view: Download for their calendar, and Cancel in a dialog until it starts.
            own = schedule_view == 'table' and not manage_bookings and appointment.clinician_id == self.request.user.pk
            appointment.can_download = own and appointment.status != Appointment.Status.CANCELLED
            if own and appointment.status == Appointment.Status.BOOKED and appointment.starts_at > now:
                appointment.cancel_context = make_workflow_context(self.request, self.company, 'appointment-status', appointment)
            if manage_bookings and appointment.status == Appointment.Status.BOOKED:
                appointment.time_form = AppointmentTimeForm(appointment=appointment, membership=self.membership,
                                                            auto_id=f'time_{appointment.pk}_%s')
                appointment.time_context = make_workflow_context(self.request, self.company, 'appointment-time', appointment)
                appointment.cancel_context = make_workflow_context(self.request, self.company, 'appointment-status', appointment)
        context['manage_bookings'] = manage_bookings
        context.update(
            filter_form=form, appointments=appointments, day_diaries=diaries,
            selected_date=selected_date, selected_clinician=selected_clinician,
            next_appointment_date=next_appointment_date, next_appointment_url=next_appointment_url,
            table_view_url=schedule_url(day=selected_date or data.get('date', '')[:32], clinician=selected_clinician or data.get('clinician', '')[:32], view='table'),
            week_view_url=schedule_url(day=selected_date or data.get('date', '')[:32], clinician=selected_clinician or data.get('clinician', '')[:32], view='week'),
            calendar_view_url=schedule_url(day=selected_date or data.get('date', '')[:32], clinician=selected_clinician or data.get('clinician', '')[:32], view='calendar'),
            today_url=schedule_url(day=timezone.localdate(), clinician=selected_clinician or data.get('clinician', '')[:32], view=schedule_view, section=section),
            schedule_section=section,
            # Appointments lists the chosen clinician's, as the Calendar shows their diary; a doctor's are their own.
            appointments_tab_url=reverse('portal:schedule-appointments') + (
                f'?{urlencode({"clinician": selected_clinician.pk})}' if selected_clinician and not context['is_doctor'] else ''),
            bookings_tab_url=schedule_url(day=selected_date or data.get('date', '')[:32], clinician=selected_clinician or data.get('clinician', '')[:32], view=schedule_view),
            availability_tab_url=schedule_url(day=selected_date or data.get('date', '')[:32], clinician=selected_clinician or data.get('clinician', '')[:32], view=schedule_view, section='availability'),
        )
        # Whose calendar, in a Filters menu in the calendar card's header before Day / Week / Month (James, 20 Nov
        # 2026): all clinicians or one, as the calendar shows one diary at a time. It sends ?clinician= as the
        # Appointments tab's Clinician filter does, so the choice carries between the tabs. A doctor sees only their
        # own calendar, so has no Filters; Working hours & availability keeps its Clinician list above the cards.
        if not context['is_doctor'] and section == 'bookings':
            day = selected_date or timezone.localdate()
            context['filter_menu'] = filter_menu(
                action=reverse('portal:staff-schedule'), label='Filter the calendar',
                hidden=[('view', schedule_view), ('date', day.isoformat())],
                clear_url=schedule_url(day=day, view=schedule_view),
                sections=[choice_section(
                    'clinician', 'Clinician',
                    [('', 'All clinicians')] + [(person.pk, person.titled_name) for person in form.fields['clinician'].queryset],
                    [selected_clinician.pk if selected_clinician else ''], icon='person', pick=True, default='',
                )])
        if selected_date:
            from .availability_views import schedule_availability_context

            context.update(schedule_availability_context(
                self.request, self.company, self.membership, selected_clinician, selected_date, schedule_view, section,
            ))
        return context
