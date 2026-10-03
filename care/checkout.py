"""Initial-consultation checkout and lead conversion.

No payment provider is connected, so card and EFT payments cannot complete.
Only the configured CHECKOUT_TEST_CODE settles the consultation at R0; it then
runs the real conversion: login, patient record, booking, invoice and payment.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from practices.models import CompanyMembership, Patient
from practices.tenancy import company_is_enabled

from .availability import SAST, open_slots_for_day
from .models import (
    Appointment, AvailabilitySlot, ConsentRecord, Invoice, InvoiceLine, Lead, Payment, PatientEvent,
    PracticeSettings, ScreeningQuestionnaire,
)
from .scheduling import ensure_clinician_available, ensure_patient_available
from .services import record_audit


CONSULT_MINUTES = 30
BOOKING_DAYS = 90
DEMO_RULES_VERSION = 'prototype-rev2.6-demo'
PAYMENT_METHODS = (('card', 'Card'), ('eft', 'Instant EFT'))
PAYMENT_UNAVAILABLE = 'Online payment is not connected yet, so no payment was taken. Your enquiry is still saved.'
ACCOUNT_EXISTS = 'An account already uses this email address. Sign in with it, then return to this checkout.'


def can_check_out(lead, questionnaire):
    return bool(settings.DEBUG and questionnaire and
                questionnaire.answers.get('screening_rules_version') == DEMO_RULES_VERSION and
                lead.screening_status == Lead.ScreeningStatus.CLEARED and
                lead.stage not in (Lead.Stage.CONVERTED, Lead.Stage.CLOSED) and not lead.converted_patient_id)


def is_test_code(code):
    expected = settings.CHECKOUT_TEST_CODE
    if not expected or not isinstance(code, str):
        return False
    return secrets.compare_digest(code.strip().casefold().encode(), expected.casefold().encode())


@dataclass(frozen=True)
class Quote:
    fee: Decimal
    discount: Decimal = Decimal('0.00')
    code: str = ''

    @property
    def total(self):
        return self.fee - self.discount


def quote_for(pricing, code=''):
    """The initial-consultation price. No fee is invented for an unconfigured practice."""
    if pricing is None:
        return None
    fee = pricing.initial_consult_fee
    if is_test_code(code):
        return Quote(fee=fee, discount=fee, code=settings.CHECKOUT_TEST_CODE)
    return Quote(fee=fee)


def checkout_identity(lead, user):
    """Which login a conversion would use. It never takes over someone else's account."""
    if user is not None and user.is_authenticated:
        if user.email.casefold() != lead.email.casefold():
            return 'other_account'
        if Patient.objects.filter(company_id=lead.company_id, user=user).exists():
            return 'already_patient'
        return 'signed_in'
    if get_user_model().objects.filter(email__iexact=lead.email).exists():
        return 'sign_in_required'
    return 'new_account'


def consultation_slots(company, day, limit=100):
    # The initial consultation decides treatment, so only doctors are bookable here.
    doctors = get_user_model().objects.filter(
        is_active=True, company_memberships__company=company, company_memberships__is_active=True,
        company_memberships__clinician_type__in=CompanyMembership.PRESCRIBER_TYPES,
    ).distinct()
    return open_slots_for_day(company=company, clinicians=doctors, day=day, duration_minutes=CONSULT_MINUTES, limit=limit)


@dataclass(frozen=True)
class CheckoutResult:
    user: object
    patient: Patient
    appointment: Appointment
    payment: Payment
    account_created: bool


def _create_login(lead, password):
    User = get_user_model()
    if User.objects.filter(email__iexact=lead.email).exists():
        raise ValidationError(ACCOUNT_EXISTS)
    if not isinstance(password, str) or not password:
        raise ValidationError('Choose a password for your new login.')
    user = User(email=User.objects.normalize_email(lead.email), first_name=lead.first_name, last_name=lead.last_name)
    validate_password(password, user=user)
    user.set_password(password)
    try:
        with transaction.atomic():
            user.save()
    except IntegrityError:
        raise ValidationError(ACCOUNT_EXISTS) from None
    return user


