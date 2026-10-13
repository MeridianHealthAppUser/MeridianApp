from datetime import timedelta
from urllib.parse import quote, urlencode

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.appointment_lifecycle import book_staff_appointment, change_appointment_status
from care.availability import bookable_start_times
from care.forms import BOOKING_DURATIONS, AppointmentForm
from care.models import Appointment, ClinicalEncounter, MessageThread
from practices.models import Patient
from .clinical_forms import ConsultationForm, make_clinical_context
from .patient_views import patient_page_context
from .views import PatientPortalRequiredMixin, StaffCompanyRequiredMixin
from .workflow_context import make_workflow_context, validate_workflow_context


class BookingTimesForm(forms.Form):
    """What the booking form's Time list asks for: a practice clinician, a date, a length and, if chosen, the patient."""

    clinician = forms.ModelChoiceField(queryset=None)
    date = forms.DateField()
    duration = forms.TypedChoiceField(coerce=int, choices=[(minutes, minutes) for minutes in BOOKING_DURATIONS])
    patient = forms.ModelChoiceField(queryset=None, required=False)

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['clinician'].queryset = AppointmentForm(company=company).fields['clinician'].queryset
        self.fields['patient'].queryset = Patient.objects.for_company(company).filter(is_active=True)


def staff_booking_form(company, *args, **kwargs):
    """The booking form as staff see it: no external meeting link, as the native video room is used."""
    form = AppointmentForm(*args, company=company, **kwargs)
    form.fields['video_link'].widget = forms.HiddenInput()
    return form


