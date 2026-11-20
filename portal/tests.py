"""The app has no home page of its own: the practice's website is the front door (settings.PUBLIC_WEBSITE_URL)."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse


WEBSITE = 'https://www.meridianhealth.co.za/'


@override_settings(MULTI_PRACTICE_ENABLED=False, PUBLIC_WEBSITE_URL=WEBSITE)
class PublicFrontDoorTests(TestCase):
    def test_the_apps_own_address_opens_sign_in(self):
        self.assertRedirects(self.client.get('/'), reverse('accounts:login'))

    def test_signed_in_people_go_on_from_the_apps_address_to_their_dashboard(self):
        user = get_user_model().objects.create_user(email='front@door.test', password='Front-Door-2026!')
        self.client.force_login(user)
        response = self.client.get('/', follow=True)
        self.assertEqual(response.redirect_chain[0], (reverse('accounts:login'), 302))
        self.assertNotEqual(response.request['PATH_INFO'], reverse('accounts:login'))

    def test_back_arrows_and_logos_go_to_the_website(self):
        back = f'href="{WEBSITE}" aria-label="Back to the Meridian Health website"'
        login = self.client.get(reverse('accounts:login'))
        self.assertContains(login, back)
        self.assertContains(login, f'<a class="login-brand" href="{WEBSITE}"')
        screening = self.client.get(reverse('portal:questionnaire'))
        self.assertContains(screening, back)
        self.assertContains(screening, f'<a class="brand" href="{WEBSITE}">')
        self.assertContains(self.client.get(reverse('portal:public-privacy')), f'<a class="privacy-link" href="{WEBSITE}">')

    def test_signing_out_opens_sign_in(self):
        user = get_user_model().objects.create_user(email='out@door.test', password='Front-Door-2026!')
        self.client.force_login(user)
        response = self.client.post(reverse('accounts:logout'))
        self.assertRedirects(response, reverse('accounts:login'))
