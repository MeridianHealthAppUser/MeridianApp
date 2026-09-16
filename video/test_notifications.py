import os
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings


@override_settings(DEBUG=True, VIDEO_ENABLED=True, VIDEO_REDIS_URL='')
@override_settings(MULTI_PRACTICE_ENABLED=True)
class NotificationTemplateTests(TestCase):
    def test_anonymous_page_has_no_call_socket_or_banner(self):
        response = self.client.get('/privacy/')
        self.assertNotContains(response,'call_notifications.js')

    def test_logged_in_page_has_one_notification_banner(self):
        user = get_user_model().objects.create_user(email='notification@example.test')
        self.client.force_login(user)
        response = self.client.get('/privacy/')
        self.assertContains(response,'id="incoming-call"',count=1)
        self.assertContains(response,'call_notifications.js',count=1)

    @override_settings(VIDEO_ENABLED=False)
    def test_disabled_feature_does_not_open_notification_socket(self):
        user = get_user_model().objects.create_user(email='disabled-notification@example.test')
        self.client.force_login(user)
        self.assertNotContains(self.client.get('/privacy/'),'call_notifications.js')

    @skipUnless(os.getenv('MERIDIAN_PLAYWRIGHT_PATH'), 'Optional Chrome notification test')
    def test_notification_browser_safety(self):
        node = os.getenv('MERIDIAN_NODE') or shutil.which('node')
        self.assertIsNotNone(node, 'Install Node or set MERIDIAN_NODE for the optional browser test.')
        result = subprocess.run([node,str(Path(settings.BASE_DIR)/'scripts/test_call_notifications.cjs')],
            cwd=settings.BASE_DIR,env=os.environ.copy(),text=True,capture_output=True,timeout=45)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
