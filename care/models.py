"""Company-scoped clinical, pharmacy, finance, and audit records.

Every operational record inherits the protected company key so querying data from
one practice cannot accidentally return data from another practice.
"""

from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from practices.models import CompanyMembership, CompanyScopedModel, Patient


class PatientCompanyScopedModel(CompanyScopedModel):
    """Base for records owned by a single patient in a single practice."""

    patient = models.ForeignKey(
        Patient,
        on_delete=models.PROTECT,
        related_name='%(app_label)s_%(class)s_records',
    )

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if self.patient_id and self.company_id and self.patient.company_id != self.company_id:
            raise ValidationError({'patient': 'The patient must belong to the same company as this record.'})


class PracticeSettings(CompanyScopedModel):
    """Operational defaults that belong to one practice, never to a user."""

    initial_consult_fee = models.DecimalField(max_digits=10, decimal_places=2, default=630)
    standard_subscription_amount = models.DecimalField(max_digits=10, decimal_places=2, default=1995)
    ongoing_subscription_amount = models.DecimalField(max_digits=10, decimal_places=2, default=3295)
    review_interval_days = models.PositiveSmallIntegerField(default=180)
    delivery_interval_days = models.PositiveSmallIntegerField(default=28)
    payment_grace_days = models.PositiveSmallIntegerField(default=7)
    support_email = models.EmailField(blank=True)
    timezone_name = models.CharField(max_length=64, default='Africa/Johannesburg')
    # Off by default: access is governed by permissions, and every change is logged regardless.
    store_view_log = models.BooleanField(
        'Store view log on database', default=False,
        help_text='Also record when people view, download or export records. Stored for the practice audit only and never shown in the app.',
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=('company',), name='one_practice_settings_per_company')]


class ConsentDocument(CompanyScopedModel):
    """A versioned legal document; historical text is retained for auditability."""

    class Kind(models.TextChoices):
        SERVICE = 'service', 'Service and privacy notice'
        MARKETING = 'marketing', 'Marketing communications'
        TELEHEALTH = 'telehealth', 'Telehealth treatment'

    kind = models.CharField(max_length=16, choices=Kind.choices)
    version = models.CharField(max_length=64)
    title = models.CharField(max_length=255)
    body = models.TextField()
    content_hash = models.CharField(max_length=128, blank=True)
    effective_from = models.DateField(default=timezone.localdate)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('company', 'kind', 'version'), name='unique_consent_document_version')]
        ordering = ('kind', '-effective_from')


class Lead(CompanyScopedModel):
    # A submission is an enquiry, not an authentication identity. Nullable for
    # historical/imported leads; the public form always supplies a unique key.
    submission_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)

    class ScreeningStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        CLEARED = 'cleared', 'Cleared for consult'
        REFERRED = 'referred', 'Needs clinician review'
        DECLINED = 'declined', 'Not eligible'

    class Stage(models.TextChoices):
        QUESTIONNAIRE = 'questionnaire', 'Questionnaire'
        BOOKING = 'booking', 'Booking'
        CONSULTATION = 'consultation', 'Consultation'
        CONVERTED = 'converted', 'Converted to patient'
        CLOSED = 'closed', 'Closed'

    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    email = models.EmailField()
    phone = models.CharField(max_length=32, blank=True)
    id_number = models.CharField(max_length=32, blank=True)
    bmi = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    screening_status = models.CharField(max_length=16, choices=ScreeningStatus.choices, default=ScreeningStatus.PENDING)
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.QUESTIONNAIRE)
    converted_patient = models.OneToOneField(Patient, null=True, blank=True, on_delete=models.SET_NULL, related_name='source_lead')

    class Meta:
        indexes = [models.Index(fields=('company', 'stage', 'screening_status')), models.Index(fields=('company', 'email'))]
        ordering = ('-created_at',)

    def __str__(self):
        return f'{self.first_name} {self.last_name}'

    def clean(self):
        super().clean()
        if self.converted_patient_id and self.company_id and self.converted_patient.company_id != self.company_id:
            raise ValidationError({'converted_patient': 'The patient must belong to the same company as this lead.'})


class ScreeningQuestionnaire(CompanyScopedModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        SUBMITTED = 'submitted', 'Submitted'
        REVIEWED = 'reviewed', 'Reviewed'

    lead = models.ForeignKey(Lead, on_delete=models.PROTECT, related_name='questionnaires')
    stage = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    answers = models.JSONField(default=dict, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='reviewed_questionnaires')
    clinical_notes = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('lead', 'stage'), name='one_questionnaire_stage_per_lead')]
        ordering = ('-created_at',)

    def clean(self):
        super().clean()
        if self.lead_id and self.company_id and self.lead.company_id != self.company_id:
            raise ValidationError({'lead': 'The lead must belong to the same company as this questionnaire.'})


class ConsentRecord(CompanyScopedModel):
    class ConsentType(models.TextChoices):
        SERVICE = 'service', 'Service and privacy notice'
        MARKETING = 'marketing', 'Marketing communications'
        TELEHEALTH = 'telehealth', 'Telehealth treatment'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='consent_records')
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name='consent_records')
    patient = models.ForeignKey(Patient, null=True, blank=True, on_delete=models.SET_NULL, related_name='consent_records')
    document = models.ForeignKey(ConsentDocument, null=True, blank=True, on_delete=models.PROTECT, related_name='acceptances')
    consent_type = models.CharField(max_length=16, choices=ConsentType.choices)
    document_version = models.CharField(max_length=64)
    accepted = models.BooleanField(default=False)
    accepted_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=100, default='web')
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=('company', 'consent_type', 'accepted'))]
        ordering = ('-created_at',)

    def clean(self):
        super().clean()
        for field in ('lead', 'patient', 'document'):
            related = getattr(self, field, None)
            if related and self.company_id and related.company_id != self.company_id:
                raise ValidationError({field: f'The {field} must belong to the same company as this consent.'})


class MedicationProduct(CompanyScopedModel):
    class Category(models.TextChoices):
        WEIGHT_MANAGEMENT = 'weight_management', 'Weight management'
        HORMONE = 'hormone', 'Hormone therapy'
        OTHER = 'other', 'Other'

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=24, choices=Category.choices, default=Category.WEIGHT_MANAGEMENT)
    strength = models.CharField(max_length=80, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    allowance = models.CharField(max_length=120, blank=True)
    requires_authorisation = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    requires_cold_chain = models.BooleanField(default=True)
    is_compounded = models.BooleanField(default=False)
    allowance_group = models.CharField(max_length=80, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('company', 'name', 'strength'), name='unique_product_strength_per_company')]
        ordering = ('name', 'strength')

    def __str__(self):
        return ' '.join(part for part in (self.name, self.strength) if part)


