"""The patient overview is compact without changing care navigation or data."""
from django.test import override_settings
import json
import os
import shutil
import subprocess
from datetime import timedelta
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, MedicationProduct, PatientEvent, PatientSubscription, TreatmentAuthorization, WeightEntry
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_PATIENT_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
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

    def prescribe(self, *, starts=None, ends=None, **overrides):
        product, _ = MedicationProduct.objects.get_or_create(company=self.company, name='Prescribed treatment', defaults={'price': 100})
        return TreatmentAuthorization.objects.create(
            company=self.company, patient=self.patient, product=product, prescribed_by=self.doctor,
            starts_on=starts or timezone.localdate() - timedelta(weeks=8),
            expires_on=ends or timezone.localdate() + timedelta(weeks=16), max_dose='Recorded dose', **overrides,
        )

    def populate(self):
        authorization = self.prescribe()
        PatientSubscription.objects.create(
            company=self.company, patient=self.patient, authorization=authorization,
            plan_name='Comprehensive ongoing care and follow-up support plan',
        )
        Appointment.objects.create(
            company=self.company, patient=self.patient, clinician=self.doctor,
            starts_at=timezone.now() + timedelta(days=2), duration_minutes=30,
        )
        WeightEntry.objects.create(company=self.company, patient=self.patient, weight_kg='94.50', recorded_on=timezone.localdate())
        WeightEntry.objects.create(company=self.company, patient=self.patient, weight_kg='98.00', recorded_on=timezone.localdate() - timedelta(weeks=8))
        PatientEvent.objects.create(company=self.company, patient=self.patient, category='appointment', title='Your appointment is confirmed')

    def test_duplicate_practice_strip_is_removed_from_all_patient_pages(self):
        for name in ('patient-dashboard', 'patient-appointments', 'patient-progress', 'patient-messages', 'patient-profile', 'patient-updates'):
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
        # Home keeps care navigation and logging, without the two history links.
        for content in ('Latest 94.5 kg', 'Next consult', 'Log my weight'):
            self.assertContains(response, content)
        for name in ('patient-treatment', 'patient-appointments', 'patient-messages'):
            self.assertContains(response, f'href="{reverse(f"portal:{name}")}"')
        self.assertNotContains(response, 'See everything that’s happened')
        self.assertNotContains(response, 'See all your check-ins')
        self.assertContains(response, 'Illustrative reference')
        self.assertContains(response, '500g–1kg/week')
        self.assertContains(response, 'not a prediction or a personal target')

    def test_home_reference_uses_treatment_course_start_across_renewals(self):
        today = timezone.localdate()
        product = MedicationProduct.objects.create(company=self.company, name='Prescribed treatment', price=100)
        started = today - timedelta(weeks=12)
        first = TreatmentAuthorization.objects.create(
            company=self.company, patient=self.patient, product=product, prescribed_by=self.doctor,
            starts_on=started, expires_on=today - timedelta(weeks=4), max_dose='Starting dose',
            status=TreatmentAuthorization.Status.CANCELLED,
        )
        TreatmentAuthorization.objects.create(
            company=self.company, patient=self.patient, product=product, prescribed_by=self.doctor,
            starts_on=today - timedelta(weeks=4), expires_on=today + timedelta(weeks=8),
            max_dose='Current dose', replaces=first,
        )
        WeightEntry.objects.create(company=self.company, patient=self.patient, weight_kg='100', recorded_on=started)
        WeightEntry.objects.create(company=self.company, patient=self.patient, weight_kg='96', recorded_on=today)
        reference = self.page().context['weight_chart']['reference']
        self.assertEqual(reference['treatment_start'], started)
        self.assertEqual(reference['starts_on'], started + timedelta(weeks=4))
        self.assertEqual(reference['baseline_weight'], 100)
        self.assertEqual(reference['ends_on'], started + timedelta(weeks=16))

    def test_home_reference_anchor_survives_300_entry_limit_and_excludes_other_records(self):
        today = timezone.localdate()
        self.prescribe(starts=today - timedelta(days=304))
        WeightEntry.objects.bulk_create([
            WeightEntry(company=self.company, patient=self.patient, recorded_on=today - timedelta(days=index),
                        weight_kg='100' if index == 304 else '90')
            for index in range(305)
        ])
        other = Patient.objects.create(company=self.company, first_name='Other', last_name='Patient')
        WeightEntry.objects.create(company=self.company, patient=other, weight_kg='200', recorded_on=today - timedelta(days=400))
        response = self.page()
        chart = response.context['weight_chart']
        self.assertEqual(chart['count'], 300)
        self.assertTrue(response.context['chart_is_limited'])
        self.assertEqual(chart['reference']['baseline_weight'], 100)
        self.assertEqual(chart['reference']['baseline_date'], today - timedelta(days=304))
        self.assertTrue(all(point['weight'] == 90 for point in chart['points']))

    def test_never_prescribed_or_cancelled_treatment_shows_actual_weights_without_reference(self):
        WeightEntry.objects.create(company=self.company, patient=self.patient, weight_kg=100, recorded_on=timezone.localdate())
        for state in ('never_prescribed', 'cancelled'):
            if state == 'cancelled':
                self.prescribe(status=TreatmentAuthorization.Status.CANCELLED)
            with self.subTest(state=state):
                response = self.page()
                self.assertIsNone(response.context['weight_chart']['reference'])
                self.assertEqual(response.context['weight_chart']['count'], 1)
                self.assertContains(response, 'Log today’s weight')
                self.assertNotContains(response, '500g')
                self.assertNotContains(response, 'listed in your weight history')

    def test_home_queries_do_not_grow_with_a_long_renewal_chain(self):
        from .patient_summary import weight_reference_course

        today = timezone.localdate()
        started = today - timedelta(weeks=30)
        authorization = self.prescribe(starts=started)
        WeightEntry.objects.create(company=self.company, patient=self.patient, weight_kg=100, recorded_on=started)
        with CaptureQueriesContext(connection) as short:
            self.page()
        for week in range(1, 30):
            authorization.status = TreatmentAuthorization.Status.CANCELLED
            authorization.save(update_fields=['status'])
            authorization = TreatmentAuthorization.objects.create(
                company=self.company, patient=self.patient, product=authorization.product, prescribed_by=self.doctor,
                starts_on=started + timedelta(weeks=week), expires_on=today + timedelta(weeks=16),
                max_dose='Renewed dose', replaces=authorization,
            )
        with CaptureQueriesContext(connection) as long:
            response = self.page()
        self.assertEqual(len(short), len(long))
        self.assertEqual(response.context['weight_chart']['reference']['treatment_start'], started)
        with self.assertNumQueries(1):
            course = weight_reference_course(self.company, self.patient, authorization)
        self.assertEqual(course['started_on'], started)

    def test_course_lookup_does_not_follow_another_patients_replacement(self):
        from .patient_summary import weight_reference_course

        other = Patient.objects.create(company=self.company, first_name='Other', last_name='Patient')
        foreign = self.prescribe(starts=timezone.localdate() - timedelta(weeks=100))
        TreatmentAuthorization.objects.filter(pk=foreign.pk).update(patient=other)
        own = self.prescribe(replaces=foreign)
        course = weight_reference_course(self.company, self.patient, own)
        self.assertEqual(course['started_on'], own.starts_on)

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
        self.assertEqual(json.loads(result.stdout)['checked'], 12)
