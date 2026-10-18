import json
import os
import subprocess
import sys
from unittest.mock import patch

from django.db import DatabaseError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse


class ASGIDatabaseConfigurationTests(SimpleTestCase):
    def test_settings_disable_persistent_connections_and_keep_health_checks_and_tls(self):
        # Import real deployment settings in a clean interpreter. No database is
        # contacted: these synthetic URLs only exercise the configuration parser.
        code = (
            'import json; from config.settings import DATABASES; db = DATABASES["default"]; '
            'print(json.dumps({"max_age": db["CONN_MAX_AGE"], '
            '"health_checks": db["CONN_HEALTH_CHECKS"], "sslmode": db["OPTIONS"]["sslmode"]}))'
        )
        for query in ('sslmode=require', 'sslmode=require&conn_max_age=600'):
            with self.subTest(query=query):
                environment = {**os.environ, 'DJANGO_DEBUG': 'true',
                               'DATABASE_URL': f'postgresql://example:example@localhost/example?{query}'}
                result = subprocess.run([sys.executable, '-c', code], env=environment,
                                        capture_output=True, text=True, check=True, timeout=10)
                self.assertEqual(json.loads(result.stdout), {'max_age': 0, 'health_checks': True, 'sslmode': 'require'})


@override_settings(MULTI_PRACTICE_ENABLED=True)
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
