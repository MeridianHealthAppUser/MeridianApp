from datetime import datetime, time as time_of_day, timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.cache import never_cache

from care.appointment_lifecycle import book_staff_appointment, change_appointment_status, reschedule_appointment
from care.availability import bookable_start_times, start_slots
from care.forms import BOOKING_DURATIONS, AppointmentForm
from care.widgets import QUARTER_HOUR_CHOICES
from care.models import Appointment, ClinicalEncounter, MessageThread
from practices.models import Patient
from .clinical_forms import ConsultationForm, make_clinical_context
from .record_access import limit_patient_choices, limited_by_assignment, require_record_view
from .views import PatientPortalRequiredMixin, StaffCompanyRequiredMixin
from .workflow_context import make_workflow_context, validate_workflow_context


class BookingTimesForm(forms.Form):
    """What the booking form's Time list asks for: a practice clinician, a date, a length and, if chosen, the patient."""

    clinician = forms.ModelChoiceField(queryset=None)
    date = forms.DateField()
    duration = forms.TypedChoiceField(coerce=int, choices=[(minutes, minutes) for minutes in BOOKING_DURATIONS])
    patient = forms.ModelChoiceField(queryset=None, required=False)
    # Change time: the appointment being moved doesn't block times near its own.
    appointment = forms.ModelChoiceField(queryset=None, required=False)

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['clinician'].queryset = AppointmentForm(company=company).fields['clinician'].queryset
        self.fields['patient'].queryset = Patient.objects.for_company(company).filter(is_active=True)
        self.fields['appointment'].queryset = Appointment.objects.for_company(company).filter(status=Appointment.Status.BOOKED)


# What each slot in a booking form's Time grid says. Patients see only whether they can take it.
SLOT_LABELS = {'free': 'Free', 'booked': 'Booked', 'leave': 'Leave', 'past': 'Past', 'short': 'Too short',
               'clash': 'Patient busy', 'current': 'Current', 'taken': 'Unavailable'}


def labelled_slots(slots, *, patient=False):
    """A booking form's Time grid (care.availability.start_slots), each slot with its label. For a patient, anything
    they can't book is just "Unavailable", so a clinician's leave or other bookings aren't shown."""
    if patient:
        slots = [dict(slot, state=slot['state'] if slot['state'] in ('free', 'past') else 'taken') for slot in slots]
    return [dict(slot, label=SLOT_LABELS[slot['state']]) for slot in slots]


# What a practice administrator books; six-month reviews and ad-hoc appointments are the clinician's (Dr Czech, 4 Nov 2026).
PRACTICE_ADMIN_TYPES = (Appointment.Type.INITIAL, Appointment.Type.FOLLOW_UP)


def staff_booking_form(company, *args, membership, **kwargs):
    """The booking form as staff see it: no external meeting link, as the native video room is used, and for a
    practice administrator, only initial consultations and follow-ups."""
    form = AppointmentForm(*args, company=company, **kwargs)
    form.fields['video_link'].widget = forms.HiddenInput()
    if membership.role == membership.Role.PRACTICE_ADMIN:
        field = form.fields['appointment_type']
        field.choices = [(value, label) for value, label in field.choices if value in ('', *PRACTICE_ADMIN_TYPES)]
    return form


@method_decorator(never_cache, name='dispatch')
class BookingTimesView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    """The clinician's free quarter hours on a date, for the booking form's Time list."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        form = BookingTimesForm(request.GET, company=self.company)
        limit_patient_choices(form.fields['patient'], self.membership, request.user)
        if not form.is_valid():
            return JsonResponse({'times': []}, status=400)
        data = form.cleaned_data
        times = bookable_start_times(company=self.company, clinician=data['clinician'], day=data['date'],
                                     duration_minutes=data['duration'], patient=data['patient'],
                                     exclude_appointment=data['appointment'])
        # The free times for the list, and the whole day for the grid that shows it.
        return JsonResponse({'times': times, 'slots': labelled_slots(start_slots(
            company=self.company, clinician=data['clinician'], day=data['date'], duration_minutes=data['duration'],
            free_times=times, exclude_appointment=data['appointment']))})


def schedule_return(request, page='portal:staff-schedule'):
    """Back to the list a change was made from, as it was: Calendar & Bookings' Day view, or a patient's
    My Appointments. Otherwise None."""
    target = request.POST.get('next', '')
    schedule = reverse(page)
    if (target.startswith(schedule) and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                                                        require_https=request.is_secure())):
        return redirect(target)
    return None


class AppointmentTimeForm(forms.Form):
    """Change time: a new date and one of the clinician's free quarter hours on it. Same clinician and length."""

    date = forms.DateField(label='New date', widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))
    time = forms.ChoiceField(label='New time', choices=[('', 'Choose a time'), *QUARTER_HOUR_CHOICES])

    def starts_at(self):
        return timezone.make_aware(datetime.combine(self.cleaned_data['date'], time_of_day.fromisoformat(self.cleaned_data['time'])))