class TreatmentAuthorization(PatientCompanyScopedModel):
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        EXPIRED = 'expired', 'Expired'
        PAUSED = 'paused', 'Paused'
        CANCELLED = 'cancelled', 'Cancelled'

    product = models.ForeignKey(MedicationProduct, on_delete=models.PROTECT, related_name='authorizations')
    prescribed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='treatment_authorizations')
    max_dose = models.CharField(max_length=80)
    quantity_per_cycle = models.PositiveSmallIntegerField(default=1)
    starts_on = models.DateField(default=timezone.localdate)
    expires_on = models.DateField()
    review_interval_days = models.PositiveSmallIntegerField(default=180)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    instructions = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=('company', 'patient', 'status', 'expires_on'))]
        ordering = ('-starts_on',)

    def clean(self):
        super().clean()
        if self.product_id and self.company_id and self.product.company_id != self.company_id:
            raise ValidationError({'product': 'The product must belong to the same company as the authorization.'})
        if self.expires_on and self.starts_on and self.expires_on < self.starts_on:
            raise ValidationError({'expires_on': 'The authorization cannot expire before it starts.'})
        if self.prescribed_by_id and self.company_id and not CompanyMembership.objects.filter(
            user_id=self.prescribed_by_id,
            company_id=self.company_id,
            clinician_type__in=CompanyMembership.PRESCRIBER_TYPES,
            is_active=True,
        ).exists():
            raise ValidationError({'prescribed_by': 'Only an active doctor in this practice can prescribe treatment.'})


class PatientSubscription(PatientCompanyScopedModel):
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        PAYMENT_RETRY = 'payment_retry', 'Payment retry'
        PAUSED = 'paused', 'Paused'
        CANCELLED = 'cancelled', 'Cancelled'

    authorization = models.ForeignKey(TreatmentAuthorization, null=True, blank=True, on_delete=models.SET_NULL, related_name='subscriptions')
    plan_name = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    cycle_number = models.PositiveIntegerField(default=1)
    monthly_amount = models.DecimalField(max_digits=10, decimal_places=2)
    starts_on = models.DateField(default=timezone.localdate)
    next_debit_on = models.DateField(null=True, blank=True)
    review_due_on = models.DateField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=('company', 'status', 'next_debit_on'))]
        ordering = ('-created_at',)

    def clean(self):
        super().clean()
        if self.authorization_id and self.company_id and self.authorization.company_id != self.company_id:
            raise ValidationError({'authorization': 'The authorization must belong to the same company as the subscription.'})


