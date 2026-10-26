"""Company-scoped clinical, pharmacy, finance, and audit records.

Every operational record inherits the protected company key so querying data from
one practice cannot accidentally return data from another practice.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from practices.models import CompanyMembership, CompanyScopedModel, Patient


# South Africa's provinces, as delivery and collection addresses give them.
PROVINCES = (
    ('EC', 'Eastern Cape'), ('FS', 'Free State'), ('GP', 'Gauteng'), ('KZN', 'KwaZulu-Natal'), ('LP', 'Limpopo'),
    ('MP', 'Mpumalanga'), ('NC', 'Northern Cape'), ('NW', 'North West'), ('WC', 'Western Cape'),
)


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

    class Weekday(models.IntegerChoices):
        MONDAY = 0, 'Monday'
        TUESDAY = 1, 'Tuesday'
        WEDNESDAY = 2, 'Wednesday'
        THURSDAY = 3, 'Thursday'
        FRIDAY = 4, 'Friday'
        SATURDAY = 5, 'Saturday'
        SUNDAY = 6, 'Sunday'

    initial_consult_fee = models.DecimalField(max_digits=10, decimal_places=2, default=630)
    # Treatment prices and how often parcels go out belong to each dose in the catalogue (MedicationDose).
    review_interval_days = models.PositiveSmallIntegerField(default=180)
    # The day of the week parcels go out. Pens are ordered for the week's parcels before it.
    shipping_weekday = models.PositiveSmallIntegerField('Parcels go out on', choices=Weekday.choices, default=Weekday.TUESDAY)
    payment_grace_days = models.PositiveSmallIntegerField(default=7)
    support_email = models.EmailField(blank=True)
    timezone_name = models.CharField(max_length=64, default='Africa/Johannesburg')
    # Off by default: access is governed by permissions, and every change is logged regardless.
    store_view_log = models.BooleanField(
        'Store view log on database', default=False,
        help_text='Also record when people view, download or export records. Stored for the practice audit only and never shown in the app.',
    )
    # Billing (care.billing). A parcel is locked for its week only once what it is for has been paid.
    require_payment = models.BooleanField(
        'Parcels need payment before they go out', default=True,
        help_text='Check and lock week holds back any parcel whose subscription payment or order is not paid.',
    )
    payment_retry_days = models.CharField(
        'Retry a failed debit after', max_length=40, default='3,7',
        help_text='Days after the debit date, separated by commas. After the last retry the payment goes to the failure queue.',
    )

    class NewDebitOrder(models.TextChoices):
        ANY_CHANGE = 'any', 'Whenever the prescription or its price changes'
        HIGHER = 'higher', 'Only when the amount goes above what the patient approved'

    # When a prescription or price change asks the patient to approve a new debit order (care.billing.review_mandates).
    new_debit_order = models.CharField('Ask for a new debit order', max_length=10, choices=NewDebitOrder.choices,
                                       default=NewDebitOrder.ANY_CHANGE)
    # Weekly shipping and Dispatch history (care.operations) for doctors as well as administrators.
    doctors_ship = models.BooleanField(
        'Doctors can set up and ship parcels', default=True,
        help_text='Weekly shipping and Dispatch history for doctors: marking parcels ready, checking payment, locking the '
                  'week, booking The Courier Guy and recording the collection.',
    )
    # The Courier Guy (care.courier): where parcels are collected and the parcel the practice usually sends.
    collection_business = models.CharField('Business name', max_length=150, blank=True)
    collection_street = models.CharField('Street address', max_length=200, blank=True)
    collection_suburb = models.CharField('Suburb', max_length=120, blank=True)
    collection_city = models.CharField('City', max_length=120, blank=True)
    collection_province = models.CharField('Province', max_length=3, blank=True, choices=PROVINCES)
    collection_postal_code = models.CharField('Postal code', max_length=10, blank=True)
    collection_contact = models.CharField('Contact name', max_length=150, blank=True)
    collection_phone = models.CharField('Contact number', max_length=32, blank=True)
    collection_email = models.EmailField('Contact email', blank=True)
    courier_service_level = models.CharField(
        'Service level', max_length=20, default='ECO',
        help_text='The Courier Guy service code: ECO for economy road (the default), OVN for overnight.',
    )
    parcel_length_cm = models.PositiveSmallIntegerField('Parcel length (cm)', default=30)
    parcel_width_cm = models.PositiveSmallIntegerField('Parcel width (cm)', default=20)
    parcel_height_cm = models.PositiveSmallIntegerField('Parcel height (cm)', default=15)
    parcel_weight_kg = models.DecimalField('Parcel weight (kg)', max_digits=5, decimal_places=2, default=Decimal('2.00'))

    class Meta:
        constraints = [models.UniqueConstraint(fields=('company',), name='one_practice_settings_per_company')]

    def retry_days(self):
        """The retry schedule as whole days, in order: '3,7' is [3, 7]. Anything unreadable is left out."""
        days = []
        for part in (self.payment_retry_days or '').split(','):
            part = part.strip()
            if part.isdigit() and 0 < int(part) <= 60:
                days.append(int(part))
        return sorted(set(days))


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
    # From the unsubscribe link at the foot of a journey email (care.journeys); the enquiry itself is kept.
    emails_unsubscribed_at = models.DateTimeField(null=True, blank=True)

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
    """What the dispensary orders and sends: for a main medicine, a pen (Semaglutide 0.25 mg). What a doctor prescribes
    is a dose (MedicationDose), and several doses can come in the same pen."""

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


class MedicationDose(CompanyScopedModel):
    """A dose a doctor can prescribe, kept in the Pharmacy catalogue by Super Admins: how much of a medicine is taken
    how often, the pen it comes in, how many pens go out how often, and what the patient pays how often. The medicine
    is the pen's name. A medicine's doses are its dose ladder when a doctor changes a prescription.

    Supply is stored as the practice dispenses it, never worked out from the mg in a pen: pens hold more than their
    label (Tirzepatide 7.5 mg weekly comes as a 10 mg pen every 5 weeks)."""

    class Frequency(models.TextChoices):
        WEEKLY = 'weekly', 'Weekly'
        DAILY = 'daily', 'Daily'
        AS_DIRECTED = 'as_directed', 'As directed'

    class Tier(models.TextChoices):
        STANDARD = 'standard', 'Standard'
        STEP_UP = 'step_up', 'Step-up'
        IF_NECESSARY = 'if_necessary', 'Only if necessary'

    pen = models.ForeignKey(MedicationProduct, on_delete=models.PROTECT, related_name='doses')
    amount_mg = models.DecimalField(max_digits=7, decimal_places=3)
    frequency = models.CharField(max_length=16, choices=Frequency.choices, default=Frequency.WEEKLY)
    pens_per_dispatch = models.PositiveSmallIntegerField(default=1)
    dispatch_weeks = models.PositiveSmallIntegerField(default=4)
    # Charged every billing_weeks. Left blank until the practice knows it; a dose without a price can't be prescribed.
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    billing_weeks = models.PositiveSmallIntegerField(default=4)
    tier = models.CharField(max_length=16, choices=Tier.choices, default=Tier.STANDARD)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ('pen__name', 'amount_mg', 'pk')

    def __str__(self):
        return f'{self.medicine} {self.label}'

    @property
    def medicine(self):
        return self.pen.name

    @property
    def amount_text(self):
        """'0.25', '1', '12.5': the amount as a doctor writes it."""
        return f'{Decimal(str(self.amount_mg)).normalize():f}'

    @property
    def label(self):
        """'0.25 mg weekly': the dose and how often it is taken."""
        return f'{self.amount_text} mg {self.get_frequency_display().lower()}'

    @property
    def dispatch_text(self):
        """'1 × 4 mg pen every 4 weeks'."""
        pens = 'pen' if self.pens_per_dispatch == 1 else 'pens'
        return f'{self.pens_per_dispatch} × {self.pen.strength} {pens} {weeks_phrase(self.dispatch_weeks)}'.replace('  ', ' ')

    @property
    def billing_text(self):
        """'every 4 weeks', for after the price."""
        return weeks_phrase(self.billing_weeks)

    @property
    def is_prescribable(self):
        return self.is_active and self.price is not None and self.pen.is_active

    def clean(self):
        super().clean()
        if self.pen_id and self.company_id and self.pen.company_id != self.company_id:
            raise ValidationError({'pen': 'The pen must belong to the same company as the dose.'})


def weeks_phrase(weeks):
    return 'every week' if weeks == 1 else f'every {weeks} weeks'


class TreatmentAuthorization(PatientCompanyScopedModel):
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        EXPIRED = 'expired', 'Expired'
        PAUSED = 'paused', 'Paused'
        CANCELLED = 'cancelled', 'Cancelled'

    product = models.ForeignKey(MedicationProduct, on_delete=models.PROTECT, related_name='authorizations')
    # The catalogue dose a prescription was written for: product is its pen, max_dose its label and
    # quantity_per_cycle its pens per dispatch. Its interval sets the supply cycle. Blank on other authorisations.
    dose = models.ForeignKey(MedicationDose, null=True, blank=True, on_delete=models.PROTECT, related_name='authorizations')
    prescribed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='treatment_authorizations')
    max_dose = models.CharField(max_length=80)
    quantity_per_cycle = models.PositiveSmallIntegerField(default=1)
    starts_on = models.DateField(default=timezone.localdate)
    expires_on = models.DateField()
    review_interval_days = models.PositiveSmallIntegerField(default=180)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    instructions = models.TextField(blank=True)
    # A renewal or change writes a new authorisation and supersedes the one it replaces.
    replaces = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='replacements')
    change_reason = models.CharField(max_length=500, blank=True)
    log_text = models.TextField(blank=True, help_text='The line written to the clinical log when this prescription was made.')
    # Initiate prescription saves a starting plan as one authorisation per step, each with its own strength and
    # dates, so dispensing only allows each month's strength while its step is current. The steps share plan_key.
    plan_key = models.UUIDField(null=True, blank=True, db_index=True)
    plan_step = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=('company', 'patient', 'status', 'expires_on'))]
        ordering = ('-starts_on',)

    def clean(self):
        super().clean()
        if self.product_id and self.company_id and self.product.company_id != self.company_id:
            raise ValidationError({'product': 'The product must belong to the same company as the authorization.'})
        if self.dose_id and self.product_id and self.dose.pen_id != self.product_id:
            raise ValidationError({'dose': 'The dose must come in the authorised product.'})
        if self.expires_on and self.starts_on and self.expires_on < self.starts_on:
            raise ValidationError({'expires_on': 'The authorization cannot expire before it starts.'})
        if self.prescribed_by_id and self.company_id and not CompanyMembership.objects.filter(
            user_id=self.prescribed_by_id,
            company_id=self.company_id,
            clinician_type__in=CompanyMembership.PRESCRIBER_TYPES,
            is_active=True,
        ).exists():
            raise ValidationError({'prescribed_by': 'Only an active doctor in this practice can prescribe treatment.'})


class StartingPlan(CompanyScopedModel):
    """A standard way to start a main medicine, offered by Initiate prescription and kept in the Pharmacy catalogue
    by Super Admins: steps of one dose for some weeks each, then a last dose until the prescription ends."""

    medicine = models.CharField(max_length=255)
    name = models.CharField(max_length=80, blank=True)
    then_dose = models.ForeignKey(MedicationDose, on_delete=models.PROTECT, related_name='starting_plans_ending')
    # The doctor picks the prescription's total weeks; this is the choice the dialog starts on.
    default_weeks = models.PositiveSmallIntegerField(default=24)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ('medicine', 'name', 'pk')


class StartingPlanStep(CompanyScopedModel):
    plan = models.ForeignKey(StartingPlan, on_delete=models.CASCADE, related_name='steps')
    position = models.PositiveSmallIntegerField()
    dose = models.ForeignKey(MedicationDose, on_delete=models.PROTECT, related_name='starting_plan_steps')
    weeks = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ('position',)
        constraints = [models.UniqueConstraint(fields=('plan', 'position'), name='unique_starting_plan_step')]


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
    # What the plan costs is the price of the dose in effect (care.treatment.dose_in_effect), so it follows the
    # prescription through a starting plan and any change.
    starts_on = models.DateField(default=timezone.localdate)
    next_debit_on = models.DateField(null=True, blank=True)
    review_due_on = models.DateField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    # Why the patient cancelled: at least one of CANCELLATION_REASONS, and anything they added. One win-back email
    # follows a cancellation (care.treatment.change_subscription_status).
    cancellation_reasons = models.JSONField(default=list, blank=True)
    cancellation_note = models.CharField(max_length=500, blank=True)
    winback_sent_at = models.DateTimeField(null=True, blank=True)

    CANCELLATION_REASONS = (
        ('cost', 'The cost'),
        ('side_effects', 'Side effects'),
        ('slow_results', 'My results are too slow'),
        ('goal_reached', 'I’ve reached my goal'),
        ('break', 'I want a break'),
        ('other', 'Something else'),
    )

    class Meta:
        indexes = [models.Index(fields=('company', 'status', 'next_debit_on'))]
        ordering = ('-created_at',)

    def cancellation_reason_labels(self):
        labels = dict(self.CANCELLATION_REASONS)
        return [labels[reason] for reason in self.cancellation_reasons if reason in labels]

    def clean(self):
        super().clean()
        if self.authorization_id and self.company_id and self.authorization.company_id != self.company_id:
            raise ValidationError({'authorization': 'The authorization must belong to the same company as the subscription.'})


class SubscriptionCycle(PatientCompanyScopedModel):
    """One billing period of a subscription (care.billing): what is collected for it, when, and whether it was paid.
    Its parcels are the ones planned up to its last day, so a paid cycle is what lets them go out.

    Pending covers a debit not yet collected and one being retried (attempts > 0); Failed is a debit whose retries
    have all failed, in the failure queue until someone pays it or records the payment."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PAID = 'paid', 'Paid'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'

    subscription = models.ForeignKey(PatientSubscription, on_delete=models.PROTECT, related_name='cycles')
    cycle_number = models.PositiveIntegerField()
    starts_on = models.DateField()
    # The last day the cycle pays for. Older cycles from before billing ran have none.
    ends_on = models.DateField(null=True, blank=True)
    due_on = models.DateField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    idempotency_key = models.CharField(max_length=128, blank=True)
    mandate = models.ForeignKey('DebitMandate', null=True, blank=True, on_delete=models.PROTECT, related_name='cycles')
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_on = models.DateField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.CharField(max_length=300, blank=True)

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
    # Who it was issued to, as the patient record had them then (care.billing.billed_to): a later correction to the
    # patient's details doesn't change an invoice already issued.
    billed_to = models.JSONField(default=dict, blank=True)

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
    # When the reminder email went (care/notifications.py), or the confirmation if booked within the reminder time.
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    # Why it was cancelled, as the patient sees it. A reason the practice gives is in the patient's cancellation email.
    cancellation_reason = models.CharField(max_length=500, blank=True)

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


