"""Local demo data for billing, Weekly shipping and the email journeys (the 24–25 Oct 2026 build): patients whose
subscriptions sit in each billing state, the coming shipping week's parcels with what Check and lock week would say
about each, earlier parcels in Dispatch history with courier statuses, and non-prescription requests. Cards go through
the sandbox provider and the billing code itself, so cycles, invoices, payments, retries, the failure queue and tasks
are what the app would make.

Since 22 Nov 2026 it also shows card-only payments and what came with them: a same-price dose change that doesn't ask
for the card again (Sipho), a course that starts on the first payment (Kagiso), prescriptions too old to start on
without the doctor (Naledi, who hasn't asked yet, and Johan, who has), a first payment so late the first parcel waits
for the doctor (Precious), and a parcel paid for before a pause that still goes out (Busi).

Five Tirzepatide patients show its 4- and 5-weekly doses at different points of a prescription: Ashleigh starting the
standard plan today, Pierre four weeks in (stepping up to 5 mg), James thirteen weeks in (the doctor stepped up to
7.5 mg, billed every 5 weeks, with its pens in week 9, and steps up again to 10 mg, every 4 weeks, today: its pens
start in week 14), Andre twelve weeks into 7.5 mg (every 5 weeks) and Mark seventeen weeks into 12.5 mg, whose
missed payment, paid late, carried the treatment on.

Since 25 Nov 2026 payments follow the order cut-off (Practice settings): the first payment's parcel goes on the first
shipping day whose cut-off is still to come, and each payment after it is taken a week before its parcel. Billing is
run day by day through each patient's weeks (run()), as `manage.py run_debits` would have, so retries fall on their
days too.
Run `manage.py seed_demo` first. Development only; run it once."""

import uuid
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from care.billing import (
    approve_mandate, bill_subscription, complete_card_payment, covering_cycle, parcel_payment_reason, record_manual_payment,
    review_mandates, start_cycle_payment, start_debit_order, start_order_payment,
)
from care.journeys import default_journeys
from care.models import (
    DebitMandate, DeliveryAddress, Invoice, InvoiceLine, MedicationDose, MedicationProduct, PatientEvent, PatientSubscription,
    PracticeSettings,
    Shipment, ShipmentItem, StartingPlan, SubscriptionCycle, TreatmentAuthorization,
)
from care.prescriptions import ask_to_confirm, change_prescription, course_steps, current_prescription
from care.starting_plans import initiate_prescription, plan_schedule
from care.notifications import paused as notifications_paused
from care.operations import prepare_week
from care.parcels import (
    PAYMENT_LEAD, first_parcel_day, first_shipping_day, order_cutoff, schedule_parcels, shipping_day_from, shipping_weekday,
)
from care.pharmacy import accept_order, set_basket_quantity, submit_basket
from care.treatment import change_subscription_status, enroll_local_subscription
from practices.models import Company, Patient

