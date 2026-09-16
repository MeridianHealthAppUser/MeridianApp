"""Reversible deployment access boundary; never changes or merges stored records.

Do not apply this to default model managers: safety checks must still detect a
doctor's existing bookings at a disabled practice. Apply it at access boundaries.
"""

from django.conf import settings
from django.core.exceptions import PermissionDenied

from .models import Company


def multi_practice_enabled():
    return settings.MULTI_PRACTICE_ENABLED


def enabled_companies():
    companies = Company.objects.filter(is_active=True)
    if not multi_practice_enabled():
        companies = companies.filter(slug=settings.SINGLE_PRACTICE_SLUG)
    return companies


def scope_queryset(queryset, company_field='company'):
    """Restrict a related-company queryset without widening existing permissions."""
    if multi_practice_enabled():
        return queryset
    lookup = f'{company_field}__in' if company_field else 'pk__in'
    return queryset.filter(**{lookup: enabled_companies()})


def company_is_enabled(company):
    return company is not None and enabled_companies().filter(pk=company.pk).exists()


def require_enabled_company(company):
    if not company_is_enabled(company):
        raise PermissionDenied('This practice is not available in this application.')


def require_multi_practice():
    if not multi_practice_enabled():
        raise PermissionDenied('Managing or switching practices is disabled in this application.')
