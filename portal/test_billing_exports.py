"""Billing downloads reuse the tables' scoped filters and read-only calculations."""
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal
from io import BytesIO

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from care.models import (AuditEvent, DebitMandate, MedicationDose, PatientSubscription, Payment, PracticeSettings,
                         Shipment, StockMovement, SubscriptionCycle, TreatmentAuthorization)
from practices.models import CompanyMembership
from .billing_exports import billing_table
from .test_operations import OperationsPortalFixture


class BillingExportTests(OperationsPortalFixture):
    def table(self, key, *, actor=None, membership=None, **params):
        actor = actor or self.admin
        request = RequestFactory().get('/table-export/', params)
        request.user = actor
        membership = membership or CompanyMembership.objects.filter(company=self.company, user=actor).first()
        return billing_table(key, request, self.company, membership)

    def records(self, table):
        return [dict(zip(table.headings, row)) for row in table.rows]

    def priced_course(self):
        dose = MedicationDose.objects.create(company=self.company, pen=self.product, amount_mg='1',
                                              price=Decimal('123.45'), billing_weeks=4)
        self.auth.dose = dose
        self.auth.save(update_fields=('dose', 'updated_at'))
        return dose

    def extra_plan(self, *, company=None, patient=None, **values):
        return PatientSubscription.objects.create(company=company or self.company, patient=patient or self.patient,
                                                   plan_name=values.pop('plan_name', 'Export plan'), **values)

    def test_unknown_key_returns_none_before_any_permission_guard(self):
        request = RequestFactory().get('/table-export/')
        request.user = self.doctor
        self.assertIsNone(billing_table('shipping', request, self.company, None))

    def test_subscription_progress_and_typed_values_match_the_list(self):
        self.priced_course()
        for status, offset in ((Shipment.Status.DISPATCHED, -2), (Shipment.Status.DELIVERED, -1),
                               (Shipment.Status.DRAFT, 2), (Shipment.Status.READY, 3),
                               (Shipment.Status.CANCELLED, 1)):
            self.shipment(authorization=self.auth, status=status, scheduled_for=self.today + timedelta(days=offset))
        other = TreatmentAuthorization.objects.create(company=self.company, patient=self.patient, product=self.open_product,
            prescribed_by=self.doctor, max_dose='Different course', quantity_per_cycle=1,
            starts_on=self.today, expires_on=self.today + timedelta(days=30))
        self.shipment(authorization=other, scheduled_for=self.today)
        row = self.records(self.table('subscriptions'))[0]
        page = self.page('treatment-subscriptions')
        displayed = page.context['page_obj'][0]
        self.assertEqual((displayed.supply_dispatched, displayed.supply_remaining), (2, 2))
        self.assertEqual(row['Supply progress'], '2 dispatched · 2 remaining')
        self.assertEqual(row['Next shipment'], displayed.next_shipment.scheduled_for)
        self.assertEqual(row['Next shipment status'], displayed.next_shipment.get_status_display())
        self.assertEqual(row['Started'], displayed.starts_on)
        self.assertEqual(row['Reference amount'], Decimal('123.45'))
        self.assertEqual(row['Reference billing interval'], 'every 4 weeks')

    def test_subscription_filters_export_every_page_and_no_foreign_plans(self):
        plans = [self.extra_plan(plan_name=f'MATCH {index:02d}', status='active' if index % 2 else 'paused')
                 for index in range(23)]
        self.extra_plan(company=self.beta, patient=self.beta_patient, plan_name='MATCH PRIVATE')
        self.extra_plan(plan_name='MATCH CANCELLED', status='cancelled')
        params = {'q': 'MATCH', 'status': ['active', 'paused'], 'page': 2}
        rows = self.records(self.table('subscriptions', **params))
        page = self.page('treatment-subscriptions', **params)
        self.assertEqual(page.context['page_obj'].paginator.count, 23)
        self.assertEqual(len(page.context['page_obj']), 3)
        self.assertEqual([row['Plan'] for row in rows], [plan.plan_name for plan in reversed(plans)])
        comma = self.records(self.table('subscriptions', q='MATCH', status='active,paused'))
        self.assertEqual(comma, rows)

    def test_billing_state_amounts_dates_and_mandate_details_match_the_list(self):
        timestamp = datetime(2026, 11, 2, 22, 45, tzinfo=datetime_timezone.utc)
        self.cycle.paid_at = timestamp
        self.cycle.save(update_fields=('paid_at', 'updated_at'))
        self.plan.next_debit_on = self.today + timedelta(days=7)
        self.plan.save(update_fields=('next_debit_on', 'updated_at'))
        DebitMandate.objects.create(company=self.company, patient=self.patient, subscription=self.plan,
            method='debicheck', status='active', amount='150.50', provider='sandbox', account_label='Bank ···1234')
        SubscriptionCycle.objects.create(company=self.company, patient=self.patient, subscription=self.plan,
            cycle_number=2, starts_on=self.today, due_on=self.today, amount='75.25', attempts=1,
            next_attempt_on=self.today + timedelta(days=2))
        with timezone.override('UTC'):
            row = self.records(self.table('billing', state='retrying'))[0]
        page = self.page('ops-billing', state='retrying')
        displayed = page.context['page_obj'][0]
        self.assertEqual(row['Billing'], 'Retrying')
        self.assertEqual(row['Billing detail'], displayed.state_text)
        self.assertEqual(row['Debit order'], 'DebiCheck debit order')
        self.assertEqual(row['Debit order status'], 'Active')
        self.assertEqual(row['Account'], 'Bank ···1234')
        self.assertEqual(row['Debit order limit'], Decimal('150.50'))
        self.assertEqual(row['Last paid'], Decimal('100.00'))
        self.assertEqual(row['Last paid date'], date(2026, 11, 3))
        self.assertEqual(row['Next debit'], self.plan.next_debit_on)
        self.assertEqual(row['Outstanding'], displayed.owed)
        self.assertEqual(row['Outstanding'], Decimal('75.25'))

    def test_billing_export_keeps_all_matching_rows_beyond_fifty_row_page(self):
        for _ in range(51):
            self.extra_plan()
        self.extra_plan(status='cancelled')
        self.extra_plan(company=self.beta, patient=self.beta_patient)
        params = dict(q='Alpha', state='no_mandate', page=2)
        rows = self.records(self.table('billing', **params))
        page = self.page('ops-billing', **params)
        self.assertEqual(page.context['page_obj'].paginator.count, 52)
        self.assertEqual(len(page.context['page_obj']), 2)
        self.assertEqual(len(rows), 52)
        self.assertTrue(all(row['Patient'] == str(self.patient) for row in rows))
        self.assertTrue(all(row['Billing'] == 'No debit order' for row in rows))

    def test_invalid_filters_raise_validation_errors_instead_of_exporting_everything(self):
        for table, params in (('subscriptions', {'status': 'not-real'}), ('billing', {'state': 'not-real'}),
                              ('subscriptions', {'q': 'x' * 101}), ('billing', {'q': 'x' * 101})):
            with self.subTest(table=table, params=params), self.assertRaises(ValidationError):
                self.table(table, **params)

    def test_cycles_include_ordered_attempts_and_only_the_selected_patient_practice(self):
        cycle = SubscriptionCycle.objects.create(company=self.company, patient=self.patient, subscription=self.plan,
            cycle_number=2, starts_on=self.today, due_on=self.today, amount='87.65', attempts=2,
            next_attempt_on=self.today + timedelta(days=1))
        Payment.objects.create(company=self.company, patient=self.patient, subscription=self.plan,
            subscription_cycle=cycle, amount='87.65', due_on=self.today, attempt=1, method='eft',
            status='failed', failure_reason='Unpaid transfer', recorded_by=self.admin)
        Payment.objects.create(company=self.company, patient=self.patient, subscription=self.plan,
            subscription_cycle=cycle, amount='87.65', due_on=self.today, attempt=2, method='card', status='pending')
        Payment.objects.create(company=self.beta, patient=self.beta_patient, subscription_cycle=cycle,
            amount='99.00', due_on=self.today, status='failed', failure_reason='PRIVATE FOREIGN ATTEMPT')
        rows = self.records(self.table('billing-cycles', pk=self.plan.pk))
        self.assertEqual([row['Cycle'] for row in rows], [2, 1])
        row = rows[0]
        self.assertEqual((row['Period start'], row['Period end'], row['Amount']),
                         (self.today, self.today, Decimal('87.65')))
        self.assertEqual((row['Status'], row['Next attempt']), ('Retrying', self.today + timedelta(days=1)))
        self.assertIn('1. EFT · Failed: Unpaid transfer', row['Attempts'])
        self.assertIn('2. Card · Pending', row['Attempts'])
        self.assertNotIn('PRIVATE FOREIGN ATTEMPT', row['Attempts'])
        page = self.page('ops-billing-detail', [self.plan.pk])
        self.assertEqual(page.context['cycles_count'], 2)
        self.assertContains(page, page.context['cycles_export_url'])
        self.assertNotContains(page, 'PRIVATE FOREIGN ATTEMPT')

    def test_other_payments_export_all_rows_with_notes_amounts_and_sast_dates(self):
        timestamp = datetime(2026, 11, 2, 22, 45, tzinfo=datetime_timezone.utc)
        for index in range(25):
            Payment.objects.create(company=self.company, patient=self.patient, amount='12.34', due_on=self.today,
                paid_at=timestamp, kind='consultation', status='paid', note=f'External payment {index:02d}')
        Payment.objects.create(company=self.company, patient=self.other_patient, amount='99.00', due_on=self.today,
                               note='PRIVATE OTHER PATIENT')
        Payment.objects.create(company=self.beta, patient=self.beta_patient, amount='99.00', due_on=self.today,
                               note='PRIVATE OTHER PRACTICE')
        rows = self.records(self.table('billing-payments', pk=self.plan.pk))
        self.assertEqual(len(rows), 25)
        self.assertEqual(rows[0]['Note'], 'External payment 24')
        self.assertEqual(rows[0]['Amount'], Decimal('12.34'))
        self.assertEqual(rows[0]['Date'], date(2026, 11, 3))
        self.assertEqual(rows[0]['For'], 'Consultation')
        self.assertEqual(rows[0]['Status'], 'Paid')
        page = self.page('ops-billing-detail', [self.plan.pk])
        self.assertContains(page, page.context['other_payments_export_url'])
        self.assertEqual(page.context['other_payments_count'], len(page.context['other_payments']))

    def test_detail_selectors_and_permissions_are_scoped(self):
        foreign = self.extra_plan(company=self.beta, patient=self.beta_patient)
        malformed = self.extra_plan(patient=self.beta_patient)
        for table in ('billing-cycles', 'billing-payments'):
            for pk in ('', 'bad', 999999, foreign.pk, malformed.pk):
                with self.subTest(table=table, pk=pk), self.assertRaises(Http404):
                    self.table(table, pk=pk)
        for table in ('subscriptions', 'billing', 'billing-cycles', 'billing-payments'):
            for actor in (self.doctor, self.patient_user):
                with self.subTest(table=table, actor=actor.pk), self.assertRaises(PermissionDenied):
                    self.table(table, actor=actor, pk=self.plan.pk)
        self.assertEqual(len(self.records(self.table('subscriptions', actor=self.super_admin))), 1)

    def test_adapters_and_course_summaries_do_not_write_billing_or_stock_records(self):
        self.priced_course()
        self.shipment(authorization=self.auth)
        models = (PatientSubscription, SubscriptionCycle, DebitMandate, Payment, Shipment, StockMovement, AuditEvent)
        before = {model: list(model.objects.order_by('pk').values()) for model in models}
        for table in ('subscriptions', 'billing', 'billing-cycles', 'billing-payments'):
            self.records(self.table(table, pk=self.plan.pk))
        self.assertEqual({model: list(model.objects.order_by('pk').values()) for model in models}, before)

    def test_generic_download_has_typed_literal_cells_and_remains_admin_only_when_doctors_ship(self):
        self.priced_course()
        self.plan.plan_name = '=1+1'
        self.plan.save(update_fields=('plan_name', 'updated_at'))
        url = reverse('portal:ops-table-export', kwargs={'table': 'subscriptions'})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        content = b''.join(response.streaming_content)
        workbook = load_workbook(BytesIO(content))
        self.addCleanup(workbook.close)
        headings = [cell.value for cell in workbook.active[1]]
        row = dict(zip(headings, workbook.active[2]))
        self.assertEqual((row['Plan'].value, row['Plan'].data_type), ('=1+1', 's'))
        self.assertEqual((row['Reference amount'].value, row['Reference amount'].data_type), (123.45, 'n'))
        self.assertEqual((row['Started'].value, row['Started'].data_type), (self.plan.starts_on, 'd'))
        PracticeSettings.objects.filter(company=self.company).update(doctors_ship=True)
        for actor in (self.doctor, self.patient_user):
            self.login(actor)
            for table in ('subscriptions', 'billing', 'billing-cycles', 'billing-payments'):
                with self.subTest(actor=actor.pk, table=table):
                    response = self.client.get(reverse('portal:ops-table-export', kwargs={'table': table}), {'pk': self.plan.pk})
                    self.assertEqual(response.status_code, 403)