class AppointmentStartReminder(CompanyScopedModel):
    """A successful 15-minute reminder, separately for each recipient and booked start.

    A later retry sends only the recipient whose email failed. Changing the start
    time makes a new reminder eligible without erasing the earlier delivery.
    """

    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name='start_reminders')
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    starts_at = models.DateTimeField()
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=('appointment', 'starts_at', 'recipient'), name='one_start_reminder_per_recipient',
        )]


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
    """A practice-specific standing week ("Regular working hours"), including explicit non-working days.

    A working day has one row per period it's worked, such as Monday 08:00–11:00 and 13:00–16:00; a day not
    worked has a single row with no times. care.availability.save_working_pattern keeps a day's periods apart.
    """

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
        ordering = ('weekday', 'starts_at')
        indexes = [models.Index(fields=('company', 'clinician', 'weekday'), name='doctor_working_day_idx')]
        constraints = [
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
    # Days off no longer ask for a reason; older records keep the one they were given.
    reason = models.CharField(max_length=16, choices=Reason.choices, blank=True, default='')
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


class DoctorSpecialDay(CompanyScopedModel):
    """Different working hours on one date, replacing the weekly pattern for that date only.

    A day off on the same date still wins. A special day may fall on a normally non-working weekday.
    """

    clinician = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='special_days')
    date = models.DateField()
    starts_at = models.TimeField()
    ends_at = models.TimeField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_special_days',
    )

    class Meta:
        ordering = ('date', 'pk')
        constraints = [
            models.UniqueConstraint(fields=('company', 'clinician', 'date'), name='unique_doctor_special_day'),
            models.CheckConstraint(condition=models.Q(ends_at__gt=models.F('starts_at')), name='doctor_special_day_hours_valid'),
        ]

    def clean(self):
        super().clean()
        if self.company_id and self.clinician_id and not CompanyMembership.objects.filter(
            company_id=self.company_id, company__is_active=True, user_id=self.clinician_id,
            user__is_active=True, clinician_type__in=CompanyMembership.CLINICIAN_TYPES, is_active=True,
        ).exists():
            raise ValidationError({'clinician': 'The clinician must be active in this practice.'})
        if isinstance(self.starts_at, time) and isinstance(self.ends_at, time):
            if self.starts_at.tzinfo is not None or self.ends_at.tzinfo is not None:
                raise ValidationError('Special hours must be local South Africa times.')
            if self.ends_at <= self.starts_at:
                raise ValidationError({'ends_at': 'The finish time must be after the start time.'})


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
        # Written from the record's log with Create note: just "Note".
        GENERAL = 'general', 'Note'

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


