"""Standalone room safety and accessible preflight markup."""
import os
import shutil
import subprocess
import sys
from unittest import skipUnless

from django.conf import settings
from django.template.loader import render_to_string
from django.test import SimpleTestCase


class VideoRoomPresentationTests(SimpleTestCase):
    def render_room(self, **overrides):
        config = {
            'appointmentId': 17, 'myUserId': 42, 'peerName': 'Example Patient',
            'companyName': 'Example Practice', 'signalPath': '/ws/video/appointments/17/',
            'iceConfigUrl': '/video/appointments/17/ice/', 'returnUrl': '/schedule/',
            'startsAt': '2026-09-12T12:00:00+02:00', 'endsAt': '2026-09-12T12:30:00+02:00',
            **overrides,
        }
        return render_to_string('video/room.html', {'video_config': config, 'csrf_token': 'a' * 64})

    def test_room_is_standalone_and_starts_with_explicit_join_choices(self):
        html = self.render_room()
        self.assertIn('data-state="preflight"', html)
        self.assertIn('Join with camera', html)
        self.assertIn('Join with audio only', html)
        self.assertIn('id="video-csrf"', html)
        self.assertNotIn('workspace_polish.css', html)
        self.assertNotIn('site-header', html)
        self.assertNotIn('turn:', html)

    def test_config_and_visible_names_are_html_safe(self):
        html = self.render_room(peerName='</script><script>alert("x")</script>')
        self.assertIn('id="video-room-config" type="application/json"', html)
        self.assertNotIn('</script><script>alert', html)
        self.assertIn('\\u003C/script\\u003E', html)

    def test_media_controls_and_preview_have_keyboard_accessible_names(self):
        html = self.render_room()
        for label in ('Mute microphone', 'Turn camera off', 'Share your screen', 'Leave call', 'Move your preview'):
            self.assertIn(f'aria-label="{label}"', html)
        self.assertIn('aria-describedby="preview-move-help"', html)
        self.assertIn('use arrow keys to move', html)
        self.assertIn('role="timer"', html)
        self.assertIn('<noscript>', html)


class VideoRoomBrowserTests(SimpleTestCase):
    @skipUnless(os.getenv('MERIDIAN_PLAYWRIGHT_PATH'), 'Optional Chrome video-room test is not configured')
    def test_room_browser_states_and_native_two_peer_media(self):
        node = os.getenv('MERIDIAN_NODE') or shutil.which('node')
        self.assertIsNotNone(node, 'Install Node or set MERIDIAN_NODE for the optional browser test.')
        result = subprocess.run(
            [node, str(settings.BASE_DIR / 'scripts' / 'test_video_room.cjs')],
            cwd=settings.BASE_DIR,
            env={**os.environ, 'PYTHON': sys.executable},
            text=True,
            capture_output=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('native two-browser WebRTC', result.stdout)
