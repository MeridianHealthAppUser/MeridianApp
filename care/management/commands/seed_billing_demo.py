"""Local demo data for billing, Weekly shipping and the email journeys (the 24–25 Oct 2026 build): seven patients whose
subscriptions sit in each billing state, the coming shipping week's parcels with what Check and lock week would say
about each, earlier parcels in Dispatch history with courier statuses, and non-prescription requests. Debit orders go
through the sandbox provider and the billing code itself, so cycles, invoices, payments, retries, the failure queue
and tasks are what the app would make. Run `manage.py seed_demo` first. Development only; run it once."""

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from care.billing import (
    approve_mandate, bill_subscription, complete_card_payment, record_manual_payment, review_mandates,
    start_debit_order, start_order_payment,
)
from care.journeys import default_journeys
from care.models import (
    DebitMandate, DeliveryAddress, MedicationDose, MedicationProduct, PatientSubscription, Payment, PracticeSettings,
    Shipment, ShipmentItem, SubscriptionCycle, TreatmentAuthorization,
)
from care.notifications import paused as notifications_paused
from care.operations import prepare_week
from care.parcels import schedule_parcels, shipping_day_from, shipping_weekday
from care.pharmacy import accept_order, set_basket_quantity, submit_basket
from care.treatment import enroll_local_subscription
from practices.models import Company, Patient

PASSWORD = 'MeridianDemo!2026'
# (email, first name, last name, record number, city)
NEW_PATIENTS = (
    ('lerato.d@example.co.za', 'Lerato', 'Dlamini', 'MH-DEMO-101', 'Pretoria'),
    ('ruan.b@example.co.za', 'Ruan', 'Botha', 'MH-DEMO-102', 'Stellenbosch'),
    ('megan.f@example.co.za', 'Megan', 'Fourie', 'MH-DEMO-103', 'Durban'),
    ('thabo.n@example.co.za', 'Thabo', 'Nkosi', 'MH-DEMO-104', 'Johannesburg'),
)
ADDRESSES = {
    'Nadia': ('14 Kloof Street', 'Gardens', 'Cape Town', 'WC', '8001', '082 555 0100', ''),
    'Ayesha': ('22 Jan Smuts Avenue', 'Parktown', 'Johannesburg', 'GP', '2193', '082 555 0102', 'Gate code 4471'),
    'Pieter': ('8 Florida Road', 'Morningside', 'Durban', 'KZN', '4001', '082 555 0103', ''),
    'Zanele': ('31 Duncan Street', 'Hatfield', 'Pretoria', 'GP', '0083', '082 555 0104', ''),
    'Lerato': ('5 Lynnwood Road', 'Brooklyn', 'Pretoria', 'GP', '0181', '082 555 0111', 'Leave with security'),
    'Ruan': ('17 Dorp Street', 'Die Boord', 'Stellenbosch', 'WC', '7600', '082 555 0112', ''),
    # No contact number, so Check and lock week holds Megan's parcels.
    'Megan': ('40 Musgrave Road', 'Berea', 'Durban', 'KZN', '4001', '', ''),
    'Thabo': ('9 Vilakazi Street', 'Orlando West', 'Soweto', 'GP', '1804', '082 555 0114', ''),
}