class PatientFile(PatientCompanyScopedModel):
    """One private file kept for a patient, such as a PathCare form or an uploaded lab report.

    Read and write it only through care.patient_files. The bytes are in private object storage
    (storage_key) or, where no storage is configured or before they are moved there, in content.
    A file never has a public URL; the app serves it after its own access checks.
    """

    class Kind(models.TextChoices):
        PATHCARE_FORM = 'pathcare_form', 'PathCare request form'
        LAB_RESULT = 'lab_result', 'Lab result report'
        BLOOD_RESULT = 'blood_result', 'Blood result report'

    kind = models.CharField(max_length=32, choices=Kind.choices)
    filename = models.CharField(max_length=160)
    content_type = models.CharField(max_length=100, default='application/pdf')
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    storage_key = models.CharField(max_length=512, blank=True)
    content = models.BinaryField(null=True, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='uploaded_patient_files')

    class Meta:
        ordering = ('-created_at', '-pk')
        indexes = [models.Index(fields=('company', 'patient', 'kind'), name='patient_file_lookup_idx')]
        constraints = [
            models.UniqueConstraint(fields=('storage_key',), condition=~models.Q(storage_key=''),
                                    name='unique_patient_file_storage_key'),
            models.CheckConstraint(condition=models.Q(content__isnull=False) | ~models.Q(storage_key=''),
                                   name='patient_file_has_bytes'),
        ]

    def __str__(self):
        return f'{self.get_kind_display()} {self.pk}'


