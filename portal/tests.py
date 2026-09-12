from django.test import TestCase


class LandingPageTests(TestCase):
    def test_landing_page_is_public_and_has_no_revision_notice(self):
        response = self.client.get('/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Your path to lasting weight loss')
        self.assertNotContains(response, 'Revision 2.0')

# Create your tests here.
