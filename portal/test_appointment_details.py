"""Standalone appointment and conversation forms are signed and practice scoped."""

from datetime import datetime, time, timedelta
import json
import re
import os
import shutil
import subprocess
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.contrib.messages import get_messages
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from care.appointment_lifecycle import change_appointment_status, reschedule_appointment
from care.clinical import save_consultation
from care.models import Appointment, AuditEvent, ClinicalTask, ClinicianAssignment, PatientMessage
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
        data = {'workflow_context': token, 'status': 'cancelled', 'reason': 'Please cancel my booking.'}
        data.update(overrides)
        return data

    def management_data(self, token, **overrides):
        data = {'workflow_context': token, 'action': 'handover', 'doctor': self.colleague.pk, 'confirm': 'on'}
        data.update(overrides)
        return data

    def time_data(self, page, starts_at):
        local = timezone.localtime(starts_at)
        return {'workflow_context': page.context['time_context'], 'date': local.date().isoformat(), 'time': f'{local:%H:%M}'}

    @override_settings(EMAIL_ENABLED=True, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_an_administrator_changes_the_time_and_the_patient_and_clinician_are_emailed(self):
        appointment = self.appointment(appointment_type='follow_up', reminder_sent_at=timezone.now())
        new_start = self.starts_at + timedelta(days=1)
        self.login(self.admin)
        page = self.client.get(self.url('appointment-detail', appointment.pk))
        self.assertContains(page, 'data-modal-open="appointment-time"')
        # Its Time list leaves out only the clinician's other bookings, not this one.
        self.assertContains(page, f'data-exclude-appointment="{appointment.pk}"')
        data = self.time_data(page, new_start)
        before = len(mail.outbox)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url('appointment-change-time', appointment.pk), data)
        self.assertRedirects(response, self.url('appointment-detail', appointment.pk), fetch_redirect_response=False)
        appointment.refresh_from_db()
        self.assertEqual(appointment.starts_at, new_start)
        self.assertIsNone(appointment.reminder_sent_at)  # Reminded again before the new time.
        sent = {message.to[0]: message.subject for message in mail.outbox[before:]}
        self.assertEqual(set(sent), {self.patient_user.email, self.doctor.email})
        self.assertTrue(sent[self.patient_user.email].startswith('Your appointment has moved to'))
        self.assertTrue(sent[self.doctor.email].startswith('Appointment moved: Alpha Patient on'))
        self.assertTrue(AuditEvent.objects.filter(action='appointment.time_changed', target_id=str(appointment.pk)).exists())
        # The same form again is out of date.
        self.assertEqual(self.client.post(self.url('appointment-change-time', appointment.pk), data).status_code, 400)

    @override_settings(EMAIL_ENABLED=True, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_a_doctor_moves_their_own_appointment_and_only_the_patient_is_emailed(self):
        appointment = self.appointment()
        self.login(self.doctor)
        page = self.client.get(self.url('appointment-detail', appointment.pk))
        before = len(mail.outbox)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url('appointment-change-time', appointment.pk), self.time_data(page, self.starts_at + timedelta(hours=2)))
        appointment.refresh_from_db()
        self.assertEqual(appointment.starts_at, self.starts_at + timedelta(hours=2))
        self.assertEqual([message.to[0] for message in mail.outbox[before:]], [self.patient_user.email])
        # Another doctor's appointment isn't theirs to move.
        other = self.appointment(clinician=self.colleague, starts_at=self.starts_at + timedelta(days=2))
        with self.assertRaises(PermissionDenied):
            reschedule_appointment(appointment=other, actor=self.doctor, starts_at=self.starts_at + timedelta(days=4),
                                   expected_updated=other.updated_at.isoformat())

    def test_a_clash_or_a_cancelled_booking_cannot_be_moved_and_patients_ask_the_practice(self):
        appointment = self.appointment()
        self.appointment(starts_at=self.starts_at + timedelta(days=1))  # The doctor is busy then.
        self.login(self.super_admin)
        page = self.client.get(self.url('appointment-detail', appointment.pk))
        response = self.client.post(self.url('appointment-change-time', appointment.pk),
                                    self.time_data(page, self.starts_at + timedelta(days=1)))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'id="appointment-time" aria-labelledby="appointment-time-title" open', status_code=400)
        appointment.refresh_from_db()
        self.assertEqual(appointment.starts_at, self.starts_at)
        cancelled = self.appointment(starts_at=self.starts_at + timedelta(days=5), status='cancelled')
        self.assertNotContains(self.client.get(self.url('appointment-detail', cancelled.pk)), 'Change time')
        self.login(self.patient_user)
        self.assertNotContains(self.client.get(self.url('patient-appointments')), 'appointment-change-time')

    def test_booking_is_standalone_and_requires_signed_context(self):
        self.login(self.admin)
        url = self.url('appointment-book')
        response = self.client.get(url)
        self.assertTemplateUsed(response, 'portal/appointment_booking.html')
        self.assertContains(response, 'No external meeting link is needed. The patient, if they have a login, and the doctor, if someone else books, are emailed a confirmation.')
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

    def test_a_doctor_only_reaches_patients_whose_records_they_can_open(self):
        # other_patient was never this doctor's, nor booked with them. A colleague's appointment with them stays
        # closed, and booking them, which would make this doctor their cover, isn't offered or accepted.
        theirs = self.appointment(patient=self.other_patient, clinician=self.colleague)
        own = self.appointment(clinician=self.colleague, starts_at=self.starts_at + timedelta(hours=2))
        self.login(self.doctor)
        response = self.client.get(self.url('appointment-detail', theirs.pk))
        self.assertRedirects(response, self.url('patient-list'), fetch_redirect_response=False)
        self.assertEqual(self.client.get(self.url('appointment-detail', own.pk)).status_code, 200)
        page = self.client.get(self.url('appointment-book'))
        self.assertEqual(list(page.context['form'].fields['patient'].queryset), [self.patient])
        response = self.client.post(self.url('appointment-book'), self.booking_data(
            patient=self.other_patient.pk, starts_at=timezone.localtime(self.starts_at + timedelta(hours=4)).strftime('%Y-%m-%dT%H:%M'),
            workflow_context=page.context['workflow_context']))
        self.assertEqual(response.status_code, 400)
        self.assertIn('patient', response.context['form'].errors)
        self.assertFalse(Appointment.objects.filter(patient=self.other_patient, clinician=self.doctor).exists())
        for name in ('task-create', 'team-inbox', 'staff-tasks', 'staff-schedule'):
            with self.subTest(page=name):
                self.assertNotContains(self.client.get(self.url(name)), 'Other Patient')
        # Practice administrators still book anyone.
        self.login(self.admin)
        self.assertIn(self.other_patient, self.client.get(self.url('appointment-book')).context['form'].fields['patient'].queryset)

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
        self.login(self.super_admin)
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
        self.login(self.super_admin)
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
        CompanyMembership.objects.create(company=self.beta, user=self.super_admin, role='super_admin')
        self.login(self.super_admin)
        token = self.client.get(self.url('patient-detail', self.patient.pk)).context['legacy_appointment_context']
        self.login(self.super_admin, self.beta)
        response = self.client.post(self.url('patient-appointment-create', self.beta_patient.pk),
            self.booking_data(patient=self.beta_patient.pk, workflow_context=token))
        self.assertTrue(response.context['appointment_form'].non_field_errors())
        self.assertFalse(Appointment.objects.exists())

    def test_rejected_legacy_booking_does_not_mark_conversation_read(self):
        message = post_patient_message(thread=self.thread, sender=self.patient_user, body='Unread patient question')
        self.login(self.super_admin)
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
        # Recording attendance is more than cancelling, so the button stays general.
        self.assertContains(response, 'type="submit">Confirm status update</button>')
        token = response.context['workflow_context']
        # A colleague who once looked after the patient reads the appointment but can't change it.
        ClinicianAssignment.objects.create(company=self.company, patient=self.patient, clinician=self.colleague, ended_at=timezone.now())
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
        # Practice administrators can only cancel, so the button says so and the status can't be changed.
        self.assertContains(response, 'href="?update=status" data-modal-open="appointment-status" aria-haspopup="dialog">Cancel appointment</a>')
        self.assertTrue(response.context['form'].fields['status'].disabled)
        self.assertInHTML('<option value="cancelled" selected>Cancel booking</option>', html)
        # Nobody ticks a confirmation (Nov 2026): the dialog shows the 24-hour policy and its button says what it does.
        self.assertNotContains(response, 'I confirm this appointment update')
        self.assertContains(response, '<p class="app-dialog__policy">Please note that bookings missed or cancelled within 24 hours of the scheduled time will be billed for in full')
        self.assertContains(response, 'type="submit">Confirm cancellation</button>')
        self.assertContains(response, '<dialog class="app-dialog" id="appointment-status" aria-labelledby="appointment-status-title">')
        self.assertContains(response, 'js/dialogs.js')
        # Practice administrators never join consultations, so their page has no video section.
        self.assertNotContains(response, '>Video consultation</h2>')
        self.assertLess(html.index('<h2>Appointment details</h2>'), html.index('id="appointment-status"'))
        # Without scripts the button's link opens it; after a form error it opens by itself, with the error.
        opened = 'id="appointment-status" aria-labelledby="appointment-status-title" open>'
        self.assertContains(self.client.get(url, {'update': 'status'}), opened)
        token = response.context['workflow_context']
        response = self.client.post(url, self.status_data(token, reason=''))
        self.assertContains(response, opened, status_code=400)
        self.assertContains(response, 'errorlist', status_code=400)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, 'booked')
        self.login(self.super_admin)
        html = self.client.get(url).content.decode()
        self.assertIn('aria-haspopup="dialog">Update booking status</a>', html)
        self.assertNotIn('I confirm this appointment update', html)
        self.assertIn('Please note that bookings missed or cancelled within 24 hours of the scheduled time will be billed for in full', html)
        self.assertIn('type="submit">Confirm cancellation</button>', html)
        self.assertLess(html.index('<h2>Appointment details</h2>'), html.index('>Video consultation</h2>'))
        self.assertLess(html.index('>Video consultation</h2>'), html.index('id="appointment-status"'))
        # Someone who can't change the booking gets neither the button nor the modal.
        ClinicianAssignment.objects.create(company=self.company, patient=self.patient, clinician=self.colleague, ended_at=timezone.now())
        self.login(self.colleague)
        response = self.client.get(url)
        self.assertNotContains(response, 'Update booking status')
        self.assertNotContains(response, 'Cancel appointment')
        self.assertNotContains(response, '<dialog')
        # A cancellation goes through without any tick.
        self.login(self.admin)
        self.client.post(url, self.status_data(token))
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, 'cancelled')

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
        self.assertContains(response, '<td>10:45–11:15</td>')
        self.assertContains(response, '<td>&mdash;</td>', count=1)
        self.assertNotIn(cancelled.pk, [row.pk for row in response.context['rows'] if row.join_window])
        # The window has its own column, so the note behind the (i) leaves it out.
        self.assertNotContains(response, 'Join window:')
        self.assertContains(response, f'<span class="info-tip" data-info-tip><button class="info-tip__button" type="button" aria-label="When the video room opens" aria-describedby="video-join-{appointment.pk}">i</button><span class="info-tip__bubble" id="video-join-{appointment.pk}" role="tooltip">')
        self.assertContains(response, 'css/info_tips.css')
        self.assertContains(response, 'js/info_tips.js')
        # The appointment's own page keeps the note written out: the window first, why it's closed beneath it.
        self.assertContains(self.client.get(self.url('appointment-detail', appointment.pk)),
                            '<span class="video-join-note"><span>Join window: 16 Oct, 10:45–11:15.</span><small>')

    def test_record_tables_filter_by_any_choices_ticked_in_a_filters_menu(self):
        mine = self.appointment(appointment_type='initial')
        theirs = self.appointment(clinician=self.colleague, appointment_type='follow_up', starts_at=self.starts_at + timedelta(hours=1))
        cancelled = self.appointment(appointment_type='follow_up', status='cancelled', starts_at=self.starts_at + timedelta(hours=2))
        record = self.url('patient-detail', self.patient.pk)
        self.login(self.super_admin)

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
        self.login(self.super_admin)
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
        self.assertEqual(re.findall(r'<option value="(\d+)"', fields[fields.index('name="duration_minutes"'):]), ['15', '30'])
        self.assertNotIn('name="video_link" type', fields)
        self.assertContains(response, 'js/appointment_booking.js')
        # Booking from the modal: the date and time make the start.
        day = timezone.localtime(self.starts_at + timedelta(days=1)).date()
        booked = self.client.post(self.url('appointment-book'), {
            'workflow_context': response.context['booking_context'], 'patient': self.patient.pk, 'clinician': self.doctor.pk,
            'appointment_type': 'follow_up', 'date': day.isoformat(), 'time': '10:15', 'duration_minutes': '30'})
        appointment = Appointment.objects.get(patient=self.patient, duration_minutes=30)
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
        self.assertEqual(times(duration=30), ['09:00', '10:00', '10:15', '10:30'])
        # The patient's own other bookings are left out too.
        self.assertEqual(times(patient=self.patient.pk), ['09:00', '09:15', '10:00', '10:15', '10:30', '10:45'])
        self.appointment(clinician=self.colleague, starts_at=timezone.make_aware(datetime.combine(day, time(10, 0))), duration_minutes=30)
        self.assertEqual(times(patient=self.patient.pk), ['09:00', '09:15', '10:30', '10:45'])
        # Anything not offered here can't be asked about.
        self.assertEqual(self.client.get(url, {'clinician': self.doctor.pk, 'date': day.isoformat(), 'duration': 20}).status_code, 400)
        self.assertEqual(self.client.get(url, {'clinician': self.doctor.pk, 'date': day.isoformat(), 'duration': 60}).status_code, 400)
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

    def cancel_token(self, appointment):
        """The signed context of Cancel's dialog for this appointment, on My Appointments."""
        rows = self.client.get(self.url('patient-appointments')).context['appointments']
        return next(row for row in rows if row.pk == appointment.pk).cancel_context

    def test_patients_have_no_appointment_page_and_its_old_address_goes_to_my_appointments(self):
        # Dr Czech, Nov 2026: everything is on My Appointments. Reminder emails already sent link to the old address.
        appointment = self.appointment()
        other = self.appointment(patient=self.other_patient, starts_at=self.starts_at + timedelta(hours=1))
        self.login(self.patient_user)
        for target in (appointment, other):
            for method in (self.client.get, self.client.head):
                self.assertRedirects(method(self.url('patient-appointment-detail', target.pk)), self.url('patient-appointments'),
                                     fetch_redirect_response=False)
        self.assertEqual(self.client.post(self.url('patient-appointment-detail', appointment.pk), {}).status_code, 405)
        self.assertEqual(self.client.get(self.url('patient-appointment-cancel', appointment.pk)).status_code, 405)

    def test_patient_list_has_open_meeting_room_request_a_change_and_a_plain_cancel(self):
        booked = self.appointment(video_link='javascript:alert(1)')
        cancelled = self.appointment(starts_at=self.starts_at + timedelta(hours=1), status='cancelled')
        self.login(self.patient_user)
        list_url = self.url('patient-appointments')
        html = self.client.get(list_url).content.decode()
        # No appointment page, so no Details, and no calendar download: that's attached to the confirmation email.
        self.assertNotIn('>Details</a>', html)
        self.assertNotIn('calendar.ics', html)
        self.assertNotIn('Add to calendar', html)
        self.assertNotIn('javascript:', html)
        # Open meeting room for the booked one, in the next-consult banner and its row, greyed out until the room opens.
        self.assertEqual(html.count('Open meeting room</a>'), 2)
        self.assertIn('<a class="console-button console-button--secondary patient-row-action" role="link" data-appointment-video-join '
                      f'aria-disabled="true" aria-describedby="video-join-{booked.pk}"><svg', html)
        self.assertNotIn('Join video call', html)
        # Request a change and Cancel are plain buttons, neither of them red, each opening a dialog. Without scripts
        # their links open this list with the dialog open.
        self.assertNotIn('console-button--cancel', html)
        for kind, label in (('change', 'Request a change'), ('cancel', 'Cancel<span class="sr-only"> this appointment</span>')):
            self.assertIn(f'<a class="console-button console-button--secondary patient-row-action" href="{list_url}?update={kind}-appointment-{booked.pk}" '
                          f'data-modal-open="{kind}-appointment-{booked.pk}" aria-haspopup="dialog">{label}</a>', html)
        row = html[html.index(f'data-video-appointment-id="{booked.pk}"'):]
        self.assertLess(row.index('Open meeting room</a>'), row.index('>Request a change</a>'))
        self.assertLess(row.index('>Request a change</a>'), row.index('>Cancel<span'))
        self.assertEqual(html.count('>Request a change</a>'), 1)
        # A cancelled one has no actions at all.
        self.assertNotIn(f'change-appointment-{cancelled.pk}', html)
        self.assertNotIn(f'cancel-appointment-{cancelled.pk}', html)
        self.assertEqual(html.count('data-appointment-video-join'), 2)

    def test_request_a_change_shows_the_cancellation_policy_then_messages_the_practice(self):
        later = self.appointment(starts_at=timezone.now() + timedelta(days=3))
        soon = self.appointment(starts_at=timezone.now() + timedelta(hours=5))
        self.login(self.patient_user)
        list_url = self.url('patient-appointments')
        html = self.client.get(list_url).content.decode()

        def dialog(appointment):
            start = html.index(f'<dialog class="app-dialog" id="change-appointment-{appointment.pk}"')
            return html[start:html.index('</dialog>', start)]

        when = date_format(timezone.localtime(later.starts_at), 'D j M Y, H:i')
        subject = f'Request to change my appointment: {when}'
        query = urlencode({'compose': 1, 'subject': subject, 'to': self.admin.pk})
        far = dialog(later)
        self.assertIn('>Request a change</h2>', far)
        self.assertIn(f'<p class="app-dialog__sub">{later.get_appointment_type_display()} with {self.doctor.titled_name} on {when}.</p>', far)
        # The cancellation policy, as before cancelling, then on to a message to a practice administrator, who moves it.
        self.assertIn('<p class="app-dialog__policy">Please note that bookings missed or cancelled within 24 hours of the scheduled '
                      'time will be billed for in full', far)
        self.assertIn(f'<a class="console-button console-button--primary" href="{reverse("portal:patient-messages")}?'
                      f'{query.replace("&", "&amp;")}#patient-conversation"><svg aria-hidden="true"', far)
        self.assertIn('<path d="M7 8h10M7 12h7"/></svg>\nMessage the practice</a>', far)
        self.assertLess(far.index('app-dialog__policy'), far.index('Message the practice'))
        self.assertIn('data-dialog-close>Close</button>', far)
        self.assertNotIn('app-dialog__alert', far)
        compose = self.client.get(f'{reverse("portal:patient-messages")}?{query}')
        self.assertEqual(compose.context['thread_form'].initial, {'subject': subject, 'recipient': str(self.admin.pk)})
        # Within 24 hours it says plainly that a change now means a new fee, before anything else.
        near = dialog(soon)
        self.assertIn('<p class="app-dialog__alert">Your appointment is in less than 24 hours, so changing it now means a new '
                      'consultation fee.</p>', near)
        self.assertLess(near.index('app-dialog__alert'), near.index('app-dialog__text'))
        self.assertLess(near.index('app-dialog__text'), near.index('app-dialog__policy'))
        # Without scripts the link opens the list with that one dialog open.
        page = self.client.get(list_url, {'update': f'change-appointment-{later.pk}'})
        self.assertContains(page, f'id="change-appointment-{later.pk}" aria-labelledby="change-appointment-{later.pk}-title" open>')
        self.assertContains(page, f'id="change-appointment-{soon.pk}" aria-labelledby="change-appointment-{soon.pk}-title">')
        self.assertContains(page, f'id="cancel-appointment-{later.pk}" aria-labelledby="cancel-appointment-{later.pk}-title">')

    def test_patient_can_request_a_change_or_cancel_only_before_the_start(self):
        under_way = self.appointment(starts_at=timezone.now() - timedelta(minutes=5))
        self.login(self.patient_user)
        # Still on Upcoming until it's due to end, with Open meeting room but neither of the others.
        html = self.client.get(self.url('patient-appointments')).content.decode()
        self.assertIn(f'data-video-appointment-id="{under_way.pk}"', html)
        self.assertIn(f'aria-describedby="video-join-{under_way.pk}"><svg', html)
        self.assertNotIn('>Request a change</a>', html)
        self.assertNotIn(f'change-appointment-{under_way.pk}', html)
        self.assertNotIn(f'cancel-appointment-{under_way.pk}', html)
        # Nor with a dialog left open from before it started.
        Appointment.objects.filter(pk=under_way.pk).update(starts_at=timezone.now() + timedelta(hours=1))
        token = self.cancel_token(under_way)
        Appointment.objects.filter(pk=under_way.pk).update(starts_at=timezone.now() - timedelta(minutes=5))
        response = self.client.post(self.url('patient-appointment-cancel', under_way.pk), self.status_data(token), follow=True)
        self.assertRedirects(response, self.url('patient-appointments'))
        self.assertContains(response, 'Contact the practice about a past appointment.')
        under_way.refresh_from_db()
        self.assertEqual(under_way.status, 'booked')

    def test_patient_cancels_from_my_appointments_and_comes_back(self):
        booked = self.appointment()
        past = self.appointment(starts_at=timezone.now() - timedelta(days=1))
        self.login(self.patient_user)
        list_url = self.url('patient-appointments')
        response = self.client.get(list_url)
        html = response.content.decode()
        self.assertNotIn(f'cancel-appointment-{past.pk}', html)
        row = next(appointment for appointment in response.context['appointments'] if appointment.pk == booked.pk)
        start = html.index(f'<dialog class="app-dialog" id="cancel-appointment-{booked.pk}"')
        dialog = html[start:html.index('</dialog>', start)]
        # Cancel appointment (Dr Czech, Nov 2026): no status to choose, since cancelling is all a patient can do (it
        # still submits as a cancellation whatever is sent), and a reason they must give.
        self.assertIn(f'<h2 id="cancel-appointment-{booked.pk}-title">Cancel appointment</h2>', dialog)
        self.assertTrue(row.cancel_form.fields['status'].disabled)
        self.assertNotIn('name="status"', dialog)
        self.assertNotIn('<select', dialog)
        self.assertInHTML(f'<label for="cancel_{booked.pk}_reason">Reason for cancelling</label>', dialog)
        self.assertInHTML(f'<textarea name="reason" cols="40" rows="3" maxlength="500" required id="cancel_{booked.pk}_reason"></textarea>', dialog)
        self.assertNotIn('Optional', dialog)
        self.assertNotIn(f'id="cancel_{booked.pk}_confirm"', dialog)
        self.assertIn(f'<form class="console-form" method="post" action="{self.url("patient-appointment-cancel", booked.pk)}">', dialog)
        self.assertIn('<p class="app-dialog__policy">Please note that bookings missed or cancelled within 24 hours of the scheduled time will be billed for in full', dialog)
        # Keep appointment closes it, beside Confirm cancellation.
        self.assertIn('data-dialog-close>Keep appointment</button><button class="console-button console-button--primary" type="submit">Confirm cancellation</button>', dialog)
        # Three days away, so no warning about the fee.
        self.assertNotIn('app-dialog__alert', dialog)
        filtered = f'{list_url}?when=month'
        response = self.client.post(self.url('patient-appointment-cancel', booked.pk), {
            **self.status_data(row.cancel_context), 'next': filtered})
        self.assertRedirects(response, filtered, fetch_redirect_response=False)
        self.assertEqual([str(message) for message in get_messages(response.wsgi_request)], ['Appointment cancelled.'])
        booked.refresh_from_db()
        self.assertEqual(booked.status, 'cancelled')
        # Anywhere else goes to My Appointments.
        other = self.appointment(starts_at=self.starts_at + timedelta(hours=2))
        response = self.client.post(self.url('patient-appointment-cancel', other.pk), {
            **self.status_data(self.cancel_token(other)), 'next': 'https://example.com/'})
        self.assertRedirects(response, list_url, fetch_redirect_response=False)
        other.refresh_from_db()
        self.assertEqual(other.status, 'cancelled')
        # Without scripts, Cancel opens the list with its dialog open.
        third = self.appointment(starts_at=self.starts_at + timedelta(hours=4))
        page = self.client.get(list_url, {'update': f'cancel-appointment-{third.pk}'})
        self.assertContains(page, f'id="cancel-appointment-{third.pk}" aria-labelledby="cancel-appointment-{third.pk}-title" open>')

    def test_cancelling_within_24_hours_says_plainly_it_is_billed_in_full(self):
        soon = self.appointment(starts_at=timezone.now() + timedelta(hours=5))
        self.login(self.patient_user)
        html = self.client.get(self.url('patient-appointments')).content.decode()
        start = html.index(f'<dialog class="app-dialog" id="cancel-appointment-{soon.pk}"')
        dialog = html[start:html.index('</dialog>', start)]
        self.assertIn('<p class="app-dialog__alert">Your appointment is in less than 24 hours, so if you cancel now it will be '
                      'billed in full and not refunded.</p>', dialog)
        # Before the reason, and before the form.
        self.assertLess(dialog.index('app-dialog__alert'), dialog.index('<form'))

    def test_a_patient_cancel_that_does_not_go_through_says_why_on_my_appointments(self):
        appointment = self.appointment()
        self.login(self.patient_user)
        token = self.cancel_token(appointment)
        list_url = self.url('patient-appointments')
        # A reason is required (it's never emailed), and a blank one says so in the patient's terms.
        for reason in ('', '   '):
            response = self.client.post(self.url('patient-appointment-cancel', appointment.pk), {
                **self.status_data(token, reason=reason), 'next': list_url}, follow=True)
            self.assertRedirects(response, list_url)
            self.assertContains(response, 'Please give a reason for cancelling.')
            self.assertNotContains(response, 'that the patient can see')
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, 'booked')
        # A forged status is refused, and nothing changes.
        response = self.client.post(self.url('patient-appointment-cancel', appointment.pk), {
            **self.status_data(token, status='completed'), 'next': list_url}, follow=True)
        self.assertRedirects(response, list_url)
        self.assertContains(response, 'Select a valid choice.')
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, 'booked')
        # Cancelled by the practice meanwhile: the stale dialog says so, and the list has no actions for it now.
        change_appointment_status(appointment=appointment, actor=self.admin, status='cancelled',
            reason='Practice cancellation', confirm=True, expected_updated=appointment.updated_at.isoformat())
        response = self.client.post(self.url('patient-appointment-cancel', appointment.pk), {
            **self.status_data(token), 'next': list_url}, follow=True)
        self.assertRedirects(response, list_url)
        self.assertContains(response, 'record changed in another tab')
        self.assertNotContains(response, 'Appointment cancelled.')
        self.assertNotContains(response, f'cancel-appointment-{appointment.pk}')

    def test_legacy_staff_patient_detail_does_not_render_external_video_links_or_mutate_them(self):
        self.login(self.super_admin)
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
        token = self.cancel_token(appointment)
        self.assertEqual(self.client.post(self.url('patient-appointment-cancel', other.pk), self.status_data(token)).status_code, 404)
        self.login(self.patient_user, self.beta)
        self.assertEqual(self.client.post(self.url('patient-appointment-cancel', appointment.pk), self.status_data(token)).status_code, 404)
        for item in (appointment, other):
            item.refresh_from_db()
            self.assertEqual(item.status, 'booked')

    def test_status_signed_context_bound_to_exact_appointment_and_patient_actor(self):
        first = self.appointment()
        second = self.appointment(starts_at=self.starts_at + timedelta(hours=1))
        self.login(self.patient_user)
        response = self.client.post(self.url('patient-appointment-cancel', second.pk), self.status_data(self.cancel_token(first)))
        self.assertRedirects(response, self.url('patient-appointments'), fetch_redirect_response=False)
        sent = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertTrue(sent)
        self.assertNotIn('Appointment cancelled.', sent)
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
        self.assertEqual(self.client.head(self.url('patient-appointments')).status_code, 200)
        self.assertEqual(self.client.head(self.url('patient-appointment-detail', appointment.pk)).status_code, 302)
        self.assertEqual(AuditEvent.objects.count(), before)
        secure = Client(enforce_csrf_checks=True)
        self.login(self.admin, client=secure)
        for url in urls:
            self.assertEqual(secure.post(url, {'confirm': 'on'}).status_code, 403)
        self.login(self.patient_user, client=secure)
        self.assertEqual(secure.post(self.url('patient-appointment-cancel', appointment.pk), {'status': 'cancelled'}).status_code, 403)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, 'booked')

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
            ('patient-appointments', self.patient_user, self.url('patient-appointments')),
            ('patient-past-appointments', self.patient_user, self.url('patient-appointments') + '?tab=past'),
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
