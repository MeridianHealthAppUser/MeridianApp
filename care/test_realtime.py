"""Realtime transport never changes persistence, access boundaries, or read semantics."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings

from practices.models import Company, CompanyMembership, Patient
from .messaging import add_participant, mark_patient_messages_read, mark_read_by_staff, mark_unread_for_staff
from .models import MessageThread, MessageThreadParticipant, PatientMessage, TeamMessage
from .realtime import message_saved, patient_thread_changed, user_group
from .services import post_patient_message
from .team_messaging import (
    mark_team_thread_read, post_team_message, set_team_read_state,
    start_team_conversation, unread_team_messages,
)


@override_settings(MULTI_PRACTICE_ENABLED=True, EMAIL_ENABLED=False)
class RealtimeMessageDeliveryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Message practice', slug='message-practice')
        cls.other_company = Company.objects.create(name='Other practice', slug='other-message-practice')
        users = get_user_model().objects
        cls.patient_user = users.create_user(email='realtime-patient@example.test')
        cls.doctor = users.create_user(email='realtime-doctor@example.test')
        cls.colleague = users.create_user(email='realtime-colleague@example.test')
        cls.super_admin = users.create_user(email='realtime-super@example.test')
        cls.other_staff = users.create_user(email='realtime-other@example.test')
        for company, user, role in (
            (cls.company, cls.doctor, 'doctor'), (cls.company, cls.colleague, 'doctor'),
            (cls.company, cls.super_admin, 'super_admin'), (cls.other_company, cls.other_staff, 'super_admin'),
        ):
            CompanyMembership.objects.create(company=company, user=user, role=role)
        cls.patient = Patient.objects.create(
            company=cls.company, user=cls.patient_user, first_name='Pat', last_name='Example',
        )
        cls.thread = MessageThread.objects.create(company=cls.company, patient=cls.patient, subject='Private question')
        add_participant(cls.thread, cls.doctor)

    def setUp(self):
        self.channel_layer = type('ChannelLayer', (), {})()
        self.channel_layer.group_send = AsyncMock()
        self.layer_patch = patch('care.realtime.get_channel_layer', return_value=self.channel_layer)
        self.layer_patch.start()
        self.addCleanup(self.layer_patch.stop)

    def delivered_to(self):
        calls = self.channel_layer.group_send.await_args_list
        for call in calls:
            self.assertEqual(call.args[1], {'type': 'messages.changed'})
        return {call.args[0] for call in calls}

    def groups(self, *users):
        return {user_group(user.pk) for user in users}

    def team_thread(self):
        return start_team_conversation(
            company=self.company, actor=self.doctor, members=[self.colleague],
            subject='Private team question', body='Internal discussion', patient=self.patient,
        )

    def test_patient_send_delivers_only_after_commit_to_authorized_readers(self):
        # Even a stale/invalid link cannot invite a member of a different practice.
        MessageThreadParticipant.objects.create(company=self.company, thread=self.thread, user=self.other_staff)
        with self.captureOnCommitCallbacks(execute=True):
            message = post_patient_message(thread=self.thread, sender=self.patient_user, body='Confidential message')
            self.channel_layer.group_send.assert_not_awaited()
        self.assertTrue(PatientMessage.objects.filter(pk=message.pk).exists())
        self.assertEqual(self.delivered_to(), self.groups(self.patient_user, self.doctor, self.super_admin))

    def test_rollback_discards_the_notification_and_message(self):
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(RuntimeError), transaction.atomic():
                post_patient_message(thread=self.thread, sender=self.patient_user, body='Rolled back')
                raise RuntimeError('Rollback')
        self.channel_layer.group_send.assert_not_awaited()
        self.assertFalse(PatientMessage.objects.exists())

    def test_membership_is_resolved_at_commit_and_inactive_users_are_excluded(self):
        with self.captureOnCommitCallbacks(execute=True):
            post_patient_message(thread=self.thread, sender=self.patient_user, body='New message')
            add_participant(self.thread, self.colleague)
            CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(is_active=False)
            get_user_model().objects.filter(pk=self.super_admin.pk).update(is_active=False)
        self.assertEqual(self.delivered_to(), self.groups(self.patient_user, self.colleague))

    def test_team_send_never_notifies_patient_or_nonmember_admin(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.team_thread()
            self.channel_layer.group_send.assert_not_awaited()
        self.assertEqual(self.delivered_to(), self.groups(self.doctor, self.colleague))

    def test_failed_transport_does_not_fail_the_saved_message_or_expose_credentials(self):
        self.channel_layer.group_send.side_effect = ConnectionError('secret-password-and-private-host')
        with self.assertLogs('care.realtime', level='WARNING') as logs:
            with self.captureOnCommitCallbacks(execute=True):
                message = post_patient_message(thread=self.thread, sender=self.patient_user, body='Saved once')
        self.assertEqual(PatientMessage.objects.get(pk=message.pk).body, 'Saved once')
        self.assertEqual(self.channel_layer.group_send.await_count, 3)
        self.assertNotIn('secret-password', '\n'.join(logs.output))

    def test_layer_initialization_failure_is_also_nonfatal(self):
        with patch('care.realtime.get_channel_layer', side_effect=RuntimeError('private details')):
            with self.assertLogs('care.realtime', level='WARNING'):
                with self.captureOnCommitCallbacks(execute=True):
                    post_patient_message(thread=self.thread, sender=self.patient_user, body='Still saved')
        self.assertEqual(PatientMessage.objects.count(), 1)

    def test_stalled_broadcast_is_bounded_and_other_recipients_still_receive_update(self):
        cancelled, delivered = set(), set()

        async def slow_send(group, event):
            if group == user_group(self.doctor.pk):
                delivered.add(group)
                return
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.add(group)
                raise

        self.channel_layer.group_send.side_effect = slow_send
        started = time.monotonic()
        with patch('care.realtime.BROADCAST_TIMEOUT_SECONDS', .03):
            with self.assertLogs('care.realtime', level='WARNING') as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    message = post_patient_message(thread=self.thread, sender=self.patient_user, body='Saved before timeout')
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(PatientMessage.objects.filter(pk=message.pk).exists())
        self.assertEqual(delivered, self.groups(self.doctor))
        self.assertEqual(cancelled, self.groups(self.patient_user, self.super_admin))
        self.assertIn('TimeoutError', '\n'.join(logs.output))

    def test_patient_acknowledges_only_displayed_staff_messages_and_notifies_once(self):
        own = post_patient_message(thread=self.thread, sender=self.patient_user, body='Question')
        shown = post_patient_message(thread=self.thread, sender=self.doctor, body='Shown reply')
        later = post_patient_message(thread=self.thread, sender=self.doctor, body='Arrived while fetching')
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(mark_patient_messages_read(self.thread, self.patient_user, [shown.pk, own.pk]), 1)
        self.assertEqual(self.delivered_to(), self.groups(self.patient_user, self.doctor, self.super_admin))
        shown.refresh_from_db()
        later.refresh_from_db()
        own.refresh_from_db()
        self.assertIsNotNone(shown.read_at)
        self.assertIsNone(later.read_at)
        self.assertIsNone(own.read_at)
        self.channel_layer.group_send.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(mark_patient_messages_read(self.thread, self.patient_user, [shown.pk]), 0)
        self.channel_layer.group_send.assert_not_awaited()

    def test_staff_read_and_unread_changes_publish_without_a_refresh_loop(self):
        message = post_patient_message(thread=self.thread, sender=self.patient_user, body='Please read')
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(mark_read_by_staff(self.thread, self.doctor, [message.pk]), 1)
        self.assertEqual(self.delivered_to(), self.groups(self.patient_user, self.doctor, self.super_admin))
        self.channel_layer.group_send.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(mark_read_by_staff(self.thread, self.doctor, [message.pk]), 0)
        self.channel_layer.group_send.assert_not_awaited()
        with self.captureOnCommitCallbacks(execute=True):
            mark_unread_for_staff(self.thread, self.doctor)
        self.assertEqual(self.delivered_to(), self.groups(self.patient_user, self.doctor, self.super_admin))

    def test_team_acknowledgement_leaves_later_messages_unread_and_does_not_loop(self):
        thread = self.team_thread()
        shown = TeamMessage.objects.get(thread=thread)
        later = post_team_message(thread=thread, sender=self.doctor, body='Arrived after snapshot')
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(mark_team_thread_read(thread, self.colleague, [shown.pk]), 1)
        self.assertEqual(unread_team_messages(self.company, self.colleague), 1)
        self.assertEqual(self.delivered_to(), self.groups(self.doctor, self.colleague))
        self.channel_layer.group_send.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(mark_team_thread_read(thread, self.colleague, [shown.pk]), 0)
        self.channel_layer.group_send.assert_not_awaited()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(mark_team_thread_read(thread, self.colleague, [later.pk]), 1)
            self.assertEqual(set_team_read_state(thread, self.colleague, read=False), 1)
        self.assertEqual(unread_team_messages(self.company, self.colleague), 1)

    def test_thread_close_and_message_edit_publish_but_email_metadata_does_not(self):
        message = post_patient_message(thread=self.thread, sender=self.patient_user, body='Question')
        with self.captureOnCommitCallbacks(execute=True):
            self.thread.is_closed = True
            self.thread.save(update_fields=['is_closed'])
        self.assertEqual(self.delivered_to(), self.groups(self.patient_user, self.doctor, self.super_admin))
        self.channel_layer.group_send.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            message.body = 'Corrected question'
            message.save(update_fields=['body'])
        self.assertEqual(self.delivered_to(), self.groups(self.patient_user, self.doctor, self.super_admin))
        self.channel_layer.group_send.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            self.thread.patient_emailed_at = message.created_at
            self.thread.save(update_fields=['patient_emailed_at'])
        self.channel_layer.group_send.assert_not_awaited()

    def test_fixture_loading_skips_events_and_callback_uses_write_database(self):
        message = PatientMessage(thread=self.thread, company=self.company, sender=self.patient_user, body='Fixture')
        with self.captureOnCommitCallbacks(execute=True):
            message_saved(PatientMessage, message, raw=True, using='default')
        self.channel_layer.group_send.assert_not_awaited()
        with patch('care.realtime.transaction.on_commit') as committed:
            patient_thread_changed(self.thread.pk, using='other_database')
        self.assertEqual(committed.call_args.kwargs['using'], 'other_database')
