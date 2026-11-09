"""Standalone patient care pages."""

from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.availability import SAST, open_slots_for_day, start_slots
from care.clinical_summary import get_summary
from care.models import PatientEvent
from care.patient_care import book_patient_appointment, patient_busy_intervals
from .appointment_detail_views import labelled_slots
from .patient_care_forms import BookingTimesQuery, PatientBookingForm, PatientUpdatesFilterForm
from .patient_context import validate_patient_context
from .patient_summary import intake_answers, latest_weight, medical_information, medical_profile
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
        key: request.GET[key] for key in ('clinician', 'date') if request.GET.get(key)})
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
                messages.success(request, 'Appointment booked.' if created else 'This appointment is already booked.')
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
        clinician, day = query.cleaned_data['clinician'], query.cleaned_data['date']
        times = patient_booking_times(self.patient_company, self.patient, clinician, day)
        return JsonResponse({'times': times, 'slots': labelled_slots(start_slots(
            company=self.patient_company, clinician=clinician, day=day, duration_minutes=15, free_times=times), patient=True)})


class PatientMedicalProfileView(PatientCarePage):
    """What the doctor has on file, read-only. Patients tell their doctor about changes in a message; the doctor
    records medication, allergies and history in Summary data."""
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        # Summary data as the clinician sees it: what they saved, or what it starts from before they do.
        summary = get_summary(self.patient_company, self.patient)
        medical_change_query = urlencode({'compose': 1, 'subject': 'Change to my medical information'})
        context = self.context(
            'medical_profile', 'My Medical Information',
            medical_rows=medical_information(
                profile=medical_profile(self.patient_company, self.patient),
                intake=intake_answers(self.patient_company, self.patient),
                weight=latest_weight(self.patient_company, self.patient),
                recorded_height=summary.height_cm, start_weight=summary.start_weight_kg,
            ),
            medical_change_url=f"{reverse('portal:patient-messages')}?{medical_change_query}#patient-conversation",
        )
        return render(request, 'portal/patient_care_medical_profile.html', context)


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
