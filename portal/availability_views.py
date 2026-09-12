"""Self-service doctor hours; administrators can inspect but not impersonate."""

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.availability import (
    appointments_requiring_attention, cancel_time_off, create_time_off, save_working_pattern,
)
from care.models import DoctorTimeOff, DoctorWorkingPattern
from practices.models import CompanyMembership

from .availability_forms import (
    TimeOffForm, WEEKDAYS, WorkingPatternFormSet, make_schedule_context, validate_schedule_context,
)
from .schedule_calendar import schedule_url
from .staff_forms import ScheduleFilterForm
from .views import StaffCompanyRequiredMixin


def _schedule_filters(request, company, membership):
    data = request.GET.copy()
    data.setdefault('date', timezone.localdate().isoformat())
    data.setdefault('clinician', str(request.user.pk))
    form = ScheduleFilterForm(data, company=company, actor=request.user, membership=membership)
    day = form.cleaned_data['date'] if form.is_valid() else timezone.localdate()
    mode = request.GET.get('view', 'table')
    return {'date': day.isoformat(), 'clinician': request.user.pk, 'view': mode if mode in ('calendar', 'table') else 'table'}


def schedule_availability_context(request, company, membership, clinician, selected_date, mode):
    """No mutations during a schedule GET, including availability generation."""
    can_manage = bool(clinician and clinician.pk == request.user.pk and membership.role == CompanyMembership.Role.DOCTOR)
    rows_by_day = {row.weekday: row for row in DoctorWorkingPattern.objects.for_company(company).filter(clinician=clinician)} if clinician else {}
    initial = [
        {'is_working': rows_by_day[day].is_working, 'starts_at': rows_by_day[day].starts_at, 'ends_at': rows_by_day[day].ends_at}
        if day in rows_by_day else {'is_working': False, 'starts_at': None, 'ends_at': None}
        for day in range(7)
    ]
    filters = {'date': selected_date.isoformat(), 'view': mode} if selected_date else {'view': mode}
    if clinician:
        filters['clinician'] = clinician.pk
    params = urlencode(filters)
    time_off_queryset = DoctorTimeOff.objects.for_company(company).filter(
        clinician=clinician, is_active=True, ends_at__gt=timezone.now(),
    ).order_by('starts_at', 'pk') if clinician else DoctorTimeOff.objects.none()
    time_off_page = Paginator(time_off_queryset, 20).get_page(request.GET.get('time_off_page'))
    time_off = list(time_off_page.object_list)
    for entry in time_off:
        entry.cancel_url = reverse('portal:availability-time-off-cancel', args=[entry.pk]) + '?' + params
    affected = appointments_requiring_attention(company=company, clinician=clinician)
    affected_page = Paginator(affected, 20).get_page(request.GET.get('affected_page'))
    affected_appointments = list(affected_page.object_list)
    for appointment in affected_appointments:
        appointment.attention_reason = 'Outside current working hours or during time off'
        appointment.attention_url = reverse('portal:patient-detail', args=[appointment.patient_id])

    def page_url(parameter, number, anchor):
        return reverse('portal:staff-schedule') + '?' + urlencode({
            **filters, 'time_off_page': time_off_page.number, 'affected_page': affected_page.number,
            parameter: number,
        }) + '#' + anchor

    return {
        'can_manage_availability': can_manage,
        'working_pattern_configured': bool(rows_by_day),
        'working_pattern_formset': WorkingPatternFormSet(initial=initial, prefix='pattern') if can_manage else None,
        'weekly_pattern': [dict(day_label=WEEKDAYS[day], **values) for day, values in enumerate(initial)],
        'time_off_form': TimeOffForm(prefix='timeoff') if can_manage else None,
        'time_off_entries': time_off, 'affected_appointments': affected_appointments, 'affected_count': affected_page.paginator.count,
        'time_off_page_obj': time_off_page,
        'time_off_previous_url': page_url('time_off_page', time_off_page.previous_page_number(), 'time-off-heading') if time_off_page.has_previous() else None,
        'time_off_next_url': page_url('time_off_page', time_off_page.next_page_number(), 'time-off-heading') if time_off_page.has_next() else None,
        'affected_page_obj': affected_page,
        'affected_previous_url': page_url('affected_page', affected_page.previous_page_number(), 'affected-appointments-heading') if affected_page.has_previous() else None,
        'affected_next_url': page_url('affected_page', affected_page.next_page_number(), 'affected-appointments-heading') if affected_page.has_next() else None,
        'schedule_context': make_schedule_context(request, company, clinician) if can_manage else '',
        'working_pattern_url': reverse('portal:availability-working-pattern') + '?' + params,
        'time_off_create_url': reverse('portal:availability-time-off-create') + '?' + params,
    }


@method_decorator(never_cache, name='dispatch')
class DoctorAvailabilityMixin(LoginRequiredMixin, StaffCompanyRequiredMixin):
    http_method_names = ('post',)

    def require_own_diary(self):
        if self.membership.role != CompanyMembership.Role.DOCTOR:
            raise PermissionDenied('Only a doctor can change their own working hours or time off.')

    def invalid(self, **overrides):
        from .staff_views import StaffScheduleView

        page = StaffScheduleView()
        page.setup(self.request)
        page.company, page.membership = self.company, self.membership
        context = page.get_context_data()
        context.update(overrides)
        return render(self.request, 'portal/staff_schedule.html', context)

    def success(self):
        filters = _schedule_filters(self.request, self.company, self.membership)
        return redirect(schedule_url(day=filters['date'], clinician=self.request.user, view=filters['view']))


class WorkingPatternUpdateView(DoctorAvailabilityMixin, View):
    def post(self, request):
        self.require_own_diary()
        formset = WorkingPatternFormSet(request.POST, prefix='pattern')
        context_error = None
        try:
            validate_schedule_context(request, self.company, request.user)
        except ValidationError as error:
            context_error = ' '.join(error.messages)
        if not formset.is_valid() or context_error:
            return self.invalid(working_pattern_formset=formset, working_pattern_error=context_error)
        try:
            save_working_pattern(company=self.company, clinician=request.user, actor=request.user, days=formset.days(), request=request)
        except ValidationError as error:
            return self.invalid(working_pattern_formset=formset, working_pattern_error=' '.join(error.messages))
        messages.success(request, 'Your weekly working hours have been saved. Existing appointments have not been changed.')
        return self.success()


class TimeOffCreateView(DoctorAvailabilityMixin, View):
    def post(self, request):
        self.require_own_diary()
        form = TimeOffForm(request.POST, prefix='timeoff')
        try:
            validate_schedule_context(request, self.company, request.user)
        except ValidationError as error:
            form.add_error(None, error)
        if not form.is_valid():
            return self.invalid(time_off_form=form)
        try:
            create_time_off(company=self.company, clinician=request.user, actor=request.user, request=request, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
            return self.invalid(time_off_form=form)
        messages.success(request, 'Time off saved. Existing appointments have not been changed; affected bookings are flagged below.')
        return self.success()


class TimeOffCancelView(DoctorAvailabilityMixin, View):
    def post(self, request, pk):
        self.require_own_diary()
        entry = get_object_or_404(DoctorTimeOff.objects.for_company(self.company), pk=pk, clinician=request.user)
        try:
            validate_schedule_context(request, self.company, request.user)
            cancel_time_off(time_off=entry, actor=request.user, request=request)
        except ValidationError as error:
            return self.invalid(time_off_error=' '.join(error.messages))
        messages.success(request, 'Time off removed from availability. Its history is retained and appointments are unchanged.')
        return self.success()
