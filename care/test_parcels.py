"""Parcels drafted from prescriptions: on the shipping day, one per dispatch interval, within each supply period."""

from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient

from .models import (
    MedicationDose, MedicationProduct, PatientSubscription, PracticeSettings, Shipment, TreatmentAuthorization,
)
from .operations import prepare_shipment
from .parcels import schedule_parcels
from .prescriptions import change_prescription, renew_prescription
from .treatment import change_authorization_status, enroll_local_subscription


@override_settings(MULTI_PRACTICE_ENABLED=True)
class ParcelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Parcel Practice', slug='parcel-practice')
        cls.doctor = users.create_user(email='parcel-doctor@example.test')
        cls.admin = users.create_user(email='parcel-admin@example.test')
        cls.patient_user = users.create_user(email='parcel-patient@example.test')
        CompanyMembership.objects.create(company=cls.company, user=cls.doctor, role='doctor', clinician_type='doctor')
        CompanyMembership.objects.create(company=cls.company, user=cls.admin, role='practice_admin')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Nadia', last_name='Mokoena')
        pen = dict(company=cls.company, price='100.00', category=MedicationProduct.Category.WEIGHT_MANAGEMENT)
        cls.monthly = MedicationDose.objects.create(
            company=cls.company, pen=MedicationProduct.objects.create(name='Semaglutide', strength='1.0 mg', **pen),
            amount_mg='1', price='3295')
        cls.weekly = MedicationDose.objects.create(
            company=cls.company, pen=MedicationProduct.objects.create(name='Tirzepatide', strength='5 mg', **pen),
            amount_mg='5', price='2995', dispatch_weeks=1)
        cls.today = timezone.localdate()

    def prescribe(self, dose, starts_on=None, weeks=24, **values):
        starts_on = starts_on or self.today
        return TreatmentAuthorization.objects.create(
            company=self.company, patient=self.patient, prescribed_by=self.doctor, product=dose.pen, dose=dose,
            max_dose=dose.label, quantity_per_cycle=dose.pens_per_dispatch, starts_on=starts_on,
            expires_on=starts_on + timedelta(weeks=weeks), **values)

    def days(self, authorization=None):
        parcels = Shipment.objects.filter(patient=self.patient).exclude(status='cancelled')
        if authorization is not None:
            parcels = parcels.filter(authorization=authorization)
        return list(parcels.order_by('scheduled_for').values_list('scheduled_for', flat=True))

    def test_every_start_day_gets_a_parcel_each_4_weeks_on_the_shipping_day_inside_its_supply_period(self):
        for offset in range(7):
            with self.subTest(offset=offset):
                Shipment.objects.all().delete()
                TreatmentAuthorization.objects.all().delete()
                start = self.today + timedelta(days=offset)
                prescription = self.prescribe(self.monthly, starts_on=start)
                parcels = schedule_parcels(prescription)
                days = [parcel.scheduled_for for parcel in parcels]
                self.assertEqual(len(days), 6)
                self.assertTrue(all(day.weekday() == 1 for day in days))
                self.assertLess((days[0] - start).days, 7)
                self.assertEqual({(later - earlier).days for earlier, later in zip(days, days[1:])}, {28})
                self.assertLess(days[-1], prescription.expires_on)
                item = parcels[0].items.get()
                self.assertEqual((item.product, item.dose, item.quantity, parcels[0].status), (self.monthly.pen, '1 mg weekly', 1, 'draft'))
                self.assertEqual([parcel.cycle_number for parcel in parcels], [1, 2, 3, 4, 5, 6])
                # Planning it again adds nothing, even when it starts on a shipping day and ends on one.
                self.assertEqual(schedule_parcels(prescription), [])

    def test_parcels_are_numbered_in_turn_whatever_the_doses_interval(self):
        # Weekly or every 5 weeks, parcel N is the Nth (not the 28-day block it falls in), and a renewal carries on.
        five_weekly = MedicationDose.objects.create(company=self.company, pen=self.weekly.pen, amount_mg='7.5',
                                                    price='3495', dispatch_weeks=5, billing_weeks=5)
        for dose, count in ((self.weekly, 8), (five_weekly, 6)):
            with self.subTest(weeks=dose.dispatch_weeks):
                Shipment.objects.all().delete()
                TreatmentAuthorization.objects.all().delete()
                parcels = schedule_parcels(self.prescribe(dose, weeks=dose.dispatch_weeks * count))
                self.assertEqual([parcel.cycle_number for parcel in parcels], list(range(1, count + 1)))
        # One that started 12 weeks ago counts the parcels it would already have had.
        Shipment.objects.all().delete()
        TreatmentAuthorization.objects.all().delete()
        parcels = schedule_parcels(self.prescribe(self.monthly, starts_on=self.today - timedelta(weeks=12)))
        self.assertEqual(parcels[0].cycle_number, 4)

    def test_each_draft_can_be_marked_ready_within_the_supply_allowance(self):
        for parcel in schedule_parcels(self.prescribe(self.weekly, weeks=8)):
            self.assertEqual(prepare_shipment(shipment=parcel, actor=self.admin).status, 'ready')
        self.assertEqual(Shipment.objects.filter(status='ready').count(), 8)

    def test_a_one_week_step_still_gets_its_parcel_and_the_practice_chooses_the_shipping_day(self):
        PracticeSettings.objects.create(company=self.company, shipping_weekday=PracticeSettings.Weekday.FRIDAY)
        start = self.today + timedelta(days=(5 - self.today.weekday()) % 7 or 7)  # A Saturday: the next Friday ends the step.
        step = self.prescribe(self.monthly, starts_on=start, weeks=1)
        step.expires_on = start + timedelta(days=6)
        step.save()
        self.assertEqual([parcel.scheduled_for for parcel in schedule_parcels(step)], [start + timedelta(days=6)])

    def test_a_renewal_carries_on_the_rhythm_and_a_change_replaces_the_drafts(self):
        first = self.prescribe(self.monthly)
        parcels = schedule_parcels(first)
        prepare_shipment(shipment=parcels[0], actor=self.admin)
        renewal = renew_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=first, weeks=8)
        days = self.days()
        self.assertEqual(len(days), 8)
        self.assertEqual({(later - earlier).days for earlier, later in zip(days, days[1:])}, {28})
        self.assertEqual(set(Shipment.objects.values_list('authorization', flat=True)), {renewal.pk})
        changed = change_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=renewal,
                                      dose=self.weekly, weeks=4)
        ready = Shipment.objects.get(pk=parcels[0].pk)
        self.assertEqual((ready.status, ready.hold_reason), ('held', 'Prescription changed. Dispensing review is required before dispatch.'))
        self.assertEqual(Shipment.objects.filter(authorization=renewal, status='cancelled').count(), 7)
        self.assertEqual(len(self.days(changed)), 4)

    def test_pausing_the_prescription_holds_its_drafts_and_enrolling_attaches_them_to_the_plan(self):
        prescription = self.prescribe(self.monthly)
        schedule_parcels(prescription)
        plan = enroll_local_subscription(company=self.company, patient=self.patient, actor=self.patient_user,
                                         authorization=prescription, confirm=True)
        self.assertEqual(set(Shipment.objects.values_list('subscription', flat=True)), {plan.pk})
        PatientSubscription.objects.filter(pk=plan.pk).update(authorization=None)
        change_authorization_status(authorization=prescription, actor=self.doctor, action='pause')
        self.assertEqual(set(Shipment.objects.values_list('status', flat=True)), {'held'})

    def test_an_authorisation_without_a_catalogue_dose_drafts_nothing(self):
        prescription = self.prescribe(self.monthly)
        TreatmentAuthorization.objects.filter(pk=prescription.pk).update(dose=None)
        prescription.refresh_from_db()
        self.assertEqual(schedule_parcels(prescription), [])

    def test_the_command_drafts_prescriptions_written_before_parcels_were_drafted_once(self):
        self.prescribe(self.monthly)
        self.prescribe(self.weekly, status=TreatmentAuthorization.Status.CANCELLED)
        out = StringIO()
        call_command('draft_parcels', company=self.company.slug, stdout=out)
        self.assertIn('6 draft parcels added for Parcel Practice.', out.getvalue())
        call_command('draft_parcels', company=self.company.slug, stdout=out)
        self.assertIn('0 draft parcels added', out.getvalue())
        self.assertEqual(Shipment.objects.count(), 6)
