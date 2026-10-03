"""Explicit, audited practice administration; never a global privilege bypass."""

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from care.services import record_audit
from .models import Company, CompanyMembership
from .tenancy import enabled_companies, require_multi_practice


def manageable_practices(actor):
    if not getattr(actor, 'is_active', False):
        return Company.objects.none()
    return enabled_companies().filter(memberships__user=actor, memberships__user__is_active=True,
        memberships__is_active=True, memberships__role=CompanyMembership.Role.SUPER_ADMIN).distinct()


def require_super_admin(actor, company):
    if not manageable_practices(actor).filter(pk=company.pk).exists():
        raise PermissionDenied('An active Super Admin membership is required for this practice.')


def _lock_practices(actor, companies):
    ids = {company.pk for company in companies}
    # A common company lock serializes membership edits and the last-admin check.
    locked = list(Company.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
    if len(locked) != len(ids):
        raise PermissionDenied('The selected practice is no longer available.')
    for company in locked:
        require_super_admin(actor, company)
    return locked


def _role(role):
    if role not in CompanyMembership.Role.values:
        raise ValidationError('Choose a supported staff role.')


def _clinician_type(role, clinician_type, current=''):
    """None keeps the current type; clinician access defaults to Doctor and admins are never clinicians."""
    if clinician_type is None:
        clinician_type = current
        if role == CompanyMembership.Role.DOCTOR and not clinician_type:
            clinician_type = CompanyMembership.ClinicianType.DOCTOR
        elif role == CompanyMembership.Role.PRACTICE_ADMIN:
            clinician_type = ''
    if clinician_type and clinician_type not in CompanyMembership.CLINICIAN_TYPES:
        raise ValidationError('Choose a supported clinician type.')
    if role == CompanyMembership.Role.DOCTOR and not clinician_type:
        raise ValidationError('Clinician access needs a clinician type: Doctor or Dietitian.')
    if role == CompanyMembership.Role.PRACTICE_ADMIN and clinician_type:
        raise ValidationError('Practice administrators are not clinicians.')
    return clinician_type


@transaction.atomic
def create_practice(*, actor, source_company, name, slug, request=None):
    require_multi_practice()
    _lock_practices(actor, [source_company])
    company = Company(name=name.strip(), slug=slug.strip())
    company.full_clean()
    company.save()
    membership = CompanyMembership.objects.create(company=company, user=actor,
                                                   role=CompanyMembership.Role.SUPER_ADMIN)
    record_audit(company=company, actor=actor, action='practice.created', target=company,
                 request=request, metadata={'source_company_id': source_company.pk})
    record_audit(company=company, actor=actor, action='staff.membership_linked', target=membership,
                 request=request, metadata={'user_id': actor.pk, 'role': membership.role})
    return company


@transaction.atomic
def update_practice(*, actor, company, name, slug, expected_updated_at=None, request=None):
    require_multi_practice()
    company = _lock_practices(actor, [company])[0]
    if expected_updated_at and company.updated_at.isoformat() != expected_updated_at:
        raise ValidationError('This practice was updated in another tab. Reload before saving.')
    changes = [key for key, value in {'name': name.strip(), 'slug': slug.strip()}.items()
               if getattr(company, key) != value]
    company.name, company.slug = name.strip(), slug.strip()
    company.full_clean()
    if changes:
        company.save(update_fields=(*changes, 'updated_at'))
        record_audit(company=company, actor=actor, action='practice.updated', target=company,
                     request=request, metadata={'changed_fields': changes})
    return company


@transaction.atomic
def add_staff_user(*, actor, source_company, companies, mode, email, role, clinician_type=None,
                   first_name='', last_name='', password='', request=None):
    """New login or explicit SSO link. Existing identities are never edited here."""
    companies = list(companies)
    if not companies:
        raise ValidationError('Choose at least one practice.')
    _lock_practices(actor, [source_company, *companies])
    _role(role)
    clinician_type = _clinician_type(role, clinician_type)
    User = get_user_model()
    email = email.strip().lower()
    matches = list(User.objects.select_for_update().filter(email__iexact=email)[:2])
    if mode == 'create':
        if matches:
            raise ValidationError('This email already has an account. Use “Link an existing account”.')
        user = User(email=email, first_name=first_name.strip(), last_name=last_name.strip(),
                    is_staff=False, is_superuser=False)
        if not user.first_name or not user.last_name:
            raise ValidationError('A new staff account needs a first and last name.')
        validate_password(password, user=user)
        user.set_password(password)
        user.full_clean()
        user.save()
    elif mode == 'link':
        if len(matches) != 1 or not matches[0].is_active:
            raise ValidationError('No active account matches this email. Check the address with the person.')
        if password or first_name or last_name:
            raise ValidationError('Leave name and password fields empty when linking an existing account.')
        user = matches[0]
    else:
        raise ValidationError('Choose whether to create or link a staff account.')
    if CompanyMembership.objects.filter(user=user, company__in=companies).exists():
        raise ValidationError('This person already has a membership in a selected practice. Edit that membership instead.')
    for company in companies:
        membership = CompanyMembership.objects.create(company=company, user=user, role=role, clinician_type=clinician_type)
        record_audit(company=company, actor=actor,
                     action='staff.account_created' if mode == 'create' else 'staff.membership_linked',
                     target=membership, request=request,
                     metadata={'user_id': user.pk, 'role': role, 'clinician_type': clinician_type})
    return user


@transaction.atomic
def update_membership(*, actor, company, membership, role, is_active, clinician_type=None,
                      expected_updated_at=None, request=None):
    _lock_practices(actor, [company])
    if membership.company_id != company.pk:
        raise PermissionDenied('This membership belongs to another practice.')
    membership = CompanyMembership.objects.select_for_update().select_related('user').get(
        pk=membership.pk, company=company)
    if expected_updated_at and membership.updated_at.isoformat() != expected_updated_at:
        raise ValidationError('This membership was updated in another tab. Reload before saving.')
    _role(role)
    clinician_type = _clinician_type(role, clinician_type, membership.clinician_type)
    if is_active and not membership.user.is_active:
        raise ValidationError('A globally inactive account cannot be reactivated from a practice.')
    removes_super_admin = (membership.is_active and membership.role == CompanyMembership.Role.SUPER_ADMIN
                           and (not is_active or role != CompanyMembership.Role.SUPER_ADMIN))
    if removes_super_admin and not CompanyMembership.objects.filter(
        company=company, role=CompanyMembership.Role.SUPER_ADMIN, is_active=True,
        user__is_active=True).exclude(pk=membership.pk).exists():
        raise ValidationError('Keep at least one active Super Admin in this practice.')
    changed = [field for field, value in {'role': role, 'clinician_type': clinician_type, 'is_active': is_active}.items()
               if getattr(membership, field) != value]
    if changed:
        old_role, old_type, old_active = membership.role, membership.clinician_type, membership.is_active
        membership.role, membership.clinician_type, membership.is_active = role, clinician_type, is_active
        membership.full_clean()
        membership.save(update_fields=(*changed, 'updated_at'))
        record_audit(company=company, actor=actor, action='staff.membership_updated', target=membership,
                     request=request, metadata={'changed_fields': changed, 'previous_role': old_role,
                         'role': role, 'previous_clinician_type': old_type, 'clinician_type': clinician_type,
                         'previous_active': old_active, 'is_active': is_active, 'user_id': membership.user_id})
    return membership
