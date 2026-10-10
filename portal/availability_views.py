"""Doctors set their own working pattern; days off and special days are theirs or a practice administrator's."""

from datetime import date, time, timedelta
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
    SAST, appointments_requiring_attention, attention_kind, cancel_time_off, create_days_off, day_bounds,
    diary_day_states, remove_special_day, save_working_pattern, set_special_day, undo_diary_change,
)
from care.models import DoctorSpecialDay, DoctorTimeOff, DoctorWorkingPattern

from .availability_forms import (
    SpecialDayForm, TimeOffForm, WEEKDAYS, WorkingPatternFormSet, make_schedule_context, validate_schedule_context,
)
from .schedule_calendar import schedule_section, schedule_url
from .staff_forms import ScheduleFilterForm, practice_doctors
from .views import StaffCompanyRequiredMixin


ATTENTION_REASONS = {
    'day_off': 'On leave',
    'special_hours': 'Outside special hours',
    'working_hours': 'Outside current working hours',
}


def _schedule_filters(request, company, membership, clinician):
    data = request.GET.copy()
    data.setdefault('date', timezone.localdate().isoformat())
    data['clinician'] = str(clinician.pk)
    form = ScheduleFilterForm(data, company=company, actor=request.user, membership=membership)
    day = form.cleaned_data['date'] if form.is_valid() else timezone.localdate()
    mode = request.GET.get('view', 'table')
    return {'date': day.isoformat(), 'clinician': clinician.pk, 'view': mode if mode in ('calendar', 'week', 'table') else 'table',
            'section': schedule_section(request.GET.get('section'))}


def _day_range(entry):
    """A day off as its first and last South Africa dates, and whether it covers whole days."""
    starts, ends = timezone.localtime(entry.starts_at, SAST), timezone.localtime(entry.ends_at, SAST)
    whole = starts.time() == ends.time() == time.min
    last = (ends - timedelta(microseconds=1)).date()
    return starts.date(), last, whole


