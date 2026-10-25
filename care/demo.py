"""Idempotent, realistic local data for the Meridian demonstration workspace."""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from practices.models import Patient

from .models import (
    Appointment,
    ClinicalEncounter,
    ClinicalNote,
    ClinicalTask,
    ConsentDocument,
    ConsentRecord,
    DoctorPayoutLine,
    EmailDelivery,
    Invoice,
    InvoiceLine,
    LabRequest,
    Lead,
    MedicationDose,
    MedicationProduct,
    MessageThread,
    PatientEvent,
    PatientSubscription,
    PayoutRun,
    DeliveryAddress,
    PracticeSettings,
    ReviewRule,
    ScreeningQuestionnaire,
    Shipment,
    ShipmentItem,
    StartingPlan,
    StartingPlanStep,
    SubscriptionCycle,
    TreatmentAuthorization,
    WeightEntry,
)
from .journeys import default_journeys
from .parcels import schedule_parcels
from .services import post_patient_message


def _event(*, company, patient, category, title, source):
    event, _ = PatientEvent.objects.get_or_create(
        company=company,
        patient=patient,
        source_type=source._meta.label_lower,
        source_id=str(source.pk),
        defaults={
            'category': category,
            'title': title,
            'detail': '',
            'occurred_at': timezone.now(),
            'is_patient_visible': True,
        },
    )
    return event


def _message(*, thread, sender, body):
    if not thread.messages.filter(sender=sender, body=body).exists():
        post_patient_message(thread=thread, sender=sender, body=body)


# (medicine, pen) the dispensary sends, and what the patient pharmacy lists each at.
# (medicine, pen, its reference price, description). A Semaglutide pen is named by its dose and holds four of them.
DEMO_PENS = (
    ('Semaglutide', '0.25 mg', '1995.00', 'Four weekly doses of 0.25 mg.'),
    ('Semaglutide', '0.5 mg', '1995.00', 'Semagard. Four weekly doses of 0.5 mg.'),
    ('Semaglutide', '1.0 mg', '3295.00', 'Four weekly doses of 1 mg.'),
    ('Semaglutide', '1.7 mg', '4295.00', 'Four weekly doses of 1.7 mg.'),
    ('Semaglutide', '2.4 mg', '4685.00', 'Four weekly doses of 2.4 mg.'),
    ('Tirzepatide', '5 mg', '2995.00', ''), ('Tirzepatide', '10 mg', '0.00', ''), ('Tirzepatide', '15 mg', '0.00', ''),
)
# (medicine, pen, weekly mg, pens per parcel, sent every N weeks, price, billed every N weeks, tier). Each Semaglutide
# dose has its own pen, one sent and billed every 4 weeks. Prices the practice hasn't confirmed are blank, so those
# doses can't be prescribed yet.
DEMO_DOSES = (
    ('Semaglutide', '0.25 mg', '0.25', 1, 4, '1995.00', 4, 'standard'),
    ('Semaglutide', '0.5 mg', '0.5', 1, 4, '1995.00', 4, 'standard'),
    ('Semaglutide', '1.0 mg', '1', 1, 4, '3295.00', 4, 'standard'),
    ('Semaglutide', '1.7 mg', '1.7', 1, 4, '4295.00', 4, 'if_necessary'),
    ('Semaglutide', '2.4 mg', '2.4', 1, 4, '4685.00', 4, 'if_necessary'),
    ('Tirzepatide', '5 mg', '2.5', 1, 2, '2995.00', 2, 'standard'),
    ('Tirzepatide', '5 mg', '5', 1, 1, '2995.00', 4, 'standard'),
    ('Tirzepatide', '10 mg', '7.5', 1, 5, None, 5, 'step_up'),
    ('Tirzepatide', '10 mg', '10', 1, 4, None, 4, 'step_up'),
    ('Tirzepatide', '15 mg', '12.5', 1, 5, None, 5, 'step_up'),
    ('Tirzepatide', '15 mg', '15', 1, 4, None, 4, 'step_up'),
)
# (medicine, [(weekly mg, weeks)], then weekly mg): Semaglutide's 24-week cycle and Tirzepatide's start.
DEMO_PLANS = (
    ('Semaglutide', [('0.25', 4), ('0.5', 4)], '1'),
    ('Tirzepatide', [('2.5', 2)], '5'),
)
# Products earlier demos made that aren't pens: hidden, with their doses.
OLD_DEMO_PRODUCTS = (('Semaglutide', '1 mg'), ('Semaglutide', '2 mg'), ('Semaglutide', '4 mg'), ('Tirzepatide', '2.5 mg'))