@method_decorator(never_cache, name='dispatch')
class BookingTimesView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    """The clinician's free quarter hours on a date, for the booking form's Time list."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        form = BookingTimesForm(request.GET, company=self.company)
        if not form.is_valid():
            return JsonResponse({'times': []}, status=400)
        data = form.cleaned_data
        return JsonResponse({'times': bookable_start_times(
            company=self.company, clinician=data['clinician'], day=data['date'], duration_minutes=data['duration'],
            patient=data['patient'])})


class AppointmentStatusForm(forms.Form):
    status = forms.ChoiceField(choices=(('cancelled', 'Cancel booking'), ('completed', 'Record completed'), ('no_show', 'Record no show')))
    reason = forms.CharField(label='Note / cancellation reason', max_length=500, required=False, widget=forms.Textarea(attrs={'rows': 3}))
    confirm = forms.BooleanField(label='I confirm this appointment update. A new time must be agreed separately.')

    def __init__(self, *args, attendance=False, staff=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not attendance:
            self.fields['status'].choices = (('cancelled', 'Cancel booking'),)
        if staff:
            self.fields['reason'].help_text = 'For a cancellation, the patient sees this in the app and in their cancellation email.'


@method_decorator(never_cache, name='dispatch')
class StaffBookingView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('get', 'post', 'head', 'options')

    def booking_form(self, *args, **kwargs):
        return staff_booking_form(self.company, *args, **kwargs)

    def display(self, request, form, status=200):
        return render(request, 'portal/appointment_booking.html', dict(company=self.company, active_membership=self.membership, nav_section='schedule', form=form,
            workflow_context=request.POST.get('workflow_context', '') if request.method == 'POST' else make_workflow_context(request, self.company, 'staff-booking')), status=status)

    def get(self, request):
        initial = {}
        raw_patient = request.GET.get('patient', '')
        if raw_patient.isascii() and raw_patient.isdecimal() and len(raw_patient) < 19:
            initial['patient'] = raw_patient
        return self.display(request, self.booking_form(initial=initial))

    def post(self, request):
        form = self.booking_form(request.POST)
        valid = form.is_valid()
        try:
            validate_workflow_context(request, self.company, 'staff-booking')
            if valid:
                appointment = book_staff_appointment(company=self.company, actor=request.user, request=request, **form.cleaned_data)
                messages.success(request, 'Appointment booked. No payment collected.')
                return redirect('portal:appointment-detail', pk=appointment.pk)
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
        return self.display(request, form, 400)


class AppointmentDetailMixin:
    http_method_names = ('get', 'post', 'head', 'options')
    patient_portal = False

    def resolve(self, pk):
        company = self.patient_company if self.patient_portal else self.company
        queryset = Appointment.objects.for_company(company).filter(patient__company=company, patient__is_active=True).select_related('patient', 'clinician')
        if self.patient_portal:
            queryset = queryset.filter(patient=self.patient)
        return get_object_or_404(queryset, pk=pk)

    def can_edit(self, appointment):
        if appointment.status != 'booked':
            return False
        if self.patient_portal:
            return appointment.starts_at > timezone.now()
        return self.membership.role in ('practice_admin', 'super_admin') or appointment.clinician_id == self.request.user.pk

    def attendance(self, appointment):
        return (not self.patient_portal and self.membership.is_clinician and appointment.clinician_id == self.request.user.pk
                and appointment.starts_at + timedelta(minutes=appointment.duration_minutes) <= timezone.now())

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

    def display(self, request, appointment, form=None, status=200):
        from video.views import appointment_video_context

        company = self.patient_company if self.patient_portal else self.company
        context = patient_page_context(request, company, self.patient, 'appointments', 'Appointment details') if self.patient_portal else dict(company=company, active_membership=self.membership, nav_section='schedule')
        context.update(appointment=appointment, can_edit=self.can_edit(appointment),
            # Staff reach only their own conversations with the patient.
            conversation=None if self.patient_portal else MessageThread.objects.for_company(company).filter(
                patient=appointment.patient, is_closed=False, participants=request.user).first(),
            form=form if form is not None else AppointmentStatusForm(attendance=self.attendance(appointment), staff=not self.patient_portal),
            workflow_context=request.POST.get('workflow_context', '') if request.method == 'POST' else make_workflow_context(request, company, 'appointment-status', appointment, patient=self.patient if self.patient_portal else None))
        if self.patient_portal:
            # A patient asks support for a new time by email, and the practice moves the appointment.
            when = date_format(timezone.localtime(appointment.starts_at), 'D j M Y, H:i')
            subject = f'Request for Appointment Change: {appointment.patient.first_name} {appointment.patient.last_name}, {when} SAST'
            context.update(support_email=settings.SUPPORT_EMAIL,
                           change_request_url=f"mailto:{settings.SUPPORT_EMAIL}?{urlencode({'subject': subject}, quote_via=quote)}")
        allowed_role = 'patient' if self.patient_portal else ('doctor' if self.membership.is_clinician else None)
        context.update(appointment_video_context(request, appointment, allowed_role=allowed_role))
        if not self.patient_portal:
            from .patient_workspace import patient_workspace_context

            context.update(patient_workspace_context(request, company, self.membership, appointment.patient, 'appointments'))
            context.update(self.consultation_note(request, company, appointment, context.get('can_write_notes')))
            context['page_title'] = 'Appointment details'
        return render(request, 'portal/patient_appointment_detail.html' if self.patient_portal else 'portal/appointment_detail.html', context, status=status)

    def get(self, request, pk):
        return self.display(request, self.resolve(pk))

    def post(self, request, pk):
        appointment = self.resolve(pk)
        form = AppointmentStatusForm(request.POST, attendance=self.attendance(appointment), staff=not self.patient_portal)
        valid = form.is_valid()
        company = self.patient_company if self.patient_portal else self.company
        try:
            token = validate_workflow_context(request, company, 'appointment-status', appointment, patient=self.patient if self.patient_portal else None)
            if valid:
                change_appointment_status(appointment=appointment, actor=request.user, expected_updated=token['updated'], patient_portal=self.patient_portal, request=request, **form.cleaned_data)
                messages.success(request, 'Appointment status recorded.')
                return redirect('portal:patient-appointment-detail' if self.patient_portal else 'portal:appointment-detail', pk=pk)
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
        return self.display(request, appointment, form, 400)


@method_decorator(never_cache, name='dispatch')
class StaffAppointmentDetailView(LoginRequiredMixin, StaffCompanyRequiredMixin, AppointmentDetailMixin, View):
    pass


class PatientAppointmentDetailView(LoginRequiredMixin, PatientPortalRequiredMixin, AppointmentDetailMixin, View):
    patient_portal = True
