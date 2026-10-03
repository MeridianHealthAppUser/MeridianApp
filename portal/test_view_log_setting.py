"""View events are stored only when a Super Admin turns on the view log, and are never shown in the app."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from care.models import AuditEvent, PracticeSettings
from care.services import ACCESS_ACTIONS, record_audit
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class ViewLogSettingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.company = Company.objects.create(name='View Log Practice', slug='view-log-practice')
        cls.super_admin = User.objects.create_user(email='super@view-log.test')
        cls.admin = User.objects.create_user(email='admin@view-log.test')
        cls.doctor = User.objects.create_user(email='doctor@view-log.test')
        CompanyMembership.objects.create(company=cls.company, user=cls.super_admin, role='super_admin')
        CompanyMembership.objects.create(company=cls.company, user=cls.admin, role='practice_admin')
        CompanyMembership.objects.create(company=cls.company, user=cls.doctor, role='doctor')
        cls.patient = Patient.objects.create(company=cls.company, first_name='Pat', last_name='Patient')

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def test_views_are_not_stored_by_default_but_changes_always_are(self):
        for action in ACCESS_ACTIONS:
            self.assertIsNone(record_audit(company=self.company, actor=self.doctor, action=action, patient=self.patient))
        self.assertIsNotNone(record_audit(company=self.company, actor=self.doctor, action='patient.contact_updated', patient=self.patient))
        self.assertEqual(list(AuditEvent.objects.values_list('action', flat=True)), ['patient.contact_updated'])
        self.login(self.doctor)
        self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]))
        self.assertFalse(AuditEvent.objects.filter(action='patient.record_viewed').exists())

    def test_only_a_super_admin_can_turn_the_view_log_on_and_the_change_is_logged(self):
        url = reverse('portal:practice-settings')
        for user in (self.admin, self.doctor):
            self.login(user)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, {'store_view_log': 'on'}).status_code, 403)
        self.login(self.super_admin)
        page = self.client.get(url)
        self.assertContains(page, 'Store view log on database')
        self.assertRedirects(self.client.post(url, {'store_view_log': 'on'}), url, fetch_redirect_response=False)
        self.assertTrue(PracticeSettings.objects.get(company=self.company).store_view_log)
        self.assertEqual(AuditEvent.objects.get(action='practice.settings_updated').metadata, {'store_view_log': True})
        self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]))
        self.assertTrue(AuditEvent.objects.filter(action='patient.record_viewed', actor=self.super_admin).exists())

    def test_stored_view_events_are_never_shown_in_the_app(self):
        PracticeSettings.objects.create(company=self.company, store_view_log=True)
        self.login(self.doctor)
        self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]))
        self.assertTrue(AuditEvent.objects.filter(action='patient.record_viewed').exists())
        history = self.client.get(reverse('portal:account-access-history'))
        self.assertEqual(history.context['access_rows'], [])
        self.assertNotContains(history, 'Patient record viewed')
        record = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {'tab': 'history'})
        self.assertNotContains(record, 'record viewed')

    def test_event_log_settings_list_what_is_always_stored_and_only_the_view_log_changes(self):
        from .practice_settings_views import EVENT_LOG_SECTIONS

        self.login(self.super_admin)
        page = self.client.get(reverse('portal:practice-settings'))
        self.assertContains(page, 'Event log settings')
        items = [label for _, section in EVENT_LOG_SECTIONS for label, _ in section]
        for label in items:
            self.assertContains(page, label)
        html = page.content.decode()
        self.assertEqual(html.count('<fieldset class="event-log-section" disabled>'), len(EVENT_LOG_SECTIONS))
        self.assertEqual(html.count('type="checkbox" checked aria-describedby="event-log-always"'), len(items))
        # Only the view log is a real setting; nothing else can be posted.
        self.client.post(reverse('portal:practice-settings'), {'store_view_log': 'on', 'support_email': 'changed@example.test'})
        settings_row = PracticeSettings.objects.get(company=self.company)
        self.assertTrue(settings_row.store_view_log)
        self.assertEqual(settings_row.support_email, '')