def seed_demo_catalogue(company):
    for name, strength in OLD_DEMO_PRODUCTS:
        for product in MedicationProduct.objects.filter(company=company, name=name, strength=strength):
            product.is_active = False
            product.save(update_fields=('is_active', 'updated_at'))
            product.doses.update(is_active=False)
    pens = {}
    for name, strength, price, description in DEMO_PENS:
        pens[name, strength], _ = MedicationProduct.objects.update_or_create(
            company=company, name=name, strength=strength, defaults={
                'description': description or 'Demo weight-management pen.',
                'category': MedicationProduct.Category.WEIGHT_MANAGEMENT, 'price': Decimal(price),
                'allowance': 'As prescribed', 'requires_authorisation': True, 'is_active': True,
            })
    doses = {}
    for name, strength, mg, pens_per_dispatch, dispatch_weeks, price, billing_weeks, tier in DEMO_DOSES:
        doses[name, Decimal(mg)], _ = MedicationDose.objects.update_or_create(
            company=company, pen=pens[name, strength], amount_mg=Decimal(mg), frequency=MedicationDose.Frequency.WEEKLY,
            defaults={'pens_per_dispatch': pens_per_dispatch, 'dispatch_weeks': dispatch_weeks,
                      'price': Decimal(price) if price else None, 'billing_weeks': billing_weeks, 'tier': tier,
                      'is_active': True})
    for medicine, steps, then in DEMO_PLANS:
        plan = StartingPlan.objects.filter(company=company, medicine=medicine).order_by('pk').first() or StartingPlan(
            company=company, medicine=medicine)
        plan.name, plan.then_dose, plan.default_weeks, plan.is_active = 'Standard start', doses[medicine, Decimal(then)], 24, True
        plan.save()
        plan.steps.all().delete()
        for position, (mg, weeks) in enumerate(steps, start=1):
            StartingPlanStep.objects.create(company=company, plan=plan, position=position, dose=doses[medicine, Decimal(mg)],
                                            weeks=weeks)
    return pens, doses


