"""Fresh room access, protected ICE issuance and native appointment links."""

from dataclasses import FrozenInstanceError
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, AuditEvent, PatientEvent
from care.test_appointment_lifecycle import AppointmentLifecycleFixture
from practices.models import CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY

from .access import VideoAccessDenied, attach_video_join, resolve_room_access
from .models import CallParticipant, CallSession


@override_settings(DEBUG=True, VIDEO_ENABLED=True, VIDEO_JOIN_EARLY_MINUTES=5, VIDEO_JOIN_GRACE_MINUTES=0)
class VideoPolicyTests(AppointmentLifecycleFixture):
    def setUp(self):
        super().setUp()
        self.now = timezone.now().replace(microsecond=0)
        self.booking = self.appointment(starts_at=self.now + timedelta(minutes=2))

    def access(self, actor=None, **overrides):
        values = {'now': self.now, **overrides}
        return resolve_room_access(actor or self.doctor.pk, self.booking.pk, **values)

    def test_exact_participants_receive_immutable_fresh_access_snapshot(self):
        doctor = self.access()
        patient = self.access(self.patient_user)
        self.assertEqual((doctor.role, doctor.peer_id), ('doctor', self.patient_user.pk))
        self.assertEqual((patient.role, patient.peer_id), ('patient', self.doctor.pk))
        self.assertEqual(doctor.company_id, self.company.pk)
        self.assertEqual(doctor.patient_id, self.patient.pk)
        self.assertEqual(doctor.practice_name, self.company.name)
        self.assertEqual(doctor.room_url, reverse('video:room', args=[self.booking.pk]))
        with self.assertRaises(FrozenInstanceError):
            doctor.role = 'super_admin'

    def test_admin_super_other_doctor_and_other_patient_have_no_room_override(self):
        for actor in (self.admin, self.super_admin, self.colleague, self.other_user):
            with self.subTest(actor=actor.pk), self.assertRaises(VideoAccessDenied) as denied:
                self.access(actor)
            self.assertEqual(denied.exception.code, 4003)
        type(self.super_admin).objects.filter(pk=self.super_admin.pk).update(is_superuser=True, is_staff=True)
        with self.assertRaises(VideoAccessDenied):
            self.access(self.super_admin)

    def test_window_is_enforced_at_both_boundaries(self):
        opens = self.booking.starts_at - timedelta(minutes=5)
        closes = self.booking.starts_at + timedelta(minutes=self.booking.duration_minutes)
        for instant in (opens, self.booking.starts_at, closes):
            with self.subTest(instant=instant):
                self.assertEqual(self.access(now=instant).appointment_id, self.booking.pk)
        for instant in (opens - timedelta(microseconds=1), closes + timedelta(microseconds=1)):
            with self.subTest(instant=instant), self.assertRaises(VideoAccessDenied):
                self.access(now=instant)
        with self.assertRaises(VideoAccessDenied):
            self.access(now=self.now.replace(tzinfo=None))

    @override_settings(VIDEO_JOIN_EARLY_MINUTES=10, VIDEO_JOIN_GRACE_MINUTES=2)
    def test_configured_grace_does_not_change_actual_appointment_end(self):
        ends = self.booking.starts_at + timedelta(minutes=self.booking.duration_minutes)
        access = self.access(now=ends + timedelta(minutes=2))
        self.assertEqual(access.ends_at, ends)
        self.assertEqual(access.join_closes_at, ends + timedelta(minutes=2))
        self.assertEqual(access.join_opens_at, self.booking.starts_at - timedelta(minutes=10))

    def test_future_window_explanation_does_not_grant_room_access(self):
        future = self.booking.starts_at - timedelta(days=1)
        access = self.access(now=future, require_window=False)
        self.assertEqual(access.appointment_id, self.booking.pk)
        with self.assertRaises(VideoAccessDenied):
            self.access(now=future)

    def test_every_active_relationship_is_rechecked_from_database(self):
        for record in (self.company, self.patient, self.patient_user, self.doctor):
            with self.subTest(model=type(record).__name__, pk=record.pk):
                type(record).objects.filter(pk=record.pk).update(is_active=False)
                for actor in (self.doctor, self.patient_user):
                    with self.assertRaises(VideoAccessDenied):
                        self.access(actor)
                type(record).objects.filter(pk=record.pk).update(is_active=True)
        membership = CompanyMembership.objects.get(company=self.company, user=self.doctor)
        for changes in ({'is_active': False}, {'is_active': True, 'role': 'practice_admin'}):
            CompanyMembership.objects.filter(pk=membership.pk).update(**changes)
            with self.assertRaises(VideoAccessDenied):
                self.access()

    def test_cancelled_completed_missed_or_invalid_duration_booking_denied(self):
        for status in ('cancelled', 'completed', 'no_show'):
            Appointment.objects.filter(pk=self.booking.pk).update(status=status)
            with self.subTest(status=status), self.assertRaises(VideoAccessDenied):
                self.access()
        for duration in (0, 4, 121):
            Appointment.objects.filter(pk=self.booking.pk).update(status='booked', duration_minutes=duration)
            with self.subTest(duration=duration), self.assertRaises(VideoAccessDenied):
                self.access()

    def test_tenant_corruption_missing_patient_identity_and_self_call_denied(self):
        Appointment.objects.filter(pk=self.booking.pk).update(patient=self.beta_patient)
        with self.assertRaises(VideoAccessDenied):
            self.access()
        Appointment.objects.filter(pk=self.booking.pk).update(patient=self.patient)
        Patient.objects.filter(pk=self.patient.pk).update(user=None)
        with self.assertRaises(VideoAccessDenied):
            self.access()
        Patient.objects.filter(pk=self.patient.pk).update(user=self.doctor)
        with self.assertRaises(VideoAccessDenied):
            self.access()

    def test_bad_identifiers_and_disabled_feature_fail_closed(self):
        for value in (None, True, -1, 2 ** 63, '1' * 100, '١'):
            with self.subTest(value=value), self.assertRaises(VideoAccessDenied):
                resolve_room_access(value, self.booking.pk)
        with self.assertRaises(VideoAccessDenied):
            resolve_room_access(self.doctor.pk, 2 ** 63)
        with override_settings(VIDEO_ENABLED=False), self.assertRaises(VideoAccessDenied):
            self.access()

    def test_join_decoration_is_batched_and_does_not_offer_admin_join(self):
        other = self.appointment(starts_at=self.now + timedelta(minutes=1))
        with self.assertNumQueries(1):
            rows = attach_video_join([self.booking, other], self.doctor.pk, allowed_role='doctor', now=self.now)
        self.assertTrue(all(row.video_room_url for row in rows))
        with self.assertNumQueries(0):
            rows = attach_video_join(rows, self.admin.pk, allowed_role=None, now=self.now)
        self.assertTrue(all(row.video_room_url is None for row in rows))


