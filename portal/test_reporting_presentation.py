"""Charts preserve aggregate meanings and remain usable with sparse data."""
import json
import os
import shutil
import subprocess
from decimal import Decimal
from unittest import skipUnless

from django.conf import settings
from django.urls import reverse

from care.models import WeightEntry
from care.test_reporting import ReportingFixture
from practices.models import Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY
from .reporting_presentation import metrics_dashboard


class ReportingDashboardTests(ReportingFixture):
    def page(self, **params):
        self.client.force_login(self.admin)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()
        return self.client.get(reverse('portal:metrics'), {'start': self.start, 'end': self.end, **params})

    def populate(self):
        for status in ('booked', 'completed', 'no_show', 'cancelled'):
            self.appointment(status=status)
        Patient.objects.filter(pk=self.patient.pk).update(created_at=self.at(self.start))
        for day, kg in ((self.start, '100'), (self.end, '95')):
            WeightEntry.objects.create(company=self.company, patient=self.patient, recorded_on=day, weight_kg=Decimal(kg))

    def test_compact_dashboard_has_four_kpis_and_exact_data_under_each_chart(self):
        self.populate()
        response = self.page()
        self.assertContains(response, 'class="metrics-kpi ', count=4)
        for text in ('Daily practice activity', 'View daily counts', 'Booking breakdown',
                     'When your patients joined', 'Lower recorded weight', 'All measures &amp; definitions'):
            self.assertContains(response, text)
        self.assertNotContains(response, 'backoffice-metrics')
        self.assertEqual(response.context['dashboard']['appointment_total'], 4)
        self.assertEqual(sum(item['count'] for item in response.context['dashboard']['statuses']), 4)
        self.assertEqual(response.context['weight_stats']['mean'], -5)

    def test_zero_and_single_day_states_never_fabricate_activity(self):
        response = self.page(start=self.start, end=self.start)
        self.assertContains(response, 'No activity recorded in this period')
        self.assertContains(response, 'More check-ins needed')
        dashboard = response.context['dashboard']
        self.assertFalse(dashboard['has_activity'])
        self.assertEqual(dashboard['ring'], '#eaf0ef')
        for line in dashboard['lines']:
            self.assertEqual(line['total'], 0)
            self.assertEqual(line['points'][0]['x'], '300.00')
        self.appointment(starts_at=self.at(self.start))
        self.assertContains(self.page(start=self.start, end=self.start), 'class="metrics-line-chart"')

    def test_geometry_is_bounded_and_uses_integer_count_ticks(self):
        self.populate()
        dashboard = metrics_dashboard(self.metrics())
        for line in dashboard['lines']:
            for point in line['points']:
                self.assertTrue(0 <= float(point['x']) <= 600)
                self.assertTrue(0 <= float(point['y']) <= 180)
        self.assertTrue(all(isinstance(tick, int) for tick in dashboard['ticks']))
        self.assertEqual(sum(row['count'] for row in dashboard['weight_rows']), 1)
        self.assertEqual(dashboard['cohort_total'], 1)

    @skipUnless(os.getenv('MERIDIAN_PLAYWRIGHT_PATH'), 'Optional Chrome reporting dashboard check')
    def test_populated_and_empty_browser_layouts(self):
        pages = [{'name': 'metrics-empty', 'html': self.page(start=self.start, end=self.start).content.decode()}]
        self.populate()
        pages.append({'name': 'metrics-populated', 'html': self.page().content.decode()})
        node = os.getenv('MERIDIAN_NODE') or shutil.which('node')
        self.assertIsNotNone(node)
        result = subprocess.run([node, str(settings.BASE_DIR / 'scripts/operations_layout_smoke.cjs')],
                                input=json.dumps(pages), text=True, capture_output=True,
                                cwd=settings.BASE_DIR, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['checked'], 6)
