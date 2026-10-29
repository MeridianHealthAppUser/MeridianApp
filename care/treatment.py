"""Explicit doctor authorisations and patients' subscriptions (care plans). Billing is in care.billing."""

from datetime import date, datetime
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from practices.models import CompanyMembership

from .clinical import _active_actor, _lock_context, _require_doctor
from .models import AuditEvent, MedicationProduct, PatientEvent, PatientSubscription, Shipment, TreatmentAuthorization
from .notifications import queue as notify
from .services import record_audit


def current_authorizations(queryset=None, on_date=None):
    """The shared eligibility rule for persisted prescriptions, without per-row lookups."""
    day = on_date or timezone.localdate()
    queryset = queryset if queryset is not None else TreatmentAuthorization.objects.all()
    prescriber = CompanyMembership.objects.filter(
        company_id=OuterRef('company_id'), user_id=OuterRef('prescribed_by_id'),
        clinician_type__in=CompanyMembership.PRESCRIBER_TYPES, is_active=True,
    )
    return queryset.filter(
        Exists(prescriber), status=TreatmentAuthorization.Status.ACTIVE,
        starts_on__lte=day, expires_on__gte=day,
        company__is_active=True, patient__is_active=True, patient__company_id=F('company_id'),
        product__is_active=True, product__company_id=F('company_id'), prescribed_by__is_active=True,
    )


def authorization_is_current(authorization, on_date=None):
    if authorization is None or authorization.pk is None:
        return False
    day = on_date or timezone.localdate()
    if authorization.status != TreatmentAuthorization.Status.ACTIVE or not authorization.starts_on <= day <= authorization.expires_on:
        return False
    return current_authorizations(on_date=day).filter(pk=authorization.pk).exists()


def step_in_effect(authorization, on_date=None):
    """For a step of a starting plan (care.starting_plans), the plan's step in effect that day, so a care plan
    moves on to each month's strength by itself. Any other authorisation is itself."""
    if authorization is None or not authorization.plan_key:
        return authorization
    day = on_date or timezone.localdate()
    step = TreatmentAuthorization.objects.filter(
        company_id=authorization.company_id, patient_id=authorization.patient_id, plan_key=authorization.plan_key,
        starts_on__lte=day, expires_on__gte=day,
    ).exclude(status=TreatmentAuthorization.Status.CANCELLED).select_related(
        'company', 'patient', 'product', 'dose__pen', 'prescribed_by',
    ).order_by('-plan_step', '-pk').first()
    return step or authorization


def dose_in_effect(subscription, on_date=None):
    """The catalogue dose a care plan supplies that day, whose price and billing interval are the plan's, or None
    when its prescription isn't for a catalogue dose."""
    if subscription is None or not subscription.authorization_id:
        return None
    authorization = step_in_effect(subscription.authorization, on_date)
    return authorization.dose if authorization.dose_id else None


def subscription_hold_reason(subscription, on_date=None):
    if subscription is None:
        return 'A care plan is required.'
    if not subscription.company.is_active or not subscription.patient.is_active or subscription.patient.company_id != subscription.company_id:
        return 'The patient or practice is inactive.'
    # A plan whose debit is being retried stays eligible: whether its parcels are paid for is checked on its own
    # (care.billing.parcel_payment) when the week is locked.
    if subscription.status not in (PatientSubscription.Status.ACTIVE, PatientSubscription.Status.PAYMENT_RETRY):
        return 'The care plan is not active.'
    authorization = step_in_effect(subscription.authorization, on_date)
    if authorization is None or authorization.company_id != subscription.company_id or authorization.patient_id != subscription.patient_id:
        return 'A matching treatment authorisation is required.'
    if not authorization_is_current(authorization, on_date=on_date):
        return 'The treatment authorisation is not currently valid. A doctor must review it.'
    return ''


def ensure_subscription_eligible(subscription, on_date=None):
    reason = subscription_hold_reason(subscription, on_date=on_date)
    if reason:
        raise ValidationError(reason)
    return step_in_effect(subscription.authorization, on_date)