class SubscriptionCycle(PatientCompanyScopedModel):
    """An immutable financial snapshot for one recurring subscription cycle."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PAID = 'paid', 'Paid'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'

    subscription = models.ForeignKey(PatientSubscription, on_delete=models.PROTECT, related_name='cycles')
    cycle_number = models.PositiveIntegerField()
    starts_on = models.DateField()
    due_on = models.DateField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    idempotency_key = models.CharField(max_length=128, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('subscription', 'cycle_number'), name='one_subscription_cycle_number'),
            models.UniqueConstraint(
                fields=('company', 'idempotency_key'),
                condition=~models.Q(idempotency_key=''),
                name='unique_subscription_cycle_idempotency_key',
            ),
        ]
        indexes = [models.Index(fields=('company', 'status', 'due_on'))]
        ordering = ('-starts_on',)

    def clean(self):
        super().clean()
        if self.subscription_id and self.company_id and self.subscription.company_id != self.company_id:
            raise ValidationError({'subscription': 'The subscription must belong to the same company as this cycle.'})
        if self.subscription_id and self.patient_id and self.subscription.patient_id != self.patient_id:
            raise ValidationError({'patient': 'The cycle patient must match the subscription patient.'})


class Invoice(PatientCompanyScopedModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        ISSUED = 'issued', 'Issued'
        PAID = 'paid', 'Paid'
        VOID = 'void', 'Void'

    invoice_number = models.CharField(max_length=80)
    subscription_cycle = models.OneToOneField(
        SubscriptionCycle,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='invoice',
    )
    issued_on = models.DateField(default=timezone.localdate)
    due_on = models.DateField()
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('company', 'invoice_number'), name='unique_invoice_number_per_company')]
        indexes = [models.Index(fields=('company', 'status', 'due_on'))]
        ordering = ('-issued_on', '-created_at')

    def clean(self):
        super().clean()
        if self.subscription_cycle_id and self.company_id and self.subscription_cycle.company_id != self.company_id:
            raise ValidationError({'subscription_cycle': 'The cycle must belong to the same company as this invoice.'})
        if self.subscription_cycle_id and self.patient_id and self.subscription_cycle.patient_id != self.patient_id:
            raise ValidationError({'patient': 'The invoice patient must match the subscription cycle patient.'})


class InvoiceLine(CompanyScopedModel):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='lines')
    description = models.CharField(max_length=255)
    quantity = models.PositiveSmallIntegerField(default=1)
    unit_amount = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        ordering = ('id',)

    def clean(self):
        super().clean()
        if self.invoice_id and self.company_id and self.invoice.company_id != self.company_id:
            raise ValidationError({'invoice': 'The invoice must belong to the same company as this line.'})


class Appointment(PatientCompanyScopedModel):
    class Type(models.TextChoices):
        INITIAL = 'initial', 'Initial consultation'
        FOLLOW_UP = 'follow_up', 'Follow-up'
        REVIEW = 'review', 'Six-month review'
        AD_HOC = 'ad_hoc', 'Ad-hoc'

    class Status(models.TextChoices):
        BOOKED = 'booked', 'Booked'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'
        NO_SHOW = 'no_show', 'No show'

    clinician = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='appointments')
    appointment_type = models.CharField(max_length=16, choices=Type.choices, default=Type.FOLLOW_UP)
    starts_at = models.DateTimeField()
    duration_minutes = models.PositiveSmallIntegerField(default=15)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.BOOKED)
    video_link = models.URLField(blank=True)
    outcome_notes = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=('company', 'clinician', 'starts_at')), models.Index(fields=('company', 'patient', 'starts_at'))]
        ordering = ('starts_at',)

    def clean(self):
        super().clean()
        if self.clinician_id and self.company_id and not CompanyMembership.objects.filter(
            user_id=self.clinician_id,
            company_id=self.company_id,
            clinician_type__in=CompanyMembership.CLINICIAN_TYPES,
            is_active=True,
        ).exists():
            raise ValidationError({'clinician': 'The clinician must be active in this practice.'})


class AvailabilitySlot(CompanyScopedModel):
    clinician = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='availability_slots')
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    is_booked = models.BooleanField(default=False)
    appointment = models.OneToOneField(Appointment, null=True, blank=True, on_delete=models.SET_NULL, related_name='availability_slot')

    class Meta:
        constraints = [models.UniqueConstraint(fields=('company', 'clinician', 'starts_at'), name='unique_clinician_slot_per_company')]
        ordering = ('starts_at',)

    def clean(self):
        super().clean()
        if self.ends_at <= self.starts_at:
            raise ValidationError({'ends_at': 'The slot must end after it starts.'})
        if self.appointment_id and self.company_id and self.appointment.company_id != self.company_id:
            raise ValidationError({'appointment': 'The appointment must belong to the same company.'})
        if self.clinician_id and self.company_id and not CompanyMembership.objects.filter(
            user_id=self.clinician_id,
            company_id=self.company_id,
            clinician_type__in=CompanyMembership.CLINICIAN_TYPES,
            is_active=True,
        ).exists():
            raise ValidationError({'clinician': 'The clinician must be active in this practice.'})


class DoctorWorkingPattern(CompanyScopedModel):
    """A practice-specific standing week, including explicit non-working days."""

    class Weekday(models.IntegerChoices):
        MONDAY = 0, 'Monday'
        TUESDAY = 1, 'Tuesday'
        WEDNESDAY = 2, 'Wednesday'
        THURSDAY = 3, 'Thursday'
        FRIDAY = 4, 'Friday'
        SATURDAY = 5, 'Saturday'
        SUNDAY = 6, 'Sunday'

    clinician = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='working_patterns')
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    is_working = models.BooleanField(default=False)
    starts_at = models.TimeField(null=True, blank=True)
    ends_at = models.TimeField(null=True, blank=True)

    class Meta:
        ordering = ('weekday',)
        constraints = [
            models.UniqueConstraint(fields=('company', 'clinician', 'weekday'), name='unique_doctor_working_day'),
            models.CheckConstraint(condition=models.Q(weekday__gte=0, weekday__lte=6), name='doctor_working_weekday_range'),
            models.CheckConstraint(
                condition=(
                    models.Q(is_working=False, starts_at__isnull=True, ends_at__isnull=True)
                    | models.Q(is_working=True, starts_at__isnull=False, ends_at__isnull=False, ends_at__gt=models.F('starts_at'))
                ),
                name='doctor_working_hours_valid',
            ),
        ]

    def clean(self):
        super().clean()
        if self.company_id and self.clinician_id and not CompanyMembership.objects.filter(
            company_id=self.company_id, company__is_active=True, user_id=self.clinician_id,
            user__is_active=True, clinician_type__in=CompanyMembership.CLINICIAN_TYPES, is_active=True,
        ).exists():
            raise ValidationError({'clinician': 'The clinician must be active in this practice.'})
        if self.is_working:
            if not isinstance(self.starts_at, time) or not isinstance(self.ends_at, time):
                raise ValidationError('Set a start and end time for every working day.')
            if self.starts_at.tzinfo is not None or self.ends_at.tzinfo is not None:
                raise ValidationError({
                    field: 'Working hours must be local South Africa times.'
                    for field in ('starts_at', 'ends_at') if getattr(self, field).tzinfo is not None
                })
            if self.ends_at <= self.starts_at:
                raise ValidationError({'ends_at': 'Working hours must end after they start on the same day.'})
        elif self.starts_at is not None or self.ends_at is not None:
            raise ValidationError('Non-working days must not have working hours.')


class DoctorTimeOff(CompanyScopedModel):
    """Private origin-practice record whose blocked time applies everywhere."""

    class Reason(models.TextChoices):
        LEAVE = 'leave', 'Leave'
        SICK = 'sick', 'Sick leave'
        OTHER = 'other', 'Other'

    clinician = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='time_off_periods')
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    reason = models.CharField(max_length=16, choices=Reason.choices, default=Reason.LEAVE)
    is_active = models.BooleanField(default=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='cancelled_doctor_time_off',
    )

    class Meta:
        ordering = ('starts_at', 'pk')
        indexes = [
            models.Index(fields=('company', 'clinician', 'starts_at')),
            models.Index(fields=('clinician', 'is_active', 'starts_at', 'ends_at')),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(ends_at__gt=models.F('starts_at')), name='doctor_time_off_positive'),
            models.CheckConstraint(
                condition=models.Q(ends_at__lte=models.F('starts_at') + timedelta(days=366)),
                name='doctor_time_off_max_year',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(is_active=True, cancelled_at__isnull=True, cancelled_by__isnull=True)
                    | models.Q(is_active=False, cancelled_at__isnull=False)
                ),
                name='doctor_time_off_cancel_state',
            ),
        ]

    def clean(self):
        super().clean()
        if self.company_id and self.clinician_id and not CompanyMembership.objects.filter(
            company_id=self.company_id, company__is_active=True, user_id=self.clinician_id,
            user__is_active=True, clinician_type__in=CompanyMembership.CLINICIAN_TYPES, is_active=True,
        ).exists():
            raise ValidationError({'clinician': 'The clinician must be active in this practice.'})
        if isinstance(self.starts_at, datetime) and isinstance(self.ends_at, datetime):
            if timezone.is_naive(self.starts_at) or timezone.is_naive(self.ends_at):
                raise ValidationError({
                    field: 'Time off must use timezone-aware start and end times.'
                    for field in ('starts_at', 'ends_at') if timezone.is_naive(getattr(self, field))
                })
            if self.ends_at <= self.starts_at:
                raise ValidationError({'ends_at': 'Time off must end after it starts.'})
            if self.ends_at - self.starts_at > timedelta(days=366):
                raise ValidationError({'ends_at': 'Each time-off period can cover at most 366 days.'})


class ClinicalEncounter(PatientCompanyScopedModel):
    """The signed clinical result of a consultation, distinct from its booking."""

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        COMPLETED = 'completed', 'Completed'
        SIGNED = 'signed', 'Signed'

    appointment = models.OneToOneField(Appointment, null=True, blank=True, on_delete=models.SET_NULL, related_name='encounter')
    clinician = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='clinical_encounters')
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    occurred_at = models.DateTimeField(default=timezone.now)
    clinical_summary = models.TextField(blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)
    signed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='signed_encounters')
    revision = models.PositiveIntegerField(default=0)
    submission_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    signing_task = models.OneToOneField(
        'ClinicalTask', null=True, blank=True, on_delete=models.PROTECT, related_name='encounter_signing',
    )
    signed_note = models.OneToOneField(
        'ClinicalNote', null=True, blank=True, on_delete=models.PROTECT, related_name='signed_encounter',
    )

    class Meta:
        indexes = [models.Index(fields=('company', 'patient', 'occurred_at')), models.Index(fields=('company', 'clinician', 'status'))]
        ordering = ('-occurred_at',)

    def clean(self):
        super().clean()
        if self.appointment_id and self.company_id and self.appointment.company_id != self.company_id:
            raise ValidationError({'appointment': 'The appointment must belong to the same company.'})
        if self.appointment_id and self.patient_id and self.appointment.patient_id != self.patient_id:
            raise ValidationError({'patient': 'The encounter patient must match the appointment patient.'})
        if self.appointment_id and self.clinician_id and self.appointment.clinician_id != self.clinician_id:
            raise ValidationError({'clinician': 'The encounter clinician must match the appointment clinician.'})
        if self.signing_task_id and (
            self.signing_task.company_id != self.company_id or self.signing_task.patient_id != self.patient_id
        ):
            raise ValidationError({'signing_task': 'The signing task must belong to this patient and practice.'})
        if self.signed_note_id and (
            self.signed_note.company_id != self.company_id or self.signed_note.patient_id != self.patient_id
            or self.signed_note.author_id != self.clinician_id
        ):
            raise ValidationError({'signed_note': 'The signed note must belong to this clinician, patient and practice.'})
        if self.clinician_id and self.company_id and not CompanyMembership.objects.filter(
            user_id=self.clinician_id,
            company_id=self.company_id,
            clinician_type__in=CompanyMembership.CLINICIAN_TYPES,
            is_active=True,
        ).exists():
            raise ValidationError({'clinician': 'The clinician must be active in this practice.'})


class RecordTag(CompanyScopedModel):
    """A practice's free-form labels, independent of workflow status."""

    name = models.CharField(max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_record_tags')

    class Meta:
        ordering = ('name', 'pk')
        constraints = [models.UniqueConstraint(Lower('name'), 'company', name='unique_record_tag_name_per_company')]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        self.name = ' '.join(self.name.split())
        if not self.name:
            raise ValidationError({'name': 'Enter a tag name.'})


class TaskTagAssignment(CompanyScopedModel):
    task = models.ForeignKey('ClinicalTask', on_delete=models.CASCADE, related_name='tag_assignments')
    tag = models.ForeignKey(RecordTag, on_delete=models.PROTECT, related_name='task_assignments')

    class Meta:
        constraints = [models.UniqueConstraint(fields=('task', 'tag'), name='unique_task_tag_assignment')]

    def clean(self):
        super().clean()
        if self.task_id and self.company_id and self.task.company_id != self.company_id:
            raise ValidationError({'task': 'The task must belong to the same practice.'})
        if self.tag_id and self.company_id and self.tag.company_id != self.company_id:
            raise ValidationError({'tag': 'The tag must belong to the same practice.'})


class ClinicalNoteTagAssignment(CompanyScopedModel):
    note = models.ForeignKey('ClinicalNote', on_delete=models.CASCADE, related_name='tag_assignments')
    tag = models.ForeignKey(RecordTag, on_delete=models.PROTECT, related_name='note_assignments')

    class Meta:
        constraints = [models.UniqueConstraint(fields=('note', 'tag'), name='unique_clinical_note_tag_assignment')]

    def clean(self):
        super().clean()
        if self.note_id and self.company_id and self.note.company_id != self.company_id:
            raise ValidationError({'note': 'The note must belong to the same practice.'})
        if self.tag_id and self.company_id and self.tag.company_id != self.company_id:
            raise ValidationError({'tag': 'The tag must belong to the same practice.'})


class ClinicalTask(PatientCompanyScopedModel):
    class Priority(models.TextChoices):
        LOW = 'low', 'Low'
        NORMAL = 'normal', 'Normal'
        HIGH = 'high', 'High'
        URGENT = 'urgent', 'Urgent'

    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        IN_PROGRESS = 'in_progress', 'In progress'
        DONE = 'done', 'Done'
        CANCELLED = 'cancelled', 'Cancelled'

    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, null=True, blank=True, related_name='care_clinicaltask_records')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='created_clinical_tasks')
    tags = models.ManyToManyField('RecordTag', through='TaskTagAssignment', related_name='tasks', blank=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='clinical_tasks')
    priority = models.CharField(max_length=12, choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    due_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=('company', 'assigned_to', 'status', 'due_at')), models.Index(fields=('company', 'patient', 'status'))]
        ordering = ('due_at', '-created_at')

    def mark_done(self):
        self.status = self.Status.DONE
        self.completed_at = timezone.now()
        self.save(update_fields=('status', 'completed_at', 'updated_at'))

    def clean(self):
        super().clean()
        if self.assigned_to_id and self.company_id and not CompanyMembership.objects.filter(
            company_id=self.company_id, user_id=self.assigned_to_id,
            is_active=True, user__is_active=True,
        ).exists():
            raise ValidationError({'assigned_to': 'Choose an active staff member in this practice.'})