class Command(BaseCommand):
    help = 'Add demo billing, Weekly shipping and email journey data to the local Meridian demo (after seed_demo).'

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG or settings.PAYMENT_PROVIDER != 'sandbox':
            raise CommandError('Development only, with PAYMENT_PROVIDER=sandbox: it approves debit orders and payments itself.')
        company = Company.objects.filter(slug='meridian-health').first()
        users = get_user_model().objects
        if company is None or not users.filter(email='sam.marchant@meridianhealth.co.za').exists():
            raise CommandError('Run `manage.py seed_demo` first.')
        if Patient.objects.filter(company=company, medical_record_number='MH-DEMO-101').exists():
            raise CommandError('The billing demo is already seeded. Restore a database backup to seed it again.')
        self.company, self.today = company, timezone.localdate()
        self.doctor = users.get(email='sam.marchant@meridianhealth.co.za')
        self.admin = users.get(email='lindiwe.mahlangu@meridianhealth.co.za')
        with notifications_paused():  # Demo debits and failures email nobody.
            self.seed()

    # Building blocks

    def dose(self, mg):
        """A priced Semaglutide dose of this many mg, whichever pen the practice's catalogue sends it in."""
        dose = MedicationDose.objects.filter(company=self.company, pen__name='Semaglutide', amount_mg=Decimal(mg),
                                             price__isnull=False, is_active=True).select_related('pen').order_by('pk').first()
        if dose is None:
            raise CommandError(f'The catalogue has no priced Semaglutide {mg} mg dose.')
        return dose

    def patient(self, first_name):
        return Patient.objects.get(company=self.company, first_name=first_name, medical_record_number__startswith='MH-DEMO')

    def prescribe(self, patient, dose):
        """A prescription that started four weeks before the coming shipping day, so its next parcel is that day."""
        authorization = TreatmentAuthorization.objects.create(
            company=self.company, patient=patient, prescribed_by=self.doctor, product=dose.pen, dose=dose,
            max_dose=dose.label, quantity_per_cycle=1, starts_on=self.started, expires_on=self.started + timedelta(weeks=24),
            review_interval_days=180)
        plan = enroll_local_subscription(company=self.company, patient=patient, actor=patient.user,
                                         authorization=authorization, confirm=True)
        schedule_parcels(authorization)
        return authorization, plan

    def address(self, patient):
        street, suburb, city, province, postal_code, phone, instructions = ADDRESSES[patient.first_name]
        DeliveryAddress.objects.update_or_create(company=self.company, patient=patient, defaults=dict(
            street=street, suburb=suburb, city=city, province=province, postal_code=postal_code, phone=phone,
            instructions=instructions, updated_by=patient.user))

    def debit_order(self, plan, *, method, label, first_debit):
        """An approved debit order whose first debit is first_debit; nothing is collected until bill() runs."""
        plan.next_debit_on = self.today + timedelta(days=1)
        plan.save(update_fields=('next_debit_on', 'updated_at'))
        mandate, _ = start_debit_order(subscription=plan, actor=plan.patient.user, method=method)
        mandate = approve_mandate(mandate=mandate, account_label=label, actor=plan.patient.user)
        plan.next_debit_on = first_debit
        plan.save(update_fields=('next_debit_on', 'updated_at'))
        return mandate

    def bill(self, plan, *days):
        for day in days:
            bill_subscription(plan, today=day)

    def decline_from_now(self, mandate):
        DebitMandate.objects.filter(pk=mandate.pk).update(provider_reference=f'sandbox-decline-{mandate.pk}')

    def dispatched(self, patient, authorization, plan, day, *, waybill, courier_status, delivered, product=None, quantity=1):
        """An earlier parcel, already collected by The Courier Guy (recorded directly: dispatch only takes today's): the
        prescription's medicine, or a non-prescription product."""
        product = product or authorization.product
        dose = authorization.max_dose if product == authorization.product else product.strength
        address = DeliveryAddress.objects.filter(company=self.company, patient=patient).first()
        sent = timezone.make_aware(datetime.combine(day, time(11)))
        shipment = Shipment.objects.create(
            company=self.company, patient=patient, subscription=plan, authorization=authorization, cycle_number=1,
            scheduled_for=day, status=Shipment.Status.DELIVERED if delivered else Shipment.Status.DISPATCHED,
            prepared_by=self.admin, prepared_at=sent - timedelta(days=4), locked_at=sent - timedelta(days=3),
            waybill=waybill, tracking_number=waybill, courier_shipment_id=f'demo-{waybill}', courier_status=courier_status,
            waybill_created_at=sent - timedelta(days=2), dispatched_at=sent,
            delivered_at=sent + timedelta(days=2) if delivered else None,
            dispatch_snapshot={
                'patient_name': str(patient), 'patient_id_number': patient.id_number, 'company_name': self.company.name,
                'scheduled_for': day.isoformat(), 'waybill': waybill,
                'items': [{'product': str(product), 'dose': dose, 'quantity': quantity,
                           'batch_note': 'SG-2609, expiry 04/2027'}],
                **({'delivery_address': {'recipient': str(patient), 'lines': address.lines(), 'phone': address.phone}} if address else {}),
            })
        ShipmentItem.objects.create(company=self.company, shipment=shipment, product=product,
                                    dose=dose, quantity=quantity, batch_note='SG-2609, expiry 04/2027')
        return shipment

    # The demo

    def seed(self):
        company, today = self.company, self.today
        settings_row, _ = PracticeSettings.objects.get_or_create(company=company)
        weekday = shipping_weekday(company)
        self.ship_day = shipping_day_from(today + timedelta(days=1), weekday)
        self.week = self.ship_day - timedelta(days=self.ship_day.weekday())
        self.started = self.ship_day - timedelta(weeks=4)
        if not settings_row.collection_street:
            # Placeholder collection details, so Weekly shipping's courier steps have something to show. Replace them
            # with the practice's own (Practice settings > Courier) before booking a real waybill.
            for field, value in dict(collection_business='Meridian Health (demo address)', collection_street='1 Demo Road',
                                     collection_suburb='Sandton', collection_city='Johannesburg', collection_province='GP',
                                     collection_postal_code='2196', collection_contact='Lindiwe Mahlangu',
                                     collection_phone='011 555 0100', collection_email='dispensary@example.co.za').items():
                setattr(settings_row, field, value)
            settings_row.save()

        users = get_user_model().objects
        for email, first_name, last_name, record, city in NEW_PATIENTS:
            user = users.create_user(email=email, password=PASSWORD, first_name=first_name, last_name=last_name)
            Patient.objects.create(company=company, user=user, first_name=first_name, last_name=last_name,
                                   medical_record_number=record, city=city, phone=ADDRESSES[first_name][5] or '082 555 0113',
                                   assigned_doctor=self.doctor, is_active=True)
        low, mid, high = self.dose('0.25'), self.dose('0.5'), self.dose('1')
        for name in ADDRESSES:
            self.address(self.patient(name))

        # Nadia (the demo's main patient): her starting plan, paid by DebiCheck since it began.
        nadia = self.patient('Nadia')
        authorization = TreatmentAuthorization.objects.filter(company=company, patient=nadia, status='active').order_by('starts_on').first()
        plan = PatientSubscription.objects.filter(company=company, patient=nadia).exclude(status='cancelled').first()
        if plan is None and authorization is not None and authorization.starts_on <= today:
            plan = enroll_local_subscription(company=company, patient=nadia, actor=nadia.user, authorization=authorization, confirm=True)
        if plan is not None:
            self.debit_order(plan, method='debicheck', label='FNB ···1234', first_debit=authorization.starts_on)
            self.bill(plan, today)
            last = Shipment.objects.filter(company=company, patient=nadia, scheduled_for__lt=today,
                                           status__in=(Shipment.Status.DRAFT, Shipment.Status.READY)).order_by('-scheduled_for').first()
            if last is not None:
                Shipment.objects.filter(pk=last.pk).update(status=Shipment.Status.DISPATCHED, waybill='TCG90410022',
                                                           tracking_number='TCG90410022', courier_status='in-transit',
                                                           locked_at=timezone.now() - timedelta(days=6),
                                                           dispatched_at=timezone.now() - timedelta(days=5))

        # Ayesha: card, paid up. Her parcel this week passes every check.
        ayesha = self.patient('Ayesha')
        authorization, plan = self.prescribe(ayesha, low)
        self.debit_order(plan, method='card', label='Visa ···4242', first_debit=self.started - timedelta(days=2))
        self.bill(plan, self.started - timedelta(days=2), today)
        self.dispatched(ayesha, authorization, plan, self.started, waybill='TCG90387711', courier_status='delivered', delivered=True)

        # Pieter: DebiCheck; this cycle's debit failed twice and is retried in a few days.
        pieter = self.patient('Pieter')
        authorization, plan = self.prescribe(pieter, high)
        first = today - timedelta(weeks=4, days=3)
        mandate = self.debit_order(plan, method='debicheck', label='Capitec ···7781', first_debit=first)
        self.bill(plan, first)
        self.decline_from_now(mandate)
        self.bill(plan, first + timedelta(weeks=4), first + timedelta(weeks=4, days=3))
        self.dispatched(pieter, authorization, plan, self.started, waybill='TCG90387712', courier_status='delivered', delivered=True)

        # Zanele: card; three failed attempts, so the cycle is in the failure queue with a task for the practice.
        zanele = self.patient('Zanele')
        authorization, plan = self.prescribe(zanele, mid)
        first = today - timedelta(weeks=6, days=3)
        mandate = self.debit_order(plan, method='card', label='Mastercard ···5100', first_debit=first)
        self.bill(plan, first)
        self.decline_from_now(mandate)
        second = first + timedelta(weeks=4)
        self.bill(plan, second, second + timedelta(days=3), second + timedelta(days=7))
        self.dispatched(zanele, authorization, plan, self.started, waybill='TCG90387713', courier_status='delivery-failed-attempt', delivered=False)

        # Lerato: DebiCheck, paid; then her doctor raised her dose (and price), so the new amount needs her approval.
        lerato = self.patient('Lerato')
        authorization, plan = self.prescribe(lerato, low)
        self.debit_order(plan, method='debicheck', label='FNB ···4821', first_debit=self.started - timedelta(days=2))
        self.bill(plan, self.started - timedelta(days=2), today)
        self.dispatched(lerato, authorization, plan, self.started, waybill='TCG90387714', courier_status='delivered', delivered=True)
        TreatmentAuthorization.objects.filter(pk=authorization.pk).update(dose=high, max_dose=high.label, product=high.pen)
        ShipmentItem.objects.filter(shipment__patient=lerato, shipment__status=Shipment.Status.DRAFT).update(
            dose=high.label, product=high.pen)
        review_mandates(company, lerato, prescription_changed=True)

        # Ruan: no debit order yet; he paid this parcel by EFT, recorded by the doctor from Weekly shipping.
        ruan = self.patient('Ruan')
        authorization, plan = self.prescribe(ruan, mid)
        parcel = Shipment.objects.filter(company=company, patient=ruan, scheduled_for=self.ship_day).first()
        if parcel is not None:
            record_manual_payment(company=company, actor=self.doctor, amount=mid.price, method='eft',
                                  reference='EFT RBOTHA', note='Paid by EFT while his debit order is set up.',
                                  shipment=parcel, from_shipping=True)

        # Megan: card, paid; no contact number on her delivery address. She also bought creatine sachets, paid by card.
        megan = self.patient('Megan')
        authorization, plan = self.prescribe(megan, low)
        self.debit_order(plan, method='card', label='Visa ···0119', first_debit=self.started - timedelta(days=2))
        self.bill(plan, self.started - timedelta(days=2), today)
        creatine, _ = MedicationProduct.objects.get_or_create(
            company=company, name='Creatine monohydrate sachets', strength='30 × 5 g',
            defaults=dict(category=MedicationProduct.Category.OTHER, price=Decimal('240.00'), requires_authorisation=False,
                          requires_cold_chain=False, description='A month of sachets. No prescription needed.'))
        order = self.creatine_order(megan, creatine, 2)
        payment, _ = start_order_payment(order=order, actor=megan.user)
        complete_card_payment(payment=payment, paid=True, reference=f'sandbox-{payment.pk}')
        accept_order(order=order, actor=self.admin, scheduled_for=self.ship_day, confirm=True)

        # Thabo: chose a card, but hasn't finished approving it.
        thabo = self.patient('Thabo')
        authorization, plan = self.prescribe(thabo, mid)
        start_debit_order(subscription=plan, actor=thabo.user, method='card')

        # Ayesha asked for creatine too and hasn't paid yet.
        self.creatine_order(ayesha, creatine, 1)
        # Creatine that went out on its own two weeks before the coming shipping day, so last week's Dispatch history
        # (the page's first view) has parcels in it.
        self.creatine_history(creatine)

        self.backdate()
        # The coming week's parcels marked ready, as Mark ready does; Check and lock week is left to try.
        ready, left = prepare_week(company=company, actor=self.admin, week_start=self.week)
        added = default_journeys(company)
        self.stdout.write(self.style.SUCCESS(
            f'Billing demo ready. Shipping week {self.week:%-d %b}: {len(ready)} parcels ready, {len(left)} left as drafts. '
            f'{added} journey emails added (all off). New patient logins use {PASSWORD}.'))

    def creatine_history(self, creatine):
        day = self.ship_day - timedelta(weeks=2)
        for name, waybill, status in (('Pieter', 'TCG90399420', 'delivered'), ('Lerato', 'TCG90399421', 'out-for-delivery')):
            patient = self.patient(name)
            plan = PatientSubscription.objects.filter(company=self.company, patient=patient).first()
            self.dispatched(patient, plan.authorization, plan, day, waybill=waybill, courier_status=status,
                            delivered=status == 'delivered', product=creatine)

    def backdate(self):
        """The billing code stamps everything with now; date each payment, cycle, debit order and subscription by when
        it would have happened, so the pages read as weeks of history."""
        retry_days = PracticeSettings.objects.get(company=self.company).retry_days()
        at = lambda day: timezone.make_aware(datetime.combine(day, time(8)))
        for plan in PatientSubscription.objects.filter(company=self.company, cycles__isnull=False).distinct():
            first = plan.cycles.order_by('cycle_number').first()
            PatientSubscription.objects.filter(pk=plan.pk).update(starts_on=first.starts_on, created_at=at(first.starts_on - timedelta(days=1)))
            DebitMandate.objects.filter(subscription=plan).update(created_at=at(first.starts_on - timedelta(days=1)),
                                                                  approved_at=at(first.starts_on - timedelta(days=1)))
            for cycle in plan.cycles.all():
                for payment in Payment.objects.filter(subscription_cycle=cycle, kind=Payment.Kind.SUBSCRIPTION, checkout=False):
                    day = cycle.due_on + timedelta(days=retry_days[payment.attempt - 2] if payment.attempt > 1 else 0)
                    Payment.objects.filter(pk=payment.pk).update(
                        created_at=at(day), paid_at=at(day) if payment.paid_at else None, due_on=cycle.due_on)
                    if payment.paid_at:
                        SubscriptionCycle.objects.filter(pk=cycle.pk).update(paid_at=at(day))

    def creatine_order(self, patient, product, quantity):
        basket = set_basket_quantity(company=self.company, patient=patient, actor=patient.user, product=product, quantity=quantity)
        street, suburb, city, province, postal_code, phone, _ = ADDRESSES[patient.first_name]
        return submit_basket(order=basket, actor=patient.user, confirm=True, expected_revision=basket.revision,
                             delivery_address={'line1': street, 'line2': suburb, 'city': city, 'province': province,
                                               'postal_code': postal_code, 'phone': phone or '082 555 0113'})
