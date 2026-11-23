"""Practice settings: who and where purchase orders are billed to."""
from django.contrib.auth import get_user_model
from django.urls import reverse

from care.models import AuditEvent, PracticeSettings
from practices.models import CompanyMembership
from .test_operations import OperationsPortalFixture


class PurchaseOrderSettingsTests(OperationsPortalFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        CompanyMembership.objects.filter(company=cls.company, user=cls.doctor).update(clinician_type='doctor')
        users = get_user_model().objects
        cls.dietitian = users.create_user('ops-dietitian@example.test')
        CompanyMembership.objects.create(company=cls.company, user=cls.dietitian, role='doctor', clinician_type='dietitian')
        cls.beta_doctor = users.create_user('ops-beta-doctor@example.test')
        CompanyMembership.objects.create(company=cls.beta, user=cls.beta_doctor, role='doctor', clinician_type='doctor')

    def setUp(self):
        super().setUp()
        self.login(self.super_admin)

    def post(self, **values):
        return self.client.post(reverse('portal:practice-settings'), {'section': 'purchasing', **values})

    def test_super_admin_saves_purchase_order_details_and_it_is_audited(self):
        response = self.post(po_bill_to_name='Dr Example', po_billing_street='6 Harfield Rd', po_billing_city='Cape Town',
                             po_billing_province='WC', po_billing_postal_code='7708', po_vat_number='',
                             po_pharmacy_number='', po_accounts_email='accounts@example.test', po_doctor=self.doctor.pk,
                             po_delivery_instructions='Hand to reception')
        self.assertEqual(response.status_code, 302)
        row = PracticeSettings.objects.get(company=self.company)
        self.assertEqual((row.po_bill_to_name, row.po_doctor_id, row.po_billing_lines()),
                         ('Dr Example', self.doctor.pk, ['6 Harfield Rd', 'Cape Town 7708']))
        self.assertTrue(AuditEvent.objects.filter(action='practice.settings_updated',
                                                  metadata__po_bill_to_name='Dr Example').exists())

    def test_doctor_choices_are_this_practices_active_prescribers_only(self):
        form = self.client.get(reverse('portal:practice-settings')).context['purchasing_form']
        self.assertEqual(set(form.fields['po_doctor'].queryset.values_list('pk', flat=True)), {self.doctor.pk})
        response = self.post(po_doctor=self.beta_doctor.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn('po_doctor', response.context['purchasing_form'].errors)

    def test_invalid_practice_vat_number_is_rejected_and_valid_one_is_normalised(self):
        response = self.post(po_vat_number='12')
        self.assertEqual(response.status_code, 400)
        self.assertIn('po_vat_number', response.context['purchasing_form'].errors)
        self.assertEqual(self.post(po_vat_number='4350 263 226').status_code, 302)
        self.assertEqual(PracticeSettings.objects.get(company=self.company).po_vat_number, '4350263226')

    def test_section_is_shown_on_the_page(self):
        self.assertContains(self.client.get(reverse('portal:practice-settings')), 'Printed as Bill to on every purchase order.')

    def test_staff_with_an_hpcsa_number_are_offered_and_labelled_with_it(self):
        get_user_model().objects.filter(pk=self.super_admin.pk).update(hpcsa_number='MP0000000', first_name='Jo',
                                                                       last_name='Admin', title='DR')
        field = self.client.get(reverse('portal:practice-settings')).context['purchasing_form'].fields['po_doctor']
        self.assertEqual(set(field.queryset.values_list('pk', flat=True)), {self.doctor.pk, self.super_admin.pk})
        self.assertEqual(field.label_from_instance(field.queryset.get(pk=self.super_admin.pk)), 'Dr Jo Admin · HPCSA MP0000000')
        self.assertIn('HPCSA not on profile', field.label_from_instance(field.queryset.get(pk=self.doctor.pk)))
