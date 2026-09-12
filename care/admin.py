from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from . import models


class CompanyScopedAdmin(admin.ModelAdmin):
    list_filter = ('company',)
    autocomplete_fields = ('company',)
    # A universal fallback keeps every company-scoped record usable as an
    # autocomplete target. Individual admins add richer searches where useful.
    search_fields = ('id',)


class ClinicalWorkflowReadOnlyAdmin(CompanyScopedAdmin):
    """Workflow transitions must use their locked and audited portal services."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        raise PermissionDenied('Clinical workflow records cannot be changed through the admin.')

    def delete_model(self, request, obj):
        raise PermissionDenied('Clinical workflow history must be retained.')

    def delete_queryset(self, request, queryset):
        raise PermissionDenied('Clinical workflow history must be retained.')


class OperationalWorkflowReadOnlyAdmin(ClinicalWorkflowReadOnlyAdmin):
    """Administrative inspection cannot bypass tenant/stock/clinical services."""

    actions = None

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.concrete_fields)

    def save_model(self, request, obj, form, change):
        raise PermissionDenied('Use the audited practice workflow to change this record.')

    def save_related(self, request, form, formsets, change):
        raise PermissionDenied('Use the audited practice workflow to change related records.')

    def delete_model(self, request, obj):
        raise PermissionDenied('Operational records and their audit history must be retained.')

    def delete_queryset(self, request, queryset):
        raise PermissionDenied('Operational records and their audit history must be retained.')


class ProtectedClinicalRecordAdmin(CompanyScopedAdmin):
    """Ordinary legacy records remain editable; workflow-owned records do not."""

    def protected_queryset(self, queryset):
        raise NotImplementedError

    def is_protected(self, obj):
        return bool(obj and obj.pk and self.protected_queryset(self.model.objects.filter(pk=obj.pk)).exists())

    def has_change_permission(self, request, obj=None):
        return not self.is_protected(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return not self.is_protected(obj) and super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if self.is_protected(obj):
            raise PermissionDenied('This record is maintained by its clinical workflow.')
        return super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        if self.is_protected(obj):
            raise PermissionDenied('Clinical workflow history must be retained.')
        return super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        # Guard mixed selections as a whole; never partially delete ordinary rows
        # while silently skipping protected snapshots or generated tasks.
        if self.protected_queryset(queryset).exists():
            raise PermissionDenied('This selection includes protected clinical workflow records.')
        return super().delete_queryset(request, queryset)


@admin.register(models.PracticeSettings)
class PracticeSettingsAdmin(CompanyScopedAdmin):
    list_display = ('company', 'initial_consult_fee', 'standard_subscription_amount', 'review_interval_days')


@admin.register(models.ConsentDocument)
class ConsentDocumentAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('title', 'company', 'kind', 'version', 'effective_from', 'is_active')
    search_fields = ('title', 'version')


@admin.register(models.Lead)
class LeadAdmin(CompanyScopedAdmin):
    list_display = ('first_name', 'last_name', 'company', 'screening_status', 'stage', 'created_at')
    search_fields = ('first_name', 'last_name', 'email', 'id_number')


@admin.register(models.ScreeningQuestionnaire)
class ScreeningQuestionnaireAdmin(CompanyScopedAdmin):
    list_display = ('lead', 'company', 'stage', 'status', 'submitted_at')
    autocomplete_fields = ('company', 'lead', 'reviewed_by')


@admin.register(models.ConsentRecord)
class ConsentRecordAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('consent_type', 'company', 'user', 'patient', 'accepted', 'accepted_at')
    autocomplete_fields = ('company', 'user', 'lead', 'patient', 'document')


@admin.register(models.MedicationProduct)
class MedicationProductAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('name', 'strength', 'company', 'category', 'price', 'is_active')
    search_fields = ('name', 'strength')


@admin.register(models.TreatmentAuthorization)
class TreatmentAuthorizationAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('patient', 'product', 'company', 'prescribed_by', 'expires_on', 'status')
    autocomplete_fields = ('company', 'patient', 'product', 'prescribed_by')


@admin.register(models.PatientSubscription)
class PatientSubscriptionAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('patient', 'company', 'plan_name', 'cycle_number', 'monthly_amount', 'status')
    autocomplete_fields = ('company', 'patient', 'authorization')


@admin.register(models.SubscriptionCycle)
class SubscriptionCycleAdmin(CompanyScopedAdmin):
    list_display = ('subscription', 'patient', 'company', 'cycle_number', 'amount', 'due_on', 'status')
    autocomplete_fields = ('company', 'patient', 'subscription')


@admin.register(models.Invoice)
class InvoiceAdmin(CompanyScopedAdmin):
    list_display = ('invoice_number', 'patient', 'company', 'total', 'due_on', 'status')
    search_fields = ('invoice_number', 'patient__first_name', 'patient__last_name')
    autocomplete_fields = ('company', 'patient', 'subscription_cycle')


@admin.register(models.InvoiceLine)
class InvoiceLineAdmin(CompanyScopedAdmin):
    list_display = ('invoice', 'description', 'company', 'quantity', 'line_total')
    autocomplete_fields = ('company', 'invoice')


@admin.register(models.Appointment)
class AppointmentAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('starts_at', 'patient', 'clinician', 'company', 'appointment_type', 'status')
    list_filter = ('company', 'appointment_type', 'status')
    autocomplete_fields = ('company', 'patient', 'clinician')

@admin.register(models.AppointmentProposal)
class AppointmentProposalAdmin(CompanyScopedAdmin):
    list_display = ('patient', 'company', 'kind', 'proposed_starts_at', 'proposed_by', 'recipient', 'status')
    list_filter = ('company', 'status', 'kind')
    search_fields = ('patient__first_name', 'patient__last_name', 'proposed_by__email', 'recipient__email')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.AvailabilitySlot)
class AvailabilitySlotAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('starts_at', 'ends_at', 'clinician', 'company', 'is_booked')
    autocomplete_fields = ('company', 'clinician', 'appointment')


@admin.register(models.DoctorWorkingPattern, models.DoctorTimeOff)
class DoctorAvailabilityAdmin(CompanyScopedAdmin):
    """Inspect availability history; changes use the locked, audited portal."""

    list_display = ('id', 'company', 'clinician', 'updated_at')
    search_fields = ('clinician__email', 'clinician__first_name', 'clinician__last_name')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.ClinicalEncounter)
class ClinicalEncounterAdmin(ClinicalWorkflowReadOnlyAdmin):
    list_display = ('patient', 'clinician', 'company', 'status', 'occurred_at', 'signed_at')
    autocomplete_fields = ('company', 'patient', 'appointment', 'clinician', 'signed_by')


@admin.register(models.ClinicalTask)
class ClinicalTaskAdmin(ProtectedClinicalRecordAdmin):
    list_display = ('title', 'patient', 'company', 'assigned_to', 'priority', 'status', 'due_at')
    list_filter = ('company', 'priority', 'status')
    search_fields = ('title', 'patient__first_name', 'patient__last_name')
    autocomplete_fields = ('company', 'patient', 'assigned_to', 'created_by')

    def protected_queryset(self, queryset):
        return queryset.filter(Q(encounter_signing__isnull=False) | Q(lab_review_request__isnull=False)
                               | Q(compounding_record__isnull=False))


@admin.register(models.RecordTag)
class RecordTagAdmin(CompanyScopedAdmin):
    list_display = ('name', 'company', 'created_by', 'created_at')
    search_fields = ('name',)
    autocomplete_fields = ('company', 'created_by')


@admin.register(models.TaskTagAssignment, models.ClinicalNoteTagAssignment)
class RecordTagAssignmentAdmin(CompanyScopedAdmin):
    """Inspect assignments; use the audited portal to change record labels."""

    list_display = ('id', 'company', 'tag', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.ClinicalNote)
class ClinicalNoteAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('patient', 'company', 'author', 'note_type', 'created_at')
    autocomplete_fields = ('company', 'patient', 'author')

@admin.register(models.LabRequest)
class LabRequestAdmin(ClinicalWorkflowReadOnlyAdmin):
    list_display = ('patient', 'panel_name', 'company', 'status', 'requested_on', 'reviewed_at')
    autocomplete_fields = ('company', 'patient', 'requested_by', 'reviewed_by')


@admin.register(models.LabResult)
class LabResultAdmin(ClinicalWorkflowReadOnlyAdmin):
    """Metadata only: PDF bytes and downloads stay behind portal access checks."""

    list_display = ('filename', 'patient', 'company', 'uploaded_by', 'size', 'created_at')
    search_fields = ('filename', 'patient__first_name', 'patient__last_name', 'uploaded_by__email')
    autocomplete_fields = ()
    fields = readonly_fields = (
        'id', 'company', 'patient', 'lab_request', 'uploaded_by', 'filename',
        'size', 'sha256', 'created_at', 'updated_at',
    )

    def get_queryset(self, request):
        return super().get_queryset(request).defer('content')


@admin.register(models.WeightEntry)
class WeightEntryAdmin(CompanyScopedAdmin):
    list_display = ('patient', 'company', 'weight_kg', 'recorded_on', 'recorded_by')
    autocomplete_fields = ('company', 'patient', 'recorded_by')


@admin.register(models.MessageThread)
class MessageThreadAdmin(CompanyScopedAdmin):
    list_display = ('subject', 'patient', 'company', 'is_closed', 'last_message_at')
    autocomplete_fields = ('company', 'patient', 'opened_by')


@admin.register(models.PatientMessage)
class PatientMessageAdmin(CompanyScopedAdmin):
    list_display = ('thread', 'company', 'sender', 'created_at', 'read_at')
    autocomplete_fields = ('company', 'thread', 'sender')


@admin.register(models.PatientEvent)
class PatientEventAdmin(CompanyScopedAdmin):
    list_display = ('title', 'patient', 'company', 'category', 'occurred_at', 'is_patient_visible')
    search_fields = ('title', 'detail', 'patient__first_name', 'patient__last_name')
    autocomplete_fields = ('company', 'patient')


@admin.register(models.MedicationBatch)
class MedicationBatchAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('batch_number', 'product', 'company', 'quantity_on_hand', 'expires_on', 'status')
    autocomplete_fields = ('company', 'product')


@admin.register(models.StockMovement)
class StockMovementAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('batch', 'company', 'direction', 'quantity', 'reason', 'created_at')
    autocomplete_fields = ('company', 'batch', 'shipment', 'recorded_by')


@admin.register(models.Shipment)
class ShipmentAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('patient', 'company', 'cycle_number', 'scheduled_for', 'status', 'tracking_number')
    autocomplete_fields = ('company', 'patient', 'subscription')


@admin.register(models.ShipmentItem)
class ShipmentItemAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('shipment', 'product', 'batch', 'company', 'dose', 'quantity')
    autocomplete_fields = ('company', 'shipment', 'product', 'batch')


@admin.register(models.Payment)
class PaymentAdmin(CompanyScopedAdmin):
    list_display = ('patient', 'company', 'amount', 'due_on', 'status', 'paid_at')
    autocomplete_fields = ('company', 'patient', 'subscription', 'subscription_cycle', 'appointment', 'invoice')


@admin.register(models.PayoutRun)
class PayoutRunAdmin(CompanyScopedAdmin):
    list_display = ('company', 'period_start', 'period_end', 'status', 'approved_by')
    autocomplete_fields = ('company', 'approved_by')


@admin.register(models.DoctorPayoutLine)
class DoctorPayoutLineAdmin(CompanyScopedAdmin):
    list_display = ('doctor', 'payout_run', 'company', 'amount')
    autocomplete_fields = ('company', 'payout_run', 'doctor')


@admin.register(models.ReviewRule)
class ReviewRuleAdmin(CompanyScopedAdmin):
    list_display = ('name', 'company', 'product_category', 'review_interval_days', 'is_active')


@admin.register(models.EmailJourney)
class EmailJourneyAdmin(CompanyScopedAdmin):
    list_display = ('name', 'company', 'trigger', 'is_active')


@admin.register(models.EmailDelivery)
class EmailDeliveryAdmin(CompanyScopedAdmin):
    list_display = ('journey', 'recipient_email', 'company', 'status', 'sent_at', 'opened_at')
    autocomplete_fields = ('company', 'journey', 'patient', 'lead')


@admin.register(models.AuditEvent)
class AuditEventAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('action', 'company', 'actor', 'patient', 'created_at')
    search_fields = ('action', 'target_type', 'target_id')
    autocomplete_fields = ('company', 'actor', 'patient')


@admin.register(models.CompanyInvite)
class CompanyInviteAdmin(CompanyScopedAdmin):
    list_display = ('email', 'company', 'role', 'expires_at', 'accepted_at')
    autocomplete_fields = ('company', 'invited_by')


@admin.register(models.PharmacyOrder)
class PharmacyOrderAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('id', 'patient', 'company', 'status', 'submitted_at', 'shipment')
    autocomplete_fields = ('company', 'patient', 'shipment')


@admin.register(models.PharmacyOrderItem)
class PharmacyOrderItemAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('order', 'product_name', 'company', 'quantity', 'unit_price')
    autocomplete_fields = ('company', 'order', 'product')


@admin.register(models.AdministrativeFollowUp, models.PatientCommunicationPreference,
                models.PatientDataRequest, models.PatientDataRequestReply, models.DoctorActivityStatement,
                models.CompoundingRecord, models.AuthorizationReviewReminder)
class AdministrativeWorkflowAdmin(OperationalWorkflowReadOnlyAdmin):
    list_display = ('id', 'company', 'created_at', 'updated_at')
