"""Dashboard analytics respect tenancy, admin permissions and local date windows."""

from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, ClinicalTask, Invoice, LabRequest, PatientSubscription, Shipment

from .admin_dashboard import build_admin_dashboard
from .models import Company, Patient


@override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='meridian-health',
                   TIME_ZONE='Africa/Johannesburg')
class AdminDashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Meridian Health', slug='meridian-health')
        cls.other = Company.objects.create(name='Foreign private practice', slug='foreign')
        cls.archived = Company.objects.create(name='Archived private practice', slug='archived', is_active=False)
        cls.superuser = get_user_model().objects.create_superuser(email='dashboard-admin@example.test', password='Test!2026')
        cls.staff = get_user_model().objects.create_user(email='dashboard-viewer@example.test', is_staff=True)
        cls.now = timezone.make_aware(datetime(2026, 9, 29, 12), timezone.get_current_timezone())
        cls.today = cls.now.date()

    def request(self, user=None, days=None):
        request = RequestFactory().get('/admin/', {'days': days} if days is not None else {})
        request.user = user or self.superuser
        return request

    def dashboard(self, user=None, days=None):
        with patch('practices.admin_dashboard.timezone.now', return_value=self.now):
            return build_admin_dashboard(admin.site, self.request(user, days))

    def patient(self, company=None, created_at=None, is_active=True):
        record = Patient.objects.create(company=company or self.company,
                                        first_name='PRIVATE_FIRST_SENTINEL', last_name='PRIVATE_LAST_SENTINEL',
                                        is_active=is_active)
        Patient.objects.filter(pk=record.pk).update(created_at=created_at or self.now)
        return record

    def appointment(self, patient, starts_at=None, status=Appointment.Status.BOOKED):
        return Appointment.objects.create(company=patient.company, patient=patient, clinician=self.superuser,
                                          starts_at=starts_at or self.now, status=status)

    def invoice(self, patient, total, status=Invoice.Status.ISSUED):
        return Invoice.objects.create(company=patient.company, patient=patient,
                                      invoice_number=f'PRIVATE-INVOICE-{Invoice.objects.count()}',
                                      subtotal=total, total=total, due_on=self.today, status=status)

    def grant(self, model, permission='view'):
        self.staff.user_permissions.add(Permission.objects.get(
            content_type__app_label=model._meta.app_label,
            codename=f'{permission}_{model._meta.model_name}',
        ))

    @staticmethod
    def metrics(data):
        return {item['key']: item for item in data['metrics']}

    def test_all_aggregates_exclude_other_and_archived_practices(self):
        for company in (self.company, self.other, self.archived):
            patient = self.patient(company)
            self.appointment(patient)
            PatientSubscription.objects.create(company=company, patient=patient, plan_name='PRIVATE_PLAN',
                                               monthly_amount=Decimal('399.99'))
            self.invoice(patient, Decimal('100.01'))
            ClinicalTask.objects.create(company=company, patient=patient, title='PRIVATE_TASK',
                                        due_at=self.now - timedelta(hours=1))
            LabRequest.objects.create(company=company, patient=patient, requested_by=self.superuser,
                                      panel_name='PRIVATE_LAB', status=LabRequest.Status.UPLOADED)
            Shipment.objects.create(company=company, patient=patient, scheduled_for=self.today)
        data = self.dashboard()
        metrics = self.metrics(data)
        self.assertEqual(metrics['patients']['value'], '1')
        self.assertEqual(metrics['appointments']['value'], '1')
        self.assertEqual(metrics['subscriptions']['value'], '1')
        self.assertEqual(metrics['invoices']['value'], 'R 100.01')
        self.assertEqual(data['appointment_total'], 1)
        self.assertEqual(sum(data['activity']['patients']), 1)
        self.assertEqual(sum(data['activity']['appointments']), 1)
        self.assertEqual([item['count'] for item in data['workload']], [1, 1, 1, 1])
        for private in ('PRIVATE_', self.other.name, self.archived.name, self.staff.email, self.superuser.email):
            self.assertNotIn(private, str(data))

    @override_settings(MULTI_PRACTICE_ENABLED=True)
    def test_multi_practice_overview_still_excludes_archived_practices(self):
        for company in (self.company, self.other, self.archived):
            self.appointment(self.patient(company))
        data = self.dashboard()
        self.assertEqual(data['scope_label'], 'All enabled practices')
        self.assertEqual(self.metrics(data)['patients']['value'], '2')
        self.assertEqual(data['appointment_total'], 2)
        self.assertEqual(sum(data['activity']['patients']), 2)

    def test_inactive_configured_practice_has_no_dashboard_data(self):
        self.appointment(self.patient())
        self.company.is_active = False
        self.company.save(update_fields=['is_active'])
        data = self.dashboard()
        self.assertFalse(data['has_practice'])
        self.assertEqual(self.metrics(data)['patients']['value'], '0')
        self.assertEqual(data['appointment_total'], 0)
        self.assertFalse(data['activity']['has_data'])

    def test_staff_without_model_permissions_cannot_read_aggregates_or_links(self):
        self.appointment(self.patient())
        with CaptureQueriesContext(connection) as captured:
            data = self.dashboard(self.staff)
        self.assertEqual(data['metrics'], [])
        self.assertEqual(data['workload'], [])
        self.assertEqual(data['quick_links'], [])
        self.assertEqual(data['appointment_statuses'], [])
        self.assertIsNone(data['appointment_total'])
        self.assertIsNone(data['activity']['patients'])
        self.assertIsNone(data['activity']['appointments'])
        self.assertFalse(data['activity']['has_data'])
        self.assertTrue(all(row['patients'] is None and row['appointments'] is None
                            for row in data['activity']['rows']))
        for model in (Patient, Appointment, PatientSubscription, Invoice, ClinicalTask, LabRequest, Shipment):
            for query in captured.captured_queries:
                self.assertNotIn(f'FROM "{model._meta.db_table}"', query['sql'])

    def test_view_only_permission_shows_only_that_model(self):
        self.grant(Patient)
        self.appointment(self.patient())
        data = self.dashboard(self.staff)
        self.assertEqual(list(self.metrics(data)), ['patients'])
        self.assertEqual(sum(data['activity']['patients']), 1)
        self.assertIsNone(data['activity']['appointments'])
        self.assertEqual(len(data['quick_links']), 1)
        self.assertEqual(data['quick_links'][0]['url'], reverse('admin:practices_patient_changelist'))

    def test_module_permission_and_admin_queryset_restrictions_are_preserved(self):
        self.patient()
        model_admin = admin.site._registry[Patient]
        with patch.object(model_admin, 'has_module_permission', return_value=False), \
                patch.object(model_admin, 'get_queryset') as getter:
            data = self.dashboard()
        getter.assert_not_called()
        self.assertNotIn('patients', self.metrics(data))
        with patch.object(model_admin, 'get_queryset', return_value=Patient.objects.none()):
            data = self.dashboard()
        self.assertEqual(self.metrics(data)['patients']['value'], '0')
        self.assertEqual(sum(data['activity']['patients']), 0)

    def test_period_options_reject_unbounded_or_malformed_values(self):
        for supplied, expected in ((None, 30), ('30', 30), ('90', 90), ('180', 180),
                                   ('0', 30), ('9999999', 30), ('-30', 30), ('bad', 30)):
            with self.subTest(supplied=supplied):
                data = self.dashboard(days=supplied)
                self.assertEqual(data['period'], expected)
                self.assertEqual(data['period_options'], [30, 90, 180])
                self.assertLessEqual(len(data['activity']['labels']), 27)

    def test_chart_and_status_window_use_inclusive_local_dates_and_exclusive_end(self):
        start = timezone.make_aware(datetime.combine(self.today - timedelta(days=29), time.min))
        end = timezone.make_aware(datetime.combine(self.today + timedelta(days=1), time.min))
        for moment in (start - timedelta(microseconds=1), start, self.now,
                       end - timedelta(microseconds=1), end):
            self.appointment(self.patient(created_at=moment), starts_at=moment)
        data = self.dashboard()
        self.assertEqual(sum(data['activity']['patients']), 3)
        self.assertEqual(sum(data['activity']['appointments']), 3)
        self.assertEqual(data['appointment_total'], 3)
        self.assertEqual(sum(status['count'] for status in data['appointment_statuses']), 3)
        self.assertEqual(data['activity']['patients'], data['activity']['appointments'])
        self.assertEqual(sum(row['patients'] for row in data['activity']['rows']), 3)
        self.assertEqual(self.metrics(data)['patients']['value'], '5')  # Current register is not date filtered.

    def test_longer_window_includes_older_records_and_status_totals(self):
        patient = self.patient(created_at=self.now - timedelta(days=60))
        self.appointment(patient, starts_at=self.now - timedelta(days=60), status=Appointment.Status.COMPLETED)
        self.appointment(patient, status=Appointment.Status.NO_SHOW)
        self.appointment(patient, status=Appointment.Status.CANCELLED)
        self.assertEqual(self.dashboard()['appointment_total'], 2)
        data = self.dashboard(days='90')
        self.assertEqual(data['appointment_total'], 3)
        self.assertEqual(sum(data['activity']['patients']), 1)
        self.assertEqual(sum(data['activity']['appointments']), 3)
        counts = {row['label']: row['count'] for row in data['appointment_statuses']}
        self.assertEqual(counts, {'Booked': 0, 'Completed': 1, 'Cancelled': 1, 'No show': 1})

    def test_outstanding_money_sums_only_issued_invoices_with_decimal_precision(self):
        patient = self.patient()
        self.invoice(patient, Decimal('1000.10'))
        self.invoice(patient, Decimal('0.20'))
        for status in (Invoice.Status.DRAFT, Invoice.Status.PAID, Invoice.Status.VOID):
            self.invoice(patient, Decimal('9000.00'), status=status)
        metric = self.metrics(self.dashboard())['invoices']
        self.assertEqual(metric['value'], 'R 1,000.30')
        self.assertEqual(metric['detail'], '2 issued invoices · all dates')
        self.assertIn('status__exact=issued', metric['url'])

    def test_workload_excludes_closed_tasks_reviewed_labs_and_dispatched_shipments(self):
        patient = self.patient()
        for status, due_at in (
            (ClinicalTask.Status.OPEN, self.now - timedelta(seconds=1)),
            (ClinicalTask.Status.IN_PROGRESS, self.now), (ClinicalTask.Status.OPEN, None),
            (ClinicalTask.Status.DONE, self.now - timedelta(days=1)),
            (ClinicalTask.Status.CANCELLED, self.now - timedelta(days=1)),
        ):
            ClinicalTask.objects.create(company=self.company, patient=patient, title='Private task',
                                        status=status, due_at=due_at)
        for status in LabRequest.Status.values:
            LabRequest.objects.create(company=self.company, patient=patient, requested_by=self.superuser,
                                      panel_name='Private panel', status=status)
        for status in Shipment.Status.values:
            Shipment.objects.create(company=self.company, patient=patient, scheduled_for=self.today, status=status)
        self.assertEqual([row['count'] for row in self.dashboard()['workload']], [3, 1, 1, 3])

    def test_empty_database_produces_zeroes_without_fake_chart_data(self):
        data = self.dashboard()
        self.assertEqual([item['value'] for item in data['metrics']], ['0', '0', '0', 'R 0.00'])
        self.assertFalse(data['activity']['has_data'])
        self.assertEqual(data['appointment_total'], 0)
        self.assertTrue(all(row['percent'] == 0 for row in data['appointment_statuses']))

    def test_aggregation_query_count_does_not_grow_with_date_buckets_or_record_count(self):
        with CaptureQueriesContext(connection) as short:
            self.dashboard(days=30)
        for offset in range(40):
            self.appointment(self.patient(created_at=self.now - timedelta(days=offset)),
                             starts_at=self.now - timedelta(days=offset))
        with CaptureQueriesContext(connection) as long:
            self.dashboard(days=180)
        self.assertEqual(len(short), len(long))
        self.assertLessEqual(len(long), 10)

    @override_settings(DEBUG=True)
    def test_admin_index_renders_branded_template_with_scoped_chart_data(self):
        self.patient()
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('admin:index'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin/meridian_index.html')
        self.assertEqual(response.context['meridian_dashboard']['metrics'][0]['value'], '1')
        self.assertNotContains(response, 'PRIVATE_FIRST_SENTINEL')
        self.assertNotContains(response, 'PRIVATE_LAST_SENTINEL')