class ClinicalNote(PatientCompanyScopedModel):
    class NoteType(models.TextChoices):
        CONSULT = 'consult', 'Consultation note'
        REVIEW = 'review', 'Review note'
        PHONE = 'phone', 'Phone call'
        SYSTEM = 'system', 'System note'

    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='clinical_notes')
    note_type = models.CharField(max_length=16, choices=NoteType.choices, default=NoteType.CONSULT)
    body = models.TextField()
    is_private = models.BooleanField(default=False)
    tags = models.ManyToManyField('RecordTag', through='ClinicalNoteTagAssignment', related_name='clinical_notes', blank=True)

    class Meta:
        indexes = [models.Index(fields=('company', 'patient', '-created_at'))]
        ordering = ('-created_at',)


class LabRequest(PatientCompanyScopedModel):
    class Status(models.TextChoices):
        REQUESTED = 'requested', 'Requested'
        UPLOADED = 'uploaded', 'Results uploaded'
        REVIEWED = 'reviewed', 'Reviewed'

    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='lab_requests')
    panel_name = models.CharField(max_length=255)
    requested_on = models.DateField(default=timezone.localdate)
    due_on = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.REQUESTED)
    result_summary = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='reviewed_lab_requests')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    submission_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    review_task = models.OneToOneField(
        ClinicalTask, null=True, blank=True, on_delete=models.PROTECT, related_name='lab_review_request',
    )

    class Meta:
        indexes = [models.Index(fields=('company', 'patient', 'status'))]
        ordering = ('-requested_on',)


    def clean(self):
        super().clean()
        if self.requested_by_id and self.company_id and not CompanyMembership.objects.filter(
            company_id=self.company_id, user_id=self.requested_by_id,
            clinician_type__in=CompanyMembership.PRESCRIBER_TYPES, is_active=True, user__is_active=True,
        ).exists():
            raise ValidationError({'requested_by': 'The requesting clinician must be an active doctor in this practice.'})
        if self.review_task_id and (
            self.review_task.company_id != self.company_id or self.review_task.patient_id != self.patient_id
        ):
            raise ValidationError({'review_task': 'The review task must belong to this patient and practice.'})
        if self.reviewed_by_id and self.reviewed_by_id != self.requested_by_id:
            raise ValidationError({'reviewed_by': 'Only the requesting clinician can review these results.'})


