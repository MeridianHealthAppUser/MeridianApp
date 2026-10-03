"""Explicit local reminder refresh. A task acknowledgement is not clinical review."""

from datetime import datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DateField, DurationField, ExpressionWrapper, F, IntegerField, Min, Q
from django.db.models.functions import Cast, Coalesce, Least
from django.utils import timezone

from practices.models import CompanyMembership

from .models import AuthorizationReviewReminder, ClinicalTask, Shipment, TreatmentAuthorization
from .review_rules import _lock_practice
from .services import record_audit
from .treatment import authorization_is_current


def due_authorizations(*, company, on_date=None, within_days=30):
    if type(within_days) is not int or not 0 <= within_days <= 90:
        raise ValidationError('Choose a reminder window between 0 and 90 days.')
    today = on_date or timezone.localdate()
    interval = ExpressionWrapper(Cast('review_interval_days', output_field=IntegerField()) * timedelta(days=1), output_field=DurationField())
    doctor_due = Cast(F('starts_on') + interval, output_field=DateField())
    return TreatmentAuthorization.objects.for_company(company).filter(
        patient__company=company, patient__is_active=True, status__in=('active', 'expired'),
    ).select_related('patient', 'company', 'prescribed_by', 'product').annotate(
        subscription_due=Min('subscriptions__review_due_on', filter=~Q(subscriptions__status='cancelled')),
    ).annotate(
        review_due_date=Least('expires_on', doctor_due, Coalesce('subscription_due', 'expires_on')),
    ).filter(review_due_date__lte=today + timedelta(days=within_days)).order_by('review_due_date', 'pk')


@transaction.atomic
def refresh_review_tasks(*, company, actor, within_days=30, request=None):
    company = _lock_practice(company, actor)
    today = timezone.localdate()
    stats = dict(due=0, created=0, existing=0, skipped_inactive_doctor=0, shipments_held=0)
    for authorization in due_authorizations(company=company, within_days=within_days).iterator(chunk_size=100):
        stats['due'] += 1
        if AuthorizationReviewReminder.objects.for_company(company).filter(authorization=authorization).exists():
            stats['existing'] += 1
            continue
        if not authorization.prescribed_by.is_active or not CompanyMembership.objects.filter(
            company=company, user_id=authorization.prescribed_by_id, clinician_type__in=CompanyMembership.PRESCRIBER_TYPES, is_active=True,
        ).exists():
            stats['skipped_inactive_doctor'] += 1
            continue
        task = ClinicalTask(company=company, patient=authorization.patient, created_by=actor,
                            assigned_to=authorization.prescribed_by, title='Treatment review due — acknowledge reminder',
                            description='Arrange doctor review. Completing this task only acknowledges the reminder; it does not renew treatment, change an authorisation or record a clinical review.',
                            priority=ClinicalTask.Priority.HIGH if authorization.review_due_date <= today else ClinicalTask.Priority.NORMAL,
                            due_at=timezone.make_aware(datetime.combine(authorization.review_due_date, time(9))))
        task.full_clean()
        task.save()
        reminder = AuthorizationReviewReminder(company=company, patient=authorization.patient, authorization=authorization,
                                               task=task, due_on=authorization.review_due_date)
        reminder.full_clean()
        reminder.save()
        record_audit(company=company, actor=actor, patient=authorization.patient, action='review_reminder.created',
                     target=reminder, request=request, metadata={'authorization_id': authorization.pk, 'task_id': task.pk})
        stats['created'] += 1
    # Holding is separate from reminders: paused, revoked or expired treatment
    # may need a safety hold even when no reminder can be assigned to a doctor.
    shipments = Shipment.objects.for_company(company).filter(status__in=('draft', 'ready', 'held')).select_related(
        'patient', 'authorization__company', 'authorization__patient', 'authorization__product', 'authorization__prescribed_by',
        'subscription__authorization__company', 'subscription__authorization__patient', 'subscription__authorization__product',
        'subscription__authorization__prescribed_by',
    )
    for shipment in shipments.iterator(chunk_size=100):
        authorization = shipment.authorization or (shipment.subscription.authorization if shipment.subscription else None)
        if authorization is None:
            continue
        if authorization.company_id == company.pk and authorization.patient_id == shipment.patient_id and authorization_is_current(authorization, on_date=today):
            continue
        reason = 'Treatment authorisation is not currently valid. A doctor must review it before dispatch.'
        if shipment.status == Shipment.Status.HELD and shipment.hold_reason == reason:
            continue
        shipment.status, shipment.hold_reason = Shipment.Status.HELD, reason
        shipment.save(update_fields=('status', 'hold_reason', 'updated_at'))
        record_audit(company=company, actor=actor, patient=shipment.patient, action='review_reminder.shipment_held',
                     target=shipment, request=request, metadata={'authorization_id': authorization.pk})
        stats['shipments_held'] += 1
    return stats
