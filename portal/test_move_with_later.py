"""Change this parcel, with everything after it (James, 25 Nov 2026): the shipment page offers the choice for a parcel its
prescription planned, and the patient sees what moved in Prescription changes."""

from datetime import timedelta

from django.contrib.messages import get_messages
from django.urls import reverse

from care.models import PatientEvent, Shipment, ShipmentItem, TreatmentAuthorization
from care.parcels import schedule_parcels

from .patient_summary import prescription_changes
from .test_billing import BillingPageFixture

WEEK = timedelta(weeks=1)


class MoveWithLaterPageTests(BillingPageFixture):
    def setUp(self):
        super().setUp()
        self.first = self.first_parcel
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(expires_on=self.first + 24 * WEEK)
        self.auth.refresh_from_db()
        schedule_parcels(self.auth)
        self.approved()

    def test_the_shipment_page_moves_a_parcel_and_everything_after_it(self):
        second = Shipment.objects.get(scheduled_for=self.first + 4 * WEEK)
        item = second.items.get()
        self.login(self.admin)
        page = self.client.get(reverse('portal:ops-shipment-detail', args=[second.pk]))
        self.assertContains(page, 'Only this parcel')
        self.assertContains(page, 'This parcel and everything after it')
        url = reverse('portal:ops-shipment-change', args=[second.pk])
        data = {'workflow_context': page.context['change_context'], 'scheduled_for': self.first + 3 * WEEK,
                f'pens_{item.pk}': item.quantity, 'move': 'later'}
        # Not by whole weeks: refused, and nothing moved.
        refused = self.client.post(url, {**data, 'scheduled_for': self.first + 3 * WEEK + timedelta(days=1)})
        self.assertContains(refused, 'move this one by whole weeks', status_code=400)
        self.assertEqual(Shipment.objects.get(pk=second.pk).scheduled_for, self.first + 4 * WEEK)
        page = self.client.get(reverse('portal:ops-shipment-detail', args=[second.pk]))
        response = self.client.post(url, {**data, 'workflow_context': page.context['change_context']})
        self.assertRedirects(response, reverse('portal:ops-shipment-detail', args=[second.pk]), fetch_redirect_response=False)
        self.assertEqual([str(message) for message in get_messages(response.wsgi_request)], [
            f'Parcel moved a week earlier with the 4 parcels after it. Next payment {self.first + 2 * WEEK:%a, %-d %b}. '
            f'Prescription now runs to {self.first + 23 * WEEK:%a, %-d %b %Y}. Confirm & lock it again when it is ready for shipping.'])
        self.assertEqual(list(Shipment.objects.exclude(status=Shipment.Status.CANCELLED).order_by('scheduled_for')
                              .values_list('scheduled_for', flat=True)),
                         [self.first + weeks * WEEK for weeks in (0, 3, 7, 11, 15, 19)])

        # The patient's Prescription changes: what moved, and the weeks the doctor prescribed, to the new end.
        changes = prescription_changes(self.company, self.patient, TreatmentAuthorization.objects.get(pk=self.auth.pk))
        moved = [change for change in changes if change['kind'] == 'update']
        self.assertEqual([(change['title'], change['change']) for change in moved],
                         [('Parcels moved', PatientEvent.objects.get(title='Parcels moved').detail)])
        started = next(change for change in changes if change['kind'] != 'update')
        self.assertIn(('Prescribed', f'24 weeks, to {self.first + 23 * WEEK:%a, %-d %b %Y}', None), started['facts'])
        self.login(self.patient_user)
        self.assertContains(self.client.get(reverse('portal:patient-treatment'), {'changes': 1}), 'Parcels moved')

    def test_only_this_parcel_by_default_and_no_choice_without_a_prescription(self):
        second = Shipment.objects.get(scheduled_for=self.first + 4 * WEEK)
        item = second.items.get()
        self.login(self.admin)
        page = self.client.get(reverse('portal:ops-shipment-detail', args=[second.pk]))
        self.assertEqual(page.context['change_form']['move'].value(), 'this')
        self.client.post(reverse('portal:ops-shipment-change', args=[second.pk]), {
            'workflow_context': page.context['change_context'], 'scheduled_for': self.first + 3 * WEEK,
            f'pens_{item.pk}': item.quantity, 'move': 'this'})
        self.assertEqual(Shipment.objects.get(scheduled_for=self.first + 8 * WEEK).status, Shipment.Status.DRAFT)
        self.assertFalse(PatientEvent.objects.filter(title='Parcels moved').exists())
        # A parcel no prescription planned only moves itself.
        loose = Shipment.objects.create(company=self.company, patient=self.patient, scheduled_for=self.first, cycle_number=1)
        ShipmentItem.objects.create(company=self.company, shipment=loose, product=self.dose.pen, quantity=1)
        page = self.client.get(reverse('portal:ops-shipment-detail', args=[loose.pk]))
        self.assertNotIn('move', page.context['change_form'].fields)
        self.assertNotContains(page, 'This parcel and everything after it')
