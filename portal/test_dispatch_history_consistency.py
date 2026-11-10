"""Dispatch history uses the same saved-dispatch table in Shipping and patient records."""
from datetime import timedelta
from io import BytesIO
from openpyxl import load_workbook
from django.urls import reverse
from django.utils import timezone
from care.models import Shipment
from .test_operations import OperationsPortalFixture
from .test_shipping_stages import TableHeaders


class DispatchHistoryConsistencyTests(OperationsPortalFixture):
    def test_patient_history_matches_shipping_and_excludes_planned_or_other_patient_parcels(self):
        parcel = self.shipment(status=Shipment.Status.DISPATCHED, dispatched_at=timezone.now(),
            note='Sent with cold packaging', dispatch_snapshot={
                'patient_name': 'Patient name at dispatch',
                'delivery_address': {'lines': ['Original delivery address'], 'phone': '0123456789', 'recipient': 'Recipient'},
                'items': [{'product': 'Product at dispatch', 'quantity': 2, 'dose': 'Recorded dose'}]})
        self.shipment(scheduled_for=self.today + timedelta(days=30), note='FUTURE_PARCEL')
        self.shipment(status=Shipment.Status.READY, locked_at=timezone.now(), note='AWAITING_COLLECTION')
        self.shipment(company=self.beta, patient=self.beta_patient, product=self.beta_product, subscription=None,
                      status=Shipment.Status.DISPATCHED, dispatched_at=timezone.now(), note='OTHER_PRACTICE')
        shipping = self.page('ops-history')
        self.login(self.super_admin)
        patient = self.page('patient-detail', [self.patient.pk], tab='deliveries')
        self.assertEqual([row.pk for row in patient.context['rows']], [parcel.pk])
        for response in (shipping, patient):
            self.assertTemplateUsed(response, 'portal/includes/dispatch_history_table.html')
            headers = TableHeaders()
            headers.feed(response.content.decode())
            self.assertEqual(headers.headers, ['Patient', 'Doctor', 'Delivered to', 'Parcel', 'Dispatched', 'Status', 'Note', 'Actions'])
            for text in ('Patient name at dispatch', 'Original delivery address', 'Product at dispatch', 'Recorded dose', 'Sent with cold packaging'):
                self.assertContains(response, text)
            self.assertNotContains(response, 'FUTURE_PARCEL')
            self.assertNotContains(response, 'AWAITING_COLLECTION')
            self.assertNotContains(response, 'OTHER_PRACTICE')
        self.assertContains(patient, f'/shipping/{parcel.pk}/?workspace=patient')
        filters = patient.context['workspace_filter'].fields['status'].choices
        self.assertEqual([key for key, _ in filters], ['dispatched', 'delivered'])

        export = self.client.get(reverse('portal:staff-patient-record-workbook', args=[self.patient.pk]))
        self.assertEqual(export.status_code, 200)
        workbook = load_workbook(BytesIO(export.content), read_only=True)
        self.addCleanup(workbook.close)
        exported = list(workbook['Dispatch history'].values)
        self.assertIn(('Patient', 'Doctor', 'Delivered to', 'Parcel', 'Dispatched', 'Status', 'Note'), exported)
        values = [str(value) for row in exported for value in row if value is not None]
        self.assertIn('Patient name at dispatch', values)
        self.assertTrue(any('Product at dispatch' in value for value in values))
        self.assertNotIn('FUTURE_PARCEL', values)
        self.assertNotIn('AWAITING_COLLECTION', values)
        self.assertNotIn('OTHER_PRACTICE', values)
