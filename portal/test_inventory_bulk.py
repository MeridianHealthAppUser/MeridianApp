"""Inventory receipts accept several medications together without partial or duplicate stock entries."""
from django.urls import reverse
from django.core.exceptions import ValidationError
from care.inventory import receive_stock_batch

from care.models import StockMovement
from .test_operations import OperationsPortalFixture


class InventoryBulkReceiptTests(OperationsPortalFixture):
    def line(self, product=None, **changes):
        values = dict(product=(product or self.product).pk, quantity=7, batch='BULK-A',
                      expires_on='2030-01-01')
        values.update(changes)
        return values

    def receipt_data(self, lines, *, token=None, reference='Bulk delivery 17'):
        data = {
            'workflow_context': token or self.page('inventory').context['workflow_context'],
            'reference': reference,
            'items-TOTAL_FORMS': len(lines),
            'items-INITIAL_FORMS': 0,
            'items-MIN_NUM_FORMS': 1,
            'items-MAX_NUM_FORMS': 100,
        }
        for index, line in enumerate(lines):
            data.update({f'items-{index}-{name}': value for name, value in line.items()})
        return data

    def receipts(self, reference='Bulk delivery 17'):
        return StockMovement.objects.filter(company=self.company, reason=reference).order_by('pk')

    def test_multiple_medications_are_received_together_with_shared_reference(self):
        lines = [self.line(), self.line(self.open_product, batch='BULK-B', quantity=12)]
        response = self.client.post(reverse('portal:inventory'), self.receipt_data(lines))
        self.assertRedirects(response, reverse('portal:inventory'), fetch_redirect_response=False)
        self.assertEqual(list(self.receipts().values_list('product_id', 'quantity', 'batch')),
                         [(self.product.pk, 7, 'BULK-A'), (self.open_product.pk, 12, 'BULK-B')])

    def test_invalid_second_medication_does_not_receive_first_and_preserves_inputs(self):
        lines = [self.line(), self.line(self.open_product, quantity=0, batch='BULK-B')]
        response = self.client.post(reverse('portal:inventory'), self.receipt_data(lines))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.receipts().exists())
        self.assertTrue(response.context['receipt_open'])
        self.assertEqual(response.context['formset'].forms[0]['batch'].value(), 'BULK-A')
        self.assertEqual(response.context['receipt_form']['reference'].value(), 'Bulk delivery 17')

    def test_other_practice_medication_cannot_be_received_as_part_of_batch(self):
        lines = [self.line(), self.line(self.beta_product, batch='FOREIGN')]
        response = self.client.post(reverse('portal:inventory'), self.receipt_data(lines))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.receipts().exists())
        self.assertFalse(StockMovement.objects.filter(company=self.company, product=self.beta_product).exists())

    def test_replaying_same_receipt_does_not_add_any_medication_twice(self):
        data = self.receipt_data([self.line(), self.line(self.open_product, batch='BULK-B')])
        for _ in range(2):
            response = self.client.post(reverse('portal:inventory'), data)
            self.assertEqual(response.status_code, 302)
        self.assertEqual(list(self.receipts().values_list('product_id', 'quantity')),
                         [(self.product.pk, 7), (self.open_product.pk, 7)])

    def test_same_medication_can_be_received_in_two_different_batches(self):
        data = self.receipt_data([self.line(), self.line(batch='BULK-B')])
        response = self.client.post(reverse('portal:inventory'), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(self.receipts().values_list('batch', flat=True)), ['BULK-A', 'BULK-B'])

    def test_removed_row_is_ignored_even_if_its_values_are_incomplete(self):
        data = self.receipt_data([self.line(), {'product': '', 'quantity': 0, 'DELETE': 'on'}])
        response = self.client.post(reverse('portal:inventory'), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.receipts().count(), 1)
        self.assertEqual(self.receipts().get().batch, 'BULK-A')

    def test_empty_receipt_or_removing_every_row_requires_a_medication(self):
        for lines in ([], [{}], [self.line(DELETE='on')]):
            with self.subTest(lines=lines):
                response = self.client.post(reverse('portal:inventory'), self.receipt_data(lines))
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.context['receipt_open'])
                self.assertFalse(self.receipts().exists())

    def test_add_stock_modal_is_closed_on_inventory_and_opened_by_fallback_link(self):
        response = self.page('inventory')
        self.assertFalse(response.context['receipt_open'])
        self.assertContains(response, 'data-modal-open=')
        response = self.page('inventory', receive='1')
        self.assertTrue(response.context['receipt_open'])

    def test_service_failure_after_first_row_rolls_back_complete_receipt(self):
        before = StockMovement.objects.count()
        items = [
            dict(product=self.product, quantity=7, batch='BULK-A'),
            dict(product=self.beta_product, quantity=9, batch='FOREIGN'),
        ]
        with self.assertRaises(ValidationError):
            receive_stock_batch(company=self.company, actor=self.admin, items=items,
                                reference='Bulk delivery 17', submission_key='failed-receipt')
        self.assertEqual(StockMovement.objects.count(), before)
        self.assertFalse(self.receipts().exists())
