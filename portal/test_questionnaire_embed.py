"""The questionnaire inside the practice's WordPress screening page (docs/WORDPRESS_EMBED.md)."""

from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import ConsentDocument, DoctorWorkingPattern, Lead, PracticeSettings
from practices.models import Company, CompanyMembership


PASSWORD = 'Embed-Test-2026!'
EMAIL = 'lindiwe@embed.test'
WORDPRESS = ['https://www.meridianhealth.co.za', 'https://meridianhealth.co.za']
# Browsers mark every load inside a frame like this.
IN_FRAME = {'HTTP_SEC_FETCH_DEST': 'iframe'}
# Our dropdown and calendar sheets would rise from the bottom of the tall frame, below the screen: the browser's own instead.
EMBED_HTML = '<html lang="en" class="questionnaire-embed" data-theme="light" data-native-select data-native-date>'


@override_settings(DEBUG=True, MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='meridian-health',
                   CHECKOUT_TEST_CODE='devtest', QUESTIONNAIRE_FRAME_ANCESTORS=WORDPRESS)
class QuestionnaireEmbedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Meridian Health', slug='meridian-health')
        PracticeSettings.objects.create(company=cls.company)
        for kind in (ConsentDocument.Kind.SERVICE, ConsentDocument.Kind.TELEHEALTH):
            ConsentDocument.objects.create(
                company=cls.company, kind=kind, version=f'{kind}-v1', title=f'{kind} notice',
                body=f'Current {kind} notice.', effective_from=timezone.localdate() - timedelta(days=1),
            )
        doctor = get_user_model().objects.create_user(email='doctor@embed.test', password=PASSWORD,
                                                      first_name='Sam', last_name='Doctor')
        CompanyMembership.objects.create(company=cls.company, user=doctor, role=CompanyMembership.Role.DOCTOR)
        for weekday in range(7):
            DoctorWorkingPattern.objects.create(company=cls.company, clinician=doctor, weekday=weekday,
                                                is_working=True, starts_at=time(9), ends_at=time(12))
        cls.day = timezone.localdate() + timedelta(days=1)
        cls.checkout_url = reverse('portal:questionnaire-checkout')

    def submit_enquiry(self, **headers):
        page = self.client.get(reverse('portal:questionnaire'), **headers)
        response = self.client.post(reverse('portal:questionnaire'), {
            'practice': self.company.pk, 'submission_token': page.context['form']['submission_token'].value(),
            'first_name': 'Lindiwe', 'last_name': 'Embed', 'email': EMAIL, 'phone': '082 000 0000',
            'id_number': '9001015009087', 'height_cm': '168', 'weight_kg': '94', 'adult': 'yes',
            'weight_related_condition': 'no', 'pregnancy': 'no', 'thyroid_history': 'no', 'pancreatitis': 'no',
            'health_context': 'None', 'service_consent': 'on',
        }, **headers)
        self.assertRedirects(response, reverse('portal:questionnaire-result'), fetch_redirect_response=False)
        return Lead.objects.get(email=EMAIL)

    def assertFrameableByWordPress(self, response):
        self.assertNotIn('X-Frame-Options', response.headers)
        self.assertEqual(response['Content-Security-Policy'],
                         "frame-ancestors 'self' https://www.meridianhealth.co.za https://meridianhealth.co.za")
        self.assertIn('Sec-Fetch-Dest', response['Vary'])

    def assertEmbedded(self, response):
        self.assertContains(response, EMBED_HTML)
        self.assertContains(response, '<meta name="color-scheme" content="light">')
        self.assertNotContains(response, 'class="site-header')
        self.assertNotContains(response, 'data-theme-switch')
        self.assertNotContains(response, 'js/theme.js')
        self.assertNotContains(response, "localStorage.getItem('meridian-theme')")
        self.assertContains(response, 'css/questionnaire_embed.css')
        self.assertContains(response, 'data-parent-origins="https://www.meridianhealth.co.za https://meridianhealth.co.za"')

    def test_only_the_questionnaire_pages_may_be_framed_and_only_by_wordpress(self):
        self.submit_enquiry()
        for name in ('questionnaire', 'questionnaire-result', 'questionnaire-checkout'):
            with self.subTest(name):
                response = self.client.get(reverse(f'portal:{name}'))
                self.assertEqual(response.status_code, 200)
                self.assertFrameableByWordPress(response)
        self.assertFrameableByWordPress(self.client.post(reverse('portal:questionnaire-call-me'), {'phone': ''}))
        for url in (reverse('accounts:login'), reverse('accounts:password-reset'), reverse('portal:patient-dashboard')):
            with self.subTest(url):
                response = self.client.get(url, **IN_FRAME)
                self.assertEqual(response['X-Frame-Options'], 'DENY')
                self.assertNotIn('Content-Security-Policy', response.headers)

    @override_settings(QUESTIONNAIRE_FRAME_ANCESTORS=[])
    def test_with_no_framing_sites_the_questionnaire_refuses_frames_too(self):
        response = self.client.get(reverse('portal:questionnaire'))
        self.assertEqual(response['X-Frame-Options'], 'DENY')
        self.assertNotIn('Content-Security-Policy', response.headers)

    def test_an_ordinary_visit_is_unchanged(self):
        page = self.client.get(reverse('portal:questionnaire'))
        self.assertContains(page, '<html lang="en">')
        self.assertContains(page, '<meta name="color-scheme" content="light dark">')
        self.assertContains(page, 'class="site-header')
        self.assertContains(page, 'data-theme-switch')
        self.assertContains(page, 'js/theme.js')
        self.assertContains(page, 'aria-label="Back to the Meridian Health website"')
        self.assertNotContains(page, 'questionnaire_embed')

    def test_every_step_inside_the_frame_drops_the_app_header(self):
        first = self.client.get(reverse('portal:questionnaire'), {'embed': '1'})
        self.assertEmbedded(first)
        self.assertNotContains(first, 'aria-label="Back to the Meridian Health website"')
        # Later steps don't carry ?embed=1: the browser's frame header keeps them embedded.
        self.submit_enquiry(**IN_FRAME)
        self.assertEmbedded(self.client.get(reverse('portal:questionnaire-result'), **IN_FRAME))
        self.assertEmbedded(self.client.get(self.checkout_url, {'date': self.day.isoformat()}, **IN_FRAME))
        self.assertContains(self.client.get(reverse('portal:questionnaire-result')), 'class="site-header')

    def test_booking_inside_the_frame_moves_the_whole_tab_to_the_portal(self):
        lead = self.submit_enquiry(**IN_FRAME)
        page = self.client.get(self.checkout_url, {'date': self.day.isoformat()}, **IN_FRAME)
        response = self.client.post(self.checkout_url, {
            'action': 'pay', 'date': self.day.isoformat(), 'slot': page.context['slots'][0]['token'],
            'discount_code': 'devtest', 'password1': PASSWORD, 'password2': PASSWORD,
        }, **IN_FRAME)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'portal/questionnaire_embed_exit.html')
        self.assertFrameableByWordPress(response)
        portal = f'http://testserver{reverse("portal:patient-dashboard")}'
        self.assertContains(response, f'href="{portal}" target="_top" data-questionnaire-exit')
        lead.refresh_from_db()
        self.assertIsNotNone(lead.converted_patient_id)
        # The booking confirmation waits for the portal instead of flashing inside the frame.
        self.assertNotContains(response, 'is booked for')
        self.assertContains(self.client.get(reverse('portal:patient-dashboard')), 'is booked for')
        # Back on a questionnaire step inside the frame, the tab goes to the portal the same way.
        again = self.client.get(reverse('portal:questionnaire-result'), **IN_FRAME)
        self.assertTemplateUsed(again, 'portal/questionnaire_embed_exit.html')
        # Outside a frame it is still an ordinary redirect.
        self.assertRedirects(self.client.get(reverse('portal:questionnaire-result')), reverse('portal:patient-dashboard'),
                             fetch_redirect_response=False)

    def test_signing_in_from_inside_the_frame_opens_the_whole_tab(self):
        get_user_model().objects.create_user(email=EMAIL, password=PASSWORD)
        self.submit_enquiry(**IN_FRAME)
        embedded = self.client.get(self.checkout_url, {'date': self.day.isoformat()}, **IN_FRAME)
        self.assertContains(embedded, 'target="_top">Sign in to continue</a>')
        ordinary = self.client.get(self.checkout_url, {'date': self.day.isoformat()})
        self.assertContains(ordinary, '">Sign in to continue</a>')
        self.assertNotContains(ordinary, 'target="_top"')
