"""Managed demo data remains isolated after generation, including queued work and walkthroughs."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import RequestFactory, override_settings
from django.utils import timezone

from practices.models import DemoDataSet, Patient

from . import billing, courier, notifications
from .inventory import consume_stock
from .models import (Appointment, DebitMandate, DeliveryAddress, EmailDelivery, Invoice, MedicationProduct,
                     PatientSubscription, Payment, PharmacyOrder, Shipment, ShipmentItem, StockMovement,
                     SubscriptionCycle, Supplier, SupplierProduct)
from .operations import confirm_shipments, dispatch_shipment, dispatch_week, product_allowance
from .pharmacy import set_basket_quantity
from .purchasing import create_purchase_order
from .suppliers import save_supplier
from .test_purchase_orders import PurchaseOrderFixture


class DemoSafetyFixture(PurchaseOrderFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.demo = DemoDataSet.objects.create(company=cls.company, created_by=cls.super_admin)
        cls.demo_patient = Patient.objects.create(company=cls.company, first_name='Synthetic', last_name='Demo',
                                                  assigned_doctor=cls.doctor, demo_data_set=cls.demo)
        cls.demo_product = MedicationProduct.objects.create(company=cls.company, name='Synthetic product',
            requires_authorisation=False, requires_cold_chain=False, price=Decimal('40'), demo_data_set=cls.demo)
        cls.demo_supplier = Supplier.objects.create(company=cls.company, name='Synthetic supplier',
            email='supplier@example.test', demo_data_set=cls.demo)
        SupplierProduct.objects.create(company=cls.company, supplier=cls.demo_supplier, product=cls.demo_product,
                                        supplier_code='DEMO-01')
        cls.demo_plan = PatientSubscription.objects.create(company=cls.company, patient=cls.demo_patient,
            plan_name='Synthetic plan', starts_on=cls.today, next_debit_on=cls.today)
        DeliveryAddress.objects.create(company=cls.company, patient=cls.demo_patient, street='1 Demo Street',
            suburb='Gardens', city='Cape Town', province='WC', postal_code='8001', phone='0000000000')

    def demo_shipment(self, **values):
        parcel = Shipment.objects.create(company=self.company, patient=self.demo_patient,
            scheduled_for=self.today, **values)
        ShipmentItem.objects.create(company=self.company, shipment=parcel, product=self.demo_product, quantity=1)
        return parcel

    def demo_order(self):
        return create_purchase_order(company=self.company, actor=self.admin, supplier=self.demo_supplier,
            items=[{'product': self.demo_product, 'quantity': 3}], submission_key=uuid4())


class DemoBillingTests(DemoSafetyFixture):
    def test_demo_plans_are_excluded_from_billing_list_and_export(self):
        from django.http import Http404
        from portal.billing_exports import _billing
        from portal.billing_views import billing_detail_plan, billing_listing

        request = RequestFactory().get('/')
        _, plans = billing_listing(request.GET, self.company)
        self.assertIn(self.plan.pk, [plan.pk for plan in plans])
        self.assertNotIn(self.demo_plan.pk, [plan.pk for plan in plans])
        rows = list(_billing(request, self.company).rows)
        self.assertNotIn(str(self.demo_patient), [row[0] for row in rows])
        with self.assertRaises(Http404):
            billing_detail_plan(self.company, self.demo_plan.pk)
        self.assertEqual(billing.billing_state(self.demo_plan), ('demo', 'Demo · billing disabled'))
        self.assertEqual(billing.outstanding(self.demo_plan), Decimal('0'))

    def test_demo_appointments_do_not_inflate_payable_doctor_activity(self):
        from .reporting import doctor_activity

        for patient in (self.patient, self.demo_patient):
            Appointment.objects.create(company=self.company, patient=patient, clinician=self.doctor,
                status='completed', appointment_type='initial', starts_at=timezone.now()-timedelta(days=1))
        counts, sources = doctor_activity(company=self.company, doctor=self.doctor,
                                          start=self.today-timedelta(days=2), end=self.today)
        self.assertEqual(counts['initial'], 1)
        self.assertEqual(Appointment.objects.get(pk=sources['initial'][0]).patient_id, self.patient.pk)

    def test_manual_billing_rejected_before_provider_or_financial_records(self):
        parcel = self.demo_shipment()
        order = PharmacyOrder.objects.create(company=self.company, patient=self.demo_patient)
        baseline = {m: m.objects.count() for m in (Payment, Invoice, SubscriptionCycle, DebitMandate)}
        calls = (
            lambda: billing.start_debit_order(subscription=self.demo_plan, actor=self.admin),
            lambda: billing.start_order_payment(order=order, actor=self.admin),
            lambda: billing.record_manual_payment(company=self.company, actor=self.admin, amount=Decimal('40'),
                                                   method='eft', shipment=parcel),
            lambda: billing.ask_for_new_debit_order(subscription=self.demo_plan, actor=self.admin),
            lambda: billing.set_next_debit(subscription=self.demo_plan, actor=self.admin, day=self.today),
        )
        with patch('care.billing.require_provider') as provider:
            for call in calls:
                with self.subTest(call=call), self.assertRaisesMessage(ValidationError, 'Billing is disabled for demo'):
                    call()
        provider.assert_not_called()
        self.assertEqual(baseline, {m: m.objects.count() for m in baseline})

    def test_daily_billing_skips_demo_even_if_legacy_mandate_exists(self):
        DebitMandate.objects.create(company=self.company, patient=self.demo_patient, subscription=self.demo_plan,
            amount=Decimal('40'), status='active', provider='stitch', provider_reference='fake-mandate')
        with patch('care.billing.provider_named') as provider:
            self.assertEqual(billing.bill_subscription(self.demo_plan), [])
            self.assertEqual(billing.run_debits(self.company), dict(subscriptions=0, tried=0, paid=0, failed=0))
            with self.captureOnCommitCallbacks(execute=True):
                billing.release_mandates(DebitMandate.objects.all())
        provider.assert_not_called()
        self.assertFalse(SubscriptionCycle.objects.filter(patient=self.demo_patient).exists())
        self.assertFalse(Payment.objects.filter(patient=self.demo_patient).exists())

    def test_stale_records_and_provider_return_endpoints_recheck_ownership(self):
        from .peach import PeachProvider
        from .stitch import StitchProvider, debicheck_waiting

        # A queued task may hold a patient object without the ownership flag loaded.
        stale = PatientSubscription.objects.get(pk=self.demo_plan.pk)
        stale.patient.demo_data_set_id = None
        payment = Payment.objects.create(company=self.company, patient=self.demo_patient, amount=Decimal('40'),
                                          due_on=self.today, provider='stitch', method='debicheck', status='pending')
        self.assertEqual(billing.bill_subscription(stale), [])
        self.assertFalse(debicheck_waiting(self.company).filter(pk=payment.pk).exists())
        for provider in (PeachProvider(), StitchProvider()):
            with self.subTest(provider=provider.name), patch.object(provider, 'finish_checkout', wraps=provider.finish_checkout):
                with self.assertRaisesMessage(ValidationError, 'Billing is disabled for demo'):
                    provider.finish_checkout(payment, {})
        with self.assertRaisesMessage(ValidationError, 'Billing is disabled for demo'):
            billing.complete_card_payment(payment=payment, paid=True)
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'pending')

    def test_unanswered_legacy_payment_never_calls_provider(self):
        cycle = SubscriptionCycle.objects.create(company=self.company, patient=self.demo_patient,
            subscription=self.demo_plan, cycle_number=1, starts_on=self.today, ends_on=self.today+timedelta(days=28),
            due_on=self.today, amount=Decimal('40'))
        payment = Payment.objects.create(company=self.company, patient=self.demo_patient, subscription_cycle=cycle,
            amount=Decimal('40'), due_on=self.today, provider='peach', method='card', status='pending')
        Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now()-timedelta(days=2))
        with patch('care.billing.provider_named') as provider:
            self.assertIsNone(billing.collect_cycle(cycle))
            self.assertEqual(billing.check_unanswered(self.company), 0)
        provider.assert_not_called()

    def test_confirmation_uses_explicit_simulated_payment_without_payment_rows(self):
        parcel = self.demo_shipment()
        self.assertEqual(billing.parcel_payment(parcel), ('paid', 'Demo · payment simulated'))
        monday = self.today - timedelta(days=self.today.weekday())
        confirm_shipments(company=self.company, actor=self.admin, week_start=monday, shipment_ids=[parcel.pk])
        parcel.refresh_from_db()
        self.assertEqual(parcel.status, Shipment.Status.READY)
        self.assertIsNotNone(parcel.locked_at)
        self.assertFalse(Payment.objects.filter(patient=self.demo_patient).exists())


@override_settings(EMAIL_ENABLED=True, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class DemoCommunicationTests(DemoSafetyFixture):
    def test_notification_queue_and_stale_delivery_never_email_real_doctor(self):
        appointment = Appointment.objects.create(company=self.company, patient=self.demo_patient,
            clinician=self.doctor, starts_at=timezone.now()+timedelta(hours=2))
        with patch('care.tasks.send_notification.delay') as enqueue, patch('care.notifications.build_email') as build:
            with self.captureOnCommitCallbacks(execute=True):
                notifications.queue('appointment_booked', appointment.pk, self.admin.pk)
            self.assertEqual(notifications.recipients('appointment_booked', appointment.pk), [])
            notifications.deliver('appointment_booked', appointment.pk, self.doctor.pk)
        enqueue.assert_not_called()
        build.assert_not_called()
        appointment.refresh_from_db()
        self.assertIsNone(appointment.reminder_sent_at)
        self.assertFalse(EmailDelivery.objects.filter(patient=self.demo_patient).exists())

    def test_both_scheduled_reminder_paths_skip_demo(self):
        for minutes in (5, 90):
            appointment = Appointment.objects.create(company=self.company, patient=self.demo_patient,
                clinician=self.doctor, starts_at=timezone.now()+timedelta(minutes=minutes))
            with patch('care.notifications.build_email') as build:
                self.assertEqual(notifications._send_start_reminder(appointment.pk), (False, False))
                self.assertEqual(notifications.send_due_reminders(), 0)
            build.assert_not_called()

    def test_journey_membership_rejects_demo_even_if_a_login_is_later_linked(self):
        from .journeys import _patient_member

        self.demo_patient.user = self.admin
        self.demo_patient.save(update_fields=['user'])
        self.assertIsNone(_patient_member('active', self.demo_patient, self.today, 'test-demo'))

    def test_stale_journey_member_and_pathcare_task_do_not_email(self):
        from . import journeys, pathcare

        member = SimpleNamespace(patient=self.demo_patient)
        with patch('care.journeys.due', return_value=[(object(), member)]), patch('care.journeys.render') as render:
            self.assertEqual(journeys.send_due(self.company), 0)
        render.assert_not_called()
        # Delivery rechecks the stored patient's ownership before attempting to read or send the PDF.
        form = SimpleNamespace(pk=999, patient_id=self.demo_patient.pk)
        with patch('care.pathcare.PathCareRequestForm.objects.select_related') as objects, patch(
                'care.pathcare.build_email') as build:
            objects.return_value.get.return_value = form
            pathcare.deliver(form.pk)
        build.assert_not_called()

    def test_purchase_email_rejects_changed_recipient_before_smtp(self):
        from portal.purchase_document_views import _send_order_email

        order = self.demo_order()
        request = RequestFactory().post('/')
        request.user = self.admin
        with patch('portal.purchase_document_views.EmailMessage') as email, self.assertRaisesMessage(
                ValidationError, 'Demo purchase orders cannot be emailed'):
            _send_order_email(request=request, company=self.company, pk=order.pk,
                data={'recipient': 'real-supplier@example.test', 'subject': 'PO', 'message': 'Send it'})
        email.assert_not_called()


class DemoCourierAndStockTests(DemoSafetyFixture):
    def test_demo_collection_leaves_live_booked_parcels_and_stock_unchanged(self):
        demo = self.demo_shipment(status='ready', locked_at=timezone.now(), waybill='DEMO-COLLECTION')
        real = self.shipment(status='ready', locked_at=timezone.now(), waybill='REAL-COLLECTION')
        StockMovement.objects.create(company=self.company, product=self.demo_product, quantity=3,
                                     actor=self.admin, reason='Synthetic receipt')
        live_before = Shipment.objects.filter(pk=real.pk).values().get()
        stock_before = list(StockMovement.objects.exclude(product=self.demo_product).values())
        monday = self.today - timedelta(days=self.today.weekday())
        dispatched, failed = dispatch_week(company=self.company, actor=self.admin, week_start=monday, demo_only=True)
        self.assertEqual([parcel.pk for parcel in dispatched], [demo.pk])
        self.assertEqual(failed, [])
        demo.refresh_from_db()
        self.assertEqual(demo.status, Shipment.Status.DISPATCHED)
        self.assertEqual(StockMovement.objects.get(shipment=demo).quantity, -1)
        self.assertEqual(Shipment.objects.filter(pk=real.pk).values().get(), live_before)
        self.assertEqual(list(StockMovement.objects.exclude(product=self.demo_product).values()), stock_before)

    def test_demo_collection_rechecks_ownership_after_loading_current_parcel(self):
        real = self.shipment(status='ready', locked_at=timezone.now(), waybill='REAL-COLLECTION')
        real.patient.demo_data_set_id = self.demo.pk  # stale or caller-modified cached patient
        with self.assertRaisesMessage(ValidationError, 'Only demo parcels'):
            dispatch_shipment(shipment=real, actor=self.admin, tracking_number=real.waybill,
                              confirm=True, demo_only=True)
        real.refresh_from_db()
        self.assertEqual(real.status, Shipment.Status.READY)
        self.assertFalse(StockMovement.objects.filter(shipment=real).exists())

    @override_settings(TCG_API_KEY='')
    def test_demo_only_booking_and_cancel_are_simulated_in_mixed_week(self):
        demo = self.demo_shipment(status='ready', locked_at=timezone.now())
        real = self.shipment(status='ready', locked_at=timezone.now())
        monday = self.today - timedelta(days=self.today.weekday())
        with patch('care.courier._call') as http:
            booked, failed = courier.book_waybills(company=self.company, actor=self.admin, week_start=monday, demo_only=True)
            self.assertEqual([p.pk for p in booked], [demo.pk])
            self.assertEqual(failed, [])
            demo.refresh_from_db()
            self.assertTrue(courier.cancel_booking(demo))
            with self.assertRaisesMessage(ValidationError, 'simulated'):
                courier.label_url(demo)
        http.assert_not_called()
        real.refresh_from_db()
        self.assertEqual(real.waybill, '')
        self.assertTrue(demo.waybill.startswith('DEMO-'))
        self.assertEqual(demo.courier_shipment_id, f'demo-{demo.pk}')
        self.assertTrue(demo.booked_address)

    def test_demo_purchase_receives_only_owned_stock_and_keeps_ownership(self):
        real_balances = self.balances()
        order = self.demo_order()
        self.assertEqual(order.demo_data_set_id, self.demo.pk)
        self.receive(order)
        self.assertEqual(StockMovement.objects.filter(product=self.demo_product).get().quantity, 3)
        for pk, values in real_balances.items():
            if pk != self.demo_product.pk:
                self.assertEqual(self.balances()[pk], values)

    def test_mixing_products_suppliers_and_different_demo_runs_is_rejected(self):
        another = DemoDataSet.objects.create(company=self.company, created_by=self.super_admin, status='cleared')
        for supplier, product in ((self.supplier, self.demo_product), (self.demo_supplier, self.open_product)):
            SupplierProduct.objects.create(company=self.company, supplier=supplier, product=product, supplier_code='MIXED')
            with self.subTest(supplier=supplier.pk), self.assertRaisesMessage(ValidationError, 'same demo data set'):
                self.create_order(supplier=supplier, items=[{'product': product, 'quantity': 1}])
        self.demo_product.demo_data_set = another
        self.demo_product.save(update_fields=['demo_data_set'])
        with self.assertRaisesMessage(ValidationError, 'same demo data set'):
            self.demo_order()

    def test_supplier_edit_rejects_real_product_link_to_demo_supplier(self):
        with self.assertRaisesMessage(ValidationError, 'same demo data set'):
            save_supplier(company=self.company, actor=self.super_admin, supplier=self.demo_supplier,
                name=self.demo_supplier.name, products=[{'product': self.open_product, 'supplier_code': 'LIVE'}],
                submission_key=uuid4())
        self.assertEqual(list(self.demo_supplier.product_links.values_list('product_id', flat=True)), [self.demo_product.pk])

    def test_real_patient_cannot_order_demo_product_or_demo_patient_real_product(self):
        for patient, product in ((self.patient, self.demo_product), (self.demo_patient, self.open_product)):
            self.assertIn('same demo data set', product_allowance(patient, product)[2])
        with self.assertRaisesMessage(ValidationError, 'same demo data set'):
            set_basket_quantity(company=self.company, patient=self.patient, actor=self.patient_user,
                                product=self.demo_product, quantity=1)

    def test_prescribing_rejects_mixed_and_stale_cached_product_ownership(self):
        from .treatment import create_authorization

        self.demo_product.demo_data_set_id = None  # a stale form cannot turn an owned product into live stock
        with self.assertRaisesMessage(ValidationError, 'same demo data set'):
            create_authorization(company=self.company, patient=self.patient, actor=self.doctor,
                product=self.demo_product, max_dose='As prescribed', quantity_per_cycle=1,
                starts_on=self.today, expires_on=self.today+timedelta(days=28), review_interval_days=28)
        self.assertIn('same demo data set', product_allowance(self.patient, self.demo_product)[2])

    def test_corrupt_demo_shipment_cannot_deduct_real_stock(self):
        parcel = self.demo_shipment()
        parcel.items.update(product=self.open_product)
        before = list(StockMovement.objects.values_list('pk', 'quantity'))
        with self.assertRaisesMessage(ValidationError, 'same demo data set'):
            consume_stock(parcel, self.admin, override=True, reason='Even an override must isolate demo data')
        self.assertEqual(list(StockMovement.objects.values_list('pk', 'quantity')), before)