def _key(value):
    if value is None:
        return None
    try:
        return str(value if isinstance(value, UUID) else UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('The submission reference is invalid. Reload this form.') from None


def _duplicate(company, patient, actor, key, action, model):
    if not key:
        return None
    audit = AuditEvent.objects.for_company(company).filter(action=action, metadata__submission_key=key).first()
    if audit is None:
        return None
    if audit.patient_id != patient.pk or audit.actor_id != actor.pk:
        raise PermissionDenied('This submission reference belongs to another record.')
    return model.objects.select_for_update().get(pk=audit.target_id, company=company, patient=patient)


def _event(record, title, detail):
    PatientEvent.objects.create(
        company=record.company, patient=record.patient, category=PatientEvent.Category.MEDICATION,
        title=title, detail=detail, source_type=record._meta.label_lower, source_id=str(record.pk), is_patient_visible=True,
    )


def _hold_shipments(company, patient, subscriptions, reason, authorizations=()):
    # A dispatched parcel is history. Only unfulfilled parcels for the affected
    # plans or prescriptions are held; another treatment's shipments are not changed.
    shipments = Shipment.objects.select_for_update().for_company(company).filter(
        Q(subscription_id__in=subscriptions) | Q(authorization__in=authorizations), patient=patient,
        status__in=(Shipment.Status.DRAFT, Shipment.Status.READY, Shipment.Status.HELD),
    )
    return shipments.update(status=Shipment.Status.HELD, hold_reason=reason, updated_at=timezone.now())


def other_treatments(company):
    """What the Authorise treatment form is for: compounded products, and other treatments that need a doctor's
    authorisation (hormone therapy, say). Main treatments, the Semaglutide and Tirzepatide pens, are prescribed from the
    record's Overview, which also drafts their parcels, makes the doctor the Prescribing doctor and asks for a new debit
    order (care.prescriptions). Meridian has none of these, so the form doesn't show there (25 Oct 2026)."""
    return MedicationProduct.objects.for_company(company).filter(is_active=True, requires_authorisation=True).filter(
        Q(is_compounded=True) | ~Q(category=MedicationProduct.Category.WEIGHT_MANAGEMENT))


@transaction.atomic
def create_authorization(*, company, patient, actor, product, max_dose, quantity_per_cycle,
                         starts_on, expires_on, review_interval_days, instructions='', renews=None,
                         acknowledge_flags=False, submission_key=None, request=None):
    from .prescriptions import _check_flags

    company, patient = _lock_context(company, patient, actor)
    _require_doctor(company, actor)
    key = _key(submission_key)
    duplicate = _duplicate(company, patient, actor, key, 'treatment.authorized', TreatmentAuthorization)
    if duplicate is not None:
        return duplicate
    product = MedicationProduct.objects.for_company(company).filter(pk=product.pk, is_active=True).first()
    if product is None:
        raise ValidationError('Choose an active product from this practice.')
    # A safety flag in the patient's summary data is ticked as reviewed first, as on the Overview's prescriptions.
    flags = _check_flags(company, patient, acknowledge_flags)
    if not isinstance(max_dose, str) or not max_dose.strip() or len(max_dose) > 80:
        raise ValidationError('Enter the doctor’s explicit maximum dose, up to 80 characters.')
    if not isinstance(instructions, str) or len(instructions) > 10000:
        raise ValidationError('Keep clinical instructions to 10,000 characters or fewer.')
    if any(not isinstance(value, date) or isinstance(value, datetime) for value in (starts_on, expires_on)):
        raise ValidationError('Choose valid authorisation dates.')
    if expires_on < starts_on or expires_on < timezone.localdate():
        raise ValidationError('The authorisation must end on or after its start and cannot already be expired.')
    if type(quantity_per_cycle) is not int or not 1 <= quantity_per_cycle <= 1000:
        raise ValidationError('Enter a quantity between 1 and 1,000 per cycle.')
    if type(review_interval_days) is not int or not 1 <= review_interval_days <= 3650:
        raise ValidationError('Enter an explicit review interval between 1 and 3,650 days.')
    previous = None
    if renews is not None:
        previous = TreatmentAuthorization.objects.select_for_update().for_company(company).filter(pk=renews.pk, patient=patient).first()
        if previous is None:
            raise PermissionDenied('This authorisation does not belong to this patient and practice.')
        _require_doctor(company, actor, previous.prescribed_by_id)
        # Each historical record may be replaced once. A stale renewal form must
        # not rebind a plan backwards or create parallel replacement decisions.
        if AuditEvent.objects.for_company(company).filter(action='treatment.authorized', metadata__previous_authorization_id=previous.pk).exists():
            raise ValidationError('This authorisation has already been renewed. Open its replacement.')
    authorization = TreatmentAuthorization(
        company=company, patient=patient, prescribed_by=actor, product=product, max_dose=max_dose,
        quantity_per_cycle=quantity_per_cycle, starts_on=starts_on, expires_on=expires_on,
        review_interval_days=review_interval_days, instructions=instructions,
    )
    authorization.full_clean()
    authorization.save()
    if previous is not None:
        previous.status = TreatmentAuthorization.Status.CANCELLED
        previous.save(update_fields=('status', 'updated_at'))
        subscriptions = list(PatientSubscription.objects.select_for_update().for_company(company).filter(
            patient=patient, authorization=previous,
        ).exclude(status=PatientSubscription.Status.CANCELLED))
        for subscription in subscriptions:
            subscription.authorization = authorization
            subscription.review_due_on = authorization.expires_on
            subscription.full_clean()
            subscription.save(update_fields=('authorization', 'review_due_on', 'updated_at'))
        _hold_shipments(company, patient, [plan.pk for plan in subscriptions], 'Authorisation changed. Dispensing review is required before dispatch.')
    record_audit(
        company=company, actor=actor, patient=patient, action='treatment.authorized', target=authorization, request=request,
        metadata={'submission_key': key, 'previous_authorization_id': previous.pk if previous else None,
                  'safety_flags_acknowledged': bool(flags)},
    )
    _event(authorization, 'Treatment authorisation renewed' if previous else 'Treatment authorised',
           'Your doctor recorded a treatment decision. Open your treatment page to see its details.')
    return authorization


@transaction.atomic
def change_authorization_status(*, authorization, actor, action, request=None):
    company, patient = _lock_context(authorization.company, authorization.patient, actor)
    record = TreatmentAuthorization.objects.select_for_update().for_company(company).get(pk=authorization.pk, patient=patient)
    _require_doctor(company, actor, record.prescribed_by_id)
    statuses = {'pause': TreatmentAuthorization.Status.PAUSED, 'revoke': TreatmentAuthorization.Status.CANCELLED}
    if action not in statuses:
        raise ValidationError('Choose pause or revoke. Restarting treatment requires a new doctor authorisation.')
    if record.status == statuses[action]:
        return record
    if record.status == TreatmentAuthorization.Status.CANCELLED:
        raise ValidationError('This historical authorisation cannot be changed. Issue a new authorisation after review.')
    record.status = statuses[action]
    record.save(update_fields=('status', 'updated_at'))
    plans = PatientSubscription.objects.select_for_update().for_company(company).filter(patient=patient, authorization=record).exclude(status=PatientSubscription.Status.CANCELLED)
    plan_ids = list(plans.values_list('pk', flat=True))
    plans.update(status=PatientSubscription.Status.PAUSED, next_debit_on=None, updated_at=timezone.now())
    _hold_shipments(company, patient, plan_ids, 'Treatment authorisation paused or revoked. Doctor review is required.',
                    authorizations=[record])
    record_audit(company=company, actor=actor, patient=patient, action=f'treatment.{action}', target=record, request=request)
    _event(record, 'Treatment paused' if action == 'pause' else 'Treatment authorisation revoked', 'Contact your care team about the next steps. Undispatched parcels for this treatment are on hold.')
    return record


def _require_patient_owner(patient, actor):
    _active_actor(actor)
    if patient.user_id != actor.pk:
        raise PermissionDenied('Only the patient can confirm changes to their subscription.')


@transaction.atomic
def enroll_local_subscription(*, company, patient, actor, authorization, confirm=False, submission_key=None, request=None):
    company, patient = _lock_context(company, patient, actor)
    _require_patient_owner(patient, actor)
    if confirm is not True:
        raise ValidationError('Tick to agree to your subscription and how it’s collected.')
    key = _key(submission_key)
    duplicate = _duplicate(company, patient, actor, key, 'subscription.enrolled_local', PatientSubscription)
    if duplicate is not None:
        return duplicate
    authorization = TreatmentAuthorization.objects.select_for_update().for_company(company).filter(pk=authorization.pk, patient=patient).select_related('product', 'prescribed_by').first()
    if not authorization_is_current(authorization):
        raise ValidationError('A current doctor authorisation for this practice is required before enrollment.')
    existing = PatientSubscription.objects.select_for_update().for_company(company).filter(patient=patient).exclude(status=PatientSubscription.Status.CANCELLED).first()
    if existing is not None:
        if existing.status == PatientSubscription.Status.ACTIVE and existing.authorization_id == authorization.pk:
            return existing
        raise ValidationError('A subscription already exists. Manage it before starting again.')
    plan = PatientSubscription(
        company=company, patient=patient, authorization=authorization,
        plan_name=f'{company.name[:210]} care plan',
        starts_on=timezone.localdate(), review_due_on=authorization.expires_on, next_debit_on=None,
    )
    plan.full_clean()
    plan.save()
    # Parcels already planned from the patient's prescription go out under the plan, so pausing it holds them.
    Shipment.objects.for_company(company).filter(
        patient=patient, subscription__isnull=True, authorization__isnull=False,
        status__in=(Shipment.Status.DRAFT, Shipment.Status.READY, Shipment.Status.HELD),
    ).update(subscription=plan, updated_at=timezone.now())
    record_audit(
        company=company, actor=actor, patient=patient, action='subscription.enrolled_local', target=plan, request=request,
        metadata={'submission_key': key, 'authorization_id': authorization.pk},
    )
    _event(plan, 'Subscription started', 'Your first payment is collected once your debit order is approved, and your parcels go out once it’s paid.')
    return plan


# What the patient's Updates say after they pause, resume or cancel (title, detail).
STATUS_EVENTS = {
    'pause': ('Subscription paused', 'Nothing is debited while it’s paused, and parcels not yet sent are held.'),
    'resume': ('Subscription resumed', 'Debits carry on after the last period you paid for.'),
    'cancel': ('Subscription cancelled', 'Its debit order is stopped and parcels not yet sent are held. Parcels already sent are unchanged.'),
}


@transaction.atomic
def change_subscription_status(*, subscription, actor, action, confirm=False, reasons=(), note='', request=None):
    """Pause, resume or cancel, by the patient. Cancelling needs at least one of the plan's CANCELLATION_REASONS. It
    takes the patient off the Dropouts list's open help requests, and sends one win-back email."""
    company, patient = _lock_context(subscription.company, subscription.patient, actor)
    _require_patient_owner(patient, actor)
    record = PatientSubscription.objects.select_for_update().for_company(company).get(pk=subscription.pk, patient=patient)
    if confirm is not True:
        raise ValidationError('Confirm the change to your subscription.')
    statuses = {'pause': PatientSubscription.Status.PAUSED, 'cancel': PatientSubscription.Status.CANCELLED, 'resume': PatientSubscription.Status.ACTIVE}
    if action not in statuses:
        raise ValidationError('Choose pause, cancel or resume.')
    if action == 'cancel' and record.status != PatientSubscription.Status.CANCELLED:
        allowed = dict(PatientSubscription.CANCELLATION_REASONS)
        reasons = [reason for reason in allowed if reason in set(reasons or ())]
        note = note.strip() if isinstance(note, str) else ''
        if not reasons:
            raise ValidationError('Tick at least one reason for cancelling.')
        if len(note) > 500:
            raise ValidationError('Keep what you add to 500 characters or fewer.')
    if record.status == statuses[action]:
        return record
    if record.status == PatientSubscription.Status.CANCELLED:
        raise ValidationError('A cancelled plan remains in your history. Enroll in a new plan when eligible.')
    if action == 'resume':
        if record.status != PatientSubscription.Status.PAUSED:
            raise ValidationError('This plan needs practice review before it can be resumed.')
        if record.authorization is None or record.authorization.company_id != company.pk or record.authorization.patient_id != patient.pk or not authorization_is_current(record.authorization):
            raise ValidationError('Your doctor must provide a current authorisation before you can resume.')
    record.status = statuses[action]
    # Billing (care.billing): a paused plan isn't debited; resuming carries on after the last period paid for, and
    # cancelling stops it and its debit order.
    if action == 'resume':
        from .billing import resume_debit_date

        record.next_debit_on = resume_debit_date(record)
    elif action == 'cancel':
        record.next_debit_on = None
    fields = ['status', 'next_debit_on', 'cancelled_at', 'updated_at']
    if action == 'cancel':
        record.cancelled_at = timezone.now()
        record.cancellation_reasons, record.cancellation_note = reasons, note
        fields += ['cancellation_reasons', 'cancellation_note']
        # One win-back email, once the cancellation is saved.
        if settings.EMAIL_ENABLED and record.winback_sent_at is None and patient.user.email:
            record.winback_sent_at = timezone.now()
            fields.append('winback_sent_at')
            notify('subscription_winback', record.pk)
    record.save(update_fields=fields)
    if action in ('pause', 'cancel'):
        _hold_shipments(company, patient, [record.pk], 'Subscription paused or cancelled by the patient.')
    if action == 'cancel':
        from .billing import release_mandates
        from .followups import close_help_requests
        from .models import DebitMandate

        close_help_requests(record)
        ending = DebitMandate.objects.filter(subscription=record).exclude(
            status__in=(DebitMandate.Status.CANCELLED, DebitMandate.Status.FAILED))
        release_mandates(ending)
        ending.update(status=DebitMandate.Status.CANCELLED, cancelled_at=timezone.now(), updated_at=timezone.now())
    record_audit(company=company, actor=actor, patient=patient, action=f'subscription.{action}', target=record, request=request,
                 metadata={'reasons': reasons} if action == 'cancel' else None)
    _event(record, *STATUS_EVENTS[action])
    return record