def _create_patient(company, lead, user, clinician):
    if Patient.objects.filter(company=company, user=user).exists():
        raise ValidationError('You already have a patient record at this practice. Book from your patient portal instead.')
    if lead.id_number and Patient.objects.filter(company=company, id_number=lead.id_number).exists():
        raise ValidationError('A patient record with this ID or passport number already exists. Contact the practice to link it to your login.')
    # The clinician of the first booking becomes the patient's assigned clinician.
    patient = Patient(company=company, user=user, first_name=lead.first_name, last_name=lead.last_name,
                      id_number=lead.id_number, phone=lead.phone, assigned_doctor=clinician)
    patient.full_clean()
    try:
        with transaction.atomic():
            patient.save()
    except IntegrityError:
        raise ValidationError('A patient record with these details already exists. Contact the practice.') from None
    return patient


@transaction.atomic
def complete_checkout(*, lead, user, password, clinician_id, starts_at, code, request=None):
    """Convert a cleared lead. A zero total is only reachable with the test code.

    Every write happens here or not at all: a failed booking leaves no login,
    patient, invoice or payment behind.
    """
    if not is_test_code(code):
        raise ValidationError(PAYMENT_UNAVAILABLE)
    if type(clinician_id) is not int or clinician_id <= 0:
        raise ValidationError('Choose an available consultation time.')
    User = get_user_model()
    signed_in = user is not None and user.is_authenticated
    # Identity locks first, in a stable order, as every booking service does.
    locked = {row.pk: row for row in User.objects.select_for_update().filter(
        pk__in=sorted({clinician_id, user.pk} if signed_in else {clinician_id}),
    ).order_by('pk')}
    lead = Lead.objects.select_for_update().get(pk=lead.pk)
    company = lead.company
    if not company.is_active or not company_is_enabled(company):
        raise ValidationError('This practice is no longer accepting bookings.')
    questionnaire = ScreeningQuestionnaire.objects.for_company(company).filter(lead=lead, stage=1).first()
    if not can_check_out(lead, questionnaire):
        raise ValidationError('This enquiry can no longer be booked online. The practice will contact you.')
    quote = quote_for(PracticeSettings.objects.for_company(company).first(), code)
    if quote is None:
        raise ValidationError('The practice has not set its consultation fee yet, so booking is unavailable.')
    if quote.total != 0:
        raise ValidationError(PAYMENT_UNAVAILABLE)

    clinician = locked.get(clinician_id)
    if clinician is None or not clinician.is_active or not CompanyMembership.objects.filter(
        user=clinician, company=company, is_active=True, clinician_type__in=CompanyMembership.PRESCRIBER_TYPES,
    ).exists():
        raise ValidationError('This doctor is no longer available. Choose another time.')
    if not isinstance(starts_at, datetime) or timezone.is_naive(starts_at) or starts_at <= timezone.now():
        raise ValidationError('Choose a future consultation time.')
    day = timezone.localdate(starts_at, SAST)
    if day > timezone.localdate(timezone.now(), SAST) + timedelta(days=BOOKING_DAYS):
        raise ValidationError(f'Choose a consultation within the next {BOOKING_DAYS} days.')
    slots = open_slots_for_day(company=company, clinicians=[clinician], day=day, duration_minutes=CONSULT_MINUTES, limit=1000)
    slot = next((slot for slot in slots if slot.starts_at == starts_at), None)
    if slot is None:
        raise ValidationError('This time is no longer available. Choose another time.')
    ensure_clinician_available(company=company, clinician=clinician, starts_at=starts_at, duration_minutes=CONSULT_MINUTES)

    if signed_in:
        account = locked.get(user.pk)
        if account is None or not account.is_active:
            raise ValidationError('Your account is no longer active.')
        if account.email.casefold() != lead.email.casefold():
            raise ValidationError('You are signed in with a different email address from this enquiry. Sign out, then return to this checkout.')
        account_created = False
    else:
        account = _create_login(lead, password)
        account_created = True
    patient = _create_patient(company, lead, account, clinician)
    from .patient_assignment import record_clinician_history
    record_clinician_history(patient, clinician)
    ensure_patient_available(patient=patient, starts_at=starts_at, duration_minutes=CONSULT_MINUTES)

    appointment = Appointment(company=company, patient=patient, clinician=clinician, starts_at=starts_at,
                              duration_minutes=CONSULT_MINUTES, appointment_type=Appointment.Type.INITIAL)
    appointment.full_clean()
    appointment.save()
    if slot.pk is not None:
        AvailabilitySlot.objects.filter(pk=slot.pk, company=company, is_booked=False, appointment__isnull=True).update(
            is_booked=True, appointment=appointment, updated_at=timezone.now(),
        )

    today = timezone.localdate()
    invoice = Invoice(company=company, patient=patient, invoice_number=f'IC-{appointment.pk:06d}', issued_on=today,
                      due_on=today, subtotal=quote.fee, total=quote.total, status=Invoice.Status.PAID)
    invoice.full_clean()
    invoice.save()
    for description, amount in ((f'Initial consultation · {CONSULT_MINUTES} minutes', quote.fee),
                                (f'Discount · test code {quote.code}', -quote.discount)):
        line = InvoiceLine(company=company, invoice=invoice, description=description, quantity=1,
                           unit_amount=amount, line_total=amount)
        line.full_clean()
        line.save()
    payment = Payment(company=company, patient=patient, appointment=appointment, invoice=invoice, amount=quote.total,
                      due_on=today, paid_at=timezone.now(), status=Payment.Status.PAID,
                      provider_reference=f'test-code:{quote.code}')
    payment.full_clean()
    payment.save()

    when = timezone.localtime(starts_at, SAST)
    PatientEvent.objects.create(
        company=company, patient=patient, category=PatientEvent.Category.APPOINTMENT, title='Initial consultation booked',
        detail=f'{CONSULT_MINUTES}-minute initial consultation with {clinician.full_name} on {when:%d %B %Y at %H:%M} SAST.',
        source_type=appointment._meta.label_lower, source_id=str(appointment.pk), is_patient_visible=True,
    )
    PatientEvent.objects.create(
        company=company, patient=patient, category=PatientEvent.Category.PAYMENT, title='Consultation payment recorded',
        detail=f'The R{quote.fee} initial consultation fee was discounted in full with a test code. No money was collected.',
        source_type=payment._meta.label_lower, source_id=str(payment.pk), is_patient_visible=True,
    )
    # The enquiry's accepted notices now also belong to the patient and login.
    ConsentRecord.objects.filter(company=company, lead=lead, patient__isnull=True).update(
        user=account, patient=patient, updated_at=timezone.now(),
    )
    lead.converted_patient = patient
    lead.stage = Lead.Stage.CONVERTED
    lead.full_clean()
    lead.save(update_fields=('converted_patient', 'stage', 'updated_at'))

    record_audit(company=company, actor=account, patient=patient, target=patient, request=request,
                 action='patient.account_created' if account_created else 'patient.record_created',
                 metadata={'source': 'questionnaire.checkout', 'user_id': account.pk, 'lead_id': lead.pk})
    record_audit(company=company, actor=account, patient=patient, action='appointment.patient_booked',
                 target=appointment, request=request, metadata={'source': 'questionnaire.checkout'})
    record_audit(company=company, actor=account, patient=patient, action='patient.doctor_assigned', target=patient,
                 request=request, metadata={'previous_doctor_id': None, 'doctor_id': clinician.pk,
                                            'source': 'questionnaire.checkout', 'appointment_id': appointment.pk})
    record_audit(company=company, actor=account, patient=patient, action='lead.converted', target=lead, request=request,
                 metadata={'appointment_id': appointment.pk, 'invoice_id': invoice.pk, 'payment_id': payment.pk,
                           'test_code': True, 'account_created': account_created})
    return CheckoutResult(user=account, patient=patient, appointment=appointment, payment=payment,
                          account_created=account_created)
