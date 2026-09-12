"""Connection history only: no media, signalling payloads or attendance claims."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from practices.models import CompanyScopedModel


class CallSession(CompanyScopedModel):
    appointment = models.ForeignKey('care.Appointment', on_delete=models.PROTECT, related_name='video_sessions')
    patient = models.ForeignKey('practices.Patient', on_delete=models.PROTECT, related_name='video_sessions')
    doctor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='doctor_video_sessions')
    session_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    started_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    lease_expires_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    presence_revision = models.PositiveBigIntegerField(default=0)

    class Meta:
        ordering = ('-started_at', '-pk')
        constraints = [
            models.CheckConstraint(condition=models.Q(ended_at__isnull=True) | models.Q(ended_at__gte=models.F('started_at')), name='video_session_end_after_start'),
            models.CheckConstraint(condition=models.Q(lease_expires_at__gte=models.F('started_at')), name='video_session_valid_lease'),
        ]

    @property
    def is_live(self):
        return self.ended_at is None and self.lease_expires_at > timezone.now()

    @property
    def effective_ended_at(self):
        # A crashed worker cannot leave a call shown as live indefinitely.
        return self.ended_at or (self.lease_expires_at if self.lease_expires_at <= timezone.now() else None)

    @property
    def signalling_duration_seconds(self):
        end = self.effective_ended_at or timezone.now()
        return max(0, int((end - self.started_at).total_seconds()))

    def clean(self):
        super().clean()
        if self.appointment_id and (self.appointment.company_id != self.company_id or self.appointment.patient_id != self.patient_id
                                    or self.appointment.clinician_id != self.doctor_id):
            raise ValidationError('The video session must belong to its appointment participants and practice.')
        if self.patient_id and self.patient.company_id != self.company_id:
            raise ValidationError('The video patient must belong to this practice.')


class CallParticipant(CompanyScopedModel):
    session = models.ForeignKey(CallSession, on_delete=models.PROTECT, related_name='participants')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='video_participations')
    connection_key = models.UUIDField(unique=True, editable=False)
    joined_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    lease_expires_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=16, blank=True)

    class Meta:
        ordering = ('joined_at', 'pk')
        constraints = [
            models.CheckConstraint(condition=models.Q(ended_at__isnull=True) | models.Q(ended_at__gte=models.F('joined_at')), name='video_participant_end_after_join'),
            models.CheckConstraint(condition=models.Q(lease_expires_at__gte=models.F('joined_at')), name='video_participant_valid_lease'),
        ]

    @property
    def effective_ended_at(self):
        return self.ended_at or (self.lease_expires_at if self.lease_expires_at <= timezone.now() else None)

    def clean(self):
        super().clean()
        if self.session_id and self.session.company_id != self.company_id:
            raise ValidationError('Participant history must use its session practice.')
