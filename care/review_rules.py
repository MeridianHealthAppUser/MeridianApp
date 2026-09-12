"""Operational planning settings, never clinical authorisation extensions."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from practices.models import Company, CompanyMembership

from .clinical import _active_actor
from .models import MedicationProduct, PracticeSettings, ReviewRule
from .services import record_audit


def _lock_practice(company, actor):
    company = Company.objects.select_for_update().filter(pk=company.pk, is_active=True).first()
    _active_actor(actor)
    if company is None or not CompanyMembership.objects.filter(
        company=company, user=actor, role=CompanyMembership.Role.SUPER_ADMIN, is_active=True,
    ).exists():
        raise PermissionDenied('Only an active Super Admin can change this practice’s review planning settings.')
    return company


def _interval(value):
    if type(value) is not int or not 1 <= value <= 3650:
        raise ValidationError('Enter a review interval between 1 and 3,650 days.')
    return value


@transaction.atomic
def save_review_rule(*, company, actor, name, product_category, review_interval_days,
                     blood_tests_required=False, is_active=True, rule=None, expected_updated_at=None, request=None):
    company = _lock_practice(company, actor)
    if not isinstance(name, str) or not name.strip() or len(name) > 255:
        raise ValidationError('Enter a rule name of up to 255 characters.')
    if product_category not in MedicationProduct.Category.values:
        raise ValidationError('Choose a valid product category.')
    if type(blood_tests_required) is not bool or type(is_active) is not bool:
        raise ValidationError('Choose valid planning flags.')
    values = dict(name=name.strip(), product_category=product_category,
                  review_interval_days=_interval(review_interval_days), blood_tests_required=blood_tests_required, is_active=is_active)
    if rule is None:
        existing = ReviewRule.objects.select_for_update().for_company(company).filter(name=values['name']).first()
        if existing and all(getattr(existing, key) == value for key, value in values.items()):
            return existing
        record = ReviewRule(company=company)
    else:
        record = ReviewRule.objects.select_for_update().for_company(company).filter(pk=rule.pk).first()
        if record is None:
            raise PermissionDenied('This rule does not belong to the selected practice.')
        if all(getattr(record, key) == value for key, value in values.items()):
            return record
        if expected_updated_at is None or record.updated_at.isoformat() != expected_updated_at:
            raise ValidationError('This rule was changed in another tab. Reload it before saving.')
    for key, value in values.items():
        setattr(record, key, value)
    record.full_clean()
    record.save()
    record_audit(company=company, actor=actor, action='review_rule.saved', target=record, request=request)
    return record


@transaction.atomic
def update_review_default(*, company, actor, review_interval_days, expected_updated_at=None, request=None):
    company = _lock_practice(company, actor)
    interval = _interval(review_interval_days)
    settings = PracticeSettings.objects.select_for_update().for_company(company).first()
    if settings is None:
        raise ValidationError('Practice settings must be configured before updating the review default.')
    if settings.review_interval_days == interval:
        return settings
    if expected_updated_at is None or settings.updated_at.isoformat() != expected_updated_at:
        raise ValidationError('These practice settings changed in another tab. Reload before saving.')
    settings.review_interval_days = interval
    settings.full_clean()
    settings.save(update_fields=('review_interval_days', 'updated_at'))
    record_audit(company=company, actor=actor, action='review_rule.default_updated', target=settings, request=request)
    return settings
