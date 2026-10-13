"""Dedicated staff workspaces; the overview remains a summary dashboard."""

from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Case, F, IntegerField, Q, Value, When
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.generic import TemplateView

from care.models import Appointment, ClinicalTask
from care.availability import appointments_requiring_attention, attention_kind, diary_day_states, open_slots_for_day
from care.task_services import visible_tasks
from practices.models import CompanyMembership, Patient

from .staff_forms import PatientDirectoryFilterForm, ScheduleFilterForm, TaskFilterForm, practice_doctors
from .schedule_calendar import (
    MAX_DATE, MIN_DATE, calendar_range, month_context, schedule_section, schedule_url, week_context, week_range,
)
from .clinical_tasks import attach_clinical_task_links
from .filter_menus import choice_section, filter_menu
from .people_tags import person_tag
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
            'patient', 'assigned_to', 'created_by', 'encounter_signing', 'lab_review_request',
        ).prefetch_related('tags')
        if context['is_doctor']:
            context['page_title'] = 'My tasks'
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
        menu = self.filter_menu(form, data, queryset, tags=not context['is_doctor'])
        filters = {}
        if form.is_valid():
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
            filters = {'status': status, 'patient': [patient.pk for patient in patients], 'kind': kind, 'tag': [tag.pk for tag in tags]}
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
        context.update(filter_form=form, filter_menu=menu, tasks=tasks)
        return context

    def filter_menu(self, form, data, tasks, tags=True):
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
        return filter_menu(action=reverse('portal:staff-tasks'), form=form, clear_url=reverse('portal:staff-tasks'),
                           label='Filter tasks', sections=sections)


@method_decorator(never_cache, name='dispatch')
class StaffScheduleView(StaffPageView):
    template_name = 'portal/staff_schedule.html'
    nav_section = 'schedule'
    page_title = 'Calendar & Bookings'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Create appointment opens the booking form in a modal over the calendar.
        from .appointment_detail_views import staff_booking_form
        from .workflow_context import make_workflow_context
        context.update(booking_form=staff_booking_form(self.company),
                       booking_context=make_workflow_context(self.request, self.company, 'staff-booking'))
        # view=table is Day, view=week is Week and view=calendar is Month.
        schedule_view = self.request.GET.get('view', 'table')
        if schedule_view not in ('table', 'week', 'calendar'):
            schedule_view = 'table'
        # Two tabs share the diary picker and the clash banner; the chosen one is kept in every link and redirect.
        section = schedule_section(self.request.GET.get('section'))
        context.update(schedule_view=schedule_view, calendar_weeks=[], calendar_month=None,
                       prev_month_url=None, next_month_url=None, prev_day_url=None, next_day_url=None,
                       week_days=[], week_start=None, prev_week_url=None, next_week_url=None,
                       today=timezone.localdate(), weekday_labels=('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'))
        data = self.request.GET.copy()
        data.setdefault('date', timezone.localdate().isoformat())
        if context['is_doctor']:
            data.setdefault('clinician', str(self.request.user.pk))
        form = ScheduleFilterForm(data, company=self.company, actor=self.request.user, membership=self.membership)
        queryset = Appointment.objects.for_company(self.company).filter(
            patient__company=self.company, patient__is_active=True,
        ).select_related('patient__user', 'clinician')
        if context['is_doctor']:
            queryset = queryset.filter(clinician=self.request.user)
        selected_date = selected_clinician = None
        next_appointment_url = next_appointment_date = None
        slots = []
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
            # Doctors don't see open time: booking someone in is the practice's job.
            slots = [] if context['is_doctor'] else open_slots_for_day(
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
        # A new time is arranged by the practice, when the patient contacts support; the diary only shows bookings.
        for appointment in appointments:
            appointment.attention = attention.get(appointment.pk)
            appointment.needs_attention = appointment.attention is not None
        context.update(
            filter_form=form, appointments=appointments, open_slots=slots,
            selected_date=selected_date, selected_clinician=selected_clinician,
            next_appointment_date=next_appointment_date, next_appointment_url=next_appointment_url,
            table_view_url=schedule_url(day=selected_date or data.get('date', '')[:32], clinician=selected_clinician or data.get('clinician', '')[:32], view='table'),
            week_view_url=schedule_url(day=selected_date or data.get('date', '')[:32], clinician=selected_clinician or data.get('clinician', '')[:32], view='week'),
            calendar_view_url=schedule_url(day=selected_date or data.get('date', '')[:32], clinician=selected_clinician or data.get('clinician', '')[:32], view='calendar'),
            today_url=schedule_url(day=timezone.localdate(), clinician=selected_clinician or data.get('clinician', '')[:32], view=schedule_view, section=section),
            schedule_section=section,
            bookings_tab_url=schedule_url(day=selected_date or data.get('date', '')[:32], clinician=selected_clinician or data.get('clinician', '')[:32], view=schedule_view),
            availability_tab_url=schedule_url(day=selected_date or data.get('date', '')[:32], clinician=selected_clinician or data.get('clinician', '')[:32], view=schedule_view, section='availability'),
        )
        if selected_date:
            from .availability_views import schedule_availability_context

            context.update(schedule_availability_context(
                self.request, self.company, self.membership, selected_clinician, selected_date, schedule_view, section,
            ))
        return context
