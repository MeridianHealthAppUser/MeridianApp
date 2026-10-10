"""Standalone patient care pages. No payment, email or account conversion."""

from datetime import datetime, time, timedelta, timezone as datetime_timezone
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.availability import SAST, open_slots_for_day
from care.models import Appointment, PatientEvent, PatientMedicalProfile
from care.patient_care import book_patient_appointment, patient_busy_intervals, save_patient_medical_profile
from .clinical_forms import make_clinical_context, validate_clinical_context
from .patient_care_forms import BookingTimesQuery, PatientBookingForm, PatientMedicalProfileForm, PatientUpdatesFilterForm
from .patient_context import validate_patient_context
from .patient_views import JUST_BOOKED, patient_appointments_context, patient_page_context
from .views import PatientPortalRequiredMixin


class PatientCarePage(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    http_method_names = ('get', 'post', 'head', 'options')

    def context(self, section, title, **extra):
        return dict(patient_page_context(self.request, self.patient_company, self.patient, section, title), **extra)


def add_error(form, error):
    for message in error.messages:
        form.add_error(None, message)


def patient_booking_times(company, patient, clinician, day):
    """The 15-minute times the patient can book with a clinician on a day: in the clinician's working hours, not on
    leave, and clear of the clinician's and the patient's other bookings."""
    busy = list(patient_busy_intervals(patient, day))
    return [timezone.localtime(slot.starts_at, SAST).strftime('%H:%M')
            for slot in open_slots_for_day(company=company, clinicians=[clinician], day=day, duration_minutes=15, limit=1000)
            if not any(slot.starts_at < finish and slot.starts_at + timedelta(minutes=15) > begin for begin, finish in busy)]


def booking_context(request, company, patient, booking_form=None):
    """Book appointment on My Appointments: a dialog behind the button, open when asked (?book=1, as links from
    other pages ask) or after a booking that didn't go through."""
    form = booking_form or PatientBookingForm(company=company, patient=patient, initial={
        key: request.GET[key] for key in ('clinician', 'appointment_type', 'date') if request.GET.get(key)})
    return {'booking_form': form, 'booking_open': booking_form is not None or request.GET.get('book') == '1'}


class PatientBookingView(PatientCarePage):
    """Booking from the Book appointment dialog. This address also keeps old links working, opening the dialog."""

    def display(self, request, booking_form=None):
        context = patient_appointments_context(request, self.patient_company, self.patient)
        context.update(booking_context(request, self.patient_company, self.patient, booking_form))
        return render(request, 'portal/patient_appointments.html', context)

    def get(self, request):
        query = urlencode({'book': 1, **{key: value for key, value in request.GET.items() if key != 'book'}})
        return redirect(f"{reverse('portal:patient-appointments')}?{query}")

    def post(self, request):
        form = PatientBookingForm(request.POST, company=self.patient_company, patient=self.patient)
        valid = form.is_valid()
        try:
            validate_patient_context(request, self.patient_company, self.patient)
        except ValidationError as error:
            add_error(form, error)
            valid = False
        if valid:
            try:
                appointment, created = book_patient_appointment(
                    company=self.patient_company, patient=self.patient, actor=request.user,
                    clinician_id=form.cleaned_data['clinician'].pk, starts_at=form.cleaned_data['starts_at'],
                    appointment_type=form.cleaned_data['appointment_type'], request=request,
                )
            except ValidationError as error:
                add_error(form, error)
            else:
                messages.success(request, 'Appointment booked. No payment was taken.' if created else 'This appointment is already booked.')
                # Shown once at the top of My Appointments; it's gone when they come back.
                request.session[JUST_BOOKED] = appointment.pk
                return redirect('portal:patient-appointments')
        return self.display(request, form)


@method_decorator(never_cache, name='dispatch')
class PatientBookingTimesView(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    """The times Book appointment's Time list offers for a clinician on a date."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        query = BookingTimesQuery(request.GET, company=self.patient_company)
        if not query.is_valid():
            return JsonResponse({'times': []}, status=400)
        return JsonResponse({'times': patient_booking_times(
            self.patient_company, self.patient, query.cleaned_data['clinician'], query.cleaned_data['date'])})


class PatientMedicalProfileView(PatientCarePage):
    def profile(self):
        return PatientMedicalProfile.objects.for_company(self.patient_company).filter(patient=self.patient).first()

    def display(self, request, profile, form=None):
        context = self.context(
            'medical_profile', 'Your medical profile', profile=profile,
            profile_form=form if form is not None else PatientMedicalProfileForm(initial=profile.answers if profile else {}),
            clinical_context=request.POST.get('clinical_context') or make_clinical_context(
                request, self.patient_company, self.patient, 'medical-profile', profile,
            ),
        )
        return render(request, 'portal/patient_care_medical_profile.html', context)

    def get(self, request):
        return self.display(request, self.profile())

    def post(self, request):
        profile = self.profile()
        form = PatientMedicalProfileForm(request.POST)
        valid = form.is_valid()
        try:
            token = validate_clinical_context(request, self.patient_company, self.patient, 'medical-profile', profile)
        except ValidationError as error:
            add_error(form, error)
            valid = False
        if valid:
            try:
                profile, changed = save_patient_medical_profile(
                    company=self.patient_company, patient=self.patient, actor=request.user,
                    answers=form.cleaned_data, expected_revision=token.get('revision'), request=request,
                )
            except ValidationError as error:
                add_error(form, error)
            else:
                messages.success(request, 'Your medical profile is saved. You can return to update it.' if changed else 'No changes to save.')
                return redirect('portal:patient-medical-profile')
        return self.display(request, profile, form)


class PatientUpdatesView(PatientCarePage):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        data = request.GET.copy()
        data.setdefault('sort', 'newest')
        form = PatientUpdatesFilterForm(data, auto_id='updates_%s')
        events = PatientEvent.objects.for_company(self.patient_company).filter(patient=self.patient, is_patient_visible=True)
        query = {}
        if form.is_valid():
            values = form.cleaned_data
            if values['category']:
                events = events.filter(category=values['category'])
            if values['date_from']:
                events = events.filter(occurred_at__gte=datetime.combine(values['date_from'], time.min, tzinfo=SAST))
            if values['date_to']:
                # An inclusive local-date filter without overflowing date.max.
                events = events.filter(occurred_at__lte=datetime.combine(values['date_to'], time.max, tzinfo=SAST))
            direction = '-' if values['sort'] == 'newest' else ''
            events = events.order_by(f'{direction}occurred_at', f'{direction}pk')
            query = {key: value for key, value in values.items() if value not in ('', None)}
        else:
            events = events.none()
        page = Paginator(events, 20).get_page(request.GET.get('page'))
        # Category chips keep the chosen dates and order.
        chosen = {key: request.GET[key] for key in ('date_from', 'date_to', 'sort') if request.GET.get(key)}
        chips = []
        for value, label in (('', 'All'), *PatientEvent.Category.choices):
            chip_query = urlencode({**chosen, 'category': value} if value else chosen)
            chips.append({'label': label, 'active': (form.data.get('category') or '') == value,
                          'url': reverse('portal:patient-updates') + (f'?{chip_query}' if chip_query else '')})
        context = self.context('updates', 'Updates', filter_form=form, events=page.object_list, category_chips=chips,
                               page_obj=page, is_paginated=page.has_other_pages(), pagination_query=urlencode(query))
        return render(request, 'portal/patient_care_updates.html', context)


def _ical_escape(value):
    # TEXT values are escaped before line folding. Raw CR/LF cannot create new
    # calendar properties, and no clinical notes or user-entered fields appear.
    return str(value).replace('\\', '\\\\').replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')


def _ical_fold(line):
    parts, current, count = [], '', 0
    for character in line:
        size = len(character.encode('utf-8'))
        if count + size > 75:
            parts.append(current)
            current, count = ' ', 1
        current += character
        count += size
    parts.append(current)
    return '\r\n'.join(parts)


class PatientAppointmentCalendarView(PatientCarePage):
    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk):
        appointment = get_object_or_404(Appointment.objects.for_company(self.patient_company).filter(
            patient=self.patient,
        ), pk=pk)
        stamp = lambda value: value.astimezone(datetime_timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        cancelled = appointment.status in (Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW)
        lines = [
            'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Meridian//Patient appointments//EN',
            'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'BEGIN:VEVENT',
            f'UID:appointment-{appointment.pk}-practice-{self.patient_company.pk}@meridian.local',
            f'DTSTAMP:{stamp(appointment.updated_at)}', f'DTSTART:{stamp(appointment.starts_at)}',
            f'DTEND:{stamp(appointment.starts_at + timedelta(minutes=appointment.duration_minutes))}',
            'SUMMARY:Meridian appointment',
            f'DESCRIPTION:{_ical_escape("Appointment at " + self.patient_company.name + ". Check your patient portal for details and any changes.")}',
            f'URL:{_ical_escape(request.build_absolute_uri(reverse("portal:patient-appointments")))}',
            f'STATUS:{"CANCELLED" if cancelled else "CONFIRMED"}',
            'CLASS:PRIVATE', 'END:VEVENT', 'END:VCALENDAR',
        ]
        response = HttpResponse('\r\n'.join(_ical_fold(line) for line in lines) + '\r\n', content_type='text/calendar; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="appointment-{appointment.pk}.ics"'
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        return response