@override_settings(DEBUG=True, VIDEO_ENABLED=True, VIDEO_JOIN_EARLY_MINUTES=5, VIDEO_JOIN_GRACE_MINUTES=0)
class VideoHTTPTests(AppointmentLifecycleFixture):
    def setUp(self):
        super().setUp()
        self.now = timezone.now().replace(microsecond=0)
        self.booking = self.appointment(starts_at=self.now + timedelta(minutes=2))

    def login(self, actor=None, company=None, client=None):
        client = client or self.client
        client.force_login(actor or self.doctor)
        session = client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def url(self, name='room', appointment=None):
        return reverse(f'video:{name}', args=[(appointment or self.booking).pk])

    def test_room_is_login_protected_standalone_preflight_and_never_writes_history(self):
        self.assertEqual(self.client.get(self.url()).status_code, 302)
        self.login()
        before = AuditEvent.objects.count()
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'video/room.html')
        self.assertEqual(response.context['video_config']['appointmentId'], self.booking.pk)
        self.assertEqual(response.context['video_config']['myUserId'], self.doctor.pk)
        self.assertEqual(response.context['video_config']['peerName'], self.patient_user.full_name)
        self.assertEqual(response.context['video_config']['iceConfigUrl'], self.url('ice-config'))
        self.assertNotIn('iceServers', response.context['video_config'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        self.assertEqual(self.client.head(self.url()).status_code, 200)
        self.assertFalse(CallSession.objects.exists())
        self.assertFalse(CallParticipant.objects.exists())
        self.assertFalse(PatientEvent.objects.exists())
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_direct_room_from_other_owned_practice_does_not_toggle_session(self):
        self.login(self.doctor, self.beta)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['video_config']['companyName'], self.company.name)
        self.assertEqual(self.client.session[ACTIVE_COMPANY_SESSION_KEY], self.beta.pk)
        self.assertEqual(response.context['video_config']['returnUrl'], reverse('portal:staff-schedule'))
        self.login(self.patient_user, self.beta)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session[ACTIVE_PATIENT_COMPANY_SESSION_KEY], self.beta.pk)
        self.assertEqual(response.context['video_config']['returnUrl'], reverse('portal:patient-appointments'))

    def test_room_does_not_select_fallback_practices_in_an_unset_session(self):
        self.client.force_login(self.doctor)
        before = dict(self.client.session)
        self.assertEqual(self.client.get(self.url()).status_code, 200)
        self.assertEqual(dict(self.client.session), before)

    def test_non_parties_unknown_and_out_of_window_rooms_share_generic_denial(self):
        for actor in (self.admin, self.super_admin, self.colleague, self.other_user):
            self.login(actor)
            self.assertEqual(self.client.get(self.url()).status_code, 404)
            self.assertEqual(self.client.post(self.url('ice-config')).status_code, 404)
        self.login()
        self.assertEqual(self.client.get(reverse('video:room', args=[2 ** 70])).status_code, 404)
        Appointment.objects.filter(pk=self.booking.pk).update(starts_at=self.now + timedelta(days=2))
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        self.assertEqual(self.client.post(self.url('ice-config')).status_code, 404)

    def test_booking_change_between_room_and_ice_is_revalidated(self):
        self.login()
        self.assertEqual(self.client.get(self.url()).status_code, 200)
        Appointment.objects.filter(pk=self.booking.pk).update(status='cancelled')
        with patch('video.ice.build_ice_config') as build:
            self.assertEqual(self.client.post(self.url('ice-config')).status_code, 404)
        build.assert_not_called()

    def test_ice_requires_csrf_post_and_contains_only_short_lived_configuration(self):
        secure = Client(enforce_csrf_checks=True)
        self.login(client=secure)
        self.assertEqual(secure.get(self.url('ice-config')).status_code, 405)
        self.assertEqual(secure.post(self.url('ice-config')).status_code, 403)
        secure.get(self.url())
        token = secure.cookies['csrftoken'].value
        configuration = {'iceServers': [{'urls': ['stun:example.test:3478']}],
                         'expiresAt': (self.now + timedelta(minutes=10)).isoformat(),
                         'relayConfigured': False, 'iceTransportPolicy': 'all'}
        with patch('video.ice.build_ice_config', return_value=configuration) as build:
            response = secure.post(self.url('ice-config'), HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), configuration)
        self.assertEqual(build.call_args.args[0].user_id, self.doctor.pk)
        self.assertIn('no-store', response['Cache-Control'])

    @override_settings(DEBUG=False, VIDEO_REDIS_URL='', REDIS_URL='', SECURE_SSL_REDIRECT=False)
    def test_production_without_shared_redis_returns_503_not_an_unusable_room(self):
        self.login()
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 503)
        self.assertIn('no-store', response['Cache-Control'])
        with patch('video.ice.build_ice_config') as build:
            response = self.client.post(self.url('ice-config'))
        self.assertEqual(response.status_code, 503)
        build.assert_not_called()
        self.assertFalse(CallSession.objects.exists())

    def test_ice_configuration_failure_does_not_expose_secret_details(self):
        self.login()
        with patch('video.ice.build_ice_config', side_effect=ImproperlyConfigured('SUPER-SECRET-CONFIG')):
            response = self.client.post(self.url('ice-config'))
        self.assertEqual(response.status_code, 503)
        self.assertNotContains(response, 'SUPER-SECRET-CONFIG', status_code=503)
        self.assertIn('no-store', response['Cache-Control'])

    def test_native_join_links_are_shown_only_to_booked_doctor_or_patient(self):
        staff_detail = reverse('portal:appointment-detail', args=[self.booking.pk])
        for actor, permitted in ((self.doctor, True), (self.colleague, False),
                                  (self.admin, False), (self.super_admin, False)):
            self.login(actor)
            response = self.client.get(staff_detail)
            self.assertEqual(response.status_code, 200)
            if permitted:
                self.assertContains(response, self.url())
            else:
                self.assertNotContains(response, self.url())
        self.login(self.patient_user)
        self.assertContains(self.client.get(reverse('portal:patient-appointments')), self.url())
        self.assertContains(self.client.get(reverse('portal:patient-appointment-detail', args=[self.booking.pk])), self.url())

    def test_staff_schedule_links_do_not_give_admins_room_access(self):
        date = timezone.localdate(self.booking.starts_at).isoformat()
        url = reverse('portal:staff-schedule') + f'?date={date}'
        self.login()
        self.assertContains(self.client.get(url), self.url())
        self.login(self.admin)
        self.assertNotContains(self.client.get(url), self.url())

    def test_in_progress_booking_remains_on_upcoming_and_overview(self):
        Appointment.objects.filter(pk=self.booking.pk).update(starts_at=self.now - timedelta(minutes=2))
        self.login(self.patient_user)
        for name in ('portal:patient-appointments', 'portal:patient-dashboard'):
            response = self.client.get(reverse(name))
            self.assertContains(response, self.url())

    def test_future_booking_explains_join_window_without_fake_or_external_link(self):
        Appointment.objects.filter(pk=self.booking.pk).update(starts_at=self.now + timedelta(days=1),
            video_link='https://example.test/old-external-call')
        self.login(self.patient_user)
        response = self.client.get(reverse('portal:patient-appointments'))
        self.assertNotContains(response, self.url())
        self.assertNotContains(response, 'https://example.test/old-external-call')
        self.assertContains(response, 'Join window:')

    def test_new_booking_form_does_not_ask_for_external_meeting_url(self):
        self.login()
        response = self.client.get(reverse('portal:appointment-book'))
        self.assertEqual(response.context['form'].fields['video_link'].widget.input_type, 'hidden')
        self.assertContains(response, 'No external meeting link is needed')

    def test_history_is_paginated_practice_scoped_and_not_attendance(self):
        start = self.now - timedelta(days=1)
        rows = [CallSession(company=self.company, appointment=self.booking, patient=self.patient,
            doctor=self.doctor, started_at=start + timedelta(minutes=index),
            last_seen_at=start + timedelta(minutes=index, seconds=10),
            lease_expires_at=start + timedelta(minutes=index, seconds=60),
            ended_at=start + timedelta(minutes=index, seconds=20)) for index in range(23)]
        CallSession.objects.bulk_create(rows)
        # Deliberately malformed legacy metadata must never cross the page's
        # company/patient scope even when its appointment FK points here.
        CallSession.objects.create(company=self.beta, appointment=self.booking, patient=self.beta_patient,
            doctor=self.doctor, started_at=start, last_seen_at=start, lease_expires_at=start + timedelta(minutes=1))
        self.login(self.patient_user)
        url = reverse('portal:patient-appointment-detail', args=[self.booking.pk])
        before = AuditEvent.objects.count()
        response = self.client.get(url)
        self.assertEqual(response.context['call_history_page'].paginator.count, 23)
        self.assertEqual(len(response.context['call_sessions']), 20)
        self.assertContains(response, 'not recordings or proof of consultation attendance')
        self.assertEqual(len(self.client.get(url + '?call_page=2').context['call_sessions']), 3)
        self.assertEqual(AuditEvent.objects.count(), before)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'booked')
