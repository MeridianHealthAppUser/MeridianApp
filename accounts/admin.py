from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.core.exceptions import PermissionDenied
from django.db import transaction

from care.services import record_audit
from practices.admin_scope import scoped_admin_users
from practices.models import CompanyMembership, Patient
from practices.tenancy import enabled_companies, multi_practice_enabled

from .admin_forms import PATIENT_ROLE, ROLE_PERMISSIONS, PracticeUserCreationForm
from .models import User


@admin.register(User)
class MeridianUserAdmin(UserAdmin):
    add_form = PracticeUserCreationForm
    add_form_template = 'admin/accounts/user/add_form.html'

    def get_fieldsets(self, request, obj=None):
        if obj is not None:
            return super().get_fieldsets(request, obj)
        access_fields = ('practice', 'role') if multi_practice_enabled() else ('role',)
        fieldsets = (
            (None, {'fields': ('email', 'first_name', 'last_name', *access_fields, 'password1', 'password2')}),
        )
        if request.user.is_superuser:
            fieldsets += (
                ('Django administration (advanced)', {
                    'classes': ('collapse',),
                    'description': 'These permissions control the technical admin panel. '
                                   'The role above controls access to the practice and patient portals.',
                    'fields': ('is_staff', 'is_superuser'),
                }),
            )
        return fieldsets

    def get_form(self, request, obj=None, **kwargs):
        form_class = super().get_form(request, obj, **kwargs)
        if obj is not None:
            return form_class

        class AdminAddForm(form_class):
            def __init__(self, *args, **form_kwargs):
                super().__init__(*args, actor=request.user, **form_kwargs)

        return AdminAddForm

    def get_queryset(self, request):
        return scoped_admin_users(super().get_queryset(request), request)

    def has_view_permission(self, request, obj=None):
        allowed = multi_practice_enabled() or obj is None or self.get_queryset(request).filter(pk=obj.pk).exists()
        return allowed and super().has_view_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        allowed = multi_practice_enabled() or obj is None or self.get_queryset(request).filter(pk=obj.pk).exists()
        return allowed and super().has_change_permission(request, obj)

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        if change and not multi_practice_enabled() and not self.get_queryset(request).filter(pk=obj.pk).exists():
            raise PermissionDenied('This account is not available in the configured practice.')
        if change:
            return super().save_model(request, obj, form, change)

        role = form.cleaned_data['role']
        permission = ROLE_PERMISSIONS.get(role)
        if permission is None or not request.user.has_perm(permission):
            raise PermissionDenied('You do not have permission to grant this practice access.')
        if not request.user.is_superuser and (obj.is_staff or obj.is_superuser):
            raise PermissionDenied('Only a Django superuser can grant Django administration access.')
        company = enabled_companies().select_for_update().filter(pk=form.practice.pk).first()
        if company is None:
            raise PermissionDenied('The selected practice is no longer available. Reload before adding the user.')

        super().save_model(request, obj, form, change)
        if role == PATIENT_ROLE:
            access = Patient(company=company, user=obj, first_name=obj.first_name, last_name=obj.last_name)
            action = 'patient.account_created'
        else:
            access = CompanyMembership(company=company, user=obj, role=role)
            action = 'staff.account_created'
        access.full_clean()
        access.save()
        record_audit(
            company=company, actor=request.user, action=action, target=access,
            patient=access if role == PATIENT_ROLE else None, request=request,
            metadata={'source': 'admin.user_add', 'user_id': obj.pk, 'role': role},
        )

    def has_delete_permission(self, request, obj=None):
        # Global identity deletion can cascade into records outside the visible
        # practice. Disable it here; membership removal is local and audited.
        return multi_practice_enabled() and super().has_delete_permission(request, obj)

    def delete_model(self, request, obj):
        if not multi_practice_enabled():
            raise PermissionDenied('Remove practice access through the staff management page.')
        return super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        if not multi_practice_enabled():
            raise PermissionDenied('Global account deletion is unavailable in single-practice mode.')
        return super().delete_queryset(request, queryset)

    ordering = ('email',)
    list_display = ('email', 'first_name', 'last_name', 'is_staff', 'is_active')
    search_fields = ('email', 'first_name', 'last_name')
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Personal info', {'fields': ('first_name', 'last_name')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
    )
