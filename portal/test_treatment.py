"""Standalone treatment pages remain scoped and require signed confirmation."""

from django.test import override_settings
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from care.models import MedicationProduct, PatientClinicalSummary, PatientSubscription, PracticeSettings, TreatmentAuthorization
from care.treatment import create_authorization, enroll_local_subscription
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY

from .treatment_forms import make_treatment_context


@override_settings(MULTI_PRACTICE_ENABLED=True)
class TreatmentPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Treatment Pages Alpha', slug='treatment-pages-alpha')
        cls.beta = Company.objects.create(name='Treatment Pages Beta', slug='treatment-pages-beta')
        users = get_user_model().objects
        cls.doctor = users.create_user(email='treatment-pages-doctor@example.test')
        cls.admin = users.create_user(email='treatment-pages-admin@example.test')
        cls.super_admin = users.create_user(email='treatment-pages-super@example.test')
        cls.patient_user = users.create_user(email='treatment-pages-patient@example.test')
        cls.other_user = users.create_user(email='treatment-pages-other@example.test')
        for user, role in ((cls.doctor, 'doctor'), (cls.admin, 'practice_admin'), (cls.super_admin, 'super_admin')):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role)
        CompanyMembership.objects.create(company=cls.beta, user=cls.doctor, role='doctor')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Alex', last_name='Alpha', assigned_doctor=cls.doctor)
        cls.other = Patient.objects.create(company=cls.company, user=cls.other_user, first_name='Other', last_name='Alpha', assigned_doctor=cls.doctor)
        cls.beta_patient = Patient.objects.create(company=cls.beta, user=cls.patient_user, first_name='Alex', last_name='Beta', assigned_doctor=cls.doctor)
        cls.product = MedicationProduct.objects.create(company=cls.company, name='Selected treatment product', price=100)
        PracticeSettings.objects.create(company=cls.company)

    def login(self, user=None, company=None, client=None):
        client = client or self.client
        self.actor = user or self.doctor
        client.force_login(self.actor)
        session = client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def authorization(self, patient=None):
        return create_authorization(company=self.company, patient=patient or self.patient, actor=self.doctor, product=self.product,
                                    max_dose='Recorded doctor decision', quantity_per_cycle=4, starts_on=timezone.localdate(),
                                    expires_on=timezone.localdate() + timedelta(days=90), review_interval_days=90,
                                    instructions='Private to the patient and clinical team')

    def token(self, kind, record=None, patient=None):
        request = RequestFactory().get('/')
        request.user = self.actor
        patient = patient or self.patient
        return make_treatment_context(request, patient.company, patient, kind, record)

    def test_all_pages_are_standalone_authenticated_routes(self):
        for name in ('treatment-authorisations', 'treatment-subscriptions', 'patient-treatment', 'patient-subscription'):
            url = reverse(f'portal:{name}')
            self.assertEqual(self.client.get(url).status_code, 302)
        self.login()
        # Clinicians find authorisations on each patient's Treatment tab; the list is for Super Admins.
        self.assertRedirects(self.client.get(reverse('portal:treatment-authorisations')), reverse('portal:desktop-dashboard'),
                             fetch_redirect_response=False)
        # Subscriptions sit in Operations, which the Clinician role doesn't see.
        self.assertEqual(self.client.get(reverse('portal:treatment-subscriptions')).status_code, 403)
        self.login(self.admin)
        self.assertContains(self.client.get(reverse('portal:treatment-subscriptions')),
                            f'<a href="{reverse("portal:treatment-subscriptions")}" class="is-active" aria-current="page">Subscriptions</a>')
        self.login(self.super_admin)
        self.assertContains(self.client.get(reverse('portal:treatment-authorisations')), '<h1>Authorisations</h1>')
        self.login(self.patient_user)
        treatment = self.client.get(reverse('portal:patient-treatment'))
        self.assertContains(treatment, '<h1>My Treatment</h1>', html=True)
        self.assertContains(treatment, 'No treatment authorised yet')
        # The plan now lives on My Treatment; the old address keeps working.
        self.assertRedirects(self.client.get(reverse('portal:patient-subscription')),
                             reverse('portal:patient-treatment') + '#treatment-plan', fetch_redirect_response=False)

    def test_admins_cannot_prescribe_and_practice_admin_cannot_read_clinical_details(self):
        auth = self.authorization()
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse('portal:treatment-authorisations')).status_code, 403)
        self.assertEqual(self.client.get(reverse('portal:treatment-authorisation-detail', args=[auth.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('portal:treatment-subscriptions')).status_code, 200)
        self.login(self.super_admin)
        self.assertContains(self.client.get(reverse('portal:treatment-authorisation-detail', args=[auth.pk])), 'Read-only')
        self.assertEqual(self.client.get(reverse('portal:treatment-authorisation-create', args=[self.patient.pk])).status_code, 403)

    def compounded(self):
        return MedicationProduct.objects.create(company=self.company, name='Compounded cream', price=100, is_compounded=True)

    def test_create_has_no_prescribing_defaults_and_requires_context(self):
        product = self.compounded()
        self.login()
        url = reverse('portal:treatment-authorisation-create', args=[self.patient.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['form'].initial)
        data = dict(product=product.pk, max_dose='Explicit recorded dose', quantity_per_cycle=4,
                    starts_on=timezone.localdate().isoformat(), expires_on=(timezone.localdate() + timedelta(days=90)).isoformat(),
                    review_interval_days=90, instructions='Saved instructions', confirm='on')
        invalid = self.client.post(url, data)
        self.assertEqual(invalid.status_code, 400)
        self.assertTrue(invalid.context['form'].non_field_errors())
        self.assertFalse(TreatmentAuthorization.objects.exists())
        data['treatment_context'] = response.context['treatment_context']
        saved = self.client.post(url, data)
        self.assertEqual(saved.status_code, 302)
        self.assertEqual(TreatmentAuthorization.objects.count(), 1)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(TreatmentAuthorization.objects.count(), 1)

    def test_main_treatments_go_to_the_overview_and_the_form_is_for_compounded_or_other_treatments(self):
        overview = reverse('portal:patient-detail', args=[self.patient.pk])
        main = self.authorization()  # self.product is a main treatment (weight management).
        self.login()
        create = reverse('portal:treatment-authorisation-create', args=[self.patient.pk])
        renew = reverse('portal:treatment-authorisation-renew', args=[main.pk])
        # With only main treatments in the catalogue, as at Meridian, the form isn't offered anywhere.
        for url in (create, renew):
            with self.subTest(url=url):
                self.assertTrue(self.client.get(url).url.startswith(overview))
                self.assertTrue(self.client.post(url, {}).url.startswith(overview))
        detail = self.client.get(reverse('portal:treatment-authorisation-detail', args=[main.pk]))
        self.assertContains(detail, 'Renew from the Overview')
        self.assertNotContains(detail, renew)
        # The Treatment tab (Super Admins') offers it only once the catalogue has a compounded or other treatment.
        self.login(self.super_admin)
        self.assertFalse(self.client.get(overview, {'tab': 'treatment'}).context['other_treatments_offered'])
        cream = self.compounded()
        self.assertTrue(self.client.get(overview, {'tab': 'treatment'}).context['other_treatments_offered'])
        # It offers only that product, and a safety flag needs the tick.
        self.login()
        self.assertTrue(self.client.get(renew).url.startswith(overview))  # The main treatment still renews there.
        PatientClinicalSummary.objects.create(company=self.company, patient=self.patient, pancreatitis='yes')
        page = self.client.get(create)
        self.assertEqual(list(page.context['form'].fields['product'].queryset), [cream])
        self.assertContains(page, 'name="acknowledge_flags"')
        data = dict(product=cream.pk, max_dose='Apply once daily', quantity_per_cycle=1,
                    starts_on=timezone.localdate().isoformat(), expires_on=(timezone.localdate() + timedelta(days=90)).isoformat(),
                    review_interval_days=90, confirm='on', treatment_context=page.context['treatment_context'])
        self.assertContains(self.client.post(create, data), 'safety flag', status_code=400)
        self.assertEqual(self.client.post(create, {**data, 'product': self.product.pk, 'acknowledge_flags': 'on'}).status_code, 400)
        self.assertEqual(self.client.post(create, {**data, 'acknowledge_flags': 'on'}).status_code, 302)
        self.assertTrue(TreatmentAuthorization.objects.filter(product=cream).exists())

    def test_patient_enrollment_requires_confirmation_token_and_never_crosses_practice(self):
        auth = self.authorization()
        self.login(self.patient_user)
        url = reverse('portal:patient-subscription-enroll')
        response = self.client.get(reverse('portal:patient-treatment'))
        self.assertContains(response, 'Selected treatment product')
        self.assertContains(response, 'Where should we send your parcels?')
        # Starting asks for the delivery address and how to pay (24 Oct 2026).
        data = dict(authorization=auth.pk, confirm='on', street='1 Long Street', suburb='Gardens', city='Cape Town',
                    province='WC', postal_code='8001', phone='082 555 0114', method='debicheck')
        self.assertEqual(self.client.post(url, data).status_code, 400)
        self.assertFalse(PatientSubscription.objects.exists())
        data['treatment_context'] = response.context['enrollment_context']
        with override_settings(PAYMENT_PROVIDER=''):
            self.assertEqual(self.client.post(url, data).status_code, 302)
        plan = PatientSubscription.objects.get()
        self.assertEqual(plan.patient_id, self.patient.pk)
        # Without a payment provider nothing is collected: the next debit waits for a debit order.
        self.assertIsNone(plan.next_debit_on)
        self.assertEqual(self.patient.delivery_address.street, '1 Long Street')
        self.login(self.patient_user, self.beta)
        rejected = self.client.post(url, data)
        self.assertEqual(rejected.status_code, 400)
        self.assertTrue(rejected.context['enrollment_form'].non_field_errors())
        self.assertEqual(PatientSubscription.objects.count(), 1)

    def test_expired_confirmation_token_rejected_without_mutation(self):
        auth = self.authorization()
        plan = enroll_local_subscription(company=self.company, patient=self.patient, actor=self.patient_user, authorization=auth, confirm=True)
        self.login(self.patient_user)
        with patch('django.core.signing.time.time', return_value=1):
            token = self.token('subscription-status', plan)
        response = self.client.post(reverse('portal:patient-subscription-status', args=[plan.pk]),
                                    dict(treatment_context=token, confirm='on', action='cancel', reasons='cost'))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['cancel_form'].non_field_errors())
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'active')

    def test_patient_cannot_operate_another_patients_plan(self):
        auth = self.authorization(self.other)
        plan = enroll_local_subscription(company=self.company, patient=self.other, actor=self.other_user, authorization=auth, confirm=True)
        self.login(self.patient_user)
        response = self.client.post(reverse('portal:patient-subscription-status', args=[plan.pk]),
                                    dict(treatment_context=self.token('subscription-status', plan), confirm='on', action='cancel'))
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(self.client.get(reverse('portal:patient-treatment')), auth.instructions)

    def test_doctor_cannot_read_other_practice_authorization(self):
        auth = self.authorization()
        self.login(self.doctor, self.beta)
        self.assertEqual(self.client.get(reverse('portal:treatment-authorisation-detail', args=[auth.pk])).status_code, 404)

    def test_patient_post_requires_csrf_and_get_never_changes_plan(self):
        auth = self.authorization()
        self.login(self.patient_user)
        url = reverse('portal:patient-subscription-enroll')
        self.assertEqual(self.client.get(url).status_code, 405)
        secure = Client(enforce_csrf_checks=True)
        self.login(self.patient_user, client=secure)
        self.assertEqual(secure.post(url, dict(authorization=auth.pk, confirm='on')).status_code, 403)
        self.assertFalse(PatientSubscription.objects.exists())

    def test_staff_authorization_history_paginates_and_patient_history_is_not_shown(self):
        for _ in range(23):
            self.authorization()
        self.login(self.super_admin)
        response = self.client.get(reverse('portal:treatment-authorisations'))
        self.assertEqual(len(response.context['page_obj']), 20)
        response = self.client.get(reverse('portal:treatment-authorisations'), {'page': 2})
        self.assertEqual(len(response.context['page_obj']), 3)
        self.login(self.patient_user)
        response = self.client.get(reverse('portal:patient-treatment'), {'page': 2, 'plans_page': 2, 'shipments_page': 2})
        self.assertNotContains(response, 'Treatment and delivery history')
        self.assertNotContains(response, 'id="treatment-history"')
