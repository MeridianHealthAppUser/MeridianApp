"""Standalone patient care pages. No payment, email or account conversion."""

from datetime import datetime, time, timedelta, timezone as datetime_timezone
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.formats import date_format
from django.views import View

from care.availability import SAST, open_slots_for_day
from care.models import Appointment, PatientEvent, PatientMedicalProfile
from care.patient_care import book_patient_appointment, patient_busy_intervals, save_patient_medical_profile
from .clinical_forms import make_clinical_context, validate_clinical_context
from .patient_care_forms import (
    BOOKING_SLOT_MAX_AGE, BOOKING_TYPES, BookingConfirmationForm, BookingFilterForm,
    PatientMedicalProfileForm, PatientUpdatesFilterForm, make_booking_slot, read_booking_slot,
)
from .patient_context import validate_patient_context
from .patient_views import patient_appointments_context, patient_page_context
from .views import PatientPortalRequiredMixin


class PatientCarePage(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    http_method_names = ('get', 'post', 'head', 'options')

    def context(self, section, title, **extra):
        return dict(patient_page_context(self.request, self.patient_company, self.patient, section, title), **extra)


def add_error(form, error):
    for message in error.messages:
        form.add_error(None, message)


BOOKING_DAY_CHIPS = 7


def _booking_chips(filters, data):
    """Links that change one booking choice at a time and keep the others."""
    today = timezone.localdate()
    chosen = {key: str(data.get(key) or '') for key in ('date', 'clinician', 'appointment_type', 'status')}

    def link(**changes):
        params = {key: value for key, value in {**chosen, **changes}.items() if value}
        return f"{reverse('portal:patient-appointments')}?{urlencode(params)}#book"

    days = []
    for offset in range(BOOKING_DAY_CHIPS):
        day = today + timedelta(days=offset)
        label = 'Today' if offset == 0 else 'Tomorrow' if offset == 1 else date_format(day, 'D j M')
        days.append({'label': label, 'url': link(date=day.isoformat()), 'active': chosen['date'] == day.isoformat()})
    doctors = [{'label': 'Any clinician', 'url': link(clinician=''), 'active': not chosen['clinician']}]
    doctors += [{'label': doctor.full_name, 'url': link(clinician=str(doctor.pk)), 'active': chosen['clinician'] == str(doctor.pk)}
                for doctor in filters.fields['clinician'].queryset]
    types = [{'label': label.split(' · ')[0], 'url': link(appointment_type=value), 'active': chosen['appointment_type'] == value}
             for value, label in BOOKING_TYPES]
    return {'days': days, 'doctors': doctors, 'types': types}


def booking_context(request, company, patient, booking_form=None):
    """The booking card on My Appointments: choices, open 15-minute times and the confirmation form."""
    data = (request.POST if request.method == 'POST' else request.GET).copy()
    data.setdefault('date', timezone.localdate().isoformat())
    data.setdefault('appointment_type', Appointment.Type.REVIEW)
    filters = BookingFilterForm(data, company=company, auto_id='booking_filter_%s')
    slots = []
    selected = None
    if request.method == 'POST':
        try:
            selected = read_booking_slot(request.POST.get('slot', ''), request, company, patient)
        except ValidationError:
            pass
    if filters.is_valid():
        clinician = filters.cleaned_data['clinician']
        doctors = [clinician] if clinician else filters.fields['clinician'].queryset
        day = filters.cleaned_data['date']
        busy = list(patient_busy_intervals(patient, day))
        for slot in open_slots_for_day(company=company, clinicians=doctors, day=day, duration_minutes=15, limit=100):
            if any(slot.starts_at < finish and slot.starts_at + timedelta(minutes=15) > begin for begin, finish in busy):
                continue
            slots.append({
                'token': make_booking_slot(request, company, patient, slot, filters.cleaned_data['appointment_type']),
                'starts_at': slot.starts_at, 'clinician': slot.clinician,
                'selected': bool(selected and selected.get('clinician_id') == slot.clinician_id
                                 and parse_datetime(selected.get('starts_at', '')) == slot.starts_at
                                 and selected.get('appointment_type') == filters.cleaned_data['appointment_type']),
            })
    return {
        'booking_filter_form': filters, 'booking_form': booking_form or BookingConfirmationForm(),
        'slots': slots, 'slot_expiry_minutes': BOOKING_SLOT_MAX_AGE // 60,
        'booking_chips': _booking_chips(filters, data),
    }


class PatientBookingView(PatientCarePage):
    """Booking now happens on My Appointments; this address keeps old links and the booking POST working."""

    def display(self, request, booking_form=None):
        context = patient_appointments_context(request, self.patient_company, self.patient)
        context.update(booking_context(request, self.patient_company, self.patient, booking_form))
        return render(request, 'portal/patient_appointments.html', context)

    def get(self, request):
        query = request.GET.urlencode()
        return redirect(f"{reverse('portal:patient-appointments')}{'?' + query if query else ''}#book")

    def post(self, request):
        form = BookingConfirmationForm(request.POST)
        valid = form.is_valid()
        try:
            validate_patient_context(request, self.patient_company, self.patient)
            slot = read_booking_slot(request.POST.get('slot', ''), request, self.patient_company, self.patient)
        except ValidationError as error:
            add_error(form, error)
            valid = False
        if valid:
            try:
                starts_at = parse_datetime(slot.get('starts_at', ''))
                appointment, created = book_patient_appointment(
                    company=self.patient_company, patient=self.patient, actor=request.user,
                    clinician_id=slot.get('clinician_id'), starts_at=starts_at,
                    appointment_type=slot.get('appointment_type'), request=request,
                )
            except ValidationError as error:
                add_error(form, error)
            else:
                messages.success(request, 'Appointment booked. No payment was taken.' if created else 'This appointment is already booked.')
                return redirect('portal:patient-appointments')
        return self.display(request, form)


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
