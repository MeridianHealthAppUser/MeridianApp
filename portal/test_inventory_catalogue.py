"""Catalogue belongs to Inventory while existing URLs, tenant scope and editing rights remain intact."""
from html.parser import HTMLParser

from django.urls import resolve, reverse

from care.models import MedicationDose, PracticeSettings, StartingPlan, StartingPlanStep
from .test_operations import OperationsPortalFixture


class NavigationLinks(HTMLParser):
    def __init__(self, label):
        super().__init__()
        self.label, self.depth, self.links, self.current = label, 0, [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'nav':
            if self.depth:
                self.depth += 1
            elif attrs.get('aria-label') == self.label:
                self.depth = 1
        if self.depth and tag == 'a':
            self.current = dict(attrs, text=[])

    def handle_data(self, data):
        if self.current is not None:
            self.current['text'].append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.current['text'] = ' '.join(''.join(self.current['text']).split())
            self.links.append(self.current)
            self.current = None
        if tag == 'nav' and self.depth:
            self.depth -= 1


class InventoryCatalogueTests(OperationsPortalFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.catalogue_dose = MedicationDose.objects.create(company=cls.company, pen=cls.product, amount_mg=1, price=100)
        cls.starting_plan = StartingPlan.objects.create(company=cls.company, medicine=cls.product.name,
            name='Local catalogue plan', then_dose=cls.catalogue_dose, default_weeks=24)
        StartingPlanStep.objects.create(company=cls.company, plan=cls.starting_plan, dose=cls.catalogue_dose,
                                       position=1, weeks=4)
        cls.beta_dose = MedicationDose.objects.create(company=cls.beta, pen=cls.beta_product, amount_mg=2, price=200)
        cls.beta_plan = StartingPlan.objects.create(company=cls.beta, medicine=cls.beta_product.name,
            name='Private beta starting plan', then_dose=cls.beta_dose, default_weeks=24)

    def links(self, response, label):
        parser = NavigationLinks(label)
        parser.feed(response.content.decode())
        return parser.links

    def editor_routes(self):
        return (('ops-product-detail', self.product.pk), ('ops-dose-detail', self.catalogue_dose.pk),
                ('ops-starting-plan-detail', self.starting_plan.pk))

    def assert_inventory_sidebar_active(self, response):
        links = self.links(response, 'Staff pages')
        inventory = next(link for link in links if link.get('href') == reverse('portal:inventory'))
        self.assertIn(inventory.get('aria-current'), ('page', 'true'))
        operations = [link for link in links if link.get('href') == reverse('portal:treatment-subscriptions')]
        self.assertTrue(all('aria-current' not in link for link in operations))
        # Catalogue is a tab of Inventory, never a menu row of its own: a Super Admin's menu lists it once, among the
        # Inventory tabs that open beside Inventory on hover (James, 6 Nov 2026); the flat menu leaves it out.
        catalogue = [link for link in links if link.get('href') == reverse('portal:ops-catalogue')]
        self.assertEqual(len(catalogue), 1 if response.context['active_membership'].role == 'super_admin' else 0)

    def test_inventory_subtabs_have_consistent_destinations_and_active_states(self):
        pages = (('inventory', {}, 'Inventory'), ('ops-catalogue', {}, 'Catalogue'),
                 ('inventory-consumption', {}, 'Consumption'))
        for actor in (self.admin, self.super_admin):
            self.login(actor)
            for route, params, active in pages:
                with self.subTest(actor=actor.email, route=route, active=active):
                    response = self.page(route, **params)
                    tabs = self.links(response, 'Inventory views')
                    self.assertEqual([tab['text'] for tab in tabs], ['Inventory', 'Consumption', 'Catalogue'])
                    self.assertEqual([tab['text'] for tab in tabs if tab.get('aria-current') == 'page'], [active])
                    self.assertEqual(tabs[0]['href'], reverse('portal:inventory'))
                    self.assertEqual(tabs[1]['href'], reverse('portal:inventory-consumption'))
                    self.assertEqual(tabs[2]['href'], reverse('portal:ops-catalogue'))
                    self.assert_inventory_sidebar_active(response)

    def test_catalogue_has_inventory_heading_breadcrumb_and_no_operations_tabs(self):
        for actor in (self.admin, self.super_admin):
            self.login(actor)
            response = self.page('ops-catalogue')
            self.assertContains(response, '<h1>Inventory</h1>', html=True)
            self.assertNotContains(response, '<h1>Operations</h1>', html=True)
            self.assertContains(response, '<span aria-current="page">Inventory</span>', html=True)
            self.assertFalse(self.links(response, 'Operations sections'))
            operations = self.page('ops-shipping', shipping_step='all')
            tabs = self.links(operations, 'Operations sections')
            self.assertNotIn('Catalogue', [tab['text'] for tab in tabs])
            self.assertFalse(any(tab.get('href') == reverse('portal:ops-catalogue') for tab in tabs))

    def test_editor_breadcrumbs_go_through_inventory_catalogue_and_back_to_catalogue(self):
        for actor in (self.admin, self.super_admin):
            self.login(actor)
            for route, pk in self.editor_routes():
                with self.subTest(actor=actor.email, route=route):
                    response = self.page(route, [pk])
                    breadcrumbs = self.links(response, 'Breadcrumb')
                    paths = [link.get('href') for link in breadcrumbs]
                    self.assertIn(reverse('portal:inventory'), paths)
                    self.assertIn(reverse('portal:ops-catalogue'), paths)
                    inventory_position = paths.index(reverse('portal:inventory'))
                    catalogue_position = max(index for index, path in enumerate(paths) if path == reverse('portal:ops-catalogue'))
                    self.assertLess(inventory_position, catalogue_position)
                    back = next(link for link in breadcrumbs if 'page-breadcrumbs__back' in link.get('class', '').split())
                    self.assertEqual(back['href'], reverse('portal:ops-catalogue'))
                    self.assert_inventory_sidebar_active(response)

    def test_super_admin_edit_and_practice_admin_read_permissions_are_preserved(self):
        self.login(self.super_admin)
        for route, pk in self.editor_routes():
            with self.subTest(role='super_admin', route=route):
                response = self.page(route, [pk])
                self.assertTrue(response.context['can_edit'])
        for route in ('ops-product-create', 'ops-dose-create', 'ops-starting-plan-create'):
            self.assertEqual(self.client.get(reverse(f'portal:{route}')).status_code, 200)
        self.login(self.admin)
        for route, pk in self.editor_routes():
            with self.subTest(role='practice_admin', route=route):
                response = self.page(route, [pk])
                self.assertFalse(response.context['can_edit'])
                self.assertEqual(self.client.post(reverse(f'portal:{route}', args=[pk]), {}).status_code, 403)
        for route in ('ops-product-create', 'ops-dose-create'):
            self.assertEqual(self.client.get(reverse(f'portal:{route}')).status_code, 403)
        # This existing starting-plan form is readable by an administrator, but saving remains Super Admin-only.
        response = self.page('ops-starting-plan-create')
        self.assertFalse(response.context['can_edit'])
        self.assertEqual(self.client.post(reverse('portal:ops-starting-plan-create'), {}).status_code, 403)

    def test_clinicians_cannot_reach_inventory_catalogue_even_when_they_can_ship(self):
        PracticeSettings.objects.filter(company=self.company).update(doctors_ship=True)
        self.login(self.doctor)
        for route in ('inventory', 'inventory-consumption', 'ops-catalogue', 'ops-product-create', 'ops-dose-create', 'ops-starting-plan-create'):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(f'portal:{route}')).status_code, 403)
        for route, pk in self.editor_routes():
            self.assertEqual(self.client.get(reverse(f'portal:{route}', args=[pk])).status_code, 403)

    def test_catalogue_and_editor_records_are_scoped_to_active_practice(self):
        self.login(self.super_admin)
        response = self.page('ops-catalogue')
        self.assertContains(response, self.product.name)
        self.assertContains(response, self.starting_plan.name)
        self.assertNotContains(response, self.beta_product.name)
        self.assertNotContains(response, self.beta_plan.name)
        for route, pk in (('ops-product-detail', self.beta_product.pk), ('ops-dose-detail', self.beta_dose.pk),
                          ('ops-starting-plan-detail', self.beta_plan.pk)):
            self.assertEqual(self.client.get(reverse(f'portal:{route}', args=[pk])).status_code, 404)
        self.login(self.beta_admin, company=self.beta)
        response = self.page('ops-catalogue')
        self.assertContains(response, self.beta_product.name)
        self.assertNotContains(response, self.product.name)
        for route, pk in self.editor_routes():
            self.assertEqual(self.client.get(reverse(f'portal:{route}', args=[pk])).status_code, 404)

    def test_catalogue_urls_remain_stable(self):
        expected = (
            ('ops-catalogue', (), '/catalogue/'), ('ops-product-create', (), '/catalogue/new/'),
            ('ops-product-detail', (self.product.pk,), f'/catalogue/{self.product.pk}/'),
            ('ops-dose-create', (), '/catalogue/doses/new/'),
            ('ops-dose-detail', (self.catalogue_dose.pk,), f'/catalogue/doses/{self.catalogue_dose.pk}/'),
            ('ops-starting-plan-create', (), '/catalogue/starting-plans/new/'),
            ('ops-starting-plan-detail', (self.starting_plan.pk,), f'/catalogue/starting-plans/{self.starting_plan.pk}/'),
        )
        for name, args, path in expected:
            with self.subTest(route=name):
                self.assertEqual(reverse(f'portal:{name}', args=args), path)
                self.assertEqual(resolve(path).view_name, f'portal:{name}')
