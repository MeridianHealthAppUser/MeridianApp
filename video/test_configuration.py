import base64
import hashlib
import hmac
from types import SimpleNamespace
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from .context_processors import call_notifications
from .ice import build_ice_config
from .security import SameOriginWebSocketMiddleware


@override_settings(VIDEO_STUN_URLS=['stun:stun.example.test:3478'], VIDEO_TURN_URLS=[],
                   VIDEO_TURN_SECRET='', VIDEO_TURN_CREDENTIAL_TTL=3600, VIDEO_ICE_TRANSPORT_POLICY='all')
class IceConfigurationTests(SimpleTestCase):
    def test_local_stun_only_is_explicitly_not_a_configured_relay(self):
        result = build_ice_config(None)
        self.assertFalse(result['relayConfigured'])
        self.assertEqual(result['iceServers'], [{'urls': ['stun:stun.example.test:3478']}])

    @override_settings(VIDEO_TURN_URLS=['turns:turn.example.test:5349?transport=tcp'], VIDEO_TURN_SECRET='test-only-shared-secret-0123456789abcdef')
    def test_expiring_credentials_use_random_identity_and_never_expose_shared_secret(self):
        now = timezone.now()
        with patch('video.ice.timezone.now', return_value=now):
            first = build_ice_config(None)
            second = build_ice_config(None)
        relay = first['iceServers'][-1]
        expected = base64.b64encode(hmac.new(b'test-only-shared-secret-0123456789abcdef', relay['username'].encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(relay['credential'], expected)
        self.assertEqual(int(relay['username'].split(':')[0]), int(now.timestamp()+3600))
        self.assertNotEqual(relay['username'], second['iceServers'][-1]['username'])
        self.assertNotIn('test-only-shared-secret', str(first))
        self.assertTrue(first['relayConfigured'])

    @override_settings(VIDEO_TURN_URLS=['turn:127.0.0.1:3478'], VIDEO_TURN_SECRET='test-only-shared-secret-0123456789abcdef', VIDEO_ICE_TRANSPORT_POLICY='relay')
    def test_relay_only_omits_stun_servers(self):
        result = build_ice_config(None)
        self.assertEqual(result['iceTransportPolicy'], 'relay')
        self.assertEqual(len(result['iceServers']), 1)

    def test_incomplete_or_unsafe_configuration_fails_closed(self):
        for settings in (
            {'VIDEO_TURN_URLS': ['turn:relay.example.test']},
            {'VIDEO_TURN_SECRET': 'private-but-no-turn-url'},
            {'VIDEO_ICE_TRANSPORT_POLICY': 'relay'},
            {'VIDEO_TURN_CREDENTIAL_TTL': 999999},
            {'VIDEO_ICE_TRANSPORT_POLICY': 'anything'},
            {'VIDEO_STUN_URLS': ['https://external.example.test']},
            {'VIDEO_STUN_URLS': ['stun:user:password@example.test']},
            {'VIDEO_STUN_URLS': ['stun:example.test:99999']},
            {'VIDEO_STUN_URLS': ['stun:example.test/path']},
        ):
            with self.subTest(settings=settings), override_settings(**settings), self.assertRaises(ImproperlyConfigured):
                build_ice_config(None)


@override_settings(DEBUG=True, ALLOWED_HOSTS=['localhost', 'testserver'], SECURE_PROXY_SSL_HEADER=None)
class WebSocketOriginTests(SimpleTestCase):
    def invoke(self, headers, scheme='ws'):
        async def run():
            events = []
            async def application(scope, receive, send):
                await send({'type': 'websocket.accept'})
            async def send(event):
                events.append(event)
            await SameOriginWebSocketMiddleware(application)({'type': 'websocket', 'scheme': scheme, 'headers': headers}, None, send)
            return events
        return async_to_sync(run)()[0]['type']

    def test_exact_local_origin_matches(self):
        self.assertEqual(self.invoke([(b'host', b'localhost:8000'), (b'origin', b'http://localhost:8000')]), 'websocket.accept')

    def test_cross_origin_and_malformed_headers_are_rejected_even_in_debug(self):
        for headers in (
            [(b'host',b'localhost:8000')],
            [(b'host',b'localhost:8000'),(b'origin',b'http://localhost:8001')],
            [(b'host',b'localhost:8000'),(b'origin',b'https://localhost:8000')],
            [(b'host',b'localhost:8000'),(b'origin',b'http://attacker.test')],
            [(b'host',b'attacker.test'),(b'origin',b'http://attacker.test')],
            [(b'host',b'localhost:8000'),(b'origin',b'null')],
            [(b'host',b'localhost:8000'),(b'origin',b'http://user@localhost:8000')],
            [(b'host',b'localhost:8000'),(b'origin',b'http://localhost:8000/path')],
            [(b'host',b'localhost:8000'),(b'origin',b'http://localhost:8000'),(b'origin',b'http://localhost:8000')],
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.invoke(headers), 'websocket.close')

    @override_settings(DEBUG=False, SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https'))
    def test_production_requires_https_and_trusted_proxy_origin(self):
        headers = [(b'host',b'testserver'),(b'origin',b'https://testserver')]
        self.assertEqual(self.invoke(headers), 'websocket.close')
        self.assertEqual(self.invoke(headers+[(b'x-forwarded-proto',b'https')]), 'websocket.accept')
        self.assertEqual(self.invoke(headers,scheme='wss'), 'websocket.accept')

    @override_settings(VIDEO_ENABLED=True, DEBUG=False, VIDEO_REDIS_URL='')
    def test_notifications_disabled_for_unconfigured_production(self):
        request = SimpleNamespace(user=SimpleNamespace(is_authenticated=True))
        self.assertFalse(call_notifications(request)['video_notifications_enabled'])