def _check_patient_file(record):
    if record.file_id and (record.file.company_id != record.company_id or record.file.patient_id != record.patient_id):
        raise ValidationError({'file': 'The file must belong to the same patient and practice.'})


class PathCareRequestForm(PatientCompanyScopedModel):
    """A filled, flattened PathCare request form for a blood-test request, as emailed to the patient.

    The stored file is what "View form" and "Resend" serve; it is never regenerated or made public.
    """

    class EmailStatus(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Failed'

    lab_request = models.OneToOneField(LabRequest, on_delete=models.PROTECT, related_name='pathcare_form')
    tests = models.TextField()
    file = models.OneToOneField(PatientFile, on_delete=models.PROTECT, related_name='pathcare_form')
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    email_to = models.EmailField()
    email_status = models.CharField(max_length=16, choices=EmailStatus.choices, default=EmailStatus.QUEUED)
    email_attempts = models.PositiveIntegerField(default=0)
    emailed_at = models.DateTimeField(null=True, blank=True)
    send_count = models.PositiveIntegerField(default=1, help_text='The first send and every resend.')

    class Meta:
        ordering = ('-created_at', '-pk')

    def clean(self):
        super().clean()
        if self.lab_request_id and (
            self.lab_request.company_id != self.company_id or self.lab_request.patient_id != self.patient_id
        ):
            raise ValidationError({'lab_request': 'The form must belong to the request patient and practice.'})
        _check_patient_file(self)


class LabResult(PatientCompanyScopedModel):
    """A single protected PDF report (in file); never exposed through public storage."""

    lab_request = models.OneToOneField(LabRequest, on_delete=models.PROTECT, related_name='result')
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='uploaded_lab_results')
    filename = models.CharField(max_length=160)
    file = models.OneToOneField(PatientFile, on_delete=models.PROTECT, related_name='lab_result')
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
        _check_patient_file(self)


