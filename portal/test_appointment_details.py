"""Standalone appointment and conversation forms are signed and practice scoped."""

from datetime import datetime, time, timedelta
import json
import re
import os
import shutil
import subprocess
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import quote

from django.conf import settings
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from care.appointment_lifecycle import change_appointment_status
from care.clinical import save_consultation
from care.models import Appointment, AuditEvent, ClinicalTask, PatientMessage
from care.services import post_patient_message
from care.test_appointment_lifecycle import AppointmentLifecycleFixture
from care.widgets import floor_quarter_hour
from practices.models import CompanyMembership
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


class AppointmentDetailPageTests(AppointmentLifecycleFixture):
    def login(self, actor=None, company=None, client=None):
        client = client or self.client
        client.force_login(actor or self.doctor)
        session = client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def url(self, name, *args):
        return reverse(f'portal:{name}', args=args)

    def booking_data(self, **overrides):
        data = {'patient': self.patient.pk, 'clinician': self.doctor.pk,
                'appointment_type': 'initial', 'starts_at': timezone.localtime(self.starts_at).strftime('%Y-%m-%dT%H:%M'),
                'duration_minutes': 30, 'video_link': ''}
        data.update(overrides)
        return data

    def status_data(self, token, **overrides):
        data = {'workflow_context': token, 'status': 'cancelled', 'reason': 'Please cancel my booking.', 'confirm': 'on'}
        data.update(overrides)
        return data

    def management_data(self, token, **overrides):
        data = {'workflow_context': token, 'action': 'handover', 'doctor': self.colleague.pk, 'confirm': 'on'}
        data.update(overrides)
        return data

    def test_booking_is_standalone_and_requires_signed_context(self):
        self.login(self.admin)
        url = self.url('appointment-book')
        response = self.client.get(url)
        self.assertTemplateUsed(response, 'portal/appointment_booking.html')
        self.assertContains(response, 'No external meeting link is needed and no payment is created. The patient, if they have a login, and the doctor, if someone else books, are emailed a confirmation.')
        self.assertIn('no-store', response['Cache-Control'])
        rejected = self.client.post(url, self.booking_data())
        self.assertEqual(rejected.status_code, 400)
        self.assertTrue(rejected.context['form'].non_field_errors())
        self.assertFalse(Appointment.objects.exists())
        response = self.client.post(url, self.booking_data(workflow_context=response.context['workflow_context']))
        appointment = Appointment.objects.get()
        self.assertRedirects(response, self.url('appointment-detail', appointment.pk), fetch_redirect_response=False)
        self.assertEqual(appointment.patient, self.patient)
        self.assertEqual(len(mail.outbox), 0)

    def test_booking_wrong_practice_patient_doctor_and_token_are_rejected(self):
        self.login(self.admin)
        url = self.url('appointment-book')
        token = self.client.get(url).context['workflow_context']
        response = self.client.post(url, self.booking_data(patient=self.beta_patient.pk, workflow_context=token))
        self.assertEqual(response.status_code, 400)
        self.assertIn('patient', response.context['form'].errors)
        self.login(self.admin, self.beta)
        response = self.client.post(url, self.booking_data(patient=self.beta_patient.pk, workflow_context=token))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['form'].non_field_errors())
        self.assertFalse(Appointment.objects.exists())

    def test_expired_tampered_and_other_actor_tokens_do_not_book(self):
        self.login(self.admin)
        url = self.url('appointment-book')
        with patch('django.core.signing.time.time', return_value=1):
            expired = self.client.get(url).context['workflow_context']
        for token in ('tampered', expired):
            with self.subTest(token=token[:10]):
                self.assertEqual(self.client.post(url, self.booking_data(workflow_context=token)).status_code, 400)
        token = self.client.get(url).context['workflow_context']
        self.login(self.doctor)
        self.assertEqual(self.client.post(url, self.booking_data(workflow_context=token)).status_code, 400)
        self.assertFalse(Appointment.objects.exists())

    def test_old_patient_bound_booking_handler_uses_service_without_type_error(self):
        self.login(self.admin)
        token = self.client.get(self.url('patient-detail', self.patient.pk)).context['legacy_appointment_context']
        data = self.booking_data(workflow_context=token)
        data.pop('patient')
        response = self.client.post(self.url('patient-appointment-create', self.patient.pk), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Appointment.objects.get().patient, self.patient)
        # The bound URL cannot be repurposed for another record.
        rejected = self.client.post(self.url('patient-appointment-create', self.patient.pk),
            self.booking_data(patient=self.other_patient.pk, workflow_context=token,
                              starts_at=timezone.localtime(self.starts_at + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')))
        self.assertContains(rejected, 'This form belongs to a different patient.')
        self.assertEqual(Appointment.objects.count(), 1)

    def test_legacy_booking_missing_expired_stale_or_other_patient_signature_preserves_draft(self):
        self.login(self.admin)
        url = self.url('patient-appointment-create', self.patient.pk)
        detail_url = self.url('patient-detail', self.patient.pk)
        with patch('django.core.signing.time.time', return_value=1):
            expired = self.client.get(detail_url).context['legacy_appointment_context']
        other = self.client.get(self.url('patient-detail', self.other_patient.pk)).context['legacy_appointment_context']
        stale = self.client.get(detail_url).context['legacy_appointment_context']
        self.patient.city = 'Updated in another tab'
        self.patient.save(update_fields=('city', 'updated_at'))
        for token in ('', 'bad-signature', expired, other, stale):
            with self.subTest(token=token[:10]):
                response = self.client.post(url, self.booking_data(workflow_context=token,
                    video_link='https://example.test/retained-draft'))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['appointment_form'].non_field_errors())
                self.assertEqual(response.context['legacy_appointment_context'], token)
                self.assertContains(response, 'https://example.test/retained-draft')
        self.assertFalse(Appointment.objects.exists())

    def test_legacy_booking_signature_cannot_be_reused_in_other_practice(self):
        self.login(self.admin)
        token = self.client.get(self.url('patient-detail', self.patient.pk)).context['legacy_appointment_context']
        self.login(self.admin, self.beta)
        response = self.client.post(self.url('patient-appointment-create', self.beta_patient.pk),
            self.booking_data(patient=self.beta_patient.pk, workflow_context=token))
        self.assertTrue(response.context['appointment_form'].non_field_errors())
        self.assertFalse(Appointment.objects.exists())

    def test_rejected_legacy_booking_does_not_mark_conversation_read(self):
        message = post_patient_message(thread=self.thread, sender=self.patient_user, body='Unread patient question')
        self.login(self.admin)
        before = AuditEvent.objects.count()
        response = self.client.post(self.url('patient-appointment-create', self.patient.pk), self.booking_data())
        self.assertTrue(response.context['appointment_form'].non_field_errors())
        message.refresh_from_db()
        self.assertIsNone(message.read_at)
        self.assertEqual(AuditEvent.objects.count(), before)
        self.assertFalse(Appointment.objects.exists())
        self.assertEqual(self.client.head(self.url('patient-detail', self.patient.pk)).status_code, 200)
        message.refresh_from_db()
        self.assertIsNone(message.read_at)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_booking_rechecks_cross_practice_patient_clash_after_valid_form(self):
        self.appointment(company=self.beta, patient=self.beta_patient, clinician=self.colleague)
        self.login(self.admin)
        url = self.url('appointment-book')
        token = self.client.get(url).context['workflow_context']
        response = self.client.post(url, self.booking_data(workflow_context=token))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'patient already has an appointment', status_code=400)
        self.assertEqual(Appointment.objects.count(), 1)

    def test_doctor_attendance_page_is_distinct_and_colleague_read_only(self):
        appointment = self.appointment(starts_at=timezone.now() - timedelta(hours=1))
        url = self.url('appointment-detail', appointment.pk)
        self.login()
        response = self.client.get(url)
        self.assertTemplateUsed(response, 'portal/appointment_detail.html')
        self.assertContains(response, 'Record completed')
        token = response.context['workflow_context']
        self.login(self.colleague)
        response = self.client.get(url)
        self.assertFalse(response.context['can_edit'])
        self.assertNotContains(response, 'Confirm status update')
        self.assertEqual(self.client.post(url, self.status_data(response.context['workflow_context'])).status_code, 403)
        self.login()
        response = self.client.post(url, self.status_data(token, status='completed', reason=''))
        self.assertEqual(response.status_code, 302)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, 'completed')

    def test_update_booking_status_opens_in_a_modal_after_the_details_and_video(self):
        appointment = self.appointment()
        url = self.url('appointment-detail', appointment.pk)
        self.login(self.admin)
        response = self.client.get(url)
        html = response.content.decode()
        # A button with the card's other actions opens the form in a modal, below the video section in the page.
        self.assertContains(response, 'href="?update=status" data-modal-open="appointment-status" aria-haspopup="dialog">Update booking status</a>')
        self.assertContains(response, '<dialog class="app-dialog" id="appointment-status" aria-labelledby="appointment-status-title">')
        self.assertContains(response, 'js/dialogs.js')
        self.assertLess(html.index('<h2>Appointment details</h2>'), html.index('>Video consultation</h2>'))
        self.assertLess(html.index('>Video consultation</h2>'), html.index('id="appointment-status"'))
        # Without scripts the button's link opens it; after a form error it opens by itself, with the error.
        opened = 'id="appointment-status" aria-labelledby="appointment-status-title" open>'
        self.assertContains(self.client.get(url, {'update': 'status'}), opened)
        response = self.client.post(url, self.status_data(response.context['workflow_context'], confirm=''))
        self.assertContains(response, opened, status_code=400)
        self.assertContains(response, 'errorlist', status_code=400)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, 'booked')
        # Someone who can't change the booking gets neither the button nor the modal.
        self.login(self.colleague)
        response = self.client.get(url)
        self.assertNotContains(response, 'Update booking status')
        self.assertNotContains(response, '<dialog')

    def test_write_consultation_note_opens_a_modal_with_this_appointment_chosen_and_the_time_now(self):
        appointment = self.appointment()
        url = self.url('appointment-detail', appointment.pk)
        create = self.url('clinical-consultation-create', self.patient.pk)
        self.login(self.doctor)
        response = self.client.get(url)
        self.assertContains(response, f'href="{create}?appointment={appointment.pk}" data-modal-open="consultation-note" aria-haspopup="dialog">Write consultation note</a>')
        self.assertContains(response, '<dialog class="app-dialog app-dialog--wide clinical-workflow" id="consultation-note" aria-labelledby="consultation-note-title">')
        self.assertContains(response, f'<form class="console-form" method="post" action="{create}">')
        self.assertContains(response, f'<option value="{appointment.pk}" selected>')
        self.assertContains(response, 'data-requires-check="consultation_confirm_signature">Sign final note</button>')
        self.assertContains(response, 'css/clinical_workflow.css')
        # Now, to the quarter hour just gone, as the picker offers only 00, 15, 30 and 45.
        occurred = response.context['consultation_form'].initial['occurred_at']
        self.assertEqual(occurred.minute % 15, 0)
        self.assertTrue(0 <= (timezone.now() - occurred).total_seconds() < 15 * 60)
        self.assertContains(response, '<select name="occurred_at_1" class="quarter-hour-time" aria-label="Time" required id="consultation_occurred_at_1">')
        # Without scripts, the link opens the note's own page with the appointment chosen.
        self.assertContains(self.client.get(f'{create}?appointment={appointment.pk}'), f'<option value="{appointment.pk}" selected>')
        # Saving from the modal goes to the new note, linked to the appointment; the button then opens it.
        # The date and the time come from the picker's two boxes.
        saved = self.client.post(create, {'clinical_context': response.context['consultation_context'], 'appointment': appointment.pk,
                                          'occurred_at_0': occurred.strftime('%Y-%m-%d'), 'occurred_at_1': occurred.strftime('%H:%M'),
                                          'summary': 'Reviewed progress.', 'action': 'save'})
        appointment.refresh_from_db()
        note = appointment.encounter
        self.assertRedirects(saved, self.url('clinical-consultation-detail', note.pk))
        response = self.client.get(url)
        self.assertContains(response, f'href="{self.url("clinical-consultation-detail", note.pk)}">Open consultation note</a>')
        self.assertNotContains(response, 'id="consultation-note"')
        # Practice administrators never get the note or its form.
        self.login(self.admin)
        response = self.client.get(url)
        self.assertNotContains(response, 'consultation note</a>')
        self.assertNotContains(response, 'id="consultation-note"')

    def test_the_record_appointment_list_shows_the_join_window_and_the_video_room_note_behind_an_info_tip(self):
        appointment = self.appointment(starts_at=timezone.make_aware(timezone.datetime(2030, 10, 16, 11, 0)), duration_minutes=15)
        cancelled = self.appointment(starts_at=timezone.make_aware(timezone.datetime(2030, 10, 17, 11, 0)), status='cancelled')
        record = self.url('patient-detail', self.patient.pk)
        self.login(self.doctor)
        # A doctor in the Clinician role works from the Overview; as a Super Admin they also have the Appointments tab.
        self.assertRedirects(self.client.get(record, {'tab': 'appointments'}), f'{record}?tab=overview', fetch_redirect_response=False)
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(role='super_admin')
        response = self.client.get(record, {'tab': 'appointments'})
        self.assertContains(response, '<th scope="col">Date and time</th><th scope="col">Appointment type</th><th scope="col">Join window</th><th scope="col">Doctor</th>')
        self.assertContains(response, '<td>10:55–11:15<small>SAST</small></td>')
        self.assertContains(response, '<td>&mdash;</td>', count=1)
        self.assertNotIn(cancelled.pk, [row.pk for row in response.context['rows'] if row.join_window])
        # The window has its own column, so the note behind the (i) leaves it out.
        self.assertNotContains(response, 'Join window:')
        self.assertContains(response, f'<span class="info-tip" data-info-tip><button class="info-tip__button" type="button" aria-label="When the video room opens" aria-describedby="video-join-{appointment.pk}">i</button><span class="info-tip__bubble" id="video-join-{appointment.pk}" role="tooltip">')
        self.assertContains(response, 'css/info_tips.css')
        self.assertContains(response, 'js/info_tips.js')
        # The appointment's own page keeps the note written out.
        self.assertContains(self.client.get(self.url('appointment-detail', appointment.pk)), '<span class="backoffice-muted">')

    def test_record_tables_filter_by_any_choices_ticked_in_a_filters_menu(self):
        mine = self.appointment(appointment_type='initial')
        theirs = self.appointment(clinician=self.colleague, appointment_type='follow_up', starts_at=self.starts_at + timedelta(hours=1))
        cancelled = self.appointment(appointment_type='follow_up', status='cancelled', starts_at=self.starts_at + timedelta(hours=2))
        record = self.url('patient-detail', self.patient.pk)
        self.login(self.admin)

        def rows(**query):
            return {row.pk for row in self.client.get(record, {'tab': 'appointments', **query}).context['rows']}

        response = self.client.get(record, {'tab': 'appointments'})
        self.assertContains(response, '<details class="filter-menu" data-filter-menu>')
        self.assertContains(response, 'js/filter_menu.js')
        self.assertNotContains(response, 'patient-workspace-filter')
        self.assertNotContains(response, 'Clear all')
        for label in ('Status', 'Appointment type', 'Doctor'):
            self.assertContains(response, f'<span class="filter-menu__label">{label}</span>')
        # Only people in this patient's appointments are offered.
        self.assertContains(response, f'name="doctor" value="{self.doctor.pk}"')
        self.assertContains(response, f'name="doctor" value="{self.colleague.pk}"')
        self.assertNotContains(response, f'name="doctor" value="{self.admin.pk}"')
        # Ticks within a section widen it; sections narrow each other.
        self.assertEqual(rows(status=['booked', 'cancelled'], doctor=self.doctor.pk), {mine.pk, cancelled.pk})
        self.assertEqual(rows(type='follow_up'), {theirs.pk, cancelled.pk})
        self.assertEqual(rows(status='all'), {mine.pk, theirs.pk, cancelled.pk})
        response = self.client.get(record, {'tab': 'appointments', 'status': ['booked', 'cancelled'], 'type': 'follow_up'})
        self.assertContains(response, '<span class="filter-menu__badge">3<span class="sr-only"> applied</span></span>')
        self.assertContains(response, '<input type="checkbox" name="status" value="cancelled" checked>')
        self.assertContains(response, f'<a class="filter-menu__clear" href="{record}?tab=appointments">Clear all</a>')
        # A choice that isn't offered shows nothing, rather than everything.
        self.assertEqual(rows(doctor=self.super_admin.pk), set())
        self.assertContains(self.client.get(record, {'tab': 'appointments', 'doctor': self.super_admin.pk}), 'filter-menu__error')
        # Consultations filter by status and by who wrote them; the doctor is a Super Admin to have the Consultations tab.
        save_consultation(company=self.company, patient=self.patient, actor=self.doctor, summary='Final',
                          occurred_at=timezone.now() - timedelta(days=1), sign=True)
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(role='super_admin')
        self.login(self.doctor)
        draft = save_consultation(company=self.company, patient=self.patient, actor=self.doctor, summary='Draft',
                                  occurred_at=timezone.now() - timedelta(hours=1))
        response = self.client.get(record, {'tab': 'consultations', 'status': 'draft'})
        self.assertEqual([row.pk for row in response.context['rows']], [draft.pk])
        self.assertEqual([section['label'] for section in response.context['filter_menu']['sections']], ['Status', 'Doctor'])

    def test_book_an_appointment_is_a_button_beside_the_filters_that_opens_a_modal(self):
        self.appointment(clinician=self.colleague)
        record = self.url('patient-detail', self.patient.pk)
        self.login(self.admin)
        response = self.client.get(record, {'tab': 'appointments'})
        html = response.content.decode()
        button = f'href="{self.url("appointment-book")}?patient={self.patient.pk}" data-modal-open="book-appointment" aria-haspopup="dialog">Book an appointment</a>'
        self.assertContains(response, button)
        self.assertLess(html.index('data-filter-menu'), html.index(button))
        self.assertContains(response, '<dialog class="app-dialog" id="book-appointment" aria-labelledby="book-appointment-title">')
        self.assertNotContains(response, '<summary>Book an appointment</summary>')
        # A mistake opens the modal again with the error; a booking returns to the list.
        create = self.url('patient-appointment-create', self.patient.pk)
        data = {'workflow_context': response.context['legacy_appointment_context'], 'patient': self.patient.pk,
                'clinician': self.doctor.pk, 'appointment_type': 'follow_up', 'duration_minutes': 15, 'video_link': '',
                'starts_at': floor_quarter_hour(timezone.localtime(timezone.now() - timedelta(days=1))).strftime('%Y-%m-%dT%H:%M')}
        failed = self.client.post(create, data)
        self.assertContains(failed, 'id="book-appointment" aria-labelledby="book-appointment-title" open>')
        self.assertContains(failed, 'Choose a future appointment time.')
        data['starts_at'] = timezone.localtime(self.starts_at + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.assertRedirects(self.client.post(create, data), f'{record}?tab=appointments', fetch_redirect_response=False)
        self.assertTrue(Appointment.objects.filter(patient=self.patient, clinician=self.doctor, appointment_type='follow_up').exists())
        # A doctor covering the appointment, who can't change the booking, is sent to the Overview, which has neither.
        self.login(self.colleague)
        response = self.client.get(record, {'tab': 'appointments'}, follow=True)
        self.assertRedirects(response, f'{record}?tab=overview')
        self.assertNotContains(response, 'Book an appointment')
        self.assertNotContains(response, 'id="book-appointment"')

    def test_create_appointment_opens_a_modal_with_date_time_and_duration_in_thirds(self):
        self.login(self.admin)
        response = self.client.get(self.url('staff-schedule'))
        self.assertContains(response, f'href="{self.url("appointment-book")}" data-modal-open="create-appointment" aria-haspopup="dialog">Create appointment</a>')
        self.assertContains(response, '<dialog class="app-dialog" id="create-appointment" aria-labelledby="create-appointment-title">')
        html = response.content.decode()
        fields = html[html.index('class="booking-fields"'):html.index('</dialog>', html.index('id="create-appointment"'))]
        order = [fields.index(f'name="{name}"') for name in ('patient', 'clinician', 'appointment_type', 'date', 'time', 'duration_minutes')]
        self.assertEqual(order, sorted(order))
        self.assertLess(fields.index('class="booking-fields__when"'), fields.index('name="date"'))
        self.assertIn(f'data-times-url="{self.url("appointment-book-times")}"', fields)
        self.assertEqual(re.findall(r'<option value="(\d+)"', fields[fields.index('name="duration_minutes"'):]), ['15', '30', '45', '60'])
        self.assertNotIn('name="video_link" type', fields)
        self.assertContains(response, 'js/appointment_booking.js')
        # Booking from the modal: the date and time make the start.
        day = timezone.localtime(self.starts_at + timedelta(days=1)).date()
        booked = self.client.post(self.url('appointment-book'), {
            'workflow_context': response.context['booking_context'], 'patient': self.patient.pk, 'clinician': self.doctor.pk,
            'appointment_type': 'follow_up', 'date': day.isoformat(), 'time': '10:15', 'duration_minutes': '45'})
        appointment = Appointment.objects.get(patient=self.patient, duration_minutes=45)
        self.assertRedirects(booked, self.url('appointment-detail', appointment.pk), fetch_redirect_response=False)
        self.assertEqual(timezone.localtime(appointment.starts_at).strftime('%Y-%m-%d %H:%M'), f'{day.isoformat()} 10:15')

    def test_the_time_list_offers_the_clinicians_free_quarter_hours_that_day(self):
        from care.models import DoctorWorkingPattern

        day = timezone.localdate() + timedelta(days=7)
        DoctorWorkingPattern.objects.create(company=self.company, clinician=self.doctor, weekday=day.weekday(), is_working=True,
                                            starts_at=time(9, 0), ends_at=time(11, 0))
        self.appointment(starts_at=timezone.make_aware(datetime.combine(day, time(9, 30))), duration_minutes=30)
        self.login(self.admin)
        url = self.url('appointment-book-times')

        def times(**query):
            return self.client.get(url, {'clinician': self.doctor.pk, 'date': day.isoformat(), 'duration': 15, **query}).json()['times']

        # In their hours, on the quarter hour, clear of the 9:30 booking; a longer booking has fewer starts.
        self.assertEqual(times(), ['09:00', '09:15', '10:00', '10:15', '10:30', '10:45'])
        self.assertEqual(times(duration=60), ['10:00'])
        # The patient's own other bookings are left out too.
        self.assertEqual(times(patient=self.patient.pk), ['09:00', '09:15', '10:00', '10:15', '10:30', '10:45'])
        self.appointment(clinician=self.colleague, starts_at=timezone.make_aware(datetime.combine(day, time(10, 0))), duration_minutes=30)
        self.assertEqual(times(patient=self.patient.pk), ['09:00', '09:15', '10:30', '10:45'])
        # Anything not offered here can't be asked about.
        self.assertEqual(self.client.get(url, {'clinician': self.doctor.pk, 'date': day.isoformat(), 'duration': 20}).status_code, 400)
        self.assertEqual(self.client.get(url, {'clinician': self.other_user.pk, 'date': day.isoformat(), 'duration': 15}).status_code, 400)

    def test_admin_cannot_forge_attendance_action_and_own_doctor_waits_for_end(self):
        appointment = self.appointment()
        url = self.url('appointment-detail', appointment.pk)
        for actor in (self.admin, self.super_admin, self.doctor):
            with self.subTest(actor=actor.pk):
                self.login(actor)
                response = self.client.get(url)
                self.assertNotContains(response, 'Record completed')
                self.assertEqual(self.client.post(url, self.status_data(response.context['workflow_context'], status='completed')).status_code, 400)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, 'booked')

    def test_signed_consultation_cannot_be_cancelled_from_status_page(self):
        appointment = self.appointment(starts_at=timezone.now() - timedelta(hours=1))
        save_consultation(company=self.company, patient=self.patient, actor=self.doctor,
            summary='Final note', occurred_at=appointment.starts_at, appointment=appointment, sign=True)
        self.login(self.admin)
        url = self.url('appointment-detail', appointment.pk)
        token = self.client.get(url).context['workflow_context']
        response = self.client.post(url, self.status_data(token))
        self.assertContains(response, 'signed clinical encounter', status_code=400)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, 'booked')

    def test_patient_detail_has_safe_links_and_only_own_future_cancellation(self):
        appointment = self.appointment(video_link='javascript:alert(1)')
        self.login(self.patient_user)
        url = self.url('patient-appointment-detail', appointment.pk)
        response = self.client.get(url)
        self.assertTemplateUsed(response, 'portal/patient_appointment_detail.html')
        self.assertContains(response, '/static/css/patient_pages.css')
        self.assertContains(response, 'Add to calendar')
        # A new time is asked of support by email; the practice moves the appointment.
        self.assertNotContains(response, 'Discuss a new time')
        self.assertNotContains(response, 'accepted proposal')
        when = date_format(timezone.localtime(appointment.starts_at), 'D j M Y, H:i')
        subject = quote(f'Request for Appointment Change: {self.patient.first_name} {self.patient.last_name}, {when} SAST')
        self.assertContains(response, f'href="mailto:{settings.SUPPORT_EMAIL}?subject={subject}">Request a change</a>')
        self.assertContains(response, f'email {settings.SUPPORT_EMAIL} and the practice will move your appointment.')
        self.assertNotContains(response, 'javascript:')
        self.assertNotContains(response, 'Write consultation note')
        self.assertNotContains(response, 'Record completed')
        self.assertEqual(self.client.post(url, self.status_data(response.context['workflow_context'])).status_code, 302)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, 'cancelled')

    def test_legacy_staff_patient_detail_does_not_render_external_video_links_or_mutate_them(self):
        self.login(self.admin)
        unsafe = self.appointment(video_link='javascript:alert(1)')
        self.appointment(video_link='https://[', starts_at=self.starts_at + timedelta(hours=1))
        self.appointment(video_link='https://example.test/valid-video', starts_at=self.starts_at + timedelta(hours=2))
        response = self.client.get(self.url('patient-detail', self.patient.pk), {'tab': 'appointments'})
        self.assertNotContains(response, 'javascript:')
        self.assertNotContains(response, 'https://[')
        self.assertNotContains(response, 'https://example.test/valid-video')
        unsafe.refresh_from_db()
        self.assertEqual(unsafe.video_link, 'javascript:alert(1)')

    def test_patient_foreign_record_and_practice_switch_fail_closed(self):
        appointment = self.appointment()
        other = self.appointment(patient=self.other_patient, starts_at=self.starts_at + timedelta(hours=1))
        self.login(self.patient_user)
        url = self.url('patient-appointment-detail', appointment.pk)
        token = self.client.get(url).context['workflow_context']
        self.assertEqual(self.client.get(self.url('patient-appointment-detail', other.pk)).status_code, 404)
        self.login(self.patient_user, self.beta)
        self.assertEqual(self.client.post(url, self.status_data(token)).status_code, 404)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, 'booked')

    def test_stale_terminal_form_error_remains_visible_on_read_only_page(self):
        appointment = self.appointment()
        self.login(self.patient_user)
        url = self.url('patient-appointment-detail', appointment.pk)
        token = self.client.get(url).context['workflow_context']
        change_appointment_status(appointment=appointment, actor=self.admin, status='cancelled',
            reason='Practice cancellation', confirm=True, expected_updated=appointment.updated_at.isoformat())
        response = self.client.post(url, self.status_data(token))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.context['can_edit'])
        self.assertContains(response, 'No changes saved', status_code=400)
        self.assertContains(response, 'record changed in another tab', status_code=400)
        self.assertNotContains(response, 'Confirm status update', status_code=400)

    def test_status_signed_context_bound_to_exact_appointment_and_patient_actor(self):
        first = self.appointment()
        second = self.appointment(starts_at=self.starts_at + timedelta(hours=1))
        self.login(self.patient_user)
        token = self.client.get(self.url('patient-appointment-detail', first.pk)).context['workflow_context']
        response = self.client.post(self.url('patient-appointment-detail', second.pk), self.status_data(token))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['form'].non_field_errors())
        self.assertFalse(Appointment.objects.filter(status='cancelled').exists())

    def test_get_and_head_do_not_write_and_all_forms_require_csrf(self):
        appointment = self.appointment()
        urls = [self.url('appointment-book'), self.url('appointment-detail', appointment.pk),
                self.url('conversation-manage', self.thread.pk)]
        before = AuditEvent.objects.count()
        for url in urls:
            # Conversations are managed by the people in them; the administrator handles bookings.
            self.login(self.doctor if 'messages' in url else self.admin)
            self.assertEqual(self.client.get(url).status_code, 200)
            self.assertEqual(self.client.head(url).status_code, 200)
        self.login(self.patient_user)
        self.assertEqual(self.client.head(self.url('patient-appointment-detail', appointment.pk)).status_code, 200)
        self.assertEqual(AuditEvent.objects.count(), before)
        secure = Client(enforce_csrf_checks=True)
        self.login(self.admin, client=secure)
        for url in urls:
            self.assertEqual(secure.post(url, {'confirm': 'on'}).status_code, 403)

    def test_conversation_page_adds_a_clinician_and_stays_private(self):
        url = self.url('conversation-manage', self.thread.pk)
        for actor in (self.admin, self.super_admin, self.colleague):
            self.login(actor)
            self.assertEqual(self.client.get(url).status_code, 404)
        self.login(self.doctor)
        response = self.client.get(url)
        self.assertTemplateUsed(response, 'portal/message_management.html')
        # "Clinician to add" only shows while adding a clinician is the chosen action (the script hides it otherwise).
        self.assertContains(response, '<div data-shows-for-action="handover"><div class="console-form__field"><label for="id_doctor">Clinician to add</label>')
        self.assertContains(response, 'js/conversation_management.js')
        token = response.context['workflow_context']
        self.assertEqual(self.client.post(url, self.management_data('missing')).status_code, 400)
        response = self.client.post(url, self.management_data(token))
        # Once applied, it goes back to the conversation in Patient messages.
        self.assertRedirects(response, f"{reverse('portal:staff-inbox')}?thread={self.thread.pk}", fetch_redirect_response=False)
        self.assertEqual(set(self.thread.participants.all()), {self.doctor, self.colleague})
        self.assertFalse(PatientMessage.objects.exists())
        self.assertFalse(ClinicalTask.objects.exists())
        # Someone already in the conversation cannot be added again.
        self.assertEqual(self.client.post(url, self.management_data(token)).status_code, 400)
        self.login(self.colleague)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.login(self.patient_user)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_conversation_new_message_stales_form(self):
        self.login(self.doctor)
        url = self.url('conversation-manage', self.thread.pk)
        token = self.client.get(url).context['workflow_context']
        post_patient_message(thread=self.thread, sender=self.patient_user, body='Newer question')
        response = self.client.post(url, self.management_data(token))
        self.assertContains(response, 'record changed in another tab', status_code=400)
        self.assertEqual(list(self.thread.participants.all()), [self.doctor])

    def test_conversation_closes_despite_an_old_pending_suggestion_and_rejects_other_practice_token(self):
        # Time suggestions are gone, so one left pending doesn't stop the conversation closing.
        self.proposal(self.appointment())
        self.login(self.doctor)
        url = self.url('conversation-manage', self.thread.pk)
        token = self.client.get(url).context['workflow_context']
        self.assertEqual(self.client.post(url, self.management_data(token, action='close')).status_code, 302)
        self.thread.refresh_from_db()
        self.assertTrue(self.thread.is_closed)
        self.login(self.doctor, self.beta)
        self.assertEqual(self.client.post(url, self.management_data(token)).status_code, 404)
        beta_url = self.url('conversation-manage', self.beta_thread.pk)
        self.assertEqual(self.client.post(beta_url, self.management_data(token, action='close')).status_code, 400)

    @skipUnless(os.environ.get('MERIDIAN_PLAYWRIGHT_PATH'), 'Optional installed local browser layout check')
    def test_optional_real_browser_layouts(self):
        appointment = self.appointment()
        past = self.appointment(starts_at=timezone.now() - timedelta(hours=1))
        pages = []
        for name, actor, url in (
            ('staff-booking', self.admin, self.url('appointment-book')),
            ('staff-cancellation', self.admin, self.url('appointment-detail', appointment.pk)),
            ('doctor-attendance', self.doctor, self.url('appointment-detail', past.pk)),
            ('colleague-readonly', self.colleague, self.url('appointment-detail', appointment.pk)),
            ('patient-cancellation', self.patient_user, self.url('patient-appointment-detail', appointment.pk)),
            ('patient-past-readonly', self.patient_user, self.url('patient-appointment-detail', past.pk)),
            ('conversation-management', self.doctor, self.url('conversation-manage', self.thread.pk)),
        ):
            self.login(actor)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            pages.append({'name': name, 'html': response.content.decode()})
        node = os.environ.get('MERIDIAN_NODE') or shutil.which('node')
        self.assertIsNotNone(node, 'Install Node or set MERIDIAN_NODE for the optional browser test.')
        result = subprocess.run([node,
                                 str(settings.BASE_DIR / 'scripts/operations_layout_smoke.cjs')],
                                input=json.dumps(pages), text=True, capture_output=True,
                                cwd=settings.BASE_DIR, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['checked'], 21)