class LabResult(PatientCompanyScopedModel):
    """A single protected PDF attachment; never exposed through public storage."""

    lab_request = models.OneToOneField(LabRequest, on_delete=models.PROTECT, related_name='result')
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='uploaded_lab_results')
    filename = models.CharField(max_length=160)
    content = models.BinaryField()
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)

    class Meta:
        ordering = ('-created_at', 'pk')
        constraints = [
            models.CheckConstraint(condition=models.Q(size__gt=0, size__lte=5 * 1024 * 1024), name='lab_result_size_limit'),
        ]

    def clean(self):
        super().clean()
        if self.lab_request_id and (
            self.lab_request.company_id != self.company_id or self.lab_request.patient_id != self.patient_id
        ):
            raise ValidationError({'lab_request': 'The result must belong to the request patient and practice.'})


class WeightEntry(PatientCompanyScopedModel):
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='recorded_weights')
    recorded_on = models.DateField(default=timezone.localdate)
    weight_kg = models.DecimalField(max_digits=5, decimal_places=2)
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('patient', 'recorded_on'), name='one_weight_entry_per_patient_per_day')]
        ordering = ('-recorded_on',)


class MessageThread(PatientCompanyScopedModel):
    """A conversation between one patient and the staff members taking part in it.

    Staff see a conversation only when they are a participant; there is no
    practice-wide view, for administrators or Super Admins alike.
    """

    subject = models.CharField(max_length=255)
    opened_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='opened_message_threads')
    participants = models.ManyToManyField(
        settings.AUTH_USER_MODEL, through='MessageThreadParticipant', through_fields=('thread', 'user'),
        related_name='message_threads', blank=True,
    )
    is_closed = models.BooleanField(default=False)
    last_message_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=('company', 'patient', 'is_closed', 'last_message_at'))]
        ordering = ('-last_message_at', '-created_at')


class MessageThreadParticipant(CompanyScopedModel):
    """A staff member in a conversation; added_by is empty for the first recipient."""

    thread = models.ForeignKey(MessageThread, on_delete=models.CASCADE, related_name='participant_links')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='message_thread_links')
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

    class Meta:
        constraints = [models.UniqueConstraint(fields=('thread', 'user'), name='one_participant_link_per_thread')]
        ordering = ('created_at', 'pk')

    def clean(self):
        super().clean()
        if self.thread_id and self.company_id and self.thread.company_id != self.company_id:
            raise ValidationError({'thread': 'The conversation must belong to the same company.'})


class ClinicianAssignment(PatientCompanyScopedModel):
    """Each period a clinician was assigned to a patient; the open row is the current one."""

    clinician = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='clinician_assignments')
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('patient',), condition=models.Q(ended_at__isnull=True),
                                               name='one_open_clinician_assignment_per_patient')]
        ordering = ('-created_at', '-pk')


class TeamThread(CompanyScopedModel):
    """A conversation between staff members of one practice.

    Patients never see these, even when one is linked to them. Like patient
    conversations, only the members can list or read it.
    """

    subject = models.CharField(max_length=255)
    patient = models.ForeignKey(Patient, null=True, blank=True, on_delete=models.PROTECT, related_name='team_threads')
    opened_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, through='TeamThreadMember', through_fields=('thread', 'user'),
                                     related_name='team_threads', blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=('company', 'last_message_at'))]
        ordering = ('-last_message_at', '-created_at')

    def clean(self):
        super().clean()
        if self.patient_id and self.company_id and self.patient.company_id != self.company_id:
            raise ValidationError({'patient': 'The patient must belong to the same practice.'})


class TeamThreadMember(CompanyScopedModel):
    """A staff member in a team conversation, with their own read position."""

    thread = models.ForeignKey(TeamThread, on_delete=models.CASCADE, related_name='member_links')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='team_thread_links')
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    last_read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('thread', 'user'), name='one_member_link_per_team_thread')]
        ordering = ('created_at', 'pk')


class TeamMessage(CompanyScopedModel):
    thread = models.ForeignKey(TeamThread, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='sent_team_messages')
    body = models.TextField()

    class Meta:
        ordering = ('created_at', 'pk')


class PatientMessage(CompanyScopedModel):
    thread = models.ForeignKey(MessageThread, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='sent_patient_messages')
    body = models.TextField()
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('created_at',)

    def clean(self):
        super().clean()
        if self.thread_id and self.company_id and self.thread.company_id != self.company_id:
            raise ValidationError({'thread': 'The message thread must belong to the same company.'})


class PatientEvent(PatientCompanyScopedModel):
    """A patient timeline entry, with an explicit patient-visibility boundary."""

    class Category(models.TextChoices):
        CLINICAL = 'clinical', 'Clinical'
        APPOINTMENT = 'appointment', 'Appointment'
        MEDICATION = 'medication', 'Medication'
        DELIVERY = 'delivery', 'Delivery'
        PAYMENT = 'payment', 'Payment'
        MESSAGE = 'message', 'Message'
        ADMIN = 'admin', 'Administration'

    category = models.CharField(max_length=16, choices=Category.choices)
    title = models.CharField(max_length=255)
    detail = models.TextField(blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    source_type = models.CharField(max_length=100, blank=True)
    source_id = models.CharField(max_length=64, blank=True)
    is_patient_visible = models.BooleanField(default=True)

    class Meta:
        indexes = [models.Index(fields=('company', 'patient', '-occurred_at'))]
        ordering = ('-occurred_at', '-created_at')


class MedicationBatch(CompanyScopedModel):
    class Status(models.TextChoices):
        AVAILABLE = 'available', 'Available'
        LOW = 'low', 'Low stock'
        QUARANTINED = 'quarantined', 'Quarantined'
        EXPIRED = 'expired', 'Expired'
        WRITTEN_OFF = 'written_off', 'Written off'

    product = models.ForeignKey(MedicationProduct, on_delete=models.PROTECT, related_name='batches')
    batch_number = models.CharField(max_length=80)
    received_on = models.DateField()
    expires_on = models.DateField()
    quantity_received = models.PositiveIntegerField()
    quantity_on_hand = models.PositiveIntegerField()
    cold_chain_confirmed = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.AVAILABLE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('company', 'batch_number'), name='unique_batch_number_per_company')]
        indexes = [models.Index(fields=('company', 'status', 'expires_on'))]
        ordering = ('expires_on',)

    def clean(self):
        super().clean()
        if self.product_id and self.company_id and self.product.company_id != self.company_id:
            raise ValidationError({'product': 'The product must belong to the same company.'})
        if self.quantity_on_hand > self.quantity_received:
            raise ValidationError({'quantity_on_hand': 'On-hand quantity cannot exceed received quantity.'})


