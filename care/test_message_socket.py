"""The live message socket is private, revocable, and independent of video."""

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
from redis.exceptions import ConnectionError as RedisConnectionError

from video.security import SameOriginWebSocketMiddleware
from .realtime import user_group
from .routing import websocket_urlpatterns


@override_settings(DEBUG=True, VIDEO_ENABLED=False, VIDEO_REDIS_URL='',
                   ALLOWED_HOSTS=['testserver'],
                   CHANNEL_LAYERS={'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
class MessageSocketTests(TransactionTestCase):
    def setUp(self):
        self.users = [get_user_model().objects.create_user(email=f'live-{i}@example.test') for i in range(2)]
        self.cookies = {}
        for user in self.users:
            client = Client()
            client.force_login(user)
            self.cookies[user.pk] = client.cookies[settings.SESSION_COOKIE_NAME].value
        self.application = SameOriginWebSocketMiddleware(AuthMiddlewareStack(URLRouter(websocket_urlpatterns)))

    async def socket(self, user=None, *, origin='http://testserver', query=''):
        headers = [(b'host', b'testserver'), (b'origin', origin.encode())]
        if user is not None:
            headers.append((b'cookie', f'{settings.SESSION_COOKIE_NAME}={self.cookies[user.pk]}'.encode()))
        socket = WebsocketCommunicator(self.application, '/ws/messages/' + query, headers=headers)
        accepted, code = await socket.connect()
        if accepted:
            self.assertEqual(await socket.receive_json_from(), {'type': 'ready'})
        return socket, accepted, code

    async def test_anonymous_and_foreign_origin_cannot_connect(self):
        for user, origin, expected in [(None, 'http://testserver', 4001),
                                      (self.users[0], 'https://untrusted.example', 4403)]:
            socket, accepted, code = await self.socket(user, origin=origin)
            try:
                self.assertEqual((accepted, code), (False, expected))
            finally:
                await socket.disconnect()

    async def test_events_are_private_ignore_query_identity_and_never_forward_content(self):
        mine, accepted, _ = await self.socket(self.users[0], query=f'?user_id={self.users[1].pk}')
        other, other_accepted, _ = await self.socket(self.users[1])
        try:
            self.assertTrue(accepted and other_accepted)
            layer = get_channel_layer()
            await layer.group_send(user_group(self.users[0].pk), {
                'type': 'messages.changed', 'body': 'PRIVATE BODY', 'patient_id': 987,
            })
            self.assertEqual(await mine.receive_json_from(), {'type': 'messages.changed'})
            self.assertTrue(await other.receive_nothing(timeout=.05))
            await layer.group_send(user_group(self.users[1].pk), {'type': 'messages.changed'})
            self.assertEqual(await other.receive_json_from(), {'type': 'messages.changed'})
            self.assertTrue(await mine.receive_nothing(timeout=.05))
        finally:
            await mine.disconnect()
            await other.disconnect()

    async def test_logout_revokes_an_existing_socket_before_next_event(self):
        socket, accepted, _ = await self.socket(self.users[0])
        try:
            self.assertTrue(accepted)
            await database_sync_to_async(Session.objects.filter(session_key=self.cookies[self.users[0].pk]).delete)()
            await get_channel_layer().group_send(user_group(self.users[0].pk), {'type': 'messages.changed'})
            self.assertEqual(await socket.receive_output(), {'type': 'websocket.close', 'code': 4001})
        finally:
            await socket.disconnect()

    async def test_disabled_account_revokes_an_existing_socket(self):
        socket, accepted, _ = await self.socket(self.users[0])
        try:
            self.assertTrue(accepted)
            await database_sync_to_async(get_user_model().objects.filter(pk=self.users[0].pk).update)(is_active=False)
            await get_channel_layer().group_send(user_group(self.users[0].pk), {'type': 'messages.changed'})
            self.assertEqual(await socket.receive_output(), {'type': 'websocket.close', 'code': 4001})
        finally:
            await socket.disconnect()

    async def test_client_cannot_publish_or_subscribe(self):
        socket, accepted, _ = await self.socket(self.users[0])
        try:
            self.assertTrue(accepted)
            await socket.send_json_to({'type': 'subscribe', 'user_id': self.users[1].pk})
            self.assertEqual(await socket.receive_output(), {'type': 'websocket.close', 'code': 4008})
        finally:
            await socket.disconnect()

    @override_settings(DEBUG=False)
    async def test_production_requires_shared_channel_backend(self):
        # A trusted TLS origin reaches the production configuration check.
        headers = [(b'host', b'testserver'), (b'origin', b'https://testserver'),
                   (b'cookie', f'{settings.SESSION_COOKIE_NAME}={self.cookies[self.users[0].pk]}'.encode())]
        socket = WebsocketCommunicator(self.application, '/ws/messages/', headers=headers)
        socket.scope['scheme'] = 'wss'
        try:
            self.assertEqual(await socket.connect(), (False, 4503))
        finally:
            await socket.disconnect()

    async def test_backend_outage_does_not_accept_an_unsubscribed_socket(self):
        layer = get_channel_layer()
        with patch.object(layer, 'group_add', new=AsyncMock(side_effect=RedisConnectionError('unavailable'))):
            socket, accepted, code = await self.socket(self.users[0])
            try:
                self.assertEqual((accepted, code), (False, 4503))
            finally:
                await socket.disconnect()
