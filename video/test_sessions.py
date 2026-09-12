import copy
import uuid
from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, override_settings
from django.utils import timezone

from care.models import Appointment, AuditEvent, ClinicalNote, Payment
from care.test_operations import OperationsFixture
from .access import resolve_room_access
from .models import CallParticipant, CallSession
from .presence import LEASE_SECONDS, MemoryPresence
from .services import record_presence


@override_settings(VIDEO_ENABLED=True, VIDEO_JOIN_EARLY_MINUTES=5, VIDEO_JOIN_GRACE_MINUTES=0)
class ConnectionHistoryTests(OperationsFixture):
    def setUp(self):
        self.now = timezone.now()
        self.appointment = Appointment.objects.create(company=self.company, patient=self.patient, clinician=self.doctor,
            starts_at=self.now - timedelta(minutes=1), duration_minutes=30, status='booked')
        self.access = resolve_room_access(self.doctor.pk, self.appointment.pk)
        self.store = MemoryPresence()

    def apply(self, action, user, channel, seconds=0):
        return async_to_sync(self.store.apply)(action, self.appointment.pk, user.pk, channel, uuid.uuid4(), now=self.now.timestamp() + seconds)

    def record(self, result):
        return record_presence(self.access, result['state'], result['removed'])

    def test_signal_history_records_connections_without_marking_attendance_or_creating_clinical_records(self):
        first = self.apply('join', self.doctor, 'doctor')
        session = self.record(first)
        second = self.apply('join', self.patient_user, 'patient', 1)
        self.assertEqual(self.record(second).pk, session.pk)
        self.record(self.apply('leave', self.patient_user, 'patient', 20))
        self.record(self.apply('leave', self.doctor, 'doctor', 22))
        session.refresh_from_db()
        self.assertEqual(session.signalling_duration_seconds, 22)
        self.assertFalse(session.is_live)
        self.assertEqual(session.participants.count(), 2)
        self.assertFalse(session.participants.filter(ended_at__isnull=True).exists())
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, 'booked')
        self.assertFalse(ClinicalNote.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(AuditEvent.objects.filter(action='video.signalling_joined').count(), 2)
        self.assertEqual(AuditEvent.objects.filter(action='video.signalling_left').count(), 2)

    def test_replaced_connection_is_closed_and_delayed_old_revision_cannot_evict_new_connection(self):
        first = self.apply('join', self.doctor, 'old')
        session = self.record(first)
        replacement = self.apply('join', self.doctor, 'new', 2)
        self.record(replacement)
        self.record(first)
        session.refresh_from_db()
        self.assertEqual(session.presence_revision, replacement['state']['revision'])
        self.assertEqual(session.participants.filter(ended_at__isnull=True).count(), 1)
        self.assertEqual(session.participants.exclude(ended_at__isnull=True).get().end_reason, 'replaced')
        self.assertEqual(AuditEvent.objects.filter(action='video.signalling_joined').count(), 2)

    def test_crashed_workers_expire_displayed_liveness_and_duration_without_a_background_writer(self):
        session = self.record(self.apply('join', self.doctor, 'crashed'))
        with patch('video.models.timezone.now', return_value=self.now + timedelta(seconds=LEASE_SECONDS + 100)):
            self.assertFalse(session.is_live)
            self.assertEqual(session.effective_ended_at, session.lease_expires_at)
            self.assertEqual(session.signalling_duration_seconds, LEASE_SECONDS)
            self.assertEqual(session.participants.get().effective_ended_at, session.lease_expires_at)
        pruned = self.apply('inspect', self.doctor, 'crashed', LEASE_SECONDS + 100)
        self.record(pruned)
        session.refresh_from_db()
        self.assertEqual(session.signalling_duration_seconds, LEASE_SECONDS)

    def test_malformed_presence_cannot_create_foreign_participant_and_transaction_rolls_back(self):
        state = copy.deepcopy(self.apply('join', self.doctor, 'doctor')['state'])
        member = state['users'].pop(str(self.doctor.pk))
        state['users'][str(self.beta_admin.pk)] = member
        with self.assertRaises(ValueError):
            record_presence(self.access, state)
        self.assertFalse(CallSession.objects.exists())
        self.assertFalse(CallParticipant.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_call_records_are_admin_read_only_with_participant_history_inline(self):
        session = self.record(self.apply('join', self.doctor, 'doctor'))
        request = RequestFactory().get('/admin/')
        request.user = get_user_model().objects.create_superuser('video-history-admin@example.test', 'Strong!Password12')
        for record in (session, session.participants.get()):
            model_admin = admin.site._registry[type(record)]
            self.assertTrue(model_admin.has_view_permission(request, record))
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_change_permission(request, record))
            self.assertFalse(model_admin.has_delete_permission(request, record))
            self.assertEqual(model_admin.get_actions(request), {})
            with self.assertRaises(PermissionDenied):
                model_admin.save_related(request, None, [], False)
        self.assertEqual(admin.site._registry[CallSession].inlines[0].model, CallParticipant)
