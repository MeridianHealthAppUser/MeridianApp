"""Lists and single-record treatment checks share the same persisted eligibility rule."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from care.models import MedicationProduct, PatientSubscription, TreatmentAuthorization
from care.treatment import authorization_is_current, current_authorizations
from practices.models import Company, CompanyMembership, Patient
from portal.patient_summary import current_authorization, previous_prescriptions
from portal.treatment_views import _eligible_authorizations


@override_settings(MULTI_PRACTICE_ENABLED=True)
class CurrentAuthorizationEligibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.company = Company.objects.create(name='Eligibility practice', slug='eligibility-practice')
        cls.beta = Company.objects.create(name='Other practice', slug='eligibility-other')
        cls.doctor = get_user_model().objects.create_user(email='eligibility-doctor@example.test')
        cls.membership = CompanyMembership.objects.create(company=cls.company, user=cls.doctor, role='doctor')
        cls.patient = Patient.objects.create(company=cls.company, first_name='Own', last_name='Patient')
        cls.product = MedicationProduct.objects.create(company=cls.company, name='Own medication', price=100)
        cls.authorization = TreatmentAuthorization.objects.create(
            company=cls.company, patient=cls.patient, prescribed_by=cls.doctor, product=cls.product,
            max_dose='Recorded dose', starts_on=cls.today, expires_on=cls.today + timedelta(days=90),
        )

    def test_current_list_and_record_checks_each_use_one_query(self):
        with self.assertNumQueries(1):
            self.assertTrue(authorization_is_current(self.authorization))
        with self.assertNumQueries(1):
            self.assertEqual(current_authorization(self.company, self.patient).pk, self.authorization.pk)
        with self.assertNumQueries(1):
            self.assertEqual(list(_eligible_authorizations(self.company, self.patient)), [self.authorization])
        with self.assertNumQueries(1):
            self.assertEqual(list(previous_prescriptions(self.company, self.patient)), [])

    def test_all_eligibility_boundaries_agree_for_list_and_single_record(self):
        cases = [
            (TreatmentAuthorization, self.authorization.pk, {'status': 'paused'}),
            (TreatmentAuthorization, self.authorization.pk, {'status': 'cancelled'}),
            (TreatmentAuthorization, self.authorization.pk, {'starts_on': self.today + timedelta(days=1)}),
            (TreatmentAuthorization, self.authorization.pk, {'expires_on': self.today - timedelta(days=1)}),
            (Company, self.company.pk, {'is_active': False}),
            (Patient, self.patient.pk, {'is_active': False}),
            (Patient, self.patient.pk, {'company': self.beta}),
            (MedicationProduct, self.product.pk, {'is_active': False}),
            (MedicationProduct, self.product.pk, {'company': self.beta}),
            (get_user_model(), self.doctor.pk, {'is_active': False}),
            (CompanyMembership, self.membership.pk, {'is_active': False}),
            (CompanyMembership, self.membership.pk, {'clinician_type': 'dietitian'}),
            (CompanyMembership, self.membership.pk, {'company': self.beta}),
        ]
        for model, pk, changed in cases:
            with self.subTest(model=model.__name__, changed=changed), transaction.atomic():
                model.objects.filter(pk=pk).update(**changed)
                record = TreatmentAuthorization.objects.get(pk=self.authorization.pk)
                self.assertFalse(authorization_is_current(record))
                self.assertFalse(current_authorizations().filter(pk=record.pk).exists())
                self.assertIsNone(current_authorization(self.company, self.patient))
                self.assertFalse(_eligible_authorizations(self.company, self.patient).exists())
                transaction.set_rollback(True)

    def test_prescriber_revocation_moves_product_to_previous_prescriptions(self):
        CompanyMembership.objects.filter(pk=self.membership.pk).update(is_active=False)
        with self.assertNumQueries(1):
            self.assertEqual(list(previous_prescriptions(self.company, self.patient)), [self.authorization])
        self.assertFalse(authorization_is_current(self.authorization))

    def test_requested_date_and_unsaved_records_do_not_infer_current_treatment(self):
        self.assertFalse(authorization_is_current(self.authorization, self.today - timedelta(days=1)))
        self.assertFalse(current_authorizations(on_date=self.today - timedelta(days=1)).exists())
        self.assertTrue(authorization_is_current(self.authorization, self.authorization.expires_on))
        with self.assertNumQueries(0):
            self.assertFalse(authorization_is_current(None))
            self.assertFalse(authorization_is_current(TreatmentAuthorization()))

    def test_current_selection_finds_valid_record_after_many_invalid_candidates(self):
        inactive = get_user_model().objects.create_user(email='ineligible-doctor@example.test', is_active=False)
        CompanyMembership.objects.create(company=self.company, user=inactive, role='doctor')
        TreatmentAuthorization.objects.bulk_create([
            TreatmentAuthorization(company=self.company, patient=self.patient, product=self.product, prescribed_by=inactive,
                                   max_dose='Invalid prescriber', starts_on=self.today,
                                   expires_on=self.today + timedelta(days=100 + index))
            for index in range(12)
        ])
        with self.assertNumQueries(1):
            self.assertEqual(current_authorization(self.company, self.patient).pk, self.authorization.pk)

    def test_plan_preference_applies_only_to_an_eligible_prescription_for_this_patient(self):
        other = TreatmentAuthorization.objects.create(
            company=self.company, patient=self.patient, prescribed_by=self.doctor, product=self.product,
            max_dose='Later prescription', starts_on=self.today, expires_on=self.today + timedelta(days=120),
        )
        plan = PatientSubscription(company=self.company, patient=self.patient, authorization=self.authorization)
        with self.assertNumQueries(1):
            self.assertEqual(current_authorization(self.company, self.patient, plan).pk, self.authorization.pk)
        TreatmentAuthorization.objects.filter(pk=self.authorization.pk).update(status='cancelled')
        with self.assertNumQueries(1):
            self.assertEqual(current_authorization(self.company, self.patient, plan).pk, other.pk)