class StockMovement(CompanyScopedModel):
    """Append-only stock ledger; batch quantity is the cached operational balance."""

    class Direction(models.TextChoices):
        IN = 'in', 'Received'
        OUT = 'out', 'Allocated or dispatched'
        ADJUSTMENT = 'adjustment', 'Stock adjustment'

    batch = models.ForeignKey(MedicationBatch, on_delete=models.PROTECT, related_name='movements')
    direction = models.CharField(max_length=16, choices=Direction.choices)
    quantity = models.IntegerField()
    reason = models.CharField(max_length=255, blank=True)
    shipment = models.ForeignKey('Shipment', null=True, blank=True, on_delete=models.SET_NULL, related_name='stock_movements')
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='stock_movements')
    idempotency_key = models.CharField(max_length=128, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=('company', 'idempotency_key'),
            condition=~models.Q(idempotency_key=''),
            name='unique_stock_movement_idempotency_key',
        )]
        indexes = [models.Index(fields=('company', 'batch', '-created_at'))]
        ordering = ('-created_at',)

    def clean(self):
        super().clean()
        if not self.quantity:
            raise ValidationError({'quantity': 'A stock movement cannot have a zero quantity.'})
        for field in ('batch', 'shipment'):
            related = getattr(self, field, None)
            if related and self.company_id and related.company_id != self.company_id:
                raise ValidationError({field: f'The {field} must belong to the same company.'})


class Shipment(PatientCompanyScopedModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        HELD = 'held', 'Held'
        READY = 'ready', 'Ready to dispatch'
        DISPATCHED = 'dispatched', 'Dispatched'
        DELIVERED = 'delivered', 'Delivered'
        CANCELLED = 'cancelled', 'Cancelled'

    subscription = models.ForeignKey(PatientSubscription, null=True, blank=True, on_delete=models.SET_NULL, related_name='shipments')
    cycle_number = models.PositiveIntegerField(default=1)
    scheduled_for = models.DateField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    tracking_number = models.CharField(max_length=120, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    hold_reason = models.CharField(max_length=500, blank=True)
    authorization = models.ForeignKey(TreatmentAuthorization, null=True, blank=True, on_delete=models.PROTECT, related_name='recorded_shipments')
    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='prepared_shipments')
    prepared_at = models.DateTimeField(null=True, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    dispatch_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [models.Index(fields=('company', 'status', 'scheduled_for')), models.Index(fields=('company', 'patient', 'cycle_number'))]
        ordering = ('-scheduled_for',)

    def clean(self):
        super().clean()
        if self.subscription_id and self.company_id and self.subscription.company_id != self.company_id:
            raise ValidationError({'subscription': 'The subscription must belong to the same company.'})


class ShipmentItem(CompanyScopedModel):
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(MedicationProduct, on_delete=models.PROTECT, related_name='shipment_items')
    batch = models.ForeignKey(MedicationBatch, null=True, blank=True, on_delete=models.PROTECT, related_name='shipment_items')
    dose = models.CharField(max_length=80)
    quantity = models.PositiveIntegerField()

    class Meta:
        indexes = [models.Index(fields=('company', 'shipment'))]

    def clean(self):
        super().clean()
        for field in ('shipment', 'product', 'batch'):
            related = getattr(self, field, None)
            if related and self.company_id and related.company_id != self.company_id:
                raise ValidationError({field: f'The {field} must belong to the same company.'})


class Payment(PatientCompanyScopedModel):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PAID = 'paid', 'Paid'
        FAILED = 'failed', 'Failed'
        REFUNDED = 'refunded', 'Refunded'

    subscription = models.ForeignKey(PatientSubscription, null=True, blank=True, on_delete=models.SET_NULL, related_name='payments')
    subscription_cycle = models.ForeignKey(SubscriptionCycle, null=True, blank=True, on_delete=models.SET_NULL, related_name='payments')
    appointment = models.ForeignKey(Appointment, null=True, blank=True, on_delete=models.SET_NULL, related_name='payments')
    invoice = models.OneToOneField(Invoice, null=True, blank=True, on_delete=models.SET_NULL, related_name='payment')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    due_on = models.DateField()
    paid_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    provider_reference = models.CharField(max_length=120, blank=True)
    failure_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        indexes = [models.Index(fields=('company', 'status', 'due_on'))]
        ordering = ('-due_on',)

    def clean(self):
        super().clean()
        for field in ('subscription', 'subscription_cycle', 'appointment', 'invoice'):
            related = getattr(self, field, None)
            if related and self.company_id and related.company_id != self.company_id:
                raise ValidationError({field: f'The {field} must belong to the same company.'})


class PayoutRun(CompanyScopedModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        REVIEW = 'review', 'In review'
        APPROVED = 'approved', 'Approved'
        PAID = 'paid', 'Paid'

    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='approved_payout_runs')
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('company', 'period_start', 'period_end'), name='one_payout_run_per_company_period')]
        ordering = ('-period_end',)


class DoctorPayoutLine(CompanyScopedModel):
    payout_run = models.ForeignKey(PayoutRun, on_delete=models.CASCADE, related_name='lines')
    doctor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='payout_lines')
    first_consults = models.PositiveIntegerField(default=0)
    reviews = models.PositiveIntegerField(default=0)
    messages = models.PositiveIntegerField(default=0)
    rate_basis = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('payout_run', 'doctor'), name='one_doctor_line_per_payout_run')]

    def clean(self):
        super().clean()
        if self.payout_run_id and self.company_id and self.payout_run.company_id != self.company_id:
            raise ValidationError({'payout_run': 'The payout run must belong to the same company.'})


class ReviewRule(CompanyScopedModel):
    name = models.CharField(max_length=255)
    product_category = models.CharField(max_length=24, choices=MedicationProduct.Category.choices)
    review_interval_days = models.PositiveSmallIntegerField(default=180)
    blood_tests_required = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('company', 'name'), name='unique_review_rule_name_per_company')]
        ordering = ('name',)


class EmailJourney(CompanyScopedModel):
    class Trigger(models.TextChoices):
        LEAD_CREATED = 'lead_created', 'Lead created'
        CONSULT_BOOKED = 'consult_booked', 'Consult booked'
        PAYMENT_FAILED = 'payment_failed', 'Payment failed'
        REVIEW_DUE = 'review_due', 'Review due'
        SHIPMENT_DISPATCHED = 'shipment_dispatched', 'Shipment dispatched'

    name = models.CharField(max_length=255)
    trigger = models.CharField(max_length=24, choices=Trigger.choices)
    subject_template = models.CharField(max_length=255)
    body_template = models.TextField()
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('company', 'name'), name='unique_email_journey_name_per_company')]


class EmailDelivery(CompanyScopedModel):
    journey = models.ForeignKey(EmailJourney, on_delete=models.PROTECT, related_name='deliveries')
    patient = models.ForeignKey(Patient, null=True, blank=True, on_delete=models.SET_NULL, related_name='email_deliveries')
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name='email_deliveries')
    recipient_email = models.EmailField()
    sent_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default='queued')

    class Meta:
        indexes = [models.Index(fields=('company', 'status', 'sent_at'))]

    def clean(self):
        super().clean()
        if self.journey_id and self.company_id and self.journey.company_id != self.company_id:
            raise ValidationError({'journey': 'The email journey must belong to the same company.'})


