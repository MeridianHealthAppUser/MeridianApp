"""Exact, compact weight histories retain the staff record's practice boundary."""

import json
import os
import shutil
import subprocess
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from care.models import WeightEntry
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY
from .patient_progress import weight_chart


class WeightChartGeometryTests(SimpleTestCase):
    def entry(self, day, weight):
        return SimpleNamespace(recorded_on=date(2026, 1, 1) + timedelta(days=day), weight_kg=Decimal(weight))

    def test_actual_date_spacing_not_equal_entry_spacing(self):
        chart = weight_chart([self.entry(0, '94.20'), self.entry(1, '94.10'), self.entry(10, '90.25')])
        self.assertEqual([point['x'] for point in chart['points']], ['62.00', '125.00', '692.00'])
        self.assertEqual([point['weight'] for point in chart['points']], list(map(Decimal, ('94.20', '94.10', '90.25'))))
        self.assertEqual(chart['minimum'], Decimal('90.25'))

    def test_single_checkin_is_centered_and_empty_history_has_no_chart(self):
        chart = weight_chart([self.entry(0, '90')])
        self.assertEqual((chart['points'][0]['x'], chart['points'][0]['y']), ('377.00', '122.00'))
        self.assertIsNone(weight_chart([]))

    def test_flat_history_has_a_finite_horizontal_line(self):
        chart = weight_chart([self.entry(0, '90'), self.entry(30, '90')])
        self.assertEqual([point['y'] for point in chart['points']], ['122.00', '122.00'])
        self.assertNotIn('NaN', chart['polyline'])
        self.assertNotIn('Infinity', chart['polyline'])


class RecordWeightTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Weight Alpha', slug='weight-alpha')
        cls.beta = Company.objects.create(name='Weight Beta', slug='weight-beta')
        cls.doctor = get_user_model().objects.create_user(email='weight-doctor@example.test', first_name='Example', last_name='Doctor')
        cls.user = get_user_model().objects.create_user(email='weight-patient@example.test')
        for company in (cls.company, cls.beta):
            CompanyMembership.objects.create(company=company, user=cls.doctor, role=CompanyMembership.Role.DOCTOR)
        cls.patient = Patient.objects.create(company=cls.company, user=cls.user, first_name='Example', last_name='Patient')
        cls.beta_patient = Patient.objects.create(company=cls.beta, user=cls.user, first_name='Example', last_name='Patient')
        cls.other_patient = Patient.objects.create(company=cls.company, first_name='Other', last_name='Patient')
        cls.day = date(2026, 1, 1)

    def login(self, user=None, company=None):
        self.client.force_login(user or self.doctor)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def url(self, patient=None):
        return reverse('portal:patient-detail', args=[(patient or self.patient).pk]) + '?tab=weights'

    def entry(self, day=0, weight='90.00', **extra):
        values = dict(company=self.company, patient=self.patient, recorded_on=self.day + timedelta(days=day),
                      weight_kg=weight, recorded_by=self.doctor)
        values.update(extra)
        return WeightEntry.objects.create(**values)

    def test_chart_summary_and_table_use_only_this_patient_at_this_practice(self):
        self.login()
        first = self.entry(0, '94.20')
        latest = self.entry(10, '90.25', note='Real check-in <script>unsafe()</script>')
        self.entry(20, '333.33', company=self.beta, patient=self.beta_patient, note='Other practice marker')
        self.entry(30, '222.22', patient=self.other_patient, note='Other patient marker')
        self.entry(40, '111.11', company=self.beta, note='Malformed company marker')
        response = self.client.get(self.url(), {'tab': 'weights', 'patient': self.beta_patient.pk, 'company': self.beta.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['record_weight_total'], 2)
        self.assertEqual(response.context['record_weight_first'].pk, first.pk)
        self.assertEqual(response.context['record_weight_latest'].pk, latest.pk)
        self.assertEqual(response.context['record_weight_change'], Decimal('-3.95'))
        self.assertEqual([point['weight'] for point in response.context['record_weight_chart']['points']], [Decimal('94.20'), Decimal('90.25')])
        self.assertEqual([row.pk for row in response.context['weights']], [latest.pk, first.pk])
        self.assertContains(response, 'record-weight-chart-description')
        self.assertContains(response, '<table class="record-weight-table"')
        self.assertContains(response, 'aria-label="Recorded weight summary"')
        self.assertContains(response, '&lt;script&gt;unsafe()&lt;/script&gt;')
        for marker in ('Other practice marker', 'Other patient marker', 'Malformed company marker', '<script>unsafe()'):
            self.assertNotContains(response, marker)
        self.assertEqual(WeightEntry.objects.count(), 5)
        first.refresh_from_db()
        self.assertEqual(first.weight_kg, Decimal('94.20'))

    def test_empty_record_is_compact_without_fake_chart_or_summary(self):
        self.login()
        response = self.client.get(self.url())
        self.assertContains(response, 'The chart will appear after the first check-in.')
        self.assertContains(response, '0 check-ins')
        self.assertIsNone(response.context['record_weight_chart'])
        self.assertNotContains(response, 'id="record-weight-chart-title"')
        self.assertNotContains(response, 'class="record-weight-summary"')

    def test_single_and_unchanged_checkins_have_clear_non_clinical_states(self):
        self.login()
        self.entry()
        response = self.client.get(self.url())
        self.assertContains(response, 'A trend line appears after the next check-in.')
        self.assertNotContains(response, '<polyline')
        self.entry(10)
        response = self.client.get(self.url())
        self.assertEqual(response.context['record_weight_change'], Decimal('0.00'))
        self.assertContains(response, '<polyline')
        self.assertContains(response, 'not daily measurements')

    def test_every_checkin_remains_reachable_and_summary_does_not_follow_table_page(self):
        self.login()
        for day in range(13):
            self.entry(day, str(100 - day))
        response = self.client.get(self.url())
        self.assertEqual(len(response.context['weights']), 10)
        self.assertEqual(response.context['record_weight_total'], 13)
        self.assertEqual(response.context['record_weight_chart']['count'], 13)
        self.assertEqual(response.context['record_weight_next_url'], self.url() + '&weight_page=2#weights')
        response = self.client.get(self.url(), {'tab': 'weights', 'weight_page': 2})
        self.assertEqual(len(response.context['weights']), 3)
        self.assertEqual(response.context['record_weight_first'].weight_kg, Decimal('100.00'))
        self.assertEqual(response.context['record_weight_latest'].weight_kg, Decimal('88.00'))
        self.assertEqual(response.context['record_weight_change'], Decimal('-12.00'))
        self.assertEqual(response.context['record_weight_previous_url'], self.url() + '&weight_page=1#weights')
        self.assertContains(response, 'Page 2 of 2')
        for invalid in ('bad', '9' * 5000):
            response = self.client.get(self.url(), {'tab': 'weights', 'weight_page': invalid})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context['record_weight_page'].number, 1)

    def test_chart_limit_does_not_hide_old_entries_or_change_all_history_summary(self):
        self.login()
        WeightEntry.objects.bulk_create([
            WeightEntry(company=self.company, patient=self.patient, recorded_on=self.day + timedelta(days=day), weight_kg='90.00')
            for day in range(305)
        ])
        response = self.client.get(self.url(), {'tab': 'weights', 'weight_page': 31})
        self.assertEqual(response.context['record_weight_total'], 305)
        self.assertEqual(response.context['record_weight_chart']['count'], 300)
        self.assertEqual(len(response.context['weights']), 5)
        self.assertEqual(response.context['record_weight_first'].recorded_on, self.day)
        self.assertContains(response, 'Latest 300 check-ins shown.')

    def test_staff_record_permissions_and_active_practice_still_apply(self):
        self.assertEqual(self.client.get(self.url()).status_code, 302)
        self.login(self.user)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.login()
        self.assertEqual(self.client.get(self.url(self.beta_patient)).status_code, 404)
        self.login(company=self.beta)
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        self.login()
        self.patient.is_active = False
        self.patient.save(update_fields=['is_active'])
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    @skipUnless(os.getenv('MERIDIAN_PLAYWRIGHT_PATH'), 'Optional local Chrome layout test is not configured')
    def test_optional_responsive_chart_and_no_sibling_height_stretch(self):
        self.login()
        empty = self.client.get(self.url())
        for day in (0, 1, 5, 30):
            self.entry(day, str(94 - day / 10), note='A long note ' + 'x' * 160)
        populated = self.client.get(self.url())
        node = os.getenv('MERIDIAN_NODE') or shutil.which('node')
        self.assertIsNotNone(node, 'Install Node or configure MERIDIAN_NODE.')
        result = subprocess.run(
            [node, str(settings.BASE_DIR / 'scripts' / 'test_record_weight.cjs')],
            input=json.dumps([{'name': 'empty', 'html': empty.content.decode()},
                              {'name': 'populated', 'html': populated.content.decode()}]),
            cwd=settings.BASE_DIR, text=True, capture_output=True, timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['checked'], 6)
