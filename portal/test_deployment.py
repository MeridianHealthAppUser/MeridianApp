import os
import subprocess
import sys
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse


class HealthCheckTests(TestCase):
    def test_health_is_minimal_private_and_read_only(self):
        response = self.client.get(reverse('health'))
        self.assertEqual(response.json(), {'status': 'ok'})
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(self.client.head(reverse('health')).content, b'')
        self.assertEqual(self.client.post(reverse('health')).status_code, 405)

    def test_database_errors_do_not_expose_configuration(self):
        with patch('config.health.connection.cursor', side_effect=DatabaseError('private database connection detail')):
            response = self.client.get(reverse('health'))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'unavailable'})

    @override_settings(SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=[r'^health/$'])
    def test_internal_health_probe_does_not_disable_https_for_application_pages(self):
        self.assertEqual(self.client.get(reverse('health')).status_code, 200)
        response = self.client.get('/accounts/login/')
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response['Location'], 'https://testserver/accounts/login/')

    def test_production_rejects_an_empty_secret(self):
        environment = {**os.environ, 'DJANGO_DEBUG': 'false', 'DJANGO_SECRET_KEY': '   '}
        result = subprocess.run(
            [sys.executable, '-c', 'import config.settings'],
            env=environment, capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('DJANGO_SECRET_KEY must be set', result.stderr)