def seed_demo_care(*, meridian, orion, users, nadia):
    """Populate the explicitly supplied demo practices, without external integrations."""
    today = timezone.localdate()
    sam = users['sam.marchant@meridianhealth.co.za']
    joshua = users['joshua.czech@meridianhealth.co.za']
    appointment_start = (timezone.now() + timedelta(days=5)).replace(hour=10, minute=0, second=0, microsecond=0)
    encounter_time = (timezone.now() - timedelta(days=25)).replace(hour=10, minute=0, second=0, microsecond=0)

    for company in (meridian, orion):
        if company is None:
            continue
        PracticeSettings.objects.update_or_create(
            company=company,
            defaults={
                'initial_consult_fee': Decimal('630.00'),
                'review_interval_days': 180,
                'support_email': 'support@meridianhealth.co.za',
            },
        )
        service_document, _ = ConsentDocument.objects.update_or_create(
            company=company,
            kind=ConsentDocument.Kind.SERVICE,
            version='2026.09',
            defaults={
                'title': 'Service and privacy notice',
                'body': 'Demo versioned service and privacy notice.',
                'content_hash': 'demo-service-2026-09',
                'is_active': True,
            },
        )
        telehealth_document, _ = ConsentDocument.objects.update_or_create(
            company=company,
            kind=ConsentDocument.Kind.TELEHEALTH,
            version='2026.09',
            defaults={
                'title': 'Telehealth treatment consent',
                'body': 'Demo versioned telehealth consent.',
                'content_hash': 'demo-telehealth-2026-09',
                'is_active': True,
            },
        )
        # Dr Czech's catalogue: the pens the dispensary sends, the doses doctors prescribe and the standard starts.
        pens, doses = seed_demo_catalogue(company)
        product, dose = pens['Semaglutide', '0.25 mg'], doses['Semaglutide', Decimal('0.25')]
        ReviewRule.objects.update_or_create(
            company=company,
            name='Six-month GLP-1 review',
            defaults={
                'product_category': MedicationProduct.Category.WEIGHT_MANAGEMENT,
                'review_interval_days': 180,
                'blood_tests_required': False,
                'is_active': True,
            },
        )

        if company == meridian:
            patient = nadia
        else:
            patient, _ = Patient.objects.update_or_create(
                company=company,
                user=nadia.user,
                defaults={
                    'first_name': 'Nadia',
                    'last_name': 'Mokoena',
                    'date_of_birth': today.replace(year=today.year - 34),
                    'id_number': '9001015009087',
                    'phone': '+27 82 555 0100',
                    'city': 'Cape Town',
                    'assigned_doctor': sam,
                    'medical_record_number': 'OMH-DEMO-001',
                    'is_active': True,
                },
            )

        Patient.objects.filter(pk=patient.pk).update(
            date_of_birth=today.replace(year=today.year - 34),
            phone='+27 82 555 0100',
            city='Cape Town',
            assigned_doctor=sam,
        )
        patient.refresh_from_db()
        # Where Nadia's parcels go, and the practice's journey emails (switched off) to rewrite and switch on.
        DeliveryAddress.objects.update_or_create(company=company, patient=patient, defaults={
            'street': '12 Kloof Street', 'suburb': 'Gardens', 'city': 'Cape Town', 'province': 'WC',
            'postal_code': '8001', 'phone': '+27 82 555 0100'})
        default_journeys(company)

        for document in (service_document, telehealth_document):
            ConsentRecord.objects.get_or_create(
                company=company,
                patient=patient,
                consent_type=document.kind,
                document_version=document.version,
                defaults={
                    'user': patient.user,
                    'document': document,
                    'accepted': True,
                    'accepted_at': timezone.now() - timedelta(days=32),
                    'source': 'demo-seed',
                },
            )

        authorization, _ = TreatmentAuthorization.objects.update_or_create(
            company=company,
            patient=patient,
            product=product,
            defaults={
                'dose': dose,
                'prescribed_by': sam,
                'max_dose': '0.25 mg weekly',
                'quantity_per_cycle': 1,
                'starts_on': today - timedelta(days=28),
                'expires_on': today + timedelta(days=150),
                'review_interval_days': 180,
                'status': TreatmentAuthorization.Status.ACTIVE,
                'instructions': 'Demo prescription instructions.',
            },
        )
        subscription, _ = PatientSubscription.objects.update_or_create(
            company=company,
            patient=patient,
            plan_name='Meridian medical weight management',
            defaults={
                'authorization': authorization,
                'status': PatientSubscription.Status.ACTIVE,
                'cycle_number': 2,
                'starts_on': today - timedelta(days=28),
                'next_debit_on': today + timedelta(days=8),
                'review_due_on': today + timedelta(days=150),
            },
        )
        cycle, _ = SubscriptionCycle.objects.update_or_create(
            company=company,
            subscription=subscription,
            cycle_number=2,
            defaults={
                'patient': patient,
                'starts_on': today,
                'due_on': today + timedelta(days=8),
                'amount': Decimal('1995.00'),
                'status': SubscriptionCycle.Status.PENDING,
                'idempotency_key': f'{company.slug}-demo-cycle-2',
            },
        )
        invoice, _ = Invoice.objects.update_or_create(
            company=company,
            invoice_number=f'{company.slug.upper()[:5]}-DEMO-002',
            defaults={
                'patient': patient,
                'subscription_cycle': cycle,
                'issued_on': today,
                'due_on': today + timedelta(days=8),
                'subtotal': Decimal('1995.00'),
                'total': Decimal('1995.00'),
                'status': Invoice.Status.ISSUED,
            },
        )
        InvoiceLine.objects.update_or_create(
            company=company,
            invoice=invoice,
            description='Monthly care subscription',
            defaults={'quantity': 1, 'unit_amount': Decimal('1995.00'), 'line_total': Decimal('1995.00')},
        )
        appointment, _ = Appointment.objects.update_or_create(
            company=company,
            patient=patient,
            starts_at=appointment_start,
            defaults={
                'clinician': sam,
                'appointment_type': Appointment.Type.FOLLOW_UP,
                'duration_minutes': 15,
                'status': Appointment.Status.BOOKED,
                'video_link': 'https://zoom.us/j/demo-meridian',
            },
        )
        encounter, _ = ClinicalEncounter.objects.update_or_create(
            company=company,
            patient=patient,
            clinician=sam,
            occurred_at=encounter_time,
            defaults={
                'status': ClinicalEncounter.Status.SIGNED,
                'occurred_at': encounter_time,
                'clinical_summary': 'Initial demo consult completed.',
                'signed_at': timezone.now() - timedelta(days=25),
                'signed_by': sam,
            },
        )
        if not ClinicalNote.objects.filter(company=company, patient=patient, body='Demo care-plan note.').exists():
            ClinicalNote.objects.create(
                company=company,
                patient=patient,
                author=sam,
                note_type=ClinicalNote.NoteType.CONSULT,
                body='Demo care-plan note.',
            )
        ClinicalTask.objects.update_or_create(
            company=company,
            patient=patient,
            title='Review weight check-in',
            defaults={
                'description': 'Review the latest patient-reported weight check-in.',
                'assigned_to': sam,
                'priority': ClinicalTask.Priority.NORMAL,
                'status': ClinicalTask.Status.OPEN,
                'due_at': timezone.now() + timedelta(days=2),
            },
        )
        LabRequest.objects.update_or_create(
            company=company,
            patient=patient,
            panel_name='Baseline wellness panel',
            defaults={
                'requested_by': sam,
                'requested_on': today - timedelta(days=25),
                'due_on': today + timedelta(days=10),
                'status': LabRequest.Status.REQUESTED,
            },
        )
        for offset, kg in ((28, '96.40'), (14, '94.80'), (0, '93.60')):
            WeightEntry.objects.update_or_create(
                company=company,
                patient=patient,
                recorded_on=today - timedelta(days=offset),
                defaults={'weight_kg': Decimal(kg), 'recorded_by': patient.user, 'note': 'Demo check-in'},
            )
        thread, _ = MessageThread.objects.get_or_create(
            company=company,
            patient=patient,
            subject='Your care plan',
            defaults={'opened_by': sam},
        )
        from .messaging import add_participant
        add_participant(thread, sam)
        _message(thread=thread, sender=sam, body='Welcome — your care plan is ready in your Meridian portal.')
        shipment, _ = Shipment.objects.update_or_create(
            company=company,
            patient=patient,
            cycle_number=2,
            defaults={
                'subscription': subscription,
                'authorization': authorization,
                'scheduled_for': today + timedelta(days=9),
                'status': Shipment.Status.READY,
            },
        )
        ShipmentItem.objects.update_or_create(
            company=company,
            shipment=shipment,
            product=product,
            defaults={'dose': '0.25 mg weekly', 'quantity': 1},
        )
        # The rest of Nadia's prescription, drafted on the Weekly shipping list after this parcel.
        schedule_parcels(authorization)
        EmailDelivery.objects.get_or_create(
            company=company,
            kind='appointment_booked',
            patient=patient,
            recipient_email=patient.user.email,
            defaults={'subject': 'Your appointment is booked', 'sent_at': timezone.now() - timedelta(days=1), 'status': 'sent'},
        )
        _event(company=company, patient=patient, category=PatientEvent.Category.APPOINTMENT, title='Follow-up booked', source=appointment)
        _event(company=company, patient=patient, category=PatientEvent.Category.MEDICATION, title='Treatment approved', source=authorization)
        _event(company=company, patient=patient, category=PatientEvent.Category.DELIVERY, title='Next delivery prepared', source=shipment)

    lead, _ = Lead.objects.get_or_create(
        company=meridian,
        email='prospect@example.co.za',
        defaults={
            'first_name': 'Tumi',
            'last_name': 'Dlamini',
            'phone': '+27 82 555 0199',
            'bmi': Decimal('31.20'),
            'screening_status': Lead.ScreeningStatus.CLEARED,
            'stage': Lead.Stage.BOOKING,
        },
    )
    ScreeningQuestionnaire.objects.update_or_create(
        company=meridian,
        lead=lead,
        stage=1,
        defaults={
            'status': ScreeningQuestionnaire.Status.SUBMITTED,
            'answers': {'demo': True, 'goal': 'sustainable weight management'},
            'submitted_at': timezone.now() - timedelta(days=1),
            'reviewed_by': sam,
        },
    )
    payout_run, _ = PayoutRun.objects.update_or_create(
        company=meridian,
        period_start=today.replace(day=1),
        period_end=today,
        defaults={'status': PayoutRun.Status.REVIEW, 'approved_by': joshua},
    )
    DoctorPayoutLine.objects.update_or_create(
        company=meridian,
        payout_run=payout_run,
        doctor=sam,
        defaults={
            'first_consults': 2,
            'reviews': 4,
            'messages': 12,
            'rate_basis': 'Demo monthly rate',
            'amount': Decimal('5400.00'),
        },
    )
