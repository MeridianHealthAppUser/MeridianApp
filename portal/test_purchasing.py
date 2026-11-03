"""Purchasing is separate from Inventory; removed planning URLs and receipt fields have safe fallbacks."""
from urllib.parse import parse_qs, urlsplit

from django.urls import NoReverseMatch, reverse

from care.models import Invoice, PharmacyOrder, Shipment, StockMovement, SubscriptionCycle
from .test_inventory_catalogue import NavigationLinks
from .test_operations import OperationsPortalFixture


class PurchasingAndInventoryNavigationTests(OperationsPortalFixture):
    def links(self, response, label):
        parser = NavigationLinks(label)
        parser.feed(response.content.decode())
        return parser.links

    def purchasing(self, **params):
        return self.page('purchasing', **params)

    def receipt_data(self, page, *, quantity=6):
        return {
            'workflow_context': page.context['workflow_context'], 'reference': 'Inventory navigation receipt',
            'items-TOTAL_FORMS': '1', 'items-INITIAL_FORMS': '0', 'items-0-product': self.product.pk,
            'items-0-quantity': quantity, 'items-0-batch': 'INVENTORY-NAV-RECEIPT',
            'items-0-expires_on': '2030-01-01', 'return_view': 'order-plan', 'return_plan_weeks': '8',
            'return_url': 'https://example.test/untrusted-return',
        }

    def test_purchasing_tabs_have_real_tables_and_correct_active_states(self):
        for params, expected, title in (({}, 'to-order', 'To order'),
                                        ({'view': 'to-order'}, 'to-order', 'To order'),
                                        ({'view': 'on-order'}, 'on-order', 'On order'),
                                        ({'view': 'history'}, 'history', 'Purchase history'),
                                        ({'view': 'unknown'}, 'to-order', 'To order'),
                                        ({'view': 'order-plan'}, 'to-order', 'To order')):
            with self.subTest(params=params):
                response = self.purchasing(**params)
                self.assertEqual(response.context['purchasing_view'], expected)
                self.assertContains(response, '<h1>Purchasing</h1>', html=True)
                if expected == 'to-order':
                    self.assertContains(response, '<h2 id="to-order-heading">To order</h2>', html=True)
                    self.assertNotContains(response, 'purchasing-placeholder')
                    self.assertContains(response, '<th scope="col">Product</th>', html=True)
                    self.assertContains(response, response.context['table_export_url'])
                else:
                    self.assertContains(response, f'<h2 id="purchase-orders-heading">{title}</h2>', html=True)
                    self.assertNotContains(response, 'purchasing-placeholder')
                    self.assertIn('page_obj', response.context)
                    self.assertContains(response, response.context['table_export_url'])
                tabs = self.links(response, 'Purchasing views')
                self.assertEqual([tab['text'] for tab in tabs], ['To order', 'On order', 'Purchase history'])
                self.assertEqual([tab['text'] for tab in tabs if tab.get('aria-current') == 'page'], [title])
                self.assertEqual(tabs[0]['href'], reverse('portal:purchasing'))
                self.assertEqual(urlsplit(tabs[1]['href']).path, reverse('portal:purchasing'))
                self.assertEqual(parse_qs(urlsplit(tabs[1]['href']).query), {'view': ['on-order']})
                self.assertEqual(urlsplit(tabs[2]['href']).path, reverse('portal:purchasing'))
                self.assertEqual(parse_qs(urlsplit(tabs[2]['href']).query), {'view': ['history']})
                self.assertFalse(self.links(response, 'Inventory views'))
                self.assertNotContains(response, 'Order plan')
                self.assertNotContains(response, 'plan_weeks')
                self.assertNotContains(response, 'Export Excel')
                self.assertNotContains(response, 'data-modal-open="receive-stock"')
                self.assertNotContains(response, 'data-inventory-movements')

    def test_purchasing_and_inventory_keep_distinct_sidebar_active_states(self):
        for actor in (self.admin, self.super_admin):
            self.login(actor)
            for view in ('to-order', 'on-order', 'history'):
                with self.subTest(actor=actor.email, view=view):
                    response = self.purchasing(view=view)
                    sidebar = self.links(response, 'Staff pages')
                    purchase = next(link for link in sidebar if link.get('href') == reverse('portal:purchasing'))
                    inventory = next(link for link in sidebar if link.get('href') == reverse('portal:inventory'))
                    self.assertEqual(purchase.get('aria-current'), 'page')
                    self.assertNotIn('aria-current', inventory)
                    inventory_page = self.page('inventory')
                    sidebar = self.links(inventory_page, 'Staff pages')
                    purchase = next(link for link in sidebar if link.get('href') == reverse('portal:purchasing'))
                    inventory = next(link for link in sidebar if link.get('href') == reverse('portal:inventory'))
                    self.assertEqual(inventory.get('aria-current'), 'page')
                    self.assertNotIn('aria-current', purchase)

    def test_purchasing_is_admin_only_and_uses_active_practice(self):
        for actor in (self.admin, self.super_admin):
            self.login(actor)
            response = self.purchasing(company=self.beta.pk)
            self.assertEqual(response.context['company'].pk, self.company.pk)
        self.login(self.beta_admin, company=self.beta)
        for view in ('on-order', 'history'):
            response = self.purchasing(view=view)
            self.assertEqual(response.context['company'].pk, self.beta.pk)
        for actor in (self.doctor, self.patient_user):
            self.login(actor)
            for view in ('to-order', 'on-order', 'history'):
                self.assertEqual(self.client.get(reverse('portal:purchasing'), {'view': view}).status_code, 403)
        self.client.logout()
        for view in ('to-order', 'on-order', 'history'):
            self.assertEqual(self.client.get(reverse('portal:purchasing'), {'view': view}).status_code, 302)

    def test_opening_purchasing_does_not_create_orders_stock_or_billing(self):
        self.shipment(quantity=1, authorization=self.auth)
        models = (Shipment, PharmacyOrder, StockMovement, SubscriptionCycle, Invoice)
        counts = {model: model.objects.count() for model in models}
        shipments = list(Shipment.objects.values('pk', 'status', 'locked_at', 'scheduled_for', 'updated_at'))
        for view in ('to-order', 'on-order', 'history'):
            self.purchasing(view=view)
        for model, count in counts.items():
            self.assertEqual(model.objects.count(), count)
        self.assertEqual(list(Shipment.objects.values('pk', 'status', 'locked_at', 'scheduled_for', 'updated_at')), shipments)

    def test_inventory_ignores_legacy_views_and_preserves_stock_features(self):
        for view in (None, 'inventory', 'stock', 'order-plan', 'to-order', 'on-order', 'unknown'):
            with self.subTest(view=view):
                params = {'view': view, 'plan_weeks': '8'} if view else {}
                response = self.page('inventory', **params)
                self.assertEqual(response.context['inventory_view'], 'inventory')
                tabs = self.links(response, 'Inventory views')
                self.assertEqual([tab['text'] for tab in tabs], ['Inventory', 'Consumption', 'Catalogue'])
                self.assertEqual([tab['text'] for tab in tabs if tab.get('aria-current') == 'page'], ['Inventory'])
                self.assertNotIn('order_plan', response.context)
                self.assertNotIn('plan_form', response.context)
                self.assertContains(response, 'data-inventory-movements')
                self.assertContains(response, 'data-modal-open="receive-stock"')
                self.assertNotContains(response, 'Stock overview')
                self.assertNotContains(response, 'Order plan')
                self.assertNotContains(response, 'return_plan_weeks')

    def test_receipt_success_ignores_legacy_return_fields_and_returns_to_inventory(self):
        page = self.page('inventory', view='order-plan', plan_weeks=8)
        response = self.client.post(reverse('portal:inventory'), self.receipt_data(page))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('portal:inventory'))
        movement = StockMovement.objects.get(batch='INVENTORY-NAV-RECEIPT')
        self.assertEqual((movement.product_id, movement.quantity), (self.product.pk, 6))

    def test_receipt_errors_keep_inputs_and_reopen_on_inventory(self):
        page = self.page('inventory')
        data = self.receipt_data(page, quantity=0)
        data['return_view'] = 'on-order'
        before = StockMovement.objects.count()
        response = self.client.post(reverse('portal:inventory'), data)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['inventory_view'], 'inventory')
        self.assertTrue(response.context['receipt_open'])
        self.assertEqual(response.context['formset'].forms[0]['batch'].value(), 'INVENTORY-NAV-RECEIPT')
        self.assertEqual(response.context['receipt_form']['reference'].value(), 'Inventory navigation receipt')
        self.assertEqual(response.context['inventory_return_url'], reverse('portal:inventory'))
        self.assertEqual(StockMovement.objects.count(), before)

    def test_old_planning_export_is_removed_but_stock_movement_routes_remain(self):
        with self.assertRaises(NoReverseMatch):
            reverse('portal:inventory-order-plan-export')
        self.assertEqual(self.client.get('/inventory/order-plan.xlsx').status_code, 404)
        self.assertEqual(reverse('portal:inventory-movements'), '/inventory/movements/')
        self.assertEqual(reverse('portal:inventory-movements-export'), '/inventory/movements.xlsx')
        self.assertEqual(reverse('portal:purchasing'), '/purchasing/')