class AppointmentStatusForm(forms.Form):
    status = forms.ChoiceField(choices=(('cancelled', 'Cancel booking'), ('completed', 'Record completed'), ('no_show', 'Record no show')))
    reason = forms.CharField(label='Note / Cancellation Reason (Optional)', max_length=500, required=False, widget=forms.Textarea(attrs={'rows': 3}))
    # No confirmation tick (Nov 2026): opening the dialog and pressing its confirm button is already two clicks. The
    # dialog shows the 24-hour cancellation policy instead (includes/appointment_cancellation_policy.html).

    def __init__(self, *args, attendance=False, staff=False, cancel_only=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not attendance:
            self.fields['status'].choices = (('cancelled', 'Cancel booking'),)
        if cancel_only:
            # Someone who can only cancel sees the status greyed out, and it submits as a cancellation whatever is sent.
            self.fields['status'].disabled = True
            self.fields['status'].initial = 'cancelled'
        if staff:
            self.fields['reason'].help_text = 'For a cancellation, the patient sees this in the app and in their cancellation email.'
        else:
            # A patient cancelling their own (Dr Czech, Nov 2026): Cancel appointment on My Appointments shows no status,
            # as cancelling is all they can do, and asks for a reason they must give. Theirs is never emailed.
            reason = self.fields['reason']
            reason.label, reason.required = 'Reason for cancelling', True
            reason.error_messages['required'] = 'Please give a reason for cancelling.'

    def clean_status(self):
        # A greyed-out status ignores what's sent, so refuse a forged update rather than cancel instead.
        sent = self.data.get(self.add_prefix('status'))
        if self.fields['status'].disabled and sent not in (None, '', 'cancelled'):
            raise ValidationError(self.fields['status'].error_messages['invalid_choice'], code='invalid_choice', params={'value': sent})
        return self.cleaned_data['status']


@method_decorator(never_cache, name='dispatch')
class StaffBookingView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('get', 'post', 'head', 'options')

    def booking_form(self, *args, **kwargs):
        form = staff_booking_form(self.company, *args, membership=self.membership, **kwargs)
        limit_patient_choices(form.fields['patient'], self.membership, self.request.user)
        return form

    def display(self, request, form, status=200):
        return render(request, 'portal/appointment_booking.html', dict(company=self.company, active_membership=self.membership, nav_section='schedule', form=form,
            workflow_context=request.POST.get('workflow_context', '') if request.method == 'POST' else make_workflow_context(request, self.company, 'staff-booking')), status=status)

    def get(self, request):
        # The patient, or (from a free slot in Available time) the clinician, date and time, can come from the link.
        initial = {}
        for name in ('patient', 'clinician'):
            raw = request.GET.get(name, '')
            if raw.isascii() and raw.isdecimal() and len(raw) < 19:
                initial[name] = raw
        for name in ('date', 'time'):
            raw = request.GET.get(name, '')[:10]
            if raw:
                initial[name] = raw
        return self.display(request, self.booking_form(initial=initial))

    def post(self, request):
        form = self.booking_form(request.POST)
        valid = form.is_valid()
        try:
            validate_workflow_context(request, self.company, 'staff-booking')
            if valid:
                appointment = book_staff_appointment(company=self.company, actor=request.user, request=request, **form.cleaned_data)
                messages.success(request, 'Appointment booked.')
                return redirect('portal:appointment-detail', pk=appointment.pk)
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
        return self.display(request, form, 400)


class AppointmentDetailMixin:
    """An appointment's page for staff. Patients have no page for one appointment (Dr Czech, Nov 2026): Open meeting
    room, Request a change and Cancel are on My Appointments (PatientAppointmentCancelView)."""

    http_method_names = ('get', 'post', 'head', 'options')

    def resolve(self, pk):
        queryset = Appointment.objects.for_company(self.company).filter(
            patient__company=self.company, patient__is_active=True).select_related('patient', 'clinician')
        appointment = get_object_or_404(queryset, pk=pk)
        # A doctor opens their own appointments, and others only for patients whose records they can open.
        if limited_by_assignment(self.membership) and appointment.clinician_id != self.request.user.pk:
            require_record_view(self.membership, self.request.user, appointment.patient)
        return appointment

    def can_edit(self, appointment):
        if appointment.status != 'booked':
            return False
        return self.membership.role in ('practice_admin', 'super_admin') or appointment.clinician_id == self.request.user.pk

    def can_change_time(self, appointment):
        """Staff move a booked appointment: administrators any, a doctor their own. Patients ask the practice."""
        return appointment.status == Appointment.Status.BOOKED and (
            self.membership.role in ('practice_admin', 'super_admin') or appointment.clinician_id == self.request.user.pk)

    def attendance(self, appointment):
        return (self.membership.is_clinician and appointment.clinician_id == self.request.user.pk
                and appointment.starts_at + timedelta(minutes=appointment.duration_minutes) <= timezone.now())

    def status_form(self, appointment, data=None):
        # Practice administrators never record attendance, so cancelling is their only update.
        return AppointmentStatusForm(data, attendance=self.attendance(appointment), staff=True,
                                     cancel_only=self.membership.role == 'practice_admin')

    def consultation_note(self, request, company, appointment, can_write_notes):
        """Write consultation note opens a modal with a new note's form: this appointment chosen when it's yours,
        the time now. When the appointment already has a note you can read, the button opens that instead."""
        if not self.membership.is_clinician:
            return {}
        note = ClinicalEncounter.objects.for_company(company).filter(appointment=appointment).filter(
            Q(status=ClinicalEncounter.Status.SIGNED) | Q(clinician=request.user)).first()
        if note is not None:
            return dict(consultation_note=note)
        if not can_write_notes:
            return {}
        form = ConsultationForm(company=company, patient=appointment.patient, actor=request.user,
                                initial={'appointment': appointment.pk}, auto_id='consultation_%s')
        form.fields['summary'].widget.attrs['rows'] = 8
        return dict(consultation_form=form, consultation_context=make_clinical_context(request, company, appointment.patient, 'consultation'))

    def display(self, request, appointment, form=None, status=200, time_form=None):
        from video.access import attach_video_join
        from .patient_workspace import patient_workspace_context
        from .record_access import no_records

        company = self.company
        context = dict(company=company, active_membership=self.membership, nav_section='schedule', page_title='Appointment details')
        context.update(appointment=appointment, can_edit=self.can_edit(appointment),
            # Staff reach only their own conversations with the patient.
            conversation=MessageThread.objects.for_company(company).filter(
                patient=appointment.patient, is_closed=False, participants=request.user).first(),
            form=form if form is not None else self.status_form(appointment),
            workflow_context=request.POST.get('workflow_context', '') if request.method == 'POST' else make_workflow_context(request, company, 'appointment-status', appointment))
        attach_video_join([appointment], request.user.pk, allowed_role='doctor' if self.membership.is_clinician else None)
        if self.can_change_time(appointment):
            local = timezone.localtime(appointment.starts_at)
            context.update(can_change_time=True, time_dialog_open=time_form is not None or request.GET.get('update') == 'time',
                           time_form=time_form if time_form is not None else AppointmentTimeForm(
                               initial={'date': local.date(), 'time': f'{local:%H:%M}'}, auto_id='time_%s'),
                           time_context=request.POST.get('workflow_context', '') if time_form is not None
                           else make_workflow_context(request, company, 'appointment-time', appointment))
        if no_records(self.membership):
            # Practice administrators don't open patient records: the appointment is a page of its own.
            return render(request, 'portal/staff_appointment_detail.html', context, status=status)
        context.update(patient_workspace_context(request, company, self.membership, appointment.patient, 'appointments'))
        context.update(self.consultation_note(request, company, appointment, context.get('can_write_notes')))
        context['page_title'] = 'Appointment details'
        return render(request, 'portal/appointment_detail.html', context, status=status)

    def get(self, request, pk):
        return self.display(request, self.resolve(pk))

    def post(self, request, pk):
        appointment = self.resolve(pk)
        form = self.status_form(appointment, request.POST)
        valid = form.is_valid()
        try:
            token = validate_workflow_context(request, self.company, 'appointment-status', appointment)
            if valid:
                change_appointment_status(appointment=appointment, actor=request.user, expected_updated=token['updated'], request=request,
                                          confirm=True, **form.cleaned_data)
                if form.cleaned_data['status'] == 'cancelled':
                    messages.success(request, f'Appointment with {appointment.patient} cancelled.')
                    back = schedule_return(request)
                    if back:
                        return back
                else:
                    messages.success(request, 'Appointment status recorded.')
                return redirect('portal:appointment-detail', pk=pk)
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
        return self.display(request, appointment, form, 400)


@method_decorator(never_cache, name='dispatch')
class StaffAppointmentDetailView(LoginRequiredMixin, StaffCompanyRequiredMixin, AppointmentDetailMixin, View):
    pass


class StaffAppointmentCalendarView(StaffAppointmentDetailView):
    """Download on a doctor's Calendar & Bookings: the appointment for their own calendar, without the patient's name
    (care.calendar_files). The link asks them to sign in to Meridian for who it's with."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk):
        from care.calendar_files import calendar_file

        appointment = self.resolve(pk)
        kind = appointment.get_appointment_type_display()
        return calendar_file(
            appointment, product='Staff appointments', summary=f'Meridian: {kind}',
            description=f'{kind} at {self.company.name}. Open it in Meridian for the patient and any changes.',
            url=request.build_absolute_uri(reverse('portal:appointment-detail', args=[appointment.pk])),
        )


class StaffAppointmentTimeView(StaffAppointmentDetailView):
    """Change time, from the appointment's page: moves it and emails the patient and clinician."""

    http_method_names = ('post',)

    def post(self, request, pk):
        appointment = self.resolve(pk)
        form = AppointmentTimeForm(request.POST, auto_id='time_%s')
        valid = form.is_valid()
        try:
            token = validate_workflow_context(request, self.company, 'appointment-time', appointment)
            if valid:
                moved = reschedule_appointment(appointment=appointment, actor=request.user, starts_at=form.starts_at(),
                                               expected_updated=token['updated'], request=request)
                messages.success(request, f'Appointment moved to {date_format(timezone.localtime(moved.starts_at), "D j M, H:i")}. '
                                          'The patient and clinician have been emailed.')
                return schedule_return(request) or redirect('portal:appointment-detail', pk=pk)
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
        return self.display(request, appointment, status=400, time_form=form)


class PatientAppointmentDetailView(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    """The old address of a patient's appointment page, as in emails already sent: My Appointments now. Patients have
    no page for one appointment (Dr Czech, Nov 2026)."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk):
        return redirect('portal:patient-appointments')


class PatientAppointmentCancelView(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    """Cancel, from its dialog on My Appointments, then back to the list as it was. Whatever stopped it is a message
    there: someone else changed it meanwhile, it has started, or the reason is missing."""

    http_method_names = ('post',)

    def post(self, request, pk):
        appointment = get_object_or_404(Appointment.objects.for_company(self.patient_company).filter(
            patient=self.patient, patient__company=self.patient_company, patient__is_active=True,
        ).select_related('patient', 'clinician'), pk=pk)
        form = AppointmentStatusForm(request.POST, cancel_only=True)
        try:
            token = validate_workflow_context(request, self.patient_company, 'appointment-status', appointment, patient=self.patient)
            if not form.is_valid():
                raise ValidationError([error for errors in form.errors.values() for error in errors])
            change_appointment_status(appointment=appointment, actor=request.user, expected_updated=token['updated'],
                                      patient_portal=True, request=request, confirm=True, **form.cleaned_data)
        except ValidationError as error:
            for message in error.messages:
                messages.error(request, message)
        else:
            messages.success(request, 'Appointment cancelled.')
        return schedule_return(request, 'portal:patient-appointments') or redirect('portal:patient-appointments')