def schedule_availability_context(request, company, membership, clinician, selected_date, mode, section='bookings'):
    """No mutations during a schedule GET, including availability generation."""
    # The working pattern is the doctor's own; days off and special days may also be set by an administrator.
    can_manage = bool(clinician and clinician.pk == request.user.pk and membership.is_clinician)
    can_manage_days = bool(clinician and (can_manage or membership.is_admin))
    rows_by_day = {row.weekday: row for row in DoctorWorkingPattern.objects.for_company(company).filter(clinician=clinician)} if clinician else {}
    initial = [
        {'is_working': rows_by_day[day].is_working, 'starts_at': rows_by_day[day].starts_at, 'ends_at': rows_by_day[day].ends_at}
        if day in rows_by_day else {'is_working': False, 'starts_at': None, 'ends_at': None}
        for day in range(7)
    ]
    filters = {'date': selected_date.isoformat(), 'view': mode} if selected_date else {'view': mode}
    if clinician:
        filters['clinician'] = clinician.pk
    if section == 'availability':
        filters['section'] = section
    params = urlencode(filters)
    time_off_queryset = DoctorTimeOff.objects.for_company(company).filter(
        clinician=clinician, is_active=True, ends_at__gt=timezone.now(),
    ).order_by('starts_at', 'pk') if clinician else DoctorTimeOff.objects.none()
    time_off_page = Paginator(time_off_queryset, 20).get_page(request.GET.get('time_off_page'))
    time_off = list(time_off_page.object_list)
    for entry in time_off:
        entry.cancel_url = reverse('portal:availability-time-off-cancel', args=[entry.pk]) + '?' + params
        entry.first_day, entry.last_day, entry.whole_days = _day_range(entry)
    special_queryset = DoctorSpecialDay.objects.for_company(company).filter(
        clinician=clinician, date__gte=timezone.localdate(),
    ).order_by('date', 'pk') if clinician else DoctorSpecialDay.objects.none()
    special_page = Paginator(special_queryset, 20).get_page(request.GET.get('special_page'))
    special_days = list(special_page.object_list)
    if special_days:
        states = diary_day_states(company=company, clinician=clinician,
                                  first_day=special_days[0].date, last_day=special_days[-1].date)
        for special in special_days:
            special.remove_url = reverse('portal:availability-special-day-remove', args=[special.pk]) + '?' + params
            special.overridden = states[special.date]['kind'] == 'off'
    affected = appointments_requiring_attention(company=company, clinician=clinician)
    affected_page = Paginator(affected, 20).get_page(request.GET.get('affected_page'))
    affected_appointments = list(affected_page.object_list)
    for appointment in affected_appointments:
        appointment.attention_reason = ATTENTION_REASONS[attention_kind(appointment)]
        appointment.attention_url = reverse('portal:patient-detail', args=[appointment.patient_id])

    day_state = diary_day_states(company=company, clinician=clinician, first_day=selected_date,
                                 last_day=selected_date)[selected_date] if clinician and selected_date else None
    # Bookings left on a day off, or outside a special day's hours, on the day being viewed.
    clash_count = 0
    if day_state and day_state['kind'] in ('off', 'special') and mode == 'table':
        start, end = day_bounds(selected_date)
        clash_count = sum(
            attention_kind(appointment) in ('day_off', 'special_hours')
            for appointment in appointments_requiring_attention(company=company, clinician=clinician).filter(
                starts_at__gte=start, starts_at__lt=end,
            )
        )

    def page_url(parameter, number, anchor):
        return reverse('portal:staff-schedule') + '?' + urlencode({
            **filters, 'time_off_page': time_off_page.number, 'special_page': special_page.number,
            'affected_page': affected_page.number, parameter: number,
        }) + '#' + anchor

    return {
        'can_manage_availability': can_manage, 'can_manage_days': can_manage_days,
        'own_diary': bool(clinician and clinician.pk == request.user.pk),
        'working_pattern_configured': bool(rows_by_day),
        'working_pattern_formset': WorkingPatternFormSet(initial=initial, prefix='pattern') if can_manage else None,
        'weekly_pattern': [dict(day_label=WEEKDAYS[day], **values) for day, values in enumerate(initial)],
        'time_off_form': TimeOffForm(prefix='timeoff', initial={'starts_on': selected_date, 'ends_on': selected_date}) if can_manage_days else None,
        'special_day_form': SpecialDayForm(prefix='special', initial={'date': selected_date}) if can_manage_days else None,
        'time_off_entries': time_off, 'affected_appointments': affected_appointments, 'affected_count': affected_page.paginator.count,
        'time_off_page_obj': time_off_page,
        'time_off_previous_url': page_url('time_off_page', time_off_page.previous_page_number(), 'time-off-heading') if time_off_page.has_previous() else None,
        'time_off_next_url': page_url('time_off_page', time_off_page.next_page_number(), 'time-off-heading') if time_off_page.has_next() else None,
        'special_days': special_days, 'special_page_obj': special_page,
        'special_previous_url': page_url('special_page', special_page.previous_page_number(), 'special-days-heading') if special_page.has_previous() else None,
        'special_next_url': page_url('special_page', special_page.next_page_number(), 'special-days-heading') if special_page.has_next() else None,
        'affected_page_obj': affected_page,
        'affected_previous_url': page_url('affected_page', affected_page.previous_page_number(), 'affected-appointments-heading') if affected_page.has_previous() else None,
        'affected_next_url': page_url('affected_page', affected_page.next_page_number(), 'affected-appointments-heading') if affected_page.has_next() else None,
        'schedule_context': make_schedule_context(request, company, clinician) if can_manage_days else '',
        'working_pattern_url': reverse('portal:availability-working-pattern') + '?' + params,
        'time_off_create_url': reverse('portal:availability-time-off-create') + '?' + params,
        'special_day_url': reverse('portal:availability-special-day-set') + '?' + params,
        'day_state': day_state, 'clash_count': clash_count,
        'undo_url': reverse('portal:availability-undo') + '?' + params,
    }


