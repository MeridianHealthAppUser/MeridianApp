"""Prescriptions renewed or changed from the patient record, and the editable summary data."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient

from .clinical_summary import edit_summary, get_summary, summary_safety_flags
from .models import (
    AuditEvent, Lead, MedicationDose, MedicationProduct, PatientClinicalSummary, PatientMedicalProfile, PatientSubscription,
    PatientSummaryEdit, ScreeningQuestionnaire, Shipment, TreatmentAuthorization,
)
from .prescriptions import (
    add_weeks, change_prescription, current_prescription, prescription_options, prescription_state, renew_prescription,
)


class PrescriptionTestData:
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Prescribing Practice', slug='prescribing-practice')
        cls.other = Company.objects.create(name='Other Prescribing Practice', slug='other-prescribing-practice')
        cls.doctor = users.create_user(email='rx-doctor@example.test', first_name='Sam', last_name='Marchant')
        cls.colleague = users.create_user(email='rx-colleague@example.test', first_name='Joshua', last_name='Czech')
        cls.dietitian = users.create_user(email='rx-dietitian@example.test')
        cls.admin = users.create_user(email='rx-admin@example.test')
        cls.super_admin = users.create_user(email='rx-super@example.test')
        cls.outsider = users.create_user(email='rx-outsider@example.test')
        for user, role, kind in (
            (cls.doctor, 'doctor', 'doctor'), (cls.colleague, 'super_admin', 'doctor'), (cls.dietitian, 'doctor', 'dietitian'),
            (cls.admin, 'practice_admin', ''), (cls.super_admin, 'super_admin', ''),
        ):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role, clinician_type=kind)
        CompanyMembership.objects.create(company=cls.other, user=cls.outsider, role='doctor')
        cls.patient = Patient.objects.create(company=cls.company, first_name='Nadia', last_name='Mokoena')
        pen = dict(company=cls.company, price='100.00', category=MedicationProduct.Category.WEIGHT_MANAGEMENT)
        sema_small = MedicationProduct.objects.create(name='Semaglutide', strength='1 mg', **pen)
        sema_large = MedicationProduct.objects.create(name='Semaglutide', strength='4 mg', **pen)
        tirz_pen = MedicationProduct.objects.create(name='Tirzepatide', strength='5 mg', **pen)
        add_on = MedicationProduct.objects.create(name='Ondansetron', strength='4 mg', company=cls.company, price='50.00',
                                                  category=MedicationProduct.Category.OTHER)
        hidden_pen = MedicationProduct.objects.create(name='Liraglutide', strength='3 mg', is_active=False, **pen)
        dose = dict(company=cls.company, price='1995.00')
        cls.sema_low = MedicationDose.objects.create(pen=sema_small, amount_mg='0.25', **dose)
        cls.sema_high = MedicationDose.objects.create(pen=sema_large, amount_mg='1.0', **dose)
        cls.tirz = MedicationDose.objects.create(pen=tirz_pen, amount_mg='5', pens_per_dispatch=1, dispatch_weeks=1, **dose)
        cls.add_on = MedicationDose.objects.create(pen=add_on, amount_mg='4', **dose)
        cls.hidden = MedicationDose.objects.create(pen=hidden_pen, amount_mg='3', **dose)
        # Not prescribable until the practice sets its price.
        cls.unpriced = MedicationDose.objects.create(company=cls.company, pen=tirz_pen, amount_mg='7.5',
                                                     tier=MedicationDose.Tier.STEP_UP)

    def prescribe(self, dose=None, **values):
        today = timezone.localdate()
        dose = dose or self.sema_low
        defaults = dict(company=self.company, patient=self.patient, prescribed_by=self.doctor, product=dose.pen, dose=dose,
                        max_dose=dose.label, quantity_per_cycle=2, starts_on=today - timedelta(days=60),
                        expires_on=today + timedelta(days=90))
        defaults.update(values)
        return TreatmentAuthorization.objects.create(**defaults)


@override_settings(MULTI_PRACTICE_ENABLED=True)
class PrescriptionTests(PrescriptionTestData, TestCase):
    def test_any_doctor_here_can_renew_and_the_old_prescription_is_kept_as_history(self):
        today = timezone.localdate()
        previous = self.prescribe()
        plan = PatientSubscription.objects.create(company=self.company, patient=self.patient, authorization=previous,
                                                  plan_name='Semaglutide plan',
                                                  status=PatientSubscription.Status.ACTIVE, starts_on=today)
        parcel = Shipment.objects.create(company=self.company, patient=self.patient, authorization=previous,
                                         scheduled_for=today + timedelta(days=3), status=Shipment.Status.READY)
        renewal = renew_prescription(company=self.company, patient=self.patient, actor=self.colleague, current=previous, weeks=24)
        previous.refresh_from_db()
        self.assertEqual(previous.status, TreatmentAuthorization.Status.CANCELLED)
        self.assertEqual((renewal.replaces_id, renewal.prescribed_by_id, renewal.product_id, renewal.dose, renewal.max_dose),
                         (previous.pk, self.colleague.pk, self.sema_low.pen_id, self.sema_low, '0.25 mg weekly'))
        # 24 weeks on from the current expiry.
        self.assertEqual((renewal.starts_on, renewal.expires_on), (today, previous.expires_on + timedelta(days=168)))
        self.assertEqual(renewal.quantity_per_cycle, 2)
        self.assertEqual(renewal.log_text, f'Prescription renewed for 24 weeks: Semaglutide 0.25 mg weekly, '
                                           f'valid to {renewal.expires_on.day} {renewal.expires_on:%b %Y}.')
        plan.refresh_from_db()
        parcel.refresh_from_db()
        self.assertEqual(plan.authorization_id, renewal.pk)
        # The same medicine and dose carry on: the parcel moves to the renewal and isn't held.
        self.assertEqual((parcel.authorization_id, parcel.status), (renewal.pk, Shipment.Status.READY))
        self.assertEqual(current_prescription(self.company, self.patient), renewal)
        self.assertTrue(AuditEvent.objects.filter(action='prescription.renewed', target_id=str(renewal.pk)).exists())

    def test_an_expired_prescription_renews_from_today(self):
        today = timezone.localdate()
        previous = self.prescribe(starts_on=today - timedelta(days=200), expires_on=today - timedelta(days=10))
        self.assertEqual(prescription_state(previous), 'expired')
        renewal = renew_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=previous, weeks=4)
        self.assertEqual(renewal.expires_on, add_weeks(today, 4))

    def test_a_dose_no_longer_offered_is_changed_not_renewed(self):
        previous = self.prescribe()
        MedicationDose.objects.filter(pk=self.sema_low.pk).update(is_active=False)
        with self.assertRaisesMessage(ValidationError, 'no longer offered'):
            renew_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=previous, weeks=24)

    def test_prescription_state_follows_the_expiry_date(self):
        today = timezone.localdate()
        self.assertEqual(prescription_state(None), 'none')
        self.assertEqual(prescription_state(self.prescribe(expires_on=today + timedelta(days=30))), 'due')
        self.assertEqual(prescription_state(self.prescribe(expires_on=today + timedelta(days=31))), 'valid')

    def test_only_a_doctor_at_this_practice_can_prescribe(self):
        previous = self.prescribe()
        for actor in (self.dietitian, self.admin, self.super_admin, self.outsider):
            with self.subTest(actor=actor.email):
                with self.assertRaises(PermissionDenied):
                    renew_prescription(company=self.company, patient=self.patient, actor=actor, current=previous, weeks=24)
                with self.assertRaises(PermissionDenied):
                    change_prescription(company=self.company, patient=self.patient, actor=actor, current=previous,
                                        dose=self.tirz, weeks=24)
        self.assertEqual(TreatmentAuthorization.objects.count(), 1)

    def test_a_stale_form_and_out_of_range_weeks_change_nothing(self):
        previous = self.prescribe()
        renew_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=previous, weeks=24)
        with self.assertRaisesMessage(ValidationError, 'changed in another tab'):
            renew_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=previous, weeks=24)
        current = current_prescription(self.company, self.patient)
        for weeks in (0, 6, 26, '24'):
            with self.subTest(weeks=weeks):
                with self.assertRaises(ValidationError):
                    renew_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=current, weeks=weeks)
        self.assertEqual(TreatmentAuthorization.objects.count(), 2)

    def test_change_replaces_from_today_holds_parcels_and_logs_the_reason(self):
        today = timezone.localdate()
        previous = self.prescribe()
        parcel = Shipment.objects.create(company=self.company, patient=self.patient, authorization=previous,
                                         scheduled_for=today + timedelta(days=3), status=Shipment.Status.READY)
        changed = change_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=previous,
                                      dose=self.tirz, weeks=24, reason='Side effects – nausea, vomiting')
        self.assertEqual((changed.product_id, changed.dose, changed.max_dose, changed.starts_on, changed.expires_on, changed.replaces_id),
                         (self.tirz.pen_id, self.tirz, '5 mg weekly', today, add_weeks(today, 24), previous.pk))
        # What is sent per parcel comes from the dose.
        self.assertEqual(changed.quantity_per_cycle, self.tirz.pens_per_dispatch)
        self.assertEqual(changed.log_text, 'Side effects – nausea, vomiting. Prescription changed from Semaglutide 0.25 mg weekly '
                                           f'to Tirzepatide 5 mg weekly, 24 weeks, valid to {changed.expires_on.day} {changed.expires_on:%b %Y}.')
        parcel.refresh_from_db()
        self.assertEqual(parcel.status, Shipment.Status.HELD)
        self.assertTrue(AuditEvent.objects.filter(action='prescription.changed').exists())

    def test_change_rejects_the_same_dose_and_doses_outside_the_main_treatments(self):
        previous = self.prescribe()
        with self.assertRaisesMessage(ValidationError, 'Use Renew to extend it'):
            change_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=previous,
                                dose=self.sema_low, weeks=24)
        for dose in (self.add_on, self.hidden, self.unpriced):
            with self.subTest(dose=str(dose)):
                with self.assertRaises(ValidationError):
                    change_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=previous,
                                        dose=dose, weeks=24)
        options = prescription_options(self.company, previous)
        # Lowest first, and a dose without a price isn't offered until it has one.
        self.assertEqual({name: [str(item) for item in doses] for name, doses in options.items()},
                         {'Semaglutide': ['Semaglutide 0.25 mg weekly', 'Semaglutide 1 mg weekly'],
                          'Tirzepatide': ['Tirzepatide 5 mg weekly']})
        MedicationDose.objects.filter(pk=self.unpriced.pk).update(price='4500.00')
        self.assertEqual([str(item) for item in prescription_options(self.company, previous)['Tirzepatide']],
                         ['Tirzepatide 5 mg weekly', 'Tirzepatide 7.5 mg weekly'])

    def test_a_first_prescription_is_created(self):
        created = change_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=None,
                                      dose=self.sema_high, weeks=12)
        self.assertIsNone(created.replaces_id)
        self.assertTrue(created.log_text.startswith('Prescription created: Semaglutide 1 mg weekly, 12 weeks, valid to '))
        self.assertEqual((created.product_id, created.quantity_per_cycle), (self.sema_high.pen_id, 1))

    def test_a_safety_flag_must_be_acknowledged_before_prescribing(self):
        previous = self.prescribe()
        edit_summary(company=self.company, patient=self.patient, actor=self.doctor, field='pancreatitis', value='yes')
        with self.assertRaisesMessage(ValidationError, 'safety flag'):
            renew_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=previous, weeks=24)
        renewal = renew_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=previous,
                                     weeks=24, acknowledge_flags=True)
        audit = AuditEvent.objects.get(action='prescription.renewed')
        self.assertTrue(audit.metadata['safety_flags_acknowledged'])
        self.assertEqual(current_prescription(self.company, self.patient), renewal)

    def test_cancelled_and_add_on_authorisations_are_not_the_current_prescription(self):
        self.prescribe(dose=self.add_on, max_dose='4 mg as needed')
        self.assertIsNone(current_prescription(self.company, self.patient))
        self.prescribe(status=TreatmentAuthorization.Status.CANCELLED)
        self.assertIsNone(current_prescription(self.company, self.patient))


@override_settings(MULTI_PRACTICE_ENABLED=True)
class ClinicalSummaryTests(PrescriptionTestData, TestCase):
    def screen(self, **answers):
        lead = Lead.objects.create(company=self.company, first_name='Nadia', last_name='Mokoena', email='nadia@example.test',
                                   converted_patient=self.patient)
        values = {'height_cm': '168.0', 'weight_kg': '94.0', 'pancreatitis': 'no', 'thyroid_history': 'unsure',
                  'pregnancy': 'no', 'weight_related_condition': 'yes', 'health_context': 'Amlodipine 5 mg daily'}
        values.update(answers)
        return ScreeningQuestionnaire.objects.create(company=self.company, lead=lead, answers=values,
                                                     status=ScreeningQuestionnaire.Status.SUBMITTED, submitted_at=timezone.now())

    def test_summary_is_read_from_the_screening_and_profile_without_writing(self):
        self.screen()
        PatientMedicalProfile.objects.create(company=self.company, patient=self.patient, revision=1,
                                             answers={'surgical_history': 'Appendectomy (2014)', 'allergies': 'None known'})
        summary = get_summary(self.company, self.patient)
        self.assertIsNone(summary.pk)
        self.assertEqual((summary.height_cm, summary.start_weight_kg), (168, 94))
        self.assertEqual(summary.chronic_medication, 'Amlodipine 5 mg daily')
        self.assertEqual((summary.surgical_history, summary.allergies), ('Appendectomy (2014)', 'None known'))
        self.assertEqual((summary.pancreatitis, summary.thyroid_cancer, summary.pregnancy), ('no', 'unsure', 'no'))
        self.assertEqual([flag['level'] for flag in summary_safety_flags(self.company, self.patient)], ['caution'])
        self.assertFalse(PatientClinicalSummary.objects.exists())

    def test_every_edit_is_kept_and_the_screening_is_never_changed(self):
        questionnaire = self.screen()
        before = dict(questionnaire.answers)
        edit_summary(company=self.company, patient=self.patient, actor=self.doctor, field='chronic_medication',
                     value='Amlodipine 10 mg daily')
        edit_summary(company=self.company, patient=self.patient, actor=self.super_admin, field='goal_weight_kg', value='75')
        edit_summary(company=self.company, patient=self.patient, actor=self.dietitian, field='goal_weight_kg', value='75.0')
        summary = PatientClinicalSummary.objects.get()
        self.assertEqual((summary.chronic_medication, summary.goal_weight_kg), ('Amlodipine 10 mg daily', 75))
        edits = list(PatientSummaryEdit.objects.order_by('pk').values_list('field', 'old_value', 'new_value', 'edited_by'))
        self.assertEqual(edits, [
            ('chronic_medication', 'Amlodipine 5 mg daily', 'Amlodipine 10 mg daily', self.doctor.pk),
            ('goal_weight_kg', 'Not recorded', '75 kg', self.super_admin.pk),
        ])
        # The audit trail names the field only.
        self.assertEqual([audit.metadata for audit in AuditEvent.objects.filter(action='summary.updated').order_by('pk')],
                         [{'field': 'chronic_medication'}, {'field': 'goal_weight_kg'}])
        questionnaire.refresh_from_db()
        self.assertEqual(questionnaire.answers, before)

    def test_values_are_validated_and_only_clinical_staff_may_edit(self):
        for field, value in (('goal_weight_kg', '39'), ('goal_weight_kg', '251'), ('goal_weight_kg', 'heavy'),
                             ('pancreatitis', 'unsure'), ('pregnancy', 'maybe'), ('height_cm', '170')):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValidationError):
                    edit_summary(company=self.company, patient=self.patient, actor=self.doctor, field=field, value=value)
        for actor in (self.admin, self.outsider):
            with self.subTest(actor=actor.email):
                with self.assertRaises(PermissionDenied):
                    edit_summary(company=self.company, patient=self.patient, actor=actor, field='allergies', value='Penicillin')
        self.assertFalse(PatientSummaryEdit.objects.exists())