class BloodResult(PatientCompanyScopedModel):
    """A blood result a clinician recorded by hand on the Blood results tab, usually from a lab other than
    PathCare (PathCare results are mostly read on the PathCare portal). The lab's PDF may be attached."""

    taken_on = models.DateField()
    lab_name = models.CharField(max_length=120)
    results = models.TextField()
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='recorded_blood_results')
    file = models.OneToOneField(PatientFile, null=True, blank=True, on_delete=models.PROTECT, related_name='blood_result')

    class Meta:
        ordering = ('-taken_on', '-created_at', '-pk')
        indexes = [models.Index(fields=('company', 'patient', 'taken_on'), name='blood_result_lookup_idx')]

    def clean(self):
        super().clean()
        _check_patient_file(self)


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

    Staff see a conversation only when they are a participant, except Super Admins,
    who read every conversation in their practice and join one by replying (care.messaging).
    """

    subject = models.CharField(max_length=255)
    opened_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='opened_message_threads')
    participants = models.ManyToManyField(
        settings.AUTH_USER_MODEL, through='MessageThreadParticipant', through_fields=('thread', 'user'),
        related_name='message_threads', blank=True,
    )
    is_closed = models.BooleanField(default=False)
    last_message_at = models.DateTimeField(null=True, blank=True)
    # The message the patient was last emailed about (its time); the next email waits until they've read it.
    patient_emailed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=('company', 'patient', 'is_closed', 'last_message_at'))]
        ordering = ('-last_message_at', '-created_at')


class MessageThreadParticipant(CompanyScopedModel):
    """A staff member in a conversation, with their own read position; added_by is empty for the first recipient.

    Messages anyone else sent (the patient or a colleague) after last_read_at are unread for this person
    only. Empty means they haven't read it yet: everything others sent is unread, as for a new team member.
    """

    thread = models.ForeignKey(MessageThread, on_delete=models.CASCADE, related_name='participant_links')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='message_thread_links')
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    last_read_at = models.DateTimeField(null=True, blank=True)
    # The message they were last emailed about (its time); the next email waits until they've read up to it.
    last_emailed_at = models.DateTimeField(null=True, blank=True)

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
    conversations, only the members can list or read it, and any of them can
    close it (every message is kept; nobody can reply) or reopen it.
    """

    subject = models.CharField(max_length=255)
    patient = models.ForeignKey(Patient, null=True, blank=True, on_delete=models.PROTECT, related_name='team_threads')
    is_closed = models.BooleanField(default=False)
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
    # The message they were last emailed about (its time); the next email waits until they've read up to it.
    last_emailed_at = models.DateTimeField(null=True, blank=True)

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
    # The recipient's read state: the patient's for staff messages. Unchanged by staff reading.
    read_at = models.DateTimeField(null=True, blank=True)
    # The practice's receipt on a patient's message: when a staff member first opened it, and who. Kept for the
    # patient side and history only; each staff member's unread state is MessageThreadParticipant.last_read_at.
    staff_read_at = models.DateTimeField(null=True, blank=True)
    staff_read_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

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
    # The practice's own note on this parcel, from the Weekly shipping list's Notes column.
    note = models.CharField(max_length=500, blank=True)
    # The Courier Guy (care.courier): the shipment booked for the parcel once its week is locked, and its waybill.
    waybill = models.CharField(max_length=60, blank=True)
    courier_shipment_id = models.CharField(max_length=60, blank=True)
    courier_status = models.CharField(max_length=60, blank=True)
    courier_error = models.CharField(max_length=500, blank=True)
    waybill_created_at = models.DateTimeField(null=True, blank=True)
    # The name, address and number The Courier Guy was given when the parcel was booked (care.courier.booked_delivery):
    # dispatch records where it was actually sent, even if the patient's address changed after booking.
    booked_address = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [models.Index(fields=('company', 'status', 'scheduled_for')), models.Index(fields=('company', 'patient', 'cycle_number'))]
        ordering = ('-scheduled_for',)

    def clean(self):
        super().clean()
        if self.subscription_id and self.company_id and self.subscription.company_id != self.company_id:
            raise ValidationError({'subscription': 'The subscription must belong to the same company.'})

    @property
    def courier_status_label(self):
        """The Courier Guy's latest status for the parcel, in words ("At the hub"), or ''."""
        from .courier import status_label

        return status_label(self.courier_status) if self.courier_status else ''


