from django.test import override_settings
from django.test import TestCase


@override_settings(MULTI_PRACTICE_ENABLED=False)
class LandingPageTests(TestCase):
    def test_landing_page_is_public_and_has_no_revision_notice(self):
        response = self.client.get('/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<h1>Start the countdown to your <span class="teal-text">goal weight</span>.</h1>', html=True)
        self.assertNotContains(response, 'Revision 2.0')

# Create your tests here.
