"""Apply the configured practice boundary to technical admin as well as the portal."""

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from .models import Company
from .tenancy import company_is_enabled, enabled_companies, multi_practice_enabled, require_enabled_company, scope_queryset


def scoped_admin_users(queryset, request):
    if multi_practice_enabled():
        return queryset
    companies = enabled_companies()
    return queryset.filter(Q(pk=request.user.pk) | Q(company_memberships__company__in=companies)
                           | Q(patient_records__company__in=companies)).distinct()


def scoped_admin_related(queryset, request):
    if multi_practice_enabled():
        return queryset
    model = queryset.model
    if model is Company:
        return scope_queryset(queryset, company_field='')
    if model is get_user_model():
        return scoped_admin_users(queryset, request)
    if any(field.name == 'company' for field in model._meta.fields):
        return scope_queryset(queryset)
    return queryset


class SinglePracticeAdminScope:
    def get_queryset(self, request):
        return scope_queryset(super().get_queryset(request))

    def get_list_filter(self, request):
        filters = super().get_list_filter(request)
        if multi_practice_enabled():
            return filters
        # RelatedFieldListFilter otherwise fetches every Company for its labels.
        return tuple(item for item in filters if not (
            (isinstance(item, str) and item.split('__')[0] == 'company') or
            (isinstance(item, (tuple, list)) and item[0].split('__')[0] == 'company')))

    def get_autocomplete_fields(self, request):
        fields = super().get_autocomplete_fields(request)
        return fields if multi_practice_enabled() else tuple(field for field in fields if field != 'company')

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        field = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if field is not None:
            field.queryset = scoped_admin_related(field.queryset, request)
        return field

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        field = super().formfield_for_manytomany(db_field, request, **kwargs)
        if field is not None:
            field.queryset = scoped_admin_related(field.queryset, request)
        return field

    def has_view_permission(self, request, obj=None):
        return (multi_practice_enabled() or obj is None or company_is_enabled(obj.company)) and super().has_view_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return (multi_practice_enabled() or obj is None or company_is_enabled(obj.company)) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return (multi_practice_enabled() or obj is None or company_is_enabled(obj.company)) and super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not multi_practice_enabled():
            require_enabled_company(obj.company)
            if change and not self.get_queryset(request).filter(pk=obj.pk).exists():
                raise PermissionDenied('This record is not available in the configured practice.')
            # A forged direct save must not attach an enabled record to a hidden
            # company-scoped relation, even if the normal ModelForm was bypassed.
            for field in obj._meta.fields:
                if field.is_relation and getattr(obj, field.attname) is not None:
                    related = getattr(obj, field.name)
                    if isinstance(related, Company):
                        require_enabled_company(related)
                    elif hasattr(related, 'company_id'):
                        require_enabled_company(related.company)
                    elif isinstance(related, get_user_model()) and not scoped_admin_users(
                            get_user_model().objects.filter(pk=related.pk), request).exists():
                        raise PermissionDenied('This account is not available in the configured practice.')
        return super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        if not multi_practice_enabled():
            require_enabled_company(obj.company)
        return super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        if not multi_practice_enabled() and queryset.exclude(company__in=enabled_companies()).exists():
            raise PermissionDenied('This selection contains unavailable practice records.')
        return super().delete_queryset(request, queryset)