class ShipmentItem(CompanyScopedModel):
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(MedicationProduct, on_delete=models.PROTECT, related_name='shipment_items')
    dose = models.CharField(max_length=80)
    quantity = models.PositiveIntegerField()
    # Pens are ordered for each week's parcels, not kept in stock. The batch and expiry that went into a parcel are
    # noted when it is dispatched, so a recall can be traced to the patients who received it.
    batch_note = models.CharField(max_length=120, blank=True)

    class Meta:
        indexes = [models.Index(fields=('company', 'shipment'))]

    def clean(self):
        super().clean()
        for field in ('shipment', 'product'):
            related = getattr(self, field, None)
            if related and self.company_id and related.company_id != self.company_id:
                raise ValidationError({field: f'The {field} must belong to the same company.'})


class Payment(PatientCompanyScopedModel):
    """One attempt to take money, or money the practice recorded by hand: a subscription debit (one row per try), a
    once-off card payment for a non-prescription request, the first consultation, or a payment received outside the
    platform (EFT, cash) recorded by the practice."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PAID = 'paid', 'Paid'
        FAILED = 'failed', 'Failed'
        REFUNDED = 'refunded', 'Refunded'

    class Kind(models.TextChoices):
        SUBSCRIPTION = 'subscription', 'Subscription debit'
        ONCE_OFF = 'once_off', 'Once-off purchase'
        CONSULTATION = 'consultation', 'Consultation'
        MANUAL = 'manual', 'Recorded by the practice'

    class Method(models.TextChoices):
        DEBICHECK = 'debicheck', 'DebiCheck debit order'
        CARD = 'card', 'Card'
        EFT = 'eft', 'EFT'
        CASH = 'cash', 'Cash'
        OTHER = 'other', 'Other'

    subscription = models.ForeignKey(PatientSubscription, null=True, blank=True, on_delete=models.SET_NULL, related_name='payments')
    subscription_cycle = models.ForeignKey(SubscriptionCycle, null=True, blank=True, on_delete=models.SET_NULL, related_name='payments')
    appointment = models.ForeignKey(Appointment, null=True, blank=True, on_delete=models.SET_NULL, related_name='payments')
    invoice = models.OneToOneField(Invoice, null=True, blank=True, on_delete=models.SET_NULL, related_name='payment')
    pharmacy_order = models.ForeignKey('PharmacyOrder', null=True, blank=True, on_delete=models.SET_NULL, related_name='payments')
    # A parcel paid for outside the platform, recorded from the Weekly shipping list.
    shipment = models.ForeignKey('Shipment', null=True, blank=True, on_delete=models.SET_NULL, related_name='payments')
    kind = models.CharField(max_length=16, choices=Kind.choices, blank=True)
    method = models.CharField(max_length=16, choices=Method.choices, blank=True)
    provider = models.CharField(max_length=20, blank=True)
    attempt = models.PositiveSmallIntegerField(default=1)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    due_on = models.DateField()
    paid_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    provider_reference = models.CharField(max_length=120, blank=True)
    # Paid by the patient on the provider's page (a card checkout), rather than collected with their debit order: a
    # checkout that fails isn't a failed debit, so it doesn't use up a retry.
    checkout = models.BooleanField(default=False)
    failure_reason = models.CharField(max_length=500, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        indexes = [models.Index(fields=('company', 'status', 'due_on'))]
        ordering = ('-due_on',)

    def clean(self):
        super().clean()
        for field in ('subscription', 'subscription_cycle', 'appointment', 'invoice', 'pharmacy_order', 'shipment'):
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
    """One email of a journey (care.journeys): sent delay_days after someone joins its track, while they are still in
    it. A person moves between tracks as their care changes (they book, start treatment, cancel, a debit fails), so
    each track's emails stop by themselves once they no longer apply. Super Admins write and switch them on under
    Settings > Email journeys."""

    class Track(models.TextChoices):
        SCREENED = 'screened', 'Completed screening but did not book'
        CONSULTED = 'consulted', 'Had a consultation'
        EARLY = 'early', 'Treatment months 1 to 6'
        ONGOING = 'ongoing', 'Treatment month 5 onwards'
        CANCELLED = 'cancelled', 'Cancelled subscription'
        PAYMENT_FAILED = 'payment_failed', 'Failed subscription payment'

    class Button(models.TextChoices):
        NONE = '', 'No button'
        QUESTIONNAIRE = 'questionnaire', 'Finish booking (questionnaire)'
        BOOK = 'book', 'Book a consult'
        SUBSCRIPTION = 'subscription', 'Start my subscription'
        WEIGHT = 'weight', 'Log my weight'
        MEDICAL = 'medical', 'Check my medical information'
        BLOODS = 'bloods', 'My blood tests'
        TREATMENT = 'treatment', 'My treatment'
        BILLING = 'billing', 'Update my payment details'
        MESSAGES = 'messages', 'Message my care team'

    name = models.CharField(max_length=255)
    track = models.CharField(max_length=24, choices=Track.choices)
    delay_days = models.PositiveSmallIntegerField('Days after joining the track', default=0)
    subject_template = models.CharField('Subject', max_length=255)
    body_template = models.TextField('Message', help_text='Plain text. {first_name} is replaced with the person’s first name.')
    button = models.CharField(max_length=20, choices=Button.choices, blank=True)
    is_active = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('company', 'name'), name='unique_email_journey_name_per_company')]
        ordering = ('track', 'delay_days', 'pk')

    def __str__(self):
        return self.name


class EmailDelivery(CompanyScopedModel):
    """An email the platform sent, logged against the patient (or the enquiry, for someone without a record yet): a
    journey email, or one of the notifications in care.notifications. The communications log lists these."""

    journey = models.ForeignKey(EmailJourney, null=True, blank=True, on_delete=models.PROTECT, related_name='deliveries')
    patient = models.ForeignKey(Patient, null=True, blank=True, on_delete=models.SET_NULL, related_name='email_deliveries')
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name='email_deliveries')
    recipient_email = models.EmailField()
    # The notification it was (care.notifications.KINDS), or "journey".
    kind = models.CharField(max_length=40, blank=True)
    subject = models.CharField(max_length=255, blank=True)
    # Which time the person joined the track: the subscription or cycle that put them there, so a later return gets
    # the emails again.
    entry_key = models.CharField(max_length=64, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default='queued')

    class Meta:
        indexes = [models.Index(fields=('company', 'status', 'sent_at')), models.Index(fields=('company', 'patient', 'sent_at'))]
        ordering = ('-created_at', '-pk')
        constraints = [
            models.UniqueConstraint(fields=('journey', 'patient', 'entry_key'),
                                    condition=models.Q(journey__isnull=False, patient__isnull=False),
                                    name='one_journey_email_per_patient_entry'),
            models.UniqueConstraint(fields=('journey', 'lead', 'entry_key'),
                                    condition=models.Q(journey__isnull=False, lead__isnull=False, patient__isnull=True),
                                    name='one_journey_email_per_lead_entry'),
        ]

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


class PatientClinicalSummary(PatientCompanyScopedModel):
    """The doctor's editable summary, first filled from the screening questionnaire and medical profile.

    The questionnaire and the patient's own profile are never changed by edits here.
    """

    class Answer(models.TextChoices):
        NO = 'no', 'No'
        YES = 'yes', 'Yes'
        UNSURE = 'unsure', 'Unsure'
        NOT_APPLICABLE = 'not_applicable', 'Not applicable'

    patient = models.OneToOneField(Patient, on_delete=models.PROTECT, related_name='clinical_summary')
    height_cm = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    start_weight_kg = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    goal_weight_kg = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    medical_history = models.TextField(blank=True)
    surgical_history = models.TextField(blank=True)
    chronic_medication = models.TextField(blank=True)
    allergies = models.TextField(blank=True)
    pancreatitis = models.CharField(max_length=16, choices=Answer.choices, blank=True)
    thyroid_cancer = models.CharField(max_length=16, choices=Answer.choices, blank=True)
    pregnancy = models.CharField(max_length=16, choices=Answer.choices, blank=True)
    seeded_from = models.DateTimeField(null=True, blank=True, help_text='When the screening assessment it was filled from was submitted.')

    def clean(self):
        super().clean()
        if self.goal_weight_kg is not None and not 40 <= self.goal_weight_kg <= 250:
            raise ValidationError({'goal_weight_kg': 'Give a goal weight between 40 and 250 kg.'})
        if self.height_cm is not None and not 80 <= self.height_cm <= 260:
            raise ValidationError({'height_cm': 'Give a height between 80 and 260 cm.'})


class PatientSummaryEdit(PatientCompanyScopedModel):
    """One change to a summary field: who, when, and the value before and after."""

    summary = models.ForeignKey(PatientClinicalSummary, on_delete=models.CASCADE, related_name='edits')
    field = models.CharField(max_length=40)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    edited_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')

    class Meta:
        ordering = ('-created_at', '-pk')


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
    # A once-off card payment (care.billing.start_order_payment), or a payment the practice recorded for it.
    paid_at = models.DateTimeField(null=True, blank=True)

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


class HelpRequest(PatientCompanyScopedModel):
    """A patient's "Something is wrong, help me" from their care plan. It's on Administration > Dropouts until the
    practice confirms it's resolved, or until the patient cancels, when they move to the cancelled list there."""

    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        RESOLVED = 'resolved', 'Resolved'
        CANCELLED = 'cancelled', 'Subscription cancelled'

    subscription = models.ForeignKey(PatientSubscription, null=True, blank=True, on_delete=models.SET_NULL, related_name='help_requests')
    # The conversation it started with the patient's clinician and the practice administrators.
    thread = models.ForeignKey('MessageThread', null=True, blank=True, on_delete=models.SET_NULL, related_name='help_requests')
    reason = models.TextField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='resolved_help_requests')
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.CharField(max_length=1000, blank=True)

    class Meta:
        ordering = ('-created_at', '-pk')
        indexes = [models.Index(fields=('company', 'status'))]


