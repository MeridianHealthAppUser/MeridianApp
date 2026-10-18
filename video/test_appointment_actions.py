"""Patient appointment Join actions follow the server's window without a page reload."""

import json
import os
import shutil
import subprocess
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, AuditEvent
from care.test_appointment_lifecycle import AppointmentLifecycleFixture
from practices.models import CompanyMembership
from practices.services import ACTIVE_PATIENT_COMPANY_SESSION_KEY

from .access import VideoAccessDenied, resolve_room_access
from .models import CallSession


@override_settings(DEBUG=True, VIDEO_ENABLED=True, VIDEO_JOIN_EARLY_MINUTES=15, VIDEO_JOIN_GRACE_MINUTES=0)
class PatientAppointmentVideoActionTests(AppointmentLifecycleFixture):
    def setUp(self):
        super().setUp()
        self.now = timezone.now().replace(microsecond=0)
        self.booking = self.appointment(starts_at=self.now + timedelta(minutes=15))

    def login(self, user=None, company=None):
        self.client.force_login(user or self.patient_user)
        session = self.client.session
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def status(self, *, now=None, ids=None, **headers):
        with patch('portal.patient_views.timezone.now', return_value=now or self.now):
            return self.client.get(reverse('portal:patient-appointment-video-status'),
                                   {'id': ids if ids is not None else [self.booking.pk]}, **headers)

    def test_fifteen_minute_opening_and_scheduled_end_are_authoritative(self):
        self.login()
        closes = self.booking.starts_at + timedelta(minutes=self.booking.duration_minutes)
        for instant, allowed in ((self.now - timedelta(microseconds=1), False), (self.now, True),
                                 (self.booking.starts_at, True), (closes, True), (closes + timedelta(microseconds=1), False)):
            with self.subTest(instant=instant):
                response = self.status(now=instant)
                self.assertEqual(response.status_code, 200)
                row = response.json()['appointments'][0]
                self.assertEqual(row['joinUrl'], reverse('video:room', args=[self.booking.pk]) if allowed else None)
                self.assertEqual(row['opensAt'], self.now.isoformat())
                self.assertEqual(row['closesAt'], closes.isoformat())
                if allowed:
                    self.assertEqual(resolve_room_access(self.patient_user.pk, self.booking.pk, now=instant).role, 'patient')
                else:
                    with self.assertRaises(VideoAccessDenied):
                        resolve_room_access(self.patient_user.pk, self.booking.pk, now=instant)

    def test_only_current_patients_visible_ids_are_returned(self):
        self.login()
        same_practice_other = self.appointment(patient=self.other_patient, starts_at=self.now)
        other_practice = self.appointment(company=self.beta, patient=self.beta_patient, starts_at=self.now)
        response = self.status(ids=[self.booking.pk, same_practice_other.pk, other_practice.pk, 999999])
        self.assertEqual([row['id'] for row in response.json()['appointments']], [self.booking.pk])
        self.assertEqual(response.json()['scope'], f'patient:{self.company.pk}:{self.patient.pk}:{self.patient_user.pk}')
        self.assertIn('no-store', response['Cache-Control'])

    def test_login_patient_access_and_current_scope_are_required(self):
        self.assertEqual(self.status().status_code, 302)
        self.login(self.doctor)
        self.assertEqual(self.status().status_code, 403)
        self.login()
        self.assertEqual(self.status(HTTP_X_MERIDIAN_SCOPE='patient:999:999:999').status_code, 409)

    def test_cancelled_completed_missed_and_revoked_clinician_never_offer_join(self):
        self.login()
        for status in ('cancelled', 'completed', 'no_show'):
            with self.subTest(status=status):
                Appointment.objects.filter(pk=self.booking.pk).update(status=status)
                row = self.status().json()['appointments'][0]
                self.assertIsNone(row['joinUrl'])
                self.assertIsNone(row['opensAt'])
        Appointment.objects.filter(pk=self.booking.pk).update(status='booked')
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(is_active=False)
        self.assertIsNone(self.status().json()['appointments'][0]['joinUrl'])

    def test_reschedule_and_service_availability_are_rechecked(self):
        self.login()
        self.assertIsNotNone(self.status().json()['appointments'][0]['joinUrl'])
        moved = self.booking.starts_at + timedelta(days=1)
        Appointment.objects.filter(pk=self.booking.pk).update(starts_at=moved)
        row = self.status().json()['appointments'][0]
        self.assertIsNone(row['joinUrl'])
        self.assertEqual(row['opensAt'], (moved - timedelta(minutes=15)).isoformat())
        Appointment.objects.filter(pk=self.booking.pk).update(starts_at=self.booking.starts_at)
        with override_settings(DEBUG=False, VIDEO_REDIS_URL=''):
            self.assertIsNone(self.status().json()['appointments'][0]['joinUrl'])
        with override_settings(VIDEO_ENABLED=False):
            self.assertIsNone(self.status().json()['appointments'][0]['joinUrl'])

    def test_bounded_input_and_status_refreshes_do_not_create_call_or_audit_records(self):
        self.login()
        before = AuditEvent.objects.count()
        for ids in ([], ['invalid'], ['0'], [str(2 ** 63)], ['1'] * 51):
            with self.subTest(ids=ids[:2]):
                self.assertEqual(self.status(ids=ids).status_code, 400)
        for _ in range(2):
            self.assertEqual(self.status().status_code, 200)
        self.assertEqual(AuditEvent.objects.count(), before)
        self.assertFalse(CallSession.objects.exists())

    def test_actions_column_contains_live_join_placeholder_and_server_rendered_open_link(self):
        self.login()
        url = reverse('portal:patient-appointments')
        with patch('portal.patient_views.timezone.now', return_value=self.now):
            page = self.client.get(url)
        self.assertContains(page, 'data-appointment-video')
        self.assertContains(page, 'data-video-appointment-id="%s"' % self.booking.pk)
        self.assertContains(page, 'data-appointment-video-join href="%s">Join video call</a>' % reverse('video:room', args=[self.booking.pk]))
        with patch('portal.patient_views.timezone.now', return_value=self.now - timedelta(minutes=1)):
            future = self.client.get(url)
        self.assertContains(future, '<span data-appointment-video-action hidden>')
        self.assertNotContains(future, 'data-appointment-video-join href=')

    @skipUnless(os.getenv('MERIDIAN_PLAYWRIGHT_PATH'), 'Optional installed local browser runner is not configured')
    def test_browser_updates_actions_and_expiry_without_reloading(self):
        self.login()
        with patch('portal.patient_views.timezone.now', return_value=self.now - timedelta(seconds=1)):
            page = self.client.get(reverse('portal:patient-appointments'))
        result = subprocess.run(
            [os.environ.get('MERIDIAN_NODE') or shutil.which('node'), str(settings.BASE_DIR / 'scripts/test_appointment_video.cjs')],
            input=json.dumps({'html': page.content.decode(), 'id': self.booking.pk,
                              'scope': f'patient:{self.company.pk}:{self.patient.pk}:{self.patient_user.pk}',
                              'opensAt': self.now.isoformat()}),
            text=True, capture_output=True, timeout=75, cwd=settings.BASE_DIR,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
