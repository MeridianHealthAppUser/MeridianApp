"""Local clinical-workflow tracking, not a prescription or external submission."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from .models import PatientCompanyScopedModel


class CompoundingRecord(PatientCompanyScopedModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Awaiting doctor review'
        READY = 'ready', 'Reviewed, ready for manual submission'
        SUBMITTED = 'submitted', 'Manual submission recorded'
        CANCELLED = 'cancelled', 'Cancelled before submission'

    authorization = models.ForeignKey('care.TreatmentAuthorization', on_delete=models.PROTECT, related_name='compounding_records')
    clinician = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='compounding_records')
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    preparation_note = models.TextField(blank=True)
    snapshot = models.JSONField(default=dict)
    submission_key = models.UUIDField(null=True, blank=True, unique=True)
    revision = models.PositiveIntegerField(default=1)
    task = models.OneToOneField('care.ClinicalTask', null=True, blank=True, on_delete=models.PROTECT, related_name='compounding_record')
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='reviewed_compounding_records')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    external_reference = models.CharField(max_length=120, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at', '-pk')
        indexes = [models.Index(fields=('company', 'clinician', 'status'))]
        constraints = [
            models.CheckConstraint(condition=models.Q(revision__gte=1), name='compounding_revision_positive'),
            models.CheckConstraint(condition=~models.Q(status='ready') | models.Q(reviewed_by__isnull=False, reviewed_at__isnull=False), name='ready_compounding_has_review'),
            models.CheckConstraint(condition=~models.Q(status='submitted') | (
                models.Q(reviewed_by__isnull=False, reviewed_at__isnull=False, submitted_at__isnull=False) & ~models.Q(external_reference='')
            ), name='submitted_compounding_has_record'),
        ]

    def clean(self):
        super().clean()
        if self.authorization_id and (self.authorization.company_id != self.company_id or self.authorization.patient_id != self.patient_id):
            raise ValidationError('The authorisation must belong to this patient and practice.')
        if self.authorization_id and self.clinician_id != self.authorization.prescribed_by_id:
            raise ValidationError('The prescribing doctor must own this compounding record.')
        if self.reviewed_by_id and self.reviewed_by_id != self.clinician_id:
            raise ValidationError('Only the owner doctor can review this record.')
        if self.task_id and (self.task.company_id != self.company_id or self.task.patient_id != self.patient_id or self.task.assigned_to_id != self.clinician_id):
            raise ValidationError('The workflow task must match the patient, doctor and practice.')


class AuthorizationReviewReminder(PatientCompanyScopedModel):
    authorization = models.OneToOneField('care.TreatmentAuthorization', on_delete=models.PROTECT, related_name='review_reminder')
    task = models.OneToOneField('care.ClinicalTask', on_delete=models.PROTECT, related_name='authorization_reminder')
    due_on = models.DateField()

    class Meta:
        ordering = ('due_on', 'pk')

    def clean(self):
        super().clean()
        if self.authorization_id and (self.authorization.company_id != self.company_id or self.authorization.patient_id != self.patient_id):
            raise ValidationError('The reminder authorisation must belong to this patient and practice.')
        if self.task_id and (self.task.company_id != self.company_id or self.task.patient_id != self.patient_id):
            raise ValidationError('The reminder task must belong to this patient and practice.')
