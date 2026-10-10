"""Forgot password: a one-hour, single-use emailed link that never reveals whether an account exists."""

import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import AuditEvent
from practices.models import Company, CompanyMembership

NEW_PASSWORD = 'Harbour-Lantern!7Moss'


@override_settings(MULTI_PRACTICE_ENABLED=True, PASSWORD_RESET_EMAIL_ENABLED=True,
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class PasswordResetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.company = Company.objects.create(name='Reset Practice', slug='reset-practice')
        cls.user = User.objects.create_user('Doctor@Reset.test', 'Old-Meadow!4Stones', first_name='Dana')
        CompanyMembership.objects.create(company=cls.company, user=cls.user, role='doctor')

    def setUp(self):
        cache.clear()

    def request_link(self, email='doctor@reset.test', client=None):
        return (client or self.client).post(reverse('accounts:password-reset'), {'email': email})

    def emailed_link(self):
        return re.search(r'https?://[^/\s]+(/\S+/password/reset/\S+/)', mail.outbox[-1].body).group(1)

    def test_sign_in_page_has_a_forgot_password_button_and_no_intro_panel(self):
        page = self.client.get(reverse('accounts:login'))
        self.assertContains(page, f'<a class="login-secondary" href="{reverse("accounts:password-reset")}">Forgot password</a>', html=True)
        self.assertContains(page, '<h1 id="login-title">Welcome back</h1>', html=True)
        self.assertContains(page, 'class="login-brand"')
        self.assertNotContains(page, 'Care that stays connected')
        self.assertNotContains(page, 'class="site-header')

    def test_known_email_gets_one_link_and_unknown_or_inactive_get_the_same_reply(self):
        response = self.request_link()
        self.assertRedirects(response, reverse('accounts:password-reset-done'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        self.assertEqual(mail.outbox[0].subject, 'Reset your Meridian Health password')
        self.assertIn('Hello Dana', mail.outbox[0].body)

        inactive = get_user_model().objects.create_user('gone@reset.test', 'Old-Meadow!4Stones', is_active=False)
        for email in ('nobody@reset.test', inactive.email):
            self.assertRedirects(self.request_link(email), reverse('accounts:password-reset-done'))
        self.assertEqual(len(mail.outbox), 1)

    def test_the_email_says_when_the_link_expires_shows_the_logo_and_points_to_support(self):
        sent_at = timezone.localtime()
        self.request_link()
        email = mail.outbox[0]
        html, mimetype = email.alternatives[0]
        self.assertEqual(mimetype, 'text/html')
        # "Link valid until 20:49 SAST, 10 October 2026.", from either side of a minute boundary.
        expiries = {f'Link valid until {moment:%H:%M %Z}, {moment.day} {moment:%B %Y}.'
                    for moment in (sent_at + timedelta(hours=1), timezone.localtime() + timedelta(hours=1))}
        for part in (email.body, html):
            self.assertTrue(any(line in part for line in expiries), part)
            self.assertIn('Please do not reply to this email. If you need help, please email', part)
        self.assertIn('please email support@meridianhealth.co.za.', email.body)
        self.assertIn(f'<a href="http://testserver{self.emailed_link()}"', html)
        self.assertIn('<a href="mailto:support@meridianhealth.co.za"', html)
        self.assertRegex(html, r'<img src="http://testserver/static/images/meridian-logo[^"]*\.png" alt="Meridian Health - Medical Weight Loss"')

    def test_staff_are_greeted_by_title_and_surname(self):
        get_user_model().objects.filter(pk=self.user.pk).update(title='DR', last_name='Czech')
        self.request_link()
        self.assertIn('Hello Dr Czech,', mail.outbox[0].body)
        self.assertIn('Hello Dr Czech,', mail.outbox[0].alternatives[0][0])

    def test_link_sets_a_new_password_once_and_is_audited(self):
        other_device = Client()
        other_device.force_login(self.user)
        self.request_link()
        link = self.emailed_link()
        form_url = self.client.get(link)['Location']
        page = self.client.get(form_url)
        self.assertContains(page, 'Choose a new password')
        self.assertIsNone(self.user.last_forgot_password_reset)
        response = self.client.post(form_url, {'new_password1': NEW_PASSWORD, 'new_password2': NEW_PASSWORD})
        self.assertRedirects(response, reverse('accounts:password-reset-complete'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertLess(timezone.now() - self.user.last_forgot_password_reset, timedelta(minutes=1))
        self.assertIsNone(self.user.last_change_password)
        self.assertFalse(self.user.check_password('Old-Meadow!4Stones'))
        # Other sessions end, and the same link can't be used again.
        self.assertEqual(other_device.get(reverse('accounts:profile')).status_code, 302)
        self.assertContains(Client().get(link), 'This link has expired')
        event = AuditEvent.objects.get(action='account.password_reset')
        self.assertEqual((event.company, event.actor, event.metadata), (self.company, self.user, {}))

    def test_changing_the_password_records_when_and_my_profile_shows_it(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('accounts:profile')), 'Not changed yet')
        response = self.client.post(reverse('accounts:password-change'), {
            'old_password': 'Old-Meadow!4Stones', 'new_password1': NEW_PASSWORD, 'new_password2': NEW_PASSWORD})
        self.assertRedirects(response, reverse('accounts:password-change-done'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertLess(timezone.now() - self.user.last_change_password, timedelta(minutes=1))
        self.assertIsNone(self.user.last_forgot_password_reset)
        profile = self.client.get(reverse('accounts:profile'))
        self.assertContains(profile, f'Last changed {timezone.localtime(self.user.last_change_password):%-d %b %Y, %H:%M}')
        self.assertContains(profile, 'Never reset with Forgot password')

    def test_a_tampered_link_shows_the_expired_page(self):
        self.request_link()
        link = self.emailed_link()
        page = self.client.get(link[:-3] + 'xx/')
        self.assertContains(page, 'This link has expired')
        self.assertContains(page, 'Request a new link')
        self.assertNotContains(page, 'name="new_password1"')

    def test_repeated_requests_for_one_email_stop_sending_but_reply_the_same(self):
        for _ in range(7):
            self.assertRedirects(self.request_link(), reverse('accounts:password-reset-done'))
        self.assertEqual(len(mail.outbox), 5)

    @override_settings(PASSWORD_RESET_EMAIL_ENABLED=False)
    def test_without_email_set_up_the_page_points_to_support_and_sends_nothing(self):
        page = self.client.get(reverse('accounts:password-reset'))
        self.assertContains(page, 'isn’t available yet')
        self.assertNotContains(page, 'name="email"')
        self.assertRedirects(self.request_link(), reverse('accounts:password-reset'))
        self.assertEqual(len(mail.outbox), 0)
