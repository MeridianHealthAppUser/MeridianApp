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
    EmailJourney,
    Invoice,
    InvoiceLine,
    LabRequest,
    Lead,
    MedicationBatch,
    MedicationProduct,
    MessageThread,
    PatientEvent,
    PatientSubscription,
    PayoutRun,
    PracticeSettings,
    ReviewRule,
    ScreeningQuestionnaire,
    Shipment,
    ShipmentItem,
    StockMovement,
    SubscriptionCycle,
    TreatmentAuthorization,
    WeightEntry,
)
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
                'standard_subscription_amount': Decimal('1995.00'),
                'ongoing_subscription_amount': Decimal('3295.00'),
                'review_interval_days': 180,
                'delivery_interval_days': 28,
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
        product, _ = MedicationProduct.objects.update_or_create(
            company=company,
            name='Semaglutide',
            strength='0.25 mg',
            defaults={
                'description': 'Demo weight-management medicine catalogue item.',
                'category': MedicationProduct.Category.WEIGHT_MANAGEMENT,
                'price': Decimal('1995.00'),
                'allowance': 'One four-week supply',
                'requires_authorisation': True,
                'is_active': True,
            },
        )
        batch, _ = MedicationBatch.objects.update_or_create(
            company=company,
            batch_number=f'{company.slug.upper()[:6]}-DEMO-01',
            defaults={
                'product': product,
                'received_on': today - timedelta(days=14),
                'expires_on': today + timedelta(days=300),
                'quantity_received': 100,
                'quantity_on_hand': 88,
                'cold_chain_confirmed': True,
                'status': MedicationBatch.Status.AVAILABLE,
            },
        )
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
        journey, _ = EmailJourney.objects.update_or_create(
            company=company,
            name='Consult booking confirmation',
            defaults={
                'trigger': EmailJourney.Trigger.CONSULT_BOOKED,
                'subject_template': 'Your Meridian consultation',
                'body_template': 'Your consultation is booked for {{ appointment_time }}.',
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
                'monthly_amount': Decimal('1995.00'),
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
        _message(thread=thread, sender=sam, body='Welcome — your care plan is ready in your Meridian portal.')
        shipment, _ = Shipment.objects.update_or_create(
            company=company,
            patient=patient,
            cycle_number=2,
            defaults={
                'subscription': subscription,
                'scheduled_for': today + timedelta(days=9),
                'status': Shipment.Status.READY,
            },
        )
        ShipmentItem.objects.update_or_create(
            company=company,
            shipment=shipment,
            product=product,
            defaults={'batch': batch, 'dose': '0.25 mg weekly', 'quantity': 1},
        )
        StockMovement.objects.update_or_create(
            company=company,
            idempotency_key=f'{company.slug}-demo-stock-allocation-2',
            defaults={
                'batch': batch,
                'direction': StockMovement.Direction.OUT,
                'quantity': -1,
                'reason': 'Demo shipment allocation',
                'shipment': shipment,
                'recorded_by': sam,
            },
        )
        EmailDelivery.objects.get_or_create(
            company=company,
            journey=journey,
            patient=patient,
            recipient_email=patient.user.email,
            defaults={'sent_at': timezone.now() - timedelta(days=1), 'status': 'sent'},
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