class AuditEvent(CompanyScopedModel):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='audit_events')
    patient = models.ForeignKey(Patient, null=True, blank=True, on_delete=models.SET_NULL, related_name='audit_events')
    action = models.CharField(max_length=255)
    target_type = models.CharField(max_length=100, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=('company', '-created_at')), models.Index(fields=('company', 'patient', '-created_at'))]
        ordering = ('-created_at',)


class CompanyInvite(CompanyScopedModel):
    email = models.EmailField()
    role = models.CharField(max_length=32, choices=(
        ('doctor', 'Doctor'),
        ('practice_admin', 'Practice administrator'),
        ('super_admin', 'Super admin'),
    ))
    token = models.CharField(max_length=128, unique=True)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='sent_company_invites')
    accepted_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=('company', 'email', 'accepted_at'))]
        ordering = ('-created_at',)


class PatientMedicalProfile(PatientCompanyScopedModel):
    """Patient-maintained history, separate from signed clinical assessment."""

    patient = models.OneToOneField(Patient, on_delete=models.PROTECT, related_name='medical_profile')
    answers = models.JSONField(default=dict, blank=True)
    revision = models.PositiveIntegerField(default=0)
    saved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')


class PatientMedicalProfileRevision(PatientCompanyScopedModel):
    profile = models.ForeignKey(PatientMedicalProfile, on_delete=models.PROTECT, related_name='history')
    revision = models.PositiveIntegerField()
    answers = models.JSONField(default=dict, blank=True)
    saved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

    class Meta:
        constraints = [models.UniqueConstraint(fields=('profile', 'revision'), name='unique_medical_profile_revision')]
        ordering = ('-revision',)

    def clean(self):
        super().clean()
        if self.profile_id and (self.profile.patient_id != self.patient_id or self.profile.company_id != self.company_id):
            raise ValidationError('The profile history must belong to the same patient and practice.')


class PharmacyOrder(PatientCompanyScopedModel):
    """A local supply request, not a payment or permission to dispense."""

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Basket'
        SUBMITTED = 'submitted', 'Requested'
        ACCEPTED = 'accepted', 'Accepted for preparation'
        CANCELLED = 'cancelled', 'Cancelled'

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    revision = models.PositiveIntegerField(default=0)
    delivery_address = models.JSONField(default=dict, blank=True)
    note = models.TextField(blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipment = models.OneToOneField(Shipment, null=True, blank=True, on_delete=models.PROTECT, related_name='pharmacy_order')

    class Meta:
        ordering = ('-created_at', '-pk')
        constraints = [models.UniqueConstraint(fields=('patient',), condition=models.Q(status='draft'), name='one_patient_draft_basket')]

    def clean(self):
        super().clean()
        if self.shipment_id and (self.shipment.patient_id != self.patient_id or self.shipment.company_id != self.company_id):
            raise ValidationError('The order shipment must belong to this patient and practice.')


class PharmacyOrderItem(CompanyScopedModel):
    order = models.ForeignKey(PharmacyOrder, on_delete=models.PROTECT, related_name='items')
    product = models.ForeignKey(MedicationProduct, on_delete=models.PROTECT, related_name='order_items')
    quantity = models.PositiveSmallIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    product_name = models.CharField(max_length=255)
    strength = models.CharField(max_length=80, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('order', 'product'), name='one_product_per_pharmacy_order'),
            models.CheckConstraint(condition=models.Q(quantity__gt=0, quantity__lte=100), name='pharmacy_order_quantity_limit'),
        ]

    def clean(self):
        super().clean()
        if self.order_id and self.order.company_id != self.company_id:
            raise ValidationError('The basket must belong to this practice.')
        if self.product_id and self.product.company_id != self.company_id:
            raise ValidationError('The product must belong to this practice.')


class AdministrativeFollowUp(CompanyScopedModel):
    """Append-only administrative notes; never a clinical task or login identity."""

    class Status(models.TextChoices):
        OPEN = 'open', 'Follow-up required'
        DONE = 'done', 'Follow-up complete'

    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.PROTECT, related_name='follow_ups')
    subscription = models.ForeignKey(PatientSubscription, null=True, blank=True, on_delete=models.PROTECT, related_name='follow_ups')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='administrative_follow_ups')
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='assigned_administrative_follow_ups')
    note = models.TextField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    next_contact_on = models.DateField(null=True, blank=True)
    stage_before = models.CharField(max_length=20, blank=True)
    stage_after = models.CharField(max_length=20, blank=True)
    submission_key = models.UUIDField(unique=True, editable=False)

    class Meta:
        ordering = ('-created_at', '-pk')
        constraints = [models.CheckConstraint(condition=(models.Q(lead__isnull=False, subscription__isnull=True) | models.Q(lead__isnull=True, subscription__isnull=False)), name='follow_up_has_one_target')]

    def clean(self):
        super().clean()
        for field in ('lead', 'subscription'):
            target = getattr(self, field)
            if target and target.company_id != self.company_id:
                raise ValidationError({field: 'Choose a record in this practice.'})
        if self.assigned_to_id and not CompanyMembership.objects.filter(company_id=self.company_id, user_id=self.assigned_to_id, user__is_active=True, is_active=True, role__in=('practice_admin', 'super_admin')).exists():
            raise ValidationError({'assigned_to': 'Choose an active administrator in this practice.'})


class PatientCommunicationPreference(PatientCompanyScopedModel):
    patient = models.OneToOneField(Patient, on_delete=models.PROTECT, related_name='communication_preference')
    marketing_enabled = models.BooleanField(default=False)


class PatientDataRequest(PatientCompanyScopedModel):
    class Kind(models.TextChoices):
        ACCESS = 'access', 'Request a copy of my information'
        CORRECTION = 'correction', 'Request a correction'
        DELETION = 'deletion', 'Request deletion review'
        QUESTION = 'question', 'Privacy question'

    class Status(models.TextChoices):
        OPEN = 'open', 'Submitted'
        IN_REVIEW = 'in_review', 'In review'
        RESOLVED = 'resolved', 'Resolved'
        DECLINED = 'declined', 'Declined with explanation'

    kind = models.CharField(max_length=16, choices=Kind.choices)
    description = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    submission_key = models.UUIDField(unique=True, editable=False)

    class Meta:
        ordering = ('-created_at', '-pk')


