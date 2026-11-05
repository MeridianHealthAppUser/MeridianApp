"""Blood tests asked for by a date pause the subscription and shipping a week later, until reviewed (care.blood_tests)."""

from datetime import timedelta

from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import override_settings
from django.utils import timezone

from .billing import run_debits
from .blood_tests import pause_reason, run_checks
from .clinical import mark_lab_done, review_lab_request
from .models import ClinicalTask, LabRequest, PatientSubscription, Shipment, ShipmentItem, SubscriptionCycle
from .operations import shipment_hold_reason
from .test_billing import BillingFixture
from .treatment import subscription_hold_reason


class BloodTestPauseTests(BillingFixture):
    def lab(self, due_on, **extra):
        return LabRequest.objects.create(company=self.company, patient=self.patient, requested_by=self.doctor,
                                         panel_name='TSH', due_on=due_on, **extra)

    def parcel(self):
        shipment = Shipment.objects.create(
            company=self.company, patient=self.patient, subscription=self.plan, authorization=self.auth, cycle_number=1,
            scheduled_for=self.today + timedelta(days=3), status=Shipment.Status.DRAFT)
        ShipmentItem.objects.create(company=self.company, shipment=shipment, product=self.dose.pen, dose=self.auth.max_dose,
                                    quantity=1)
        return shipment

    def test_the_pause_starts_a_week_after_the_requested_by_date_and_lasts_until_reviewed(self):
        self.lab(None)  # Without a date nothing is ever paused.
        lab = self.lab(self.today - timedelta(days=6))
        self.assertEqual(pause_reason(self.company.pk, self.patient.pk), '')
        self.assertEqual(subscription_hold_reason(self.plan), '')
        LabRequest.objects.filter(pk=lab.pk).update(due_on=self.today - timedelta(days=7))
        reason = pause_reason(self.company.pk, self.patient.pk)
        self.assertTrue(reason.startswith('Paused until the blood tests'))
        self.assertIn('are done and reviewed.', reason)
        # The patient's My Treatment shows it, and no parcel is prepared, locked or dispatched.
        self.assertEqual(subscription_hold_reason(self.plan), reason)
        self.assertEqual(shipment_hold_reason(self.parcel()), reason)
        # Done isn't enough: the doctor's review lifts it.
        LabRequest.objects.filter(pk=lab.pk).update(status=LabRequest.Status.DONE)
        self.assertEqual(pause_reason(self.company.pk, self.patient.pk), reason)
        LabRequest.objects.filter(pk=lab.pk).update(status=LabRequest.Status.REVIEWED)
        self.assertEqual(pause_reason(self.company.pk, self.patient.pk), '')

    def test_no_debits_while_paused_and_billing_carries_on_from_the_review_without_catching_up(self):
        self.debit_order()
        # The paid period ended two weeks ago and the next debit came round during the pause.
        SubscriptionCycle.objects.update(starts_on=self.today - timedelta(weeks=6), ends_on=self.today - timedelta(weeks=2, days=1))
        PatientSubscription.objects.filter(pk=self.plan.pk).update(next_debit_on=self.today - timedelta(weeks=2))
        lab = self.lab(self.today - timedelta(days=10))
        self.assertEqual(run_debits(self.company)['tried'], 0)
        self.assertEqual(SubscriptionCycle.objects.count(), 1)
        review_lab_request(lab_request=lab, actor=self.doctor)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.next_debit_on, self.today)
        self.assertEqual(run_debits(self.company)['tried'], 1)
        self.assertEqual(SubscriptionCycle.objects.count(), 2)

    def test_the_daily_check_holds_parcels_and_the_review_puts_them_back_to_drafts(self):
        parcel = self.parcel()
        Shipment.objects.filter(pk=parcel.pk).update(status=Shipment.Status.READY, locked_at=timezone.now())
        other_hold = self.parcel()
        Shipment.objects.filter(pk=other_hold.pk).update(status=Shipment.Status.HELD, hold_reason='Waiting for stock.')
        lab = self.lab(self.today - timedelta(days=7))
        told, held = run_checks(self.company)
        parcel.refresh_from_db()
        self.assertEqual((told, held, parcel.status), (1, 1, Shipment.Status.HELD))
        self.assertTrue(parcel.hold_reason.startswith('Paused until the blood tests'))
        review_lab_request(lab_request=lab, actor=self.doctor)
        parcel.refresh_from_db()
        other_hold.refresh_from_db()
        self.assertEqual((parcel.status, parcel.hold_reason, parcel.locked_at), (Shipment.Status.DRAFT, '', None))
        self.assertEqual((other_hold.status, other_hold.hold_reason), (Shipment.Status.HELD, 'Waiting for stock.'))

    def test_a_second_unreviewed_request_keeps_the_pause_after_the_first_is_reviewed(self):
        first, second = self.lab(self.today - timedelta(days=9)), self.lab(self.today - timedelta(days=8))
        parcel = self.parcel()
        run_checks(self.company)
        review_lab_request(lab_request=first, actor=self.doctor)
        parcel.refresh_from_db()
        self.assertEqual(parcel.status, Shipment.Status.HELD)
        review_lab_request(lab_request=second, actor=self.doctor)
        parcel.refresh_from_db()
        self.assertEqual(parcel.status, Shipment.Status.DRAFT)

    @override_settings(EMAIL_ENABLED=True)
    def test_on_the_requested_by_date_a_patient_who_hasnt_marked_them_done_is_told_once(self):
        due = self.lab(self.today)
        self.lab(self.today, status=LabRequest.Status.DONE)
        self.lab(self.today + timedelta(days=1))
        with self.captureOnCommitCallbacks(execute=True):
            told, held = run_checks(self.company)
        self.assertEqual((told, held), (1, 0))
        due.refresh_from_db()
        self.assertIsNotNone(due.due_notice_sent_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.patient_user.email])
        self.assertIn('paused until they are', mail.outbox[0].body)
        self.assertIn((self.today + timedelta(weeks=1)).strftime('%-d %B'), mail.outbox[0].body)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(run_checks(self.company)[0], 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_the_patient_marks_them_done_and_the_doctor_gets_a_review_task(self):
        lab = self.lab(self.today + timedelta(days=5))
        for actor in (self.doctor, self.admin):
            with self.assertRaises(PermissionDenied):
                mark_lab_done(lab_request=lab, actor=actor)
        done = mark_lab_done(lab_request=lab, actor=self.patient_user)
        self.assertEqual(done.status, LabRequest.Status.DONE)
        self.assertIsNotNone(done.done_at)
        self.assertEqual((done.review_task.assigned_to, done.review_task.title, done.review_task.status),
                         (self.doctor, 'Review blood tests', ClinicalTask.Status.OPEN))
        self.assertEqual(mark_lab_done(lab_request=lab, actor=self.patient_user).review_task, done.review_task)
        reviewed = review_lab_request(lab_request=lab, actor=self.doctor)
        self.assertEqual(reviewed.review_task.status, ClinicalTask.Status.DONE)
        with self.assertRaises(ValidationError):
            mark_lab_done(lab_request=lab, actor=self.patient_user)