PASSWORD = 'MeridianDemo!2026'
# (email, first name, last name, record number, city)
NEW_PATIENTS = (
    ('lerato.d@example.co.za', 'Lerato', 'Dlamini', 'MH-DEMO-101', 'Pretoria'),
    ('ruan.b@example.co.za', 'Ruan', 'Botha', 'MH-DEMO-102', 'Stellenbosch'),
    ('megan.f@example.co.za', 'Megan', 'Fourie', 'MH-DEMO-103', 'Durban'),
    ('thabo.n@example.co.za', 'Thabo', 'Nkosi', 'MH-DEMO-104', 'Johannesburg'),
    # Card payments only, and what came with them (22 Nov 2026).
    ('sipho.m@example.co.za', 'Sipho', 'Mthembu', 'MH-DEMO-105', 'Johannesburg'),
    ('kagiso.m@example.co.za', 'Kagiso', 'Molefe', 'MH-DEMO-106', 'Pretoria'),
    ('naledi.k@example.co.za', 'Naledi', 'Khumalo', 'MH-DEMO-107', 'Durban'),
    ('johan.vw@example.co.za', 'Johan', 'van Wyk', 'MH-DEMO-108', 'Bloemfontein'),
    ('precious.n@example.co.za', 'Precious', 'Ndlovu', 'MH-DEMO-109', 'Polokwane'),
    ('busi.z@example.co.za', 'Busi', 'Zulu', 'MH-DEMO-110', 'Pietermaritzburg'),
    # Tirzepatide at different points of a prescription.
    ('ashleigh.k@example.co.za', 'Ashleigh', 'Kruger', 'MH-DEMO-111', 'George'),
    ('pierre.l@example.co.za', 'Pierre', 'Lombard', 'MH-DEMO-112', 'Paarl'),
    ('james.b@example.co.za', 'James', 'Bester', 'MH-DEMO-113', 'Cape Town'),
    ('andre.v@example.co.za', 'Andre', 'Venter', 'MH-DEMO-114', 'Centurion'),
    ('mark.t@example.co.za', 'Mark', 'Thompson', 'MH-DEMO-115', 'Umhlanga'),
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
    'Sipho': ('12 Rivonia Road', 'Sandton', 'Johannesburg', 'GP', '2196', '082 555 0115', ''),
    'Kagiso': ('7 Church Street', 'Arcadia', 'Pretoria', 'GP', '0083', '082 555 0116', ''),
    'Naledi': ('3 Marine Parade', 'North Beach', 'Durban', 'KZN', '4001', '082 555 0117', ''),
    'Johan': ('21 Nelson Mandela Drive', 'Brandwag', 'Bloemfontein', 'FS', '9301', '082 555 0118', ''),
    'Precious': ('15 Landros Mare Street', 'Polokwane Central', 'Polokwane', 'LP', '0699', '082 555 0119', ''),
    'Busi': ('44 Church Street', 'Central', 'Pietermaritzburg', 'KZN', '3201', '082 555 0120', 'Ring the bell twice'),
    'Ashleigh': ('6 York Street', 'Central', 'George', 'WC', '6529', '082 555 0121', ''),
    'Pierre': ('18 Main Road', 'Paarl Central', 'Paarl', 'WC', '7646', '082 555 0122', ''),
    'James': ('9 Bree Street', 'City Centre', 'Cape Town', 'WC', '8001', '082 555 0123', 'Concierge at reception'),
    'Andre': ('27 Lenchen Avenue', 'Centurion Central', 'Centurion', 'GP', '0157', '082 555 0124', ''),
    'Mark': ('3 Chartwell Drive', 'Umhlanga Rocks', 'Umhlanga', 'KZN', '4319', '082 555 0125', ''),
}


