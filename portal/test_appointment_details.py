"""Standalone appointment and conversation forms are signed and practice scoped."""

from datetime import timedelta
import json
import os
import shutil
import subprocess
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from care.appointment_lifecycle import change_appointment_status
from care.clinical import save_consultation
from care.models import Appointment, AuditEvent, ClinicalTask, PatientMessage
from care.services import post_patient_message
from care.test_appointment_lifecycle import AppointmentLifecycleFixture
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
        data = {'workflow_context': token, 'action': 'escalate', 'doctor': self.doctor.pk,
                'priority': 'high', 'note': 'Please review.', 'confirm': 'on'}
        data.update(overrides)
        return data

    def test_booking_is_standalone_and_requires_signed_context(self):
        self.login(self.admin)
        url = self.url('appointment-book')
        response = self.client.get(url)
        self.assertTemplateUsed(response, 'portal/appointment_booking.html')
        self.assertContains(response, 'No external meeting link is needed; no email or payment is created')
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
        response = self.client.get(self.url('patient-detail', self.patient.pk))
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
        self.login(self.admin)
        urls = [self.url('appointment-book'), self.url('appointment-detail', appointment.pk),
                self.url('conversation-manage', self.thread.pk)]
        before = AuditEvent.objects.count()
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200)
            self.assertEqual(self.client.head(url).status_code, 200)
        self.login(self.patient_user)
        self.assertEqual(self.client.head(self.url('patient-appointment-detail', appointment.pk)).status_code, 200)
        self.assertEqual(AuditEvent.objects.count(), before)
        secure = Client(enforce_csrf_checks=True)
        self.login(self.admin, client=secure)
        for url in urls:
            self.assertEqual(secure.post(url, {'confirm': 'on'}).status_code, 403)

    def test_conversation_page_routes_escalation_and_disallows_patient_management(self):
        self.login(self.admin)
        url = self.url('conversation-manage', self.thread.pk)
        response = self.client.get(url)
        self.assertTemplateUsed(response, 'portal/message_management.html')
        token = response.context['workflow_context']
        self.assertEqual(self.client.post(url, self.management_data('missing')).status_code, 400)
        response = self.client.post(url, self.management_data(token))
        task = ClinicalTask.objects.get()
        self.assertRedirects(response, self.url('task-edit', task.pk), fetch_redirect_response=False)
        self.assertFalse(PatientMessage.objects.exists())
        self.assertEqual(self.client.post(url, self.management_data(token)).status_code, 302)
        self.assertEqual(ClinicalTask.objects.count(), 1)
        self.login(self.patient_user)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_conversation_new_message_stales_form_and_preserves_draft(self):
        self.login(self.admin)
        url = self.url('conversation-manage', self.thread.pk)
        token = self.client.get(url).context['workflow_context']
        post_patient_message(thread=self.thread, sender=self.patient_user, body='Newer question')
        response = self.client.post(url, self.management_data(token, note='Retain this draft'))
        self.assertContains(response, 'record changed in another tab', status_code=400)
        self.assertContains(response, 'Retain this draft', status_code=400)
        self.assertFalse(ClinicalTask.objects.exists())

    def test_conversation_pending_proposal_error_and_other_practice_token(self):
        self.proposal(self.appointment())
        self.login(self.admin)
        url = self.url('conversation-manage', self.thread.pk)
        token = self.client.get(url).context['workflow_context']
        response = self.client.post(url, self.management_data(token, action='close'))
        self.assertContains(response, 'pending appointment suggestions', status_code=400)
        self.login(self.admin, self.beta)
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
            ('conversation-management', self.admin, self.url('conversation-manage', self.thread.pk)),
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
