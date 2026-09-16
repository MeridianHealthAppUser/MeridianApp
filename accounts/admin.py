from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.core.exceptions import PermissionDenied

from practices.admin_scope import scoped_admin_users
from practices.tenancy import multi_practice_enabled

from .models import User


@admin.register(User)
class MeridianUserAdmin(UserAdmin):
    def get_queryset(self, request):
        return scoped_admin_users(super().get_queryset(request), request)

    def has_view_permission(self, request, obj=None):
        allowed = multi_practice_enabled() or obj is None or self.get_queryset(request).filter(pk=obj.pk).exists()
        return allowed and super().has_view_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        allowed = multi_practice_enabled() or obj is None or self.get_queryset(request).filter(pk=obj.pk).exists()
        return allowed and super().has_change_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if change and not multi_practice_enabled() and not self.get_queryset(request).filter(pk=obj.pk).exists():
            raise PermissionDenied('This account is not available in the configured practice.')
        return super().save_model(request, obj, form, change)

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
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'password1', 'password2', 'is_staff', 'is_superuser'),
        }),
    )

# Register your models here.