class Command(BaseCommand):
    help = 'Add demo billing, Weekly shipping and email journey data to the local Meridian demo (after seed_demo).'

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG or settings.PAYMENT_PROVIDER != 'sandbox':
            raise CommandError(
                f'Development only, with the sandbox payment provider: it saves cards and takes payments itself. '
                f'PAYMENT_PROVIDER is "{settings.PAYMENT_PROVIDER}" here (your .env may set it). Run it as '
                f'`PAYMENT_PROVIDER=sandbox python manage.py seed_billing_demo`.')
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

    def prescribe(self, patient, dose, start=None):
        """A prescription that started on start: by default the day before the order cut-off for the shipping day four
        weeks before the coming one, so its first parcel was that day and its next is the coming one. A patient who
        pays starts on their first payment (treatment starts on the first payment)."""
        start = start or self.started
        authorization = TreatmentAuthorization.objects.create(
            company=self.company, patient=patient, prescribed_by=self.doctor, product=dose.pen, dose=dose,
            max_dose=dose.label, quantity_per_cycle=dose.pens_per_dispatch or 1, starts_on=start,
            expires_on=first_shipping_day(start, self.company) + timedelta(weeks=24), review_interval_days=180)
        # Written on the day it starts, as a doctor writes one.
        TreatmentAuthorization.objects.filter(pk=authorization.pk).update(
            created_at=timezone.make_aware(datetime.combine(start, time(10))))
        authorization.refresh_from_db()
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

    def run(self, plan, start, end):
        """The daily payment run (manage.py run_debits) for this care plan, each day from start to end, as of that day:
        payments and retries on their days, with that day's dates on them and on the patient's updates."""
        day = start
        while day <= end:
            with self.as_of(day):
                bill_subscription(plan, today=day)
            day += timedelta(days=1)

    def decline_from_now(self, mandate):
        DebitMandate.objects.filter(pk=mandate.pk).update(provider_reference=f'sandbox-decline-{mandate.pk}')

    def dispatched(self, patient, authorization, plan, day, *, waybill, courier_status, delivered, product=None, quantity=1,
                   cycle_number=1):
        """An earlier parcel, already collected by The Courier Guy (recorded directly: dispatch only takes today's): the
        prescription's medicine, or a non-prescription product."""
        product = product or authorization.product
        dose = authorization.max_dose if product == authorization.product else product.strength
        address = DeliveryAddress.objects.filter(company=self.company, patient=patient).first()
        sent = timezone.make_aware(datetime.combine(day, time(11)))
        shipment = Shipment.objects.create(
            company=self.company, patient=patient, subscription=plan, authorization=authorization, cycle_number=cycle_number,
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

    @contextmanager
    def as_of(self, day):
        """Run the app's own code as if it were day: a doctor's change is always made today, so an earlier one is made
        on its day (10:00), with that day's dates on the prescription, its parcels, payments, audit and updates."""
        moment = timezone.make_aware(datetime.combine(day, time(10)))
        latest = PatientEvent.objects.order_by('-pk').values_list('pk', flat=True).first() or 0
        localtime = timezone.localtime
        with patch.object(timezone, 'now', lambda: moment), patch.object(
                timezone, 'localdate', lambda value=None, *args, **kwargs: localtime(moment if value is None else value).date()):
            yield
        # A patient's updates are stamped by a field default the clock doesn't reach.
        PatientEvent.objects.filter(pk__gt=latest).update(occurred_at=moment)

    def history(self, patient, authorization, plan, *, waybill, last_courier='delivered', until=None):
        """The parcels a prescription had before today (or until), as Weekly shipping would have left them: sent when
        the payment period covering its day was paid, held (a missed parcel, on the patient's timeline) when it wasn't."""
        past, until = [], until or self.today
        # The drafts planned when the course moved to start with its first pens, for the weeks already gone.
        Shipment.objects.filter(authorization__in=course_steps(authorization), status=Shipment.Status.DRAFT,
                                scheduled_for__lt=until).delete()
        for step in course_steps(authorization):  # A starting plan's steps each send their own pens.
            day, interval = first_parcel_day(step), timedelta(weeks=step.dose.dispatch_weeks or 4)
            while day < until and day <= step.expires_on:
                past.append((step, day))
                day += interval
        parcels = [Shipment(company=self.company, patient=patient, subscription=plan, authorization=step,
                            cycle_number=number, scheduled_for=day) for number, (step, day) in enumerate(past, start=1)]
        paid = [parcel for parcel in parcels if (cycle := covering_cycle(parcel)) is not None
                and cycle.status == SubscriptionCycle.Status.PAID]
        for parcel in parcels:
            number, day = parcel.cycle_number, parcel.scheduled_for
            step = parcel.authorization
            if parcel in paid:
                courier = last_courier if parcel is paid[-1] else 'delivered'
                self.dispatched(patient, step, plan, day, waybill=f'{waybill}{number}', courier_status=courier,
                                delivered=courier == 'delivered', cycle_number=number, quantity=step.quantity_per_cycle)
                continue
            parcel.save()
            ShipmentItem.objects.create(company=self.company, shipment=parcel, product=step.product,
                                        dose=step.max_dose, quantity=step.quantity_per_cycle)
            # Check and lock week held it: its payment didn't go through.
            Shipment.objects.filter(pk=parcel.pk).update(status=Shipment.Status.HELD,
                                                         hold_reason=parcel_payment_reason(parcel) or 'Not paid.')

    # The demo

    def seed(self):
        company, today = self.company, self.today
        settings_row, _ = PracticeSettings.objects.get_or_create(company=company)
        weekday = shipping_weekday(company)
        # The coming shipping day: today, when seeded on the shipping day.
        self.ship_day = shipping_day_from(today, weekday)
        self.week = self.ship_day - timedelta(days=self.ship_day.weekday())
        # The paid examples start (prescription and first payment, the same day: treatment starts on the first
        # payment) the day before the order cut-off for the shipping day four weeks ago, so their first parcel was that
        # day and the payment for the coming one (a week before it) is already in.
        self.first_parcel = self.ship_day - timedelta(weeks=4)
        self.started = paid_from = order_cutoff(self.first_parcel, company) - timedelta(days=1)
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

        # Nadia (the demo's main patient): her starting plan, paid by card since it began.
        nadia = self.patient('Nadia')
        authorization = TreatmentAuthorization.objects.filter(company=company, patient=nadia, status='active').order_by('starts_on').first()
        plan = PatientSubscription.objects.filter(company=company, patient=nadia).exclude(status='cancelled').first()
        if plan is None and authorization is not None and authorization.starts_on <= today:
            plan = enroll_local_subscription(company=company, patient=nadia, actor=nadia.user, authorization=authorization, confirm=True)
        if plan is not None:
            # seed_demo's placeholder payment period (due in a week, never collected) gives way to the real history
            # below; collected when the card is saved, it would count as a first payment made today and move the course.
            placeholders = plan.cycles.filter(status=SubscriptionCycle.Status.PENDING, attempts=0, payments__isnull=True)
            InvoiceLine.objects.filter(invoice__subscription_cycle__in=placeholders).delete()
            Invoice.objects.filter(subscription_cycle__in=placeholders).delete()
            placeholders.delete()
            self.debit_order(plan, method='card', label='Visa ···1234', first_debit=authorization.starts_on)
            # Billed from the course's start, so the first payment is on the day it began and nothing moves.
            self.run(plan, authorization.starts_on, today)
            last = Shipment.objects.filter(company=company, patient=nadia, scheduled_for__lt=today,
                                           status__in=(Shipment.Status.DRAFT, Shipment.Status.READY)).order_by('-scheduled_for').first()
            if last is not None:
                Shipment.objects.filter(pk=last.pk).update(status=Shipment.Status.DISPATCHED, waybill='TCG90410022',
                                                           tracking_number='TCG90410022', courier_status='in-transit',
                                                           locked_at=timezone.now() - timedelta(days=6),
                                                           dispatched_at=timezone.now() - timedelta(days=5))

        # Ayesha: card, paid up. Her parcel this week passes every check.
        ayesha = self.patient('Ayesha')
        authorization, plan = self.prescribe(ayesha, low, start=paid_from)
        self.debit_order(plan, method='card', label='Visa ···4242', first_debit=paid_from)
        self.run(plan, paid_from, today)
        self.history(ayesha, authorization, plan, waybill='TCG9038771')

        # Pieter: card; the payment for the coming parcel, a week before it, didn't go through and is being retried
        # (first on the order cut-off). Today's run hasn't happened yet.
        pieter = self.patient('Pieter')
        authorization, plan = self.prescribe(pieter, high, start=paid_from)
        mandate = self.debit_order(plan, method='card', label='Mastercard ···7781', first_debit=paid_from)
        due = self.ship_day - PAYMENT_LEAD
        self.run(plan, paid_from, due - timedelta(days=1))
        self.decline_from_now(mandate)
        self.run(plan, due, today - timedelta(days=1))
        self.history(pieter, authorization, plan, waybill='TCG9038772')

        # Zanele: card; three failed attempts, so the cycle is in the failure queue with a task for the practice.
        zanele = self.patient('Zanele')
        first = today - timedelta(weeks=6, days=3)
        authorization, plan = self.prescribe(zanele, mid, start=first)
        mandate = self.debit_order(plan, method='card', label='Mastercard ···5100', first_debit=first)
        self.run(plan, first, first)
        self.decline_from_now(mandate)
        self.run(plan, first + timedelta(days=1), today)
        # The first parcel went out (the courier is still trying to deliver it); the next was missed, unpaid.
        self.history(zanele, authorization, plan, waybill='TCG9038773', last_courier='delivery-failed-attempt')

        # Lerato: card, paid; then her doctor raised her dose (and price), so she needs to confirm the new amount.
        lerato = self.patient('Lerato')
        authorization, plan = self.prescribe(lerato, low, start=paid_from)
        self.debit_order(plan, method='card', label='Visa ···4821', first_debit=paid_from)
        self.run(plan, paid_from, today)
        self.history(lerato, authorization, plan, waybill='TCG9038774')
        TreatmentAuthorization.objects.filter(pk=authorization.pk).update(dose=high, max_dose=high.label, product=high.pen)
        ShipmentItem.objects.filter(shipment__patient=lerato, shipment__status=Shipment.Status.DRAFT).update(
            dose=high.label, product=high.pen)
        review_mandates(company, lerato, prescription_changed=True)

        # Ruan: no debit order yet; he paid this parcel by EFT, recorded by the practice administrator from Weekly shipping.
        ruan = self.patient('Ruan')
        authorization, plan = self.prescribe(ruan, mid)
        parcel = Shipment.objects.filter(company=company, patient=ruan, scheduled_for=self.ship_day).first()
        if parcel is not None:
            record_manual_payment(company=company, actor=self.admin, amount=mid.price, method='eft',
                                  reference='EFT RBOTHA', note='Paid by EFT while he adds his card.',
                                  shipment=parcel, from_shipping=True)

        # Megan: card, paid; no contact number on her delivery address. She also bought creatine sachets, paid by card.
        megan = self.patient('Megan')
        authorization, plan = self.prescribe(megan, low, start=paid_from)
        self.debit_order(plan, method='card', label='Visa ···0119', first_debit=paid_from)
        self.run(plan, paid_from, today)
        self.history(megan, authorization, plan, waybill='TCG9038777')
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

        self.card_only_examples(low, mid, paid_from)
        self.tirzepatide_examples()

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

    def written(self, patient, dose, days_ago, *, enroll=False):
        """A prescription the doctor wrote days_ago, with its parcels planned from then (and a care plan, if enroll)."""
        start = self.today - timedelta(days=days_ago)
        authorization = TreatmentAuthorization.objects.create(
            company=self.company, patient=patient, prescribed_by=self.doctor, product=dose.pen, dose=dose,
            max_dose=dose.label, quantity_per_cycle=1, starts_on=start,
            expires_on=first_shipping_day(start, self.company) + timedelta(weeks=24), review_interval_days=180)
        # Written on the day it starts, as a doctor writes one.
        TreatmentAuthorization.objects.filter(pk=authorization.pk).update(
            created_at=timezone.make_aware(datetime.combine(start, time(10))))
        authorization.refresh_from_db()
        schedule_parcels(authorization)
        plan = enroll_local_subscription(company=self.company, patient=patient, actor=patient.user,
                                         authorization=authorization, confirm=True) if enroll else None
        return authorization, plan

    def card_saved_today(self, plan, label):
        """The patient adds their card today: the sandbox saves it and the first payment is taken at once."""
        mandate, _ = start_debit_order(subscription=plan, actor=plan.patient.user, method='card')
        return approve_mandate(mandate=mandate, account_label=label, actor=plan.patient.user)

    def card_only_examples(self, low, mid, paid_from):
        """Card payments only, and what came with them (22 Nov 2026)."""
        company = self.company
        # Sipho: paid up by card; the doctor stepped Sipho up from 0.25 mg to 0.5 mg, the same R1 995, so the card
        # carries on without being asked for again (Practice settings: only when the amount goes above).
        sipho = self.patient('Sipho')
        authorization, plan = self.prescribe(sipho, low, start=paid_from)
        self.debit_order(plan, method='card', label='Visa ···3310', first_debit=paid_from)
        self.run(plan, paid_from, self.today)
        self.history(sipho, authorization, plan, waybill='TCG9038775')
        change_prescription(company=company, patient=sipho, actor=self.doctor, current=authorization, dose=mid, weeks=24,
                            reason='Tolerating 0.25 mg well', acknowledge_flags=True)

        # Kagiso: prescribed two weeks ago, added a card and paid today, so the whole course now starts today.
        kagiso = self.patient('Kagiso')
        authorization, plan = self.written(kagiso, low, 14, enroll=True)
        self.card_saved_today(plan, 'Visa ···6627')

        # Naledi: prescribed six weeks ago and never started. My Treatment asks Naledi to get the doctor to confirm first.
        naledi = self.patient('Naledi')
        self.written(naledi, low, 42)

        # Johan: prescribed seven weeks ago; Johan asked the doctor to confirm it, so there's a task and the record asks.
        johan = self.patient('Johan')
        authorization, _ = self.written(johan, mid, 49)
        ask_to_confirm(company=company, patient=johan, actor=johan.user, authorization=authorization)

        # Precious: prescribed six weeks ago, paid today. The course starts today, but the first parcel and further
        # payments wait for the doctor to confirm the prescription.
        precious = self.patient('Precious')
        authorization, plan = self.written(precious, low, 42, enroll=True)
        self.card_saved_today(plan, 'Mastercard ···9054')

        # Busi: paid up by card, then paused. The coming parcel is paid for, so it still goes out: the last one until
        # Busi resumes. The parcel after it is held.
        busi = self.patient('Busi')
        authorization, plan = self.prescribe(busi, mid, start=paid_from)
        self.debit_order(plan, method='card', label='Mastercard ···2280', first_debit=paid_from)
        self.run(plan, paid_from, self.today)
        self.history(busi, authorization, plan, waybill='TCG9038776')
        change_subscription_status(subscription=plan, actor=busi.user, action='pause', confirm=True)

    def tirzepatide(self, mg):
        dose = MedicationDose.objects.filter(company=self.company, pen__name='Tirzepatide', amount_mg=Decimal(mg),
                                             price__isnull=False, is_active=True).select_related('pen').order_by('pk').first()
        if dose is None:
            raise CommandError(f'The catalogue has no priced Tirzepatide {mg} mg dose.')
        return dose

    def starting_plan(self, patient, start):
        """The catalogue's Tirzepatide starting plan as the doctor would have started it on start (24 weeks): each step,
        with the care plan on the step in effect today. Its parcels are added by history() and schedule_parcels()."""
        plan = StartingPlan.objects.filter(company=self.company, medicine='Tirzepatide').order_by('pk').first()
        key, steps = uuid.uuid4(), []
        # Valid from start; its weeks run from its first pens, as a doctor writing it that day would have had.
        for position, (dose, starts_on, expires_on) in enumerate(plan_schedule(plan, 24, today=first_shipping_day(start, self.company)), start=1):
            starts_on = start if position == 1 else starts_on
            step = TreatmentAuthorization.objects.create(
                company=self.company, patient=patient, prescribed_by=self.doctor, product=dose.pen, dose=dose,
                max_dose=dose.label, quantity_per_cycle=dose.pens_per_dispatch, starts_on=starts_on, expires_on=expires_on,
                review_interval_days=180, plan_key=key, plan_step=position,
                log_text=f'Prescription initiated: Tirzepatide {plan.name.lower()}, 24 weeks.' if position == 1 else '')
            TreatmentAuthorization.objects.filter(pk=step.pk).update(created_at=timezone.make_aware(datetime.combine(start, time(10))))
            steps.append(step)
        current = next(step for step in steps if step.starts_on <= self.today <= step.expires_on)
        subscription = enroll_local_subscription(company=self.company, patient=patient, actor=patient.user,
                                                 authorization=current, confirm=True)
        return steps, subscription

    def tirzepatide_examples(self):
        """Tirzepatide at different points of a prescription: its doses are billed and sent every 4 weeks, except 7.5 mg
        and 12.5 mg, every 5 (their pens hold more than four doses)."""
        company, today = self.company, self.today
        # Ashleigh: starting the standard plan today (2.5 mg, then 5 mg from week 5), first paid today.
        ashleigh = self.patient('Ashleigh')
        first = initiate_prescription(company=company, patient=ashleigh, actor=self.doctor, current=None,
                                      plan=StartingPlan.objects.filter(company=company, medicine='Tirzepatide').order_by('pk').first(),
                                      weeks=24, acknowledge_flags=True)
        plan = enroll_local_subscription(company=company, patient=ashleigh, actor=ashleigh.user, authorization=first, confirm=True)
        self.card_saved_today(plan, 'Visa ···7720')

        # Pierre: four weeks into the plan, so this week the dose steps up to 5 mg (4 pens a parcel instead of 2).
        pierre = self.patient('Pierre')
        start = today - timedelta(weeks=4)
        steps, plan = self.starting_plan(pierre, start)
        self.debit_order(plan, method='card', label='Mastercard ···1188', first_debit=start)
        self.run(plan, start, today)
        self.history(pierre, steps[0], plan, waybill='TCG9041111')
        for step in steps:
            schedule_parcels(step)

        # James: thirteen weeks in. Paid and prescribed in week 0; the standard plan (2.5 mg, then 5 mg from week 5,
        # every 4 weeks). The day after the payment for week 9's parcel the doctor stepped up to 7.5 mg, sent and
        # billed every 5 weeks, so that payment's period ran 5 weeks (weeks 9-13). Today the doctor steps up to 10 mg,
        # every 4 weeks: its pens start in week 14, that period is 4 weeks and each payment after it comes a week
        # before a 10 mg parcel. All R3 295, so the card carries on.
        james = self.patient('James')
        start = today - timedelta(weeks=13)
        stepped_up = first_shipping_day(start, company) + timedelta(weeks=8) - PAYMENT_LEAD + timedelta(days=1)
        steps, plan = self.starting_plan(james, start)
        self.debit_order(plan, method='card', label='Visa ···5532', first_debit=start)
        self.run(plan, start, stepped_up)  # Weeks 0, 4 and 8.
        self.history(james, steps[0], plan, waybill='TCG9041112', until=stepped_up)
        with self.as_of(stepped_up):
            seven_five = change_prescription(
                company=company, patient=james, actor=self.doctor, current=current_prescription(company, james),
                dose=self.tirzepatide('7.5'), weeks=16, reason='Tolerating 5 mg well; stepping up', acknowledge_flags=True)
        # Its week 9 parcel went out (history() sends it: its period was paid); the 7.5 mg drafts after it stay.
        Shipment.objects.filter(patient=james, status=Shipment.Status.DRAFT, scheduled_for__lt=today).delete()
        self.history(james, seven_five, plan, waybill='TCG9041122')
        self.run(plan, stepped_up + timedelta(days=1), today)  # Week 13: the payment for week 14's parcel.
        change_prescription(company=company, patient=james, actor=self.doctor, current=seven_five,
                            dose=self.tirzepatide('10'), weeks=12, reason='Weight loss has slowed on 7.5 mg; stepping up',
                            acknowledge_flags=True)

        # Andre: twelve weeks into 7.5 mg, billed and sent every 5 weeks (paid in weeks 1, 6 and 11).
        andre = self.patient('Andre')
        start = today - timedelta(weeks=12)
        authorization, plan = self.prescribe(andre, self.tirzepatide('7.5'), start=start)
        self.debit_order(plan, method='card', label='Mastercard ···6604', first_debit=start)
        self.run(plan, start, today)
        self.history(andre, authorization, plan, waybill='TCG9041113')

        # Mark: seventeen weeks into 12.5 mg (every 5 weeks). The payment for his fourth parcel didn't go through, so
        # that parcel missed its order; Mark paid today, so the pens go on the first shipping day they can make, the
        # 5-week period starts with them and the prescription moved on by the same weeks.
        mark = self.patient('Mark')
        start = today - timedelta(weeks=17)
        authorization, plan = self.prescribe(mark, self.tirzepatide('12.5'), start=start)
        mandate = self.debit_order(plan, method='card', label='Visa ···8841', first_debit=start)
        fourth = first_shipping_day(start, company) + timedelta(weeks=15) - PAYMENT_LEAD
        self.run(plan, start, fourth - timedelta(days=1))
        self.decline_from_now(mandate)
        self.run(plan, fourth, today)
        self.history(mark, authorization, plan, waybill='TCG9041114')
        failed = plan.cycles.get(status=SubscriptionCycle.Status.FAILED)
        payment, _ = start_cycle_payment(cycle=failed, actor=mark.user)
        complete_card_payment(payment=payment, paid=True, reference=f'sandbox-{payment.pk}')

    def creatine_history(self, creatine):
        day = self.ship_day - timedelta(weeks=2)
        for name, waybill, status in (('Pieter', 'TCG90399420', 'delivered'), ('Lerato', 'TCG90399421', 'out-for-delivery')):
            patient = self.patient(name)
            plan = PatientSubscription.objects.filter(company=self.company, patient=patient).first()
            self.dispatched(patient, plan.authorization, plan, day, waybill=waybill, courier_status=status,
                            delivered=status == 'delivered', product=creatine)

    def backdate(self):
        """Cards are saved and subscriptions started today; date them the day before the first payment, so the pages
        read as weeks of history. Payments are dated by the run that took them (run())."""
        at = lambda day: timezone.make_aware(datetime.combine(day, time(8)))
        for plan in PatientSubscription.objects.filter(company=self.company, cycles__isnull=False).distinct():
            first = plan.cycles.order_by('cycle_number').first()
            if first.due_on >= self.today:
                continue  # Card added and first paid today (Kagiso, Precious).
            PatientSubscription.objects.filter(pk=plan.pk).update(starts_on=first.due_on, created_at=at(first.due_on - timedelta(days=1)))
            DebitMandate.objects.filter(subscription=plan).update(created_at=at(first.due_on - timedelta(days=1)),
                                                                  approved_at=at(first.due_on - timedelta(days=1)))
            # The patient's Updates: subscription started and the first card saved, the day before the first payment.
            card = DebitMandate.objects.filter(subscription=plan).order_by('pk').values_list('pk', flat=True).first()
            PatientEvent.objects.filter(patient=plan.patient, title__in=('Subscription started', 'Card saved')).filter(
                Q(source_type='care.patientsubscription', source_id=str(plan.pk))
                | Q(source_type='care.debitmandate', source_id=str(card)),
            ).update(occurred_at=at(first.due_on - timedelta(days=1)))

    def creatine_order(self, patient, product, quantity):
        basket = set_basket_quantity(company=self.company, patient=patient, actor=patient.user, product=product, quantity=quantity)
        street, suburb, city, province, postal_code, phone, _ = ADDRESSES[patient.first_name]
        return submit_basket(order=basket, actor=patient.user, confirm=True, expected_revision=basket.revision,
                             delivery_address={'line1': street, 'line2': suburb, 'city': city, 'province': province,
                                               'postal_code': postal_code, 'phone': phone or '082 555 0113'})