class PatientDataRequestReply(CompanyScopedModel):
    data_request = models.ForeignKey(PatientDataRequest, on_delete=models.PROTECT, related_name='replies')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='data_request_replies')
    body = models.TextField()
    status = models.CharField(max_length=16, choices=PatientDataRequest.Status.choices)
    submission_key = models.UUIDField(unique=True, editable=False)

    class Meta:
        ordering = ('created_at', 'pk')

    def clean(self):
        super().clean()
        if self.data_request_id and self.data_request.company_id != self.company_id:
            raise ValidationError({'data_request': 'Choose a request in this practice.'})


class DoctorActivityStatement(CompanyScopedModel):
    """Immutable activity/rate snapshot after approval. Never a transfer of money."""

    doctor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='activity_statements')
    period_start = models.DateField()
    period_end = models.DateField()
    counts = models.JSONField(default=dict)
    rates = models.JSONField(default=dict)
    source_ids = models.JSONField(default=dict)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='prepared_activity_statements')
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='approved_activity_statements')
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-period_end', 'doctor_id')
        constraints = [models.UniqueConstraint(fields=('company', 'doctor', 'period_start', 'period_end'), name='unique_doctor_activity_statement')]

    def clean(self):
        super().clean()
        if self.period_end and self.period_start and self.period_end < self.period_start:
            raise ValidationError({'period_end': 'The end date must be on or after the start date.'})
        if self.amount is not None and self.amount < 0:
            raise ValidationError({'amount': 'Amounts cannot be negative.'})


class AppointmentProposal(PatientCompanyScopedModel):
    """A proposed appointment change; pending proposals reserve no time slot."""

    class ProposerRole(models.TextChoices):
        DOCTOR = 'doctor', 'Doctor'
        PATIENT = 'patient', 'Patient'

    class Kind(models.TextChoices):
        RESCHEDULE = 'reschedule', 'Reschedule'
        REBOOK = 'rebook', 'Rebook'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Awaiting response'
        ACCEPTED = 'accepted', 'Accepted'
        DECLINED = 'declined', 'Declined'
        WITHDRAWN = 'withdrawn', 'Withdrawn'
        SUPERSEDED = 'superseded', 'Replaced by a newer proposal'

    appointment = models.ForeignKey(Appointment, on_delete=models.PROTECT, related_name='proposals')
    thread = models.ForeignKey(MessageThread, on_delete=models.PROTECT, related_name='appointment_proposals')
    proposed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='proposed_appointment_changes')
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='received_appointment_proposals')
    proposer_role = models.CharField(max_length=12, choices=ProposerRole.choices)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    original_starts_at = models.DateTimeField()
    original_status = models.CharField(max_length=16, choices=Appointment.Status.choices)
    original_clinician = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='appointment_proposal_snapshots')
    original_duration_minutes = models.PositiveSmallIntegerField()
    proposed_starts_at = models.DateTimeField()
    note = models.CharField(max_length=2000, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    responded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='appointment_proposal_responses')
    responded_at = models.DateTimeField(null=True, blank=True)
    resulting_appointment = models.ForeignKey(Appointment, null=True, blank=True, on_delete=models.PROTECT, related_name='accepted_rebooking_proposals')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('appointment',),
                condition=models.Q(status='pending'),
                name='one_pending_proposal_per_appointment',
            ),
        ]
        indexes = [models.Index(fields=('company', 'patient', 'status'))]
        ordering = ('-created_at', '-pk')

    def clean(self):
        super().clean()
        errors = {}
        for field in ('appointment', 'thread', 'resulting_appointment'):
            if getattr(self, f'{field}_id', None):
                related = getattr(self, field)
                if self.company_id and related.company_id != self.company_id:
                    errors[field] = 'The record must belong to the same practice as this proposal.'
                elif self.patient_id and related.patient_id != self.patient_id:
                    errors[field] = 'The record must belong to the same patient as this proposal.'

        if self.patient_id and self.original_clinician_id:
            patient_user_id = self.patient.user_id
            if not patient_user_id:
                errors['patient'] = 'The patient needs an active portal account to agree to a time.'
            elif self.proposer_role == self.ProposerRole.DOCTOR:
                if self.proposed_by_id != self.original_clinician_id:
                    errors['proposed_by'] = 'Only the appointment clinician can propose as the doctor.'
                if self.recipient_id != patient_user_id:
                    errors['recipient'] = 'A doctor proposal must be addressed to the patient.'
            elif self.proposer_role == self.ProposerRole.PATIENT:
                if self.proposed_by_id != patient_user_id:
                    errors['proposed_by'] = 'Only the patient can propose through their portal.'
                if self.recipient_id != self.original_clinician_id:
                    errors['recipient'] = 'A patient proposal must be addressed to the appointment clinician.'

        if self.proposed_by_id and self.proposed_by_id == self.recipient_id:
            errors['recipient'] = 'A proposal requires two different people to agree.'
        if self.kind == self.Kind.RESCHEDULE and self.original_status != Appointment.Status.BOOKED:
            errors['kind'] = 'Only a booked appointment can be rescheduled.'
        if self.kind == self.Kind.REBOOK and self.original_status not in (
            Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW,
        ):
            errors['kind'] = 'Only a cancelled or missed appointment can be rebooked.'
        if self.resulting_appointment_id:
            if self.status != self.Status.ACCEPTED or self.kind != self.Kind.REBOOK:
                errors['resulting_appointment'] = 'A new appointment belongs only to an accepted rebooking.'
            elif self.resulting_appointment_id == self.appointment_id:
                errors['resulting_appointment'] = 'A rebooking must retain the original appointment and create a new one.'
        if self.responded_by_id:
            if self.status in (self.Status.ACCEPTED, self.Status.DECLINED) and self.responded_by_id != self.recipient_id:
                errors['responded_by'] = 'Only the recipient can accept or decline a proposal.'
            elif self.status == self.Status.WITHDRAWN and self.responded_by_id != self.proposed_by_id:
                errors['responded_by'] = 'Only the proposer can withdraw a proposal.'
        if self.status == self.Status.PENDING:
            if self.responded_by_id or self.responded_at:
                errors['status'] = 'A pending proposal cannot already have a response.'
            if self.company_id and not self.company.is_active:
                errors['company'] = 'This practice is inactive.'
            if self.patient_id and not self.patient.is_active:
                errors['patient'] = 'This patient record is inactive.'
            for field in ('proposed_by', 'recipient'):
                if getattr(self, f'{field}_id', None) and not getattr(self, field).is_active:
                    errors[field] = 'Both participants need active accounts.'
            if self.original_clinician_id and self.company_id and not CompanyMembership.objects.filter(
                company_id=self.company_id, user_id=self.original_clinician_id,
                clinician_type__in=CompanyMembership.CLINICIAN_TYPES, is_active=True,
            ).exists():
                errors['original_clinician'] = 'The clinician must be active in this practice.'
        if errors:
            raise ValidationError(errors)


# Imported after the shared patient-scoped base and clinical models exist.
from .compounding_models import AuthorizationReviewReminder, CompoundingRecord  # noqa: E402, F401
