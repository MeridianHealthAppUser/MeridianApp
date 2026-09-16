import asyncio
from datetime import timedelta
from functools import wraps
from unittest.mock import AsyncMock, patch

from channels.auth import AuthMiddlewareStack
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone
from redis.exceptions import ConnectionError as RedisConnectionError

from care.models import Appointment, AuditEvent
from practices.models import Company, CompanyMembership, Patient
from .models import CallParticipant, CallSession
from .presence import MemoryPresence
from .routing import websocket_urlpatterns


def sockets(test):
    @wraps(test)
    async def wrapper(self):
        self.sockets = []
        try:
            await test(self)
        finally:
            for socket in reversed(self.sockets):
                await socket.disconnect()
    return wrapper


@override_settings(VIDEO_ENABLED=True, VIDEO_REDIS_URL='', REDIS_URL='',
                   VIDEO_HEARTBEAT_SECONDS=15, VIDEO_JOIN_EARLY_MINUTES=5, VIDEO_JOIN_GRACE_MINUTES=0,
                   CHANNEL_LAYERS={'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
@override_settings(MULTI_PRACTICE_ENABLED=True)
class VideoSocketTests(TransactionTestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Video Alpha', slug='video-alpha')
        self.beta = Company.objects.create(name='Video Beta', slug='video-beta')
        users = get_user_model().objects
        self.doctor = users.create_user('video-doctor@example.test')
        self.patient_user = users.create_user('video-patient@example.test')
        self.admin = users.create_user('video-admin@example.test')
        CompanyMembership.objects.create(company=self.company, user=self.doctor, role='doctor')
        CompanyMembership.objects.create(company=self.company, user=self.admin, role='super_admin')
        self.patient = Patient.objects.create(company=self.company, user=self.patient_user, first_name='Video', last_name='Patient')
        self.appointment = Appointment.objects.create(company=self.company, patient=self.patient, clinician=self.doctor,
            starts_at=timezone.now() - timedelta(minutes=1), duration_minutes=30, status='booked')
        self.cookies = {}
        for user in (self.doctor, self.patient_user, self.admin):
            client = Client()
            client.force_login(user)
            self.cookies[user.pk] = client.cookies[settings.SESSION_COOKIE_NAME].value
        self.store = MemoryPresence()
        self.patch = patch('video.consumers.presence_backend', return_value=self.store)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.application = AuthMiddlewareStack(URLRouter(websocket_urlpatterns))

    async def connect(self, user=None, *, appointment=None, notifications=False):
        path = '/ws/notifications/' if notifications else f'/ws/video/appointments/{(appointment or self.appointment).pk}/'
        headers = [(b'cookie', f'{settings.SESSION_COOKIE_NAME}={self.cookies[user.pk]}'.encode())] if user else []
        socket = WebsocketCommunicator(self.application, path, headers=headers)
        self.sockets.append(socket)
        accepted, code = await socket.connect()
        return socket, accepted, code

    async def pair(self):
        doctor, accepted, _ = await self.connect(self.doctor)
        self.assertTrue(accepted)
        first = await doctor.receive_json_from()
        patient, accepted, _ = await self.connect(self.patient_user)
        self.assertTrue(accepted)
        second = await patient.receive_json_from()
        joined = await doctor.receive_json_from()
        self.assertEqual(first['peer_count'], 1)
        self.assertFalse(first['should_initiate'])
        self.assertEqual(second['peer_count'], 2)
        self.assertTrue(second['should_initiate'])
        self.assertEqual(joined['type'], 'peer_joined')
        self.assertEqual(joined['room_epoch'], second['room_epoch'])
        return doctor, patient, second['room_epoch']

    @sockets
    async def test_single_practice_mode_rejects_previously_authorized_room(self):
        with override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='video-beta'):
            for actor in (self.doctor, self.patient_user):
                _, accepted, code = await self.connect(actor)
                self.assertEqual((accepted, code), (False, 4003))
        self.assertEqual(await database_sync_to_async(CallSession.objects.count)(), 0)

    @sockets
    async def test_single_practice_boundary_is_rechecked_on_existing_connection(self):
        doctor, patient, epoch = await self.pair()
        with override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='video-beta'):
            await patient.send_json_to({'type': 'offer', 'payload': {'type': 'offer', 'sdp': 'v=0\r\nblocked'},
                                        'room_epoch': epoch})
            self.assertEqual(await patient.receive_output(), {'type': 'websocket.close', 'code': 4003})
            # The peer is also revoked; no signalling content is forwarded.
            self.assertEqual(await doctor.receive_output(), {'type': 'websocket.close', 'code': 4003})

    @sockets
    async def test_anonymous_admin_and_wrong_practice_booking_are_rejected(self):
        _, accepted, code = await self.connect()
        self.assertEqual((accepted, code), (False, 4001))
        _, accepted, code = await self.connect(self.admin)
        self.assertEqual((accepted, code), (False, 4003))
        await database_sync_to_async(Appointment.objects.filter(pk=self.appointment.pk).update)(company=self.beta)
        _, accepted, code = await self.connect(self.doctor)
        self.assertEqual((accepted, code), (False, 4003))
        self.assertEqual(await database_sync_to_async(CallSession.objects.count)(), 0)

    @sockets
    async def test_offer_answer_ice_are_relayed_without_echo_or_persisting_payloads(self):
        doctor, patient, epoch = await self.pair()
        payload = {'type': 'offer', 'sdp': 'v=0\r\nSDP_PRIVATE_SENTINEL'}
        await patient.send_json_to({'type': 'offer', 'payload': payload, 'room_epoch': epoch, 'from_user_id': self.admin.pk})
        offered = await doctor.receive_json_from()
        self.assertEqual((offered['type'], offered['payload'], offered['from_user_id']), ('offer', payload, self.patient_user.pk))
        self.assertTrue(await patient.receive_nothing(timeout=.05))
        await doctor.send_json_to({'type': 'answer', 'payload': {'type': 'answer', 'sdp': 'v=0\r\nanswer'}, 'room_epoch': epoch})
        self.assertEqual((await patient.receive_json_from())['type'], 'answer')
        await patient.send_json_to({'type': 'ice-candidate', 'payload': {'candidate': 'candidate:PRIVATE_ICE', 'sdpMid': '0', 'sdpMLineIndex': 0}, 'room_epoch': epoch})
        self.assertEqual((await doctor.receive_json_from())['type'], 'ice-candidate')
        metadata = await database_sync_to_async(list)(AuditEvent.objects.values_list('metadata', flat=True))
        self.assertNotIn('PRIVATE', str(metadata))
        fields = {field.name for field in CallSession._meta.concrete_fields} | {field.name for field in CallParticipant._meta.concrete_fields}
        self.assertFalse({'sdp', 'ice', 'payload', 'recording', 'attendance'} & fields)

    @sockets
    async def test_first_peer_cannot_offer_and_old_epoch_cannot_relay(self):
        doctor, patient, epoch = await self.pair()
        signal = {'type': 'offer', 'payload': {'type': 'offer', 'sdp': 'v=0\r\nwrong-initiator'}, 'room_epoch': epoch}
        await doctor.send_json_to(signal)
        self.assertTrue(await patient.receive_nothing(timeout=.05))
        await patient.send_json_to({**signal, 'room_epoch': 'old-generation'})
        self.assertEqual((await patient.receive_json_from())['type'], 'room_status')
        self.assertTrue(await doctor.receive_nothing(timeout=.05))

    @sockets
    async def test_duplicate_tab_closes_old_socket_but_old_disconnect_does_not_evict_replacement(self):
        doctor, old, epoch = await self.pair()
        new, accepted, _ = await self.connect(self.patient_user)
        self.assertTrue(accepted)
        status = await new.receive_json_from()
        self.assertTrue(status['should_initiate'])
        self.assertNotEqual(status['room_epoch'], epoch)
        self.assertEqual((await old.receive_output())['code'], 4005)
        self.assertEqual((await doctor.receive_json_from())['type'], 'peer_joined')
        await old.disconnect()
        self.sockets.remove(old)
        await new.send_json_to({'type': 'offer', 'payload': {'type': 'offer', 'sdp': 'v=0\r\nreplacement'}, 'room_epoch': status['room_epoch']})
        self.assertEqual((await doctor.receive_json_from())['type'], 'offer')
        self.assertEqual(len((await self.store.apply('inspect', self.appointment.pk))['state']['users']), 2)

    @sockets
    async def test_logout_and_password_changes_revoke_already_connected_socket_on_next_send(self):
        doctor, accepted, _ = await self.connect(self.doctor)
        self.assertTrue(accepted)
        await doctor.receive_json_from()
        await database_sync_to_async(Session.objects.filter(session_key=self.cookies[self.doctor.pk]).delete)()
        await doctor.send_json_to({'type': 'ping'})
        self.assertEqual((await doctor.receive_output())['code'], 4001)
        patient, accepted, _ = await self.connect(self.patient_user)
        self.assertTrue(accepted)
        await patient.receive_json_from()
        def change_password():
            self.patient_user.set_password('Changed!Password12')
            self.patient_user.save(update_fields=['password'])
        await database_sync_to_async(change_password)()
        await patient.send_json_to({'type': 'ping'})
        self.assertEqual((await patient.receive_output())['code'], 4001)

    @sockets
    async def test_booking_cancellation_and_membership_revocation_stop_signalling(self):
        doctor, patient, epoch = await self.pair()
        await database_sync_to_async(Appointment.objects.filter(pk=self.appointment.pk).update)(status='cancelled')
        await patient.send_json_to({'type': 'ping'})
        self.assertEqual((await patient.receive_output())['code'], 4003)
        self.assertEqual((await doctor.receive_output())['code'], 4003)

    @sockets
    async def test_binary_and_oversized_frames_close_safely(self):
        socket, accepted, _ = await self.connect(self.doctor)
        self.assertTrue(accepted)
        await socket.receive_json_from()
        await socket.send_to(bytes_data=b'not-media')
        self.assertEqual((await socket.receive_output())['code'], 4008)
        socket, accepted, _ = await self.connect(self.patient_user)
        self.assertTrue(accepted)
        await socket.receive_json_from()
        await socket.send_to(text_data='x' * (72 * 1024 + 1))
        self.assertEqual((await socket.receive_output())['code'], 4008)

    @sockets
    async def test_invalid_payload_shapes_and_rate_flood_close_without_relay(self):
        doctor, patient, epoch = await self.pair()
        await patient.send_json_to({'type': 'offer', 'payload': {'type': 'answer', 'sdp': 'v=0'}, 'room_epoch': epoch})
        self.assertEqual((await patient.receive_output())['code'], 4008)
        self.assertEqual((await doctor.receive_json_from())['type'], 'peer_left')
        for _ in range(161):
            await doctor.send_json_to({'type': 'unsupported'})
        self.assertEqual((await doctor.receive_output())['code'], 4008)

    @sockets
    async def test_server_event_cannot_forward_from_unowned_channel_even_in_current_epoch(self):
        doctor, patient, epoch = await self.pair()
        await get_channel_layer().group_send(f'video_appointment_{self.appointment.pk}', {
            'type': 'room.signal', 'signal_type': 'offer', 'payload': {'type': 'offer', 'sdp': 'v=0\r\nspoofed'},
            'from_user_id': self.patient_user.pk, 'sender_channel': 'not-the-current-channel', 'room_epoch': epoch,
        })
        self.assertTrue(await doctor.receive_nothing(timeout=.05))
        self.assertTrue(await patient.receive_nothing(timeout=.05))

    @sockets
    async def test_idle_heartbeat_rechecks_logout_without_waiting_for_a_client_send(self):
        with override_settings(VIDEO_HEARTBEAT_SECONDS=.03):
            doctor, accepted, _ = await self.connect(self.doctor)
            self.assertTrue(accepted)
            await doctor.receive_json_from()
            await database_sync_to_async(Session.objects.filter(session_key=self.cookies[self.doctor.pk]).delete)()
            self.assertEqual((await doctor.receive_output(timeout=1))['code'], 4001)

    @sockets
    async def test_channel_layer_outage_fails_closed_without_echoing_connection_details(self):
        layer = get_channel_layer()
        with patch.object(layer, 'group_add', new=AsyncMock(side_effect=RedisConnectionError('secret-redis-password'))), \
             patch.object(layer, 'group_discard', new=AsyncMock(side_effect=RedisConnectionError('secret-redis-password'))):
            _, accepted, code = await self.connect(self.doctor)
            self.assertEqual((accepted, code), (False, 4503))
            _, accepted, code = await self.connect(self.patient_user, notifications=True)
            self.assertEqual((accepted, code), (False, 4503))

    @sockets
    async def test_notifications_ring_only_authorized_peer_and_hide_when_caller_leaves(self):
        peer_notifications, accepted, _ = await self.connect(self.patient_user, notifications=True)
        self.assertTrue(accepted)
        admin_notifications, accepted, _ = await self.connect(self.admin, notifications=True)
        self.assertTrue(accepted)
        doctor, accepted, _ = await self.connect(self.doctor)
        self.assertTrue(accepted)
        await doctor.receive_json_from()
        event = await peer_notifications.receive_json_from()
        self.assertEqual((event['type'], event['appointment_id'], event['practice_name']), ('incoming_call', self.appointment.pk, self.company.name))
        self.assertTrue(await admin_notifications.receive_nothing(timeout=.05))
        await doctor.disconnect()
        self.sockets.remove(doctor)
        self.assertEqual((await peer_notifications.receive_json_from())['type'], 'call_cancelled')

    @sockets
    async def test_client_cannot_ring_someone_and_server_event_checks_booking_peer_and_presence(self):
        socket, accepted, _ = await self.connect(self.patient_user, notifications=True)
        self.assertTrue(accepted)
        await socket.send_json_to({'type': 'notify_call', 'appointment_id': self.appointment.pk, 'caller_id': self.admin.pk})
        await get_channel_layer().group_send(f'notify_{self.patient_user.pk}', {
            'type': 'notify.call', 'appointment_id': self.appointment.pk, 'caller_id': self.admin.pk, 'caller_channel': 'fake',
        })
        await get_channel_layer().group_send(f'notify_{self.patient_user.pk}', {
            'type': 'notify.call', 'appointment_id': self.appointment.pk, 'caller_id': self.doctor.pk, 'caller_channel': 'not-present',
        })
        self.assertTrue(await socket.receive_nothing(timeout=.1))

    @sockets
    async def test_feature_off_closes_both_socket_types_without_creating_session(self):
        with override_settings(VIDEO_ENABLED=False):
            for notification in (False, True):
                _, accepted, code = await self.connect(self.doctor, notifications=notification)
                self.assertEqual((accepted, code), (False, 4503))
        self.assertEqual(await database_sync_to_async(CallSession.objects.count)(), 0)
