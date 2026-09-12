from django.contrib import admin

from .models import Company, CompanyMembership, Patient


class CompanyMembershipInline(admin.TabularInline):
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


@admin.register(CompanyMembership)
class CompanyMembershipAdmin(admin.ModelAdmin):
    list_display = ('user', 'company', 'role', 'is_active')
    list_filter = ('role', 'is_active', 'company')
    search_fields = ('user__email', 'company__name')
    autocomplete_fields = ('user', 'company')


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ('last_name', 'first_name', 'company', 'assigned_doctor', 'medical_record_number', 'is_active')
    list_filter = ('company', 'is_active')
    search_fields = ('first_name', 'last_name', 'medical_record_number', 'id_number', 'user__email')
    autocomplete_fields = ('company', 'user', 'assigned_doctor')

# Register your models here.
