"""The patient overview is compact without changing care navigation or data."""
import json
import os
import shutil
import subprocess
from datetime import timedelta
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, PatientEvent, PatientSubscription, WeightEntry
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_PATIENT_COMPANY_SESSION_KEY


class PatientDashboardPresentationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Example care practice', slug='patient-home-ui')
        cls.user = get_user_model().objects.create_user(email='home-patient@example.test', first_name='Alice')
        cls.doctor = get_user_model().objects.create_user(email='home-doctor@example.test', first_name='Dr Example')
        CompanyMembership.objects.create(company=cls.company, user=cls.doctor, role='doctor')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.user, first_name='Alice', last_name='Example')

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def page(self, name='patient-dashboard'):
        return self.client.get(reverse(f'portal:{name}'))

    def populate(self):
        PatientSubscription.objects.create(
            company=self.company, patient=self.patient, monthly_amount='1995',
            plan_name='Comprehensive ongoing care and follow-up support plan',
        )
        Appointment.objects.create(
            company=self.company, patient=self.patient, clinician=self.doctor,
            starts_at=timezone.now() + timedelta(days=2), duration_minutes=30,
        )
        WeightEntry.objects.create(company=self.company, patient=self.patient, weight_kg='94.50', recorded_on=timezone.localdate())
        PatientEvent.objects.create(company=self.company, patient=self.patient, category='appointment', title='Your appointment is confirmed')

    def test_duplicate_practice_strip_is_removed_from_all_patient_pages(self):
        for name in ('patient-dashboard', 'patient-appointments', 'patient-progress', 'patient-messages', 'patient-account', 'patient-updates'):
            with self.subTest(page=name):
                response = self.page(name)
                self.assertContains(response, 'class="patient-navigation"')
                self.assertNotContains(response, 'patient-page-topbar')
                self.assertNotContains(response, '>Your care at</span>')
                self.assertContains(response, self.company.name)
                if name != 'patient-dashboard':
                    self.assertNotContains(response, 'css/patient_dashboard.css')

    def test_overview_preserves_real_values_and_dedicated_patient_links(self):
        self.populate()
        response = self.page()
        self.assertContains(response, 'patient-home__heading')
        self.assertNotContains(response, 'class="patient-hero"')
        for content in ('94.50 kg', 'Comprehensive ongoing care and follow-up support plan', 'Your appointment is confirmed'):
            self.assertContains(response, content)
        for name in ('patient-appointments', 'patient-progress', 'patient-messages', 'patient-updates'):
            self.assertContains(response, f'href="{reverse(f"portal:{name}")}"')

    @skipUnless(os.getenv('MERIDIAN_PLAYWRIGHT_PATH'), 'Optional Chrome patient overview check')
    def test_empty_and_populated_compact_browser_layouts(self):
        pages = [{'name': 'patient-home-empty', 'html': self.page().content.decode()}]
        self.populate()
        pages.append({'name': 'patient-home-populated', 'html': self.page().content.decode()})
        node = os.getenv('MERIDIAN_NODE') or shutil.which('node')
        self.assertIsNotNone(node)
        result = subprocess.run(
            [node, str(settings.BASE_DIR / 'scripts/test_patient_dashboard.cjs')],
            input=json.dumps(pages), text=True, capture_output=True, cwd=settings.BASE_DIR, timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['checked'], 6)