class CallbackRequest(CompanyScopedModel):
    """"Please call me first" from the end of the screening questionnaire. It's on Administration > Dropouts (Please
    call) until someone confirms they called, or until the person books their consultation. Kept afterwards, with the
    caller's note."""

    class Status(models.TextChoices):
        OPEN = 'open', 'Waiting for a call'
        CALLED = 'called', 'Called'
        BOOKED = 'booked', 'Booked'

    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name='callback_requests')
    name = models.CharField(max_length=300)
    phone = models.CharField(max_length=32)
    email = models.EmailField(blank=True)
    note = models.CharField(max_length=1000, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    called_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='callback_requests_called')
    called_at = models.DateTimeField(null=True, blank=True)
    staff_note = models.CharField(max_length=1000, blank=True)

    class Meta:
        ordering = ('-created_at', '-pk')
        indexes = [models.Index(fields=('company', 'status'))]

    def clean(self):
        super().clean()
        if self.lead_id and self.company_id and self.lead.company_id != self.company_id:
            raise ValidationError({'lead': 'The enquiry must belong to the same company.'})


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
    # From the unsubscribe link in a journey email, or turned off on My profile. Emails about payments still go.
    journey_emails_off_at = models.DateTimeField(null=True, blank=True)


class DeliveryAddress(PatientCompanyScopedModel):
    """Where a patient's parcels go and the number the courier calls: one per patient. The patient keeps it on My
    profile (and gives it when they start their subscription); the practice can change it from the Weekly shipping
    list. Every change is in the audit log (care.addresses)."""

    patient = models.OneToOneField(Patient, on_delete=models.PROTECT, related_name='delivery_address')
    recipient = models.CharField('Recipient', max_length=150, blank=True, help_text='Leave blank to use the patient’s name.')
    business = models.CharField('Business or complex name', max_length=150, blank=True)
    street = models.CharField('Street address', max_length=200)
    suburb = models.CharField('Suburb', max_length=120)
    city = models.CharField('City or town', max_length=120)
    province = models.CharField('Province', max_length=3, choices=PROVINCES)
    postal_code = models.CharField('Postal code', max_length=10)
    phone = models.CharField('Contact number', max_length=32)
    instructions = models.CharField('Delivery instructions', max_length=300, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

    def __str__(self):
        return ', '.join(self.lines())

    def lines(self):
        """The address as it is written on a parcel, a line for each part that is filled in."""
        return [part for part in (self.business, self.street, self.suburb,
                                  ' '.join(part for part in (self.city, self.postal_code) if part),
                                  self.get_province_display()) if part]

    def missing(self):
        """What the courier still needs, as the field labels, or nothing when the address is complete."""
        required = ('street', 'suburb', 'city', 'province', 'postal_code', 'phone')
        return [str(self._meta.get_field(name).verbose_name) for name in required if not str(getattr(self, name)).strip()]


class DebitMandate(PatientCompanyScopedModel):
    """A patient's authority for the practice to collect their subscription (care.billing): a DebiCheck debit order,
    the default, or a card the payment provider keeps. The amount is the most collected each time; a prescription or
    price change that needs more asks the patient to confirm a new one (Update needed). Card and bank details stay
    with the provider: only a label such as "FNB ···1234" is kept here."""

    class Method(models.TextChoices):
        DEBICHECK = 'debicheck', 'DebiCheck debit order'
        CARD = 'card', 'Card'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Waiting for approval'
        ACTIVE = 'active', 'Active'
        UPDATE_NEEDED = 'update_needed', 'New amount to confirm'
        CANCELLED = 'cancelled', 'Cancelled'
        FAILED = 'failed', 'Not approved'

    subscription = models.ForeignKey(PatientSubscription, null=True, blank=True, on_delete=models.PROTECT, related_name='mandates')
    method = models.CharField(max_length=16, choices=Method.choices, default=Method.DEBICHECK)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    interval_weeks = models.PositiveSmallIntegerField(default=4)
    reference = models.CharField(max_length=40, blank=True)
    provider = models.CharField(max_length=20)
    provider_reference = models.CharField(max_length=120, blank=True)
    account_label = models.CharField(max_length=80, blank=True)
    # A DebiCheck debit order with Stitch is collected on one weekday (1 Monday to 7 Sunday): the bank only accepts
    # collections on the day the patient approved.
    collection_day = models.PositiveSmallIntegerField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

    class Meta:
        ordering = ('-created_at', '-pk')
        indexes = [models.Index(fields=('company', 'patient', 'status'))]

    def clean(self):
        super().clean()
        if self.subscription_id and self.company_id and (self.subscription.company_id != self.company_id
                                                         or self.subscription.patient_id != self.patient_id):
            raise ValidationError({'subscription': 'The subscription must belong to the same patient and practice.'})


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
    # The practice task raised with the request; marking the request Resolved or Declined completes it.
    task = models.OneToOneField(ClinicalTask, null=True, blank=True, on_delete=models.PROTECT, related_name='data_request')

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
