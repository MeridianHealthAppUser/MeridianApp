"""A technical superuser can explicitly change only their own practice role."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from care.models import Appointment, AppointmentProposal, ClinicalEncounter, CompoundingRecord, LabRequest, TreatmentAuthorization
from care.services import record_audit
from .models import Company, CompanyMembership, Patient


class ActiveClinicalWorkError(ValidationError):
    """A role switch would remove the clinician access existing care depends on."""


def _require_no_active_clinical_work(company, user):
    # Called while company and user are locked: clinical/treatment writes share
    # the company lock; booking/proposal writes share the clinician's user lock.
    # Past booked visits still need the doctor to record attendance, so there
    # is deliberately no starts_at cutoff for unresolved appointments.
    dependencies = (
        ('active treatment authorisations', TreatmentAuthorization.objects.filter(
            company=company, prescribed_by=user, status=TreatmentAuthorization.Status.ACTIVE,
            expires_on__gte=timezone.localdate(),
        )),
        ('outstanding laboratory requests', LabRequest.objects.filter(
            company=company, requested_by=user,
            status__in=(LabRequest.Status.REQUESTED, LabRequest.Status.UPLOADED),
        )),
        ('booked appointments', Appointment.objects.filter(
            company=company, clinician=user, status=Appointment.Status.BOOKED,
        )),
        ('pending appointment changes', AppointmentProposal.objects.filter(
            company=company, original_clinician=user, status=AppointmentProposal.Status.PENDING,
        )),
        ('unsigned consultations', ClinicalEncounter.objects.filter(
            company=company, clinician=user,
            status__in=(ClinicalEncounter.Status.DRAFT, ClinicalEncounter.Status.COMPLETED),
        )),
        ('unfinished compounding records', CompoundingRecord.objects.filter(
            company=company, clinician=user,
            status__in=(CompoundingRecord.Status.DRAFT, CompoundingRecord.Status.READY),
        )),
        ('active patient assignments', Patient.objects.filter(
            company=company, assigned_doctor=user, is_active=True,
        )),
    )
    blockers = [label for label, queryset in dependencies if queryset.exists()]
    if blockers:
        raise ActiveClinicalWorkError(
            'Your role remains Doctor because existing care depends on it: '
            + ', '.join(blockers) + '. Keep Doctor access for this work; '
            'your technical administration area is still available with the same account.',
            code='active_clinical_work',
        )


def _technical_superuser(user):
    return bool(
        getattr(user, 'is_authenticated', False) and user.is_active
        and user.is_staff and user.is_superuser
    )


def can_switch_practice_role(user, membership, company=None):
    """Presentation hint; supplying the resolved company avoids another query.

    This does not authorize a write. The service reloads and locks every record.
    """
    if settings.MULTI_PRACTICE_ENABLED or not _technical_superuser(user):
        return False
    if membership is None or membership.user_id != user.pk or not membership.is_active:
        return False
    if membership.role not in CompanyMembership.Role.values:
        return False
    company = company if company is not None else membership.company
    return bool(
        company is not None and company.pk == membership.company_id
        and company.is_active and company.slug == settings.SINGLE_PRACTICE_SLUG
    )


@transaction.atomic
def switch_own_practice_role(*, actor, role, request=None):
    """Persist a real membership role without changing identity or global flags.

    The selected role applies across sessions so existing ORM and clinical
    service checks remain authoritative. Only a technical superuser can use
    this route; ordinary practice role management keeps its last-admin guard.
    This superuser retains technical access and can always switch back while
    their account, practice and membership remain active.
    """
    if settings.MULTI_PRACTICE_ENABLED or not _technical_superuser(actor):
        raise PermissionDenied('Role switching is available only to an active technical administrator.')
    if role not in CompanyMembership.Role.values:
        raise ValidationError('Choose a supported practice role.')

    # Match practice-administration lock ordering. The user lock also shares
    # the boundary used by doctor availability and appointment writes.
    company = Company.objects.select_for_update().filter(
        slug=settings.SINGLE_PRACTICE_SLUG, is_active=True,
    ).first()
    if company is None:
        raise PermissionDenied('The configured practice is not available.')
    user = get_user_model().objects.select_for_update().filter(pk=actor.pk).first()
    if not _technical_superuser(user):
        raise PermissionDenied('Your technical administrator access is no longer active.')
    membership = CompanyMembership.objects.select_for_update().filter(
        user=user, company=company, is_active=True,
    ).first()
    if not can_switch_practice_role(user, membership, company):
        raise PermissionDenied('An active membership in the configured practice is required.')

    if membership.role != role:
        if membership.role == CompanyMembership.Role.DOCTOR:
            _require_no_active_clinical_work(company, user)
        previous_role = membership.role
        membership.role = role
        membership.save(update_fields=('role', 'updated_at'))
        record_audit(
            company=company, actor=user, action='account.practice_role_changed', target=membership,
            request=request, metadata={'user_id': user.pk, 'previous_role': previous_role, 'role': role},
        )
    return membership
