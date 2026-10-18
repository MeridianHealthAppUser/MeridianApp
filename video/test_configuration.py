import base64
import hashlib
import hmac
import importlib.util
import os
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
            scope = {'type': 'websocket', 'headers': headers}
            if scheme is not None:
                scope['scheme'] = scheme
            await SameOriginWebSocketMiddleware(application)(scope, None, send)
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
        self.assertEqual(self.invoke(headers+[(b'x-forwarded-proto',b'https')], scheme=None), 'websocket.accept')
        self.assertEqual(self.invoke(headers,scheme='wss'), 'websocket.accept')

    def test_debug_configuration_keeps_proxy_scheme_for_real_daphne_scope(self):
        # Load the actual settings, not an override that would conceal this
        # deployment regression: DEBUG used to remove the trusted proxy setting.
        spec = importlib.util.find_spec('config.settings')
        configured = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'DJANGO_DEBUG': 'true'}):
            spec.loader.exec_module(configured)
        self.assertEqual(configured.SECURE_PROXY_SSL_HEADER, ('HTTP_X_FORWARDED_PROTO', 'https'))
        headers = [(b'host', b'testserver'), (b'origin', b'https://testserver'), (b'x-forwarded-proto', b'https')]
        with override_settings(SECURE_PROXY_SSL_HEADER=configured.SECURE_PROXY_SSL_HEADER):
            # Daphne 4.2.3 omits scheme from WebSocket ASGI scopes even when
            # --proxy-headers is enabled. Trust the proxy, never the Origin.
            self.assertEqual(self.invoke(headers, scheme=None), 'websocket.accept')
            self.assertEqual(self.invoke(headers[:2], scheme=None), 'websocket.close')
            self.assertEqual(self.invoke([headers[0], (b'origin', b'https://other.test'), headers[2]], scheme=None), 'websocket.close')

    def test_rejection_log_contains_reason_without_request_secrets(self):
        headers = [(b'host', b'testserver'), (b'origin', b'https://testserver'),
                   (b'cookie', b'sessionid=PRIVATE_SESSION_SENTINEL')]
        with self.assertLogs('video.security', level='WARNING') as captured:
            self.assertEqual(self.invoke(headers, scheme=None), 'websocket.close')
        logged = '\n'.join(captured.output)
        self.assertIn('scheme_mismatch', logged)
        self.assertNotIn('PRIVATE_SESSION_SENTINEL', logged)
        self.assertNotIn('testserver', logged)

    @override_settings(VIDEO_ENABLED=True, DEBUG=False, VIDEO_REDIS_URL='')
    def test_notifications_disabled_for_unconfigured_production(self):
        request = SimpleNamespace(user=SimpleNamespace(is_authenticated=True))
        self.assertFalse(call_notifications(request)['video_notifications_enabled'])
