"""Add an isolated, repeat-safe local dataset for clicking through Inventory and Operations."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from care.billing import billed_to, collect_cycle, record_manual_payment
from care.inventory import receive_stock_batch
from care.models import (
    AuditEvent, DebitMandate, DeliveryAddress, Invoice, MedicationDose, MedicationProduct,
    PatientClinicalSummary, PatientEvent, PatientSubscription, PracticeSettings, Shipment, ShipmentItem,
    SubscriptionCycle, WeightEntry,
)
from care.notifications import paused
from care.operations import lock_reasons, prepare_shipment
from care.parcels import schedule_parcels, shipping_day_from, shipping_weekday
from care.pharmacy import accept_order, set_basket_quantity, submit_basket
from care.prescriptions import change_prescription
from care.services import record_audit
from care.treatment import change_subscription_status
from practices.models import Company, CompanyMembership, Patient

PASSWORD = 'MeridianDemo!2026'
RECORD_PREFIX = 'MH-OPS-DEMO-'
SCENARIOS = (
    ('Emma', 'draft', 0), ('Liam', 'ready', 0), ('Zola', 'locked', 0),
    ('Sara', 'shortage', 2), ('Noah', 'shortage', 2), ('Ava', 'unpaid', 0),
    ('Leo', 'address', 1), ('Mia', 'paused', 1), ('Nia', 'delivered', 1), ('Ben', 'transit', 0),
)


class Command(BaseCommand):
    help = 'Add local demo stock, two doctors, ten patients/subscriptions and linked shipments. Does not reseed existing records.'

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG or settings.PAYMENT_PROVIDER != 'sandbox':
            raise CommandError('This command is only for development with PAYMENT_PROVIDER=sandbox.')
        self.company = Company.objects.select_for_update().filter(slug='meridian-health', is_active=True).first()
        if self.company is None:
            raise CommandError('Create the local Meridian Health practice first.')
        if Patient.objects.filter(company=self.company, medical_record_number__startswith=RECORD_PREFIX).exists():
            self.stdout.write('Operations demo already exists. Existing stock and test progress were left unchanged.')
            return
        membership = CompanyMembership.objects.filter(company=self.company, role='practice_admin',
                                                      is_active=True, user__is_active=True).select_related('user').first()
        if membership is None:
            raise CommandError('An active practice administrator is required.')
        self.admin = membership.user
        self.today = timezone.localdate()
        self.ship_day = shipping_day_from(self.today, shipping_weekday(self.company))
        self.week = self.ship_day - timedelta(days=self.ship_day.weekday())
        with paused():
            self.seed()
        self.stdout.write(self.style.SUCCESS(
            f'Operations demo added: 2 doctors, 10 patients/subscriptions, stock and non-prescription requests. '
            f'Open shipping week {self.week:%d %b %Y}. Demo accounts use {PASSWORD}.'))

    def user(self, email, first, last, *, staff=False):
        if get_user_model().objects.filter(email=email).exists():
            raise CommandError(f'Demo account {email} already exists; no existing account was changed.')
        return get_user_model().objects.create_user(email=email, password=PASSWORD, first_name=first,
                                                   last_name=last, is_staff=staff)

    def seed(self):
        self.doctors = []
        for first, last in (('Maya', 'Naidoo'), ('Lebo', 'Mokoena')):
            user = self.user(f'dr.{first.lower()}.demo@example.test', first, f'{last} (Demo)', staff=True)
            CompanyMembership.objects.create(company=self.company, user=user, role='doctor', clinician_type='doctor')
            self.doctors.append(user)
        PracticeSettings.objects.get_or_create(company=self.company)
        self.doses = []
        for name, strength, amount, price in (
            ('Semaglutide', '1.0 mg · Demo', '1', '1995'),
            ('Semaglutide', '2.0 mg · Demo', '2', '2295'),
            ('Tirzepatide', '5.0 mg · Demo', '5', '3295'),
        ):
            product = MedicationProduct.objects.create(company=self.company, name=name, strength=strength,
                price=Decimal(price), description='Synthetic local Operations demo stock; no real supply or prescription.')
            dose = MedicationDose.objects.create(company=self.company, pen=product, amount_mg=Decimal(amount),
                price=Decimal(price), pens_per_dispatch=1, dispatch_weeks=4, billing_weeks=4)
            self.doses.append(dose)
        self.creatine = MedicationProduct.objects.create(company=self.company, name='Creatine sachets (Demo)',
            strength='30 × 5 g', price=Decimal('240'), category='other', requires_authorisation=False,
            requires_cold_chain=False, description='Synthetic non-prescription test product.')
        receive_stock_batch(company=self.company, actor=self.admin, reference='OPS-DEMO opening stock',
            submission_key='ops-demo-opening-stock-v1', items=[
                dict(product=dose.pen, quantity=quantity, batch=f'OPS-DEMO-{index}',
                     expires_on=self.today + timedelta(days=365))
                for index, (dose, quantity) in enumerate(zip(self.doses, (20, 8, 1)), 1)
            ] + [dict(product=self.creatine, quantity=25, batch='OPS-DEMO-CREATINE',
                      expires_on=self.today + timedelta(days=365))])
        self.patients = {}
        for index, (name, scenario, dose_index) in enumerate(SCENARIOS, 1):
            self.seed_patient(index, name, scenario, self.doses[dose_index])
        self.seed_order(self.patients['Emma'], paid=False)
        self.seed_order(self.patients['Liam'], paid=True)
        record_audit(company=self.company, actor=self.admin, action='demo.operations_seeded', target=self.company,
                     metadata={'patients': 10, 'shipping_week': self.week.isoformat(), 'source': 'local_demo'})

    def seed_patient(self, index, name, scenario, dose):
        user = self.user(f'{name.lower()}.demo@example.test', name, 'Demo')
        doctor = self.doctors[(index - 1) % 2]
        patient = Patient.objects.create(company=self.company, user=user, first_name=name, last_name='Demo',
            medical_record_number=f'{RECORD_PREFIX}{index:02}', assigned_doctor=doctor,
            date_of_birth=date(1985 + index, 3, 10), phone=f'082555{2000+index}', city='Johannesburg')
        self.patients[name] = patient
        DeliveryAddress.objects.create(company=self.company, patient=patient, recipient=f'{name} Demo',
            street=f'{index} Demo Road', suburb='Sandton', city='Johannesburg', province='GP', postal_code='2196',
            phone='' if scenario == 'address' else patient.phone, instructions='Synthetic local test address.')
        PatientClinicalSummary.objects.create(company=self.company, patient=patient, height_cm=170,
            start_weight_kg=95, goal_weight_kg=80, medical_history=f'Fictional test patient. Operations scenario: {scenario}.',
            pancreatitis='no', thyroid_cancer='no', pregnancy='not_applicable')
        for offset, weight in ((28, '95'), (14, '93.5'), (0, '92')):
            WeightEntry.objects.create(company=self.company, patient=patient, recorded_by=user,
                recorded_on=self.today - timedelta(days=offset), weight_kg=Decimal(weight), note='Synthetic demo measurement')
        # Use the real prescription action so the doctor can renew/change it from the patient's record.
        authorization = change_prescription(company=self.company, patient=patient, actor=doctor, current=None,
            dose=dose, weeks=24, reason='Local demo scenario; fictional patient.')
        # Backdate only this new test course, then replace only its new drafts with the historical schedule.
        historical = scenario in ('delivered', 'transit')
        started = self.today - timedelta(weeks=5 if historical else 4)
        authorization.starts_on, authorization.expires_on = started, started + timedelta(weeks=24)
        authorization.save(update_fields=('starts_on', 'expires_on', 'updated_at'))
        ShipmentItem.objects.filter(shipment__authorization=authorization).delete()
        Shipment.objects.filter(authorization=authorization).delete()
        from care.treatment import enroll_local_subscription
        plan = enroll_local_subscription(company=self.company, patient=patient, actor=user,
                                         authorization=authorization, confirm=True)
        plan.starts_on = started
        plan.plan_name = f'Demo {dose.medicine} care plan'
        plan.save(update_fields=('starts_on', 'plan_name', 'updated_at'))
        schedule_parcels(authorization)
        mandate = DebitMandate.objects.create(company=self.company, patient=patient, subscription=plan,
            method='card', status='active', amount=dose.price, interval_weeks=4, provider='sandbox',
            reference=f'OPS-DEMO-{index:02}', provider_reference=f'sandbox-{ "decline-" if scenario == "unpaid" else ""}ops-demo-{index}',
            account_label='Demo card ···4242', approved_at=timezone.now(), created_by=user)
        cycle_start, number = started, 1
        while cycle_start <= self.today:
            end = cycle_start + timedelta(days=27)
            cycle = SubscriptionCycle.objects.create(company=self.company, patient=patient, subscription=plan,
                cycle_number=number, starts_on=cycle_start, ends_on=end, due_on=cycle_start, amount=dose.price, mandate=mandate)
            Invoice.objects.create(company=self.company, patient=patient, subscription_cycle=cycle,
                invoice_number=f'OPS-DEMO-{index:02}-{number}', issued_on=cycle_start, due_on=cycle_start,
                subtotal=dose.price, total=dose.price, status='issued', billed_to=billed_to(patient))
            if scenario == 'unpaid' and end >= self.today:
                collect_cycle(cycle, today=self.today, actor=self.admin)
            else:
                record_manual_payment(company=self.company, actor=self.admin, amount=dose.price, method='eft',
                    reference=f'OPS-DEMO-{index:02}-{number}', note='Fictional local demo payment', cycle=cycle)
            cycle_start += timedelta(weeks=4)
            number += 1
        plan.next_debit_on = cycle_start
        plan.save(update_fields=('next_debit_on', 'updated_at'))
        first_day = shipping_day_from(started, shipping_weekday(self.company))
        self.history(patient, authorization, plan, first_day, index, 'delivered')
        if historical:
            self.history(patient, authorization, plan, first_day + timedelta(weeks=4), index,
                         'in-transit' if scenario == 'transit' else 'delivered')
        else:
            parcel = Shipment.objects.filter(authorization=authorization, scheduled_for__gte=self.week,
                                            scheduled_for__lt=self.week + timedelta(days=7)).first()
            if parcel is None:
                raise CommandError(f'No current-week parcel for {patient}.')
            parcel.note = f'DEMO: {scenario}. This is a fictional patient and shipment.'
            parcel.save(update_fields=('note', 'updated_at'))
            if scenario not in ('draft', 'paused'):
                prepare_shipment(shipment=parcel, actor=self.admin)
            if scenario in ('locked', 'shortage'):
                parcel.refresh_from_db()
                problems = lock_reasons(parcel)
                if problems:
                    raise CommandError(f'Demo lock failed for {patient}: {problems}')
                parcel.locked_at = timezone.now()
                fields = ['locked_at', 'updated_at']
                if scenario == 'locked':
                    parcel.waybill = f'DEMO-LOCAL-{index:03}'
                    parcel.courier_shipment_id = f'demo-local-{index}'
                    parcel.courier_status = 'submitted'
                    fields += ['waybill', 'courier_shipment_id', 'courier_status']
                parcel.save(update_fields=fields)
                record_audit(company=self.company, actor=self.admin, patient=patient, action='shipment.locked',
                             target=parcel, metadata={'source': 'local_demo'})
        if scenario == 'paused':
            change_subscription_status(subscription=plan, actor=user, action='pause', confirm=True)
        PatientEvent.objects.create(company=self.company, patient=patient, category='medication',
            title='Local Operations demo', detail=f'Fictional test scenario: {scenario}.', source_type='local_demo')

    def history(self, patient, authorization, plan, day, index, status):
        at = timezone.make_aware(datetime.combine(day, time(11)))
        address = patient.delivery_address
        reference = f'DEMO-HISTORY-{index:02}-{day:%m%d}'
        parcel = Shipment.objects.create(company=self.company, patient=patient, subscription=plan,
            authorization=authorization, cycle_number=1 if day < authorization.starts_on + timedelta(weeks=4) else 2,
            scheduled_for=day, status='delivered' if status == 'delivered' else 'dispatched', prepared_by=self.admin,
            prepared_at=at-timedelta(days=2), locked_at=at-timedelta(days=1), dispatched_at=at,
            delivered_at=at+timedelta(days=1) if status == 'delivered' else None, waybill=reference,
            tracking_number=reference, courier_shipment_id=f'demo-history-{index}-{day:%m%d}', courier_status=status,
            note='Synthetic historical parcel. Opening stock is counted after this dispatch.',
            dispatch_snapshot={'patient_name': str(patient), 'company_name': self.company.name,
                'scheduled_for': day.isoformat(), 'items': [{'product': str(authorization.product),
                'dose': authorization.max_dose, 'quantity': 1, 'batch_note': 'OPS-DEMO-HISTORY'}],
                'delivery_address': {'recipient': str(patient), 'lines': address.lines(), 'phone': address.phone}})
        ShipmentItem.objects.create(company=self.company, shipment=parcel, product=authorization.product,
            dose=authorization.max_dose, quantity=1, batch_note='OPS-DEMO-HISTORY')

    def seed_order(self, patient, *, paid):
        basket = set_basket_quantity(company=self.company, patient=patient, actor=patient.user,
                                    product=self.creatine, quantity=2 if paid else 1)
        order = submit_basket(order=basket, actor=patient.user, confirm=True, expected_revision=basket.revision,
            delivery_address={'line1': '10 Demo Road', 'line2': 'Sandton', 'city': 'Johannesburg',
                              'province': 'GP', 'postal_code': '2196', 'phone': patient.phone},
            note='Synthetic local non-prescription request.')
        if paid:
            record_manual_payment(company=self.company, actor=self.admin, amount=order.subtotal,
                method='eft', reference='OPS-DEMO-CREATINE', order=order)
            accept_order(order=order, actor=self.admin, scheduled_for=self.ship_day, confirm=True)
