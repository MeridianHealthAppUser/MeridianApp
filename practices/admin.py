from django.contrib import admin
from .admin_scope import SinglePracticeAdminScope
from .models import Company, CompanyMembership, Patient
from .tenancy import multi_practice_enabled, require_multi_practice, scope_queryset


class CompanyMembershipInline(SinglePracticeAdminScope, admin.TabularInline):
    model = CompanyMembership
    extra = 0
    autocomplete_fields = ('user',)


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'is_active')
    list_filter = ('is_active',)
    prepopulated_fields = {'slug': ('name',)}
    search_fields = ('name',)
    inlines = (CompanyMembershipInline,)

    def get_queryset(self, request):
        return scope_queryset(super().get_queryset(request), company_field='')

    def has_module_permission(self, request):
        return multi_practice_enabled() and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return multi_practice_enabled() and super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return multi_practice_enabled() and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return multi_practice_enabled() and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return multi_practice_enabled() and super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        require_multi_practice()
        return super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        require_multi_practice()
        return super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        require_multi_practice()
        return super().delete_queryset(request, queryset)


@admin.register(CompanyMembership)
class CompanyMembershipAdmin(SinglePracticeAdminScope, admin.ModelAdmin):
    list_display = ('user', 'company', 'role', 'is_active')
    list_filter = ('role', 'is_active', 'company')
    search_fields = ('user__email', 'company__name')
    autocomplete_fields = ('user', 'company')


@admin.register(Patient)
class PatientAdmin(SinglePracticeAdminScope, admin.ModelAdmin):
    list_display = ('last_name', 'first_name', 'company', 'assigned_doctor', 'medical_record_number', 'is_active')
    list_filter = ('company', 'is_active')
    search_fields = ('first_name', 'last_name', 'medical_record_number', 'id_number', 'user__email')
    autocomplete_fields = ('company', 'user', 'assigned_doctor')

# Register your models here.