@method_decorator(never_cache, name='dispatch')
class DoctorAvailabilityMixin(LoginRequiredMixin, StaffCompanyRequiredMixin):
    http_method_names = ('post',)

    def require_own_diary(self):
        if not self.membership.is_clinician:
            raise PermissionDenied('Only a clinician can change their own working hours.')
        self.diary = self.request.user

    def require_diary(self):
        """The doctor's own diary, or for an administrator the doctor chosen in the schedule filter."""
        if self.membership.is_admin:
            raw = self.request.GET.get('clinician', '')
            doctor = practice_doctors(self.company).filter(pk=int(raw)).first() if raw.isdigit() and len(raw) < 12 else None
            if doctor is not None:
                self.diary = doctor
                return doctor
        if self.membership.is_clinician:
            self.diary = self.request.user
            return self.diary
        raise PermissionDenied('Choose a doctor in the schedule filter before changing their leave or special days.')

    def invalid(self, **overrides):
        from .staff_views import StaffScheduleView

        page = StaffScheduleView()
        page.setup(self.request)
        page.company, page.membership = self.company, self.membership
        context = page.get_context_data()
        context.update(overrides)
        return render(self.request, 'portal/staff_schedule.html', context)

    def success(self):
        filters = _schedule_filters(self.request, self.company, self.membership, self.diary)
        return redirect(schedule_url(day=filters['date'], clinician=self.diary, view=filters['view'], section=filters['section']))


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
        doctor = self.require_diary()
        form = TimeOffForm(request.POST, prefix='timeoff')
        try:
            validate_schedule_context(request, self.company, doctor)
        except ValidationError as error:
            form.add_error(None, error)
        if not form.is_valid():
            return self.invalid(time_off_form=form)
        try:
            create_days_off(company=self.company, clinician=doctor, actor=request.user, request=request,
                            first_day=form.cleaned_data['starts_on'], last_day=form.cleaned_data['ends_on'])
        except ValidationError as error:
            form.add_error(None, ' '.join(error.messages))
            return self.invalid(time_off_form=form)
        messages.success(request, 'Marked unavailable. Those days have left the booking calendar; existing appointments have not been moved.')
        return self.success()


class TimeOffCancelView(DoctorAvailabilityMixin, View):
    def post(self, request, pk):
        doctor = self.require_diary()
        entry = get_object_or_404(DoctorTimeOff.objects.for_company(self.company), pk=pk, clinician=doctor)
        try:
            validate_schedule_context(request, self.company, doctor)
            cancel_time_off(time_off=entry, actor=request.user, request=request)
        except ValidationError as error:
            return self.invalid(time_off_error=' '.join(error.messages))
        messages.success(request, 'Leave removed. The working pattern applies again; its history is kept.')
        return self.success()


class SpecialDaySetView(DoctorAvailabilityMixin, View):
    def post(self, request):
        doctor = self.require_diary()
        form = SpecialDayForm(request.POST, prefix='special')
        try:
            validate_schedule_context(request, self.company, doctor)
        except ValidationError as error:
            form.add_error(None, error)
        if not form.is_valid():
            return self.invalid(special_day_form=form)
        try:
            set_special_day(company=self.company, clinician=doctor, actor=request.user, request=request,
                            day=form.cleaned_data['date'], starts_at=form.cleaned_data['starts_at'],
                            ends_at=form.cleaned_data['ends_at'])
        except ValidationError as error:
            form.add_error(None, ' '.join(error.messages))
            return self.invalid(special_day_form=form)
        day = form.cleaned_data['date']
        messages.success(request, f'Special hours set for {day:%a} {day.day} {day:%b %Y}. They replace the working pattern that day.')
        return self.success()


class SpecialDayRemoveView(DoctorAvailabilityMixin, View):
    def post(self, request, pk):
        doctor = self.require_diary()
        special = get_object_or_404(DoctorSpecialDay.objects.for_company(self.company), pk=pk, clinician=doctor)
        try:
            validate_schedule_context(request, self.company, doctor)
            remove_special_day(special_day=special, actor=request.user, request=request)
        except ValidationError as error:
            return self.invalid(special_day_error=' '.join(error.messages))
        messages.success(request, 'Special hours removed. The working pattern applies again.')
        return self.success()


class AvailabilityUndoView(DoctorAvailabilityMixin, View):
    """Undo the days off and the special day that cover the date being viewed."""

    def post(self, request):
        doctor = self.require_diary()
        filters = _schedule_filters(request, self.company, self.membership, doctor)
        try:
            validate_schedule_context(request, self.company, doctor)
            undo_diary_change(company=self.company, clinician=doctor, actor=request.user, request=request,
                              day=date.fromisoformat(filters['date']))
        except ValidationError as error:
            return self.invalid(time_off_error=' '.join(error.messages))
        messages.success(request, 'Undone. The diary stands as it was before the change.')
        return self.success()
